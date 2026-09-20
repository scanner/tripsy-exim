#!/usr/bin/env python
#
"""
Test filling an export trip's gaps from its calendar.

Trips are built here by hand rather than drawn from a generator.  What
is under test is which object a moment resolves to and where a
coordinate lands, and both are clearest when the instants are written
down where the assertion can see them.

`trip_of` is a fixture, so a test says in its signature that it builds
trips.  `at` and `placed` stay plain functions: they construct values
rather than inputs, and `at` is read at collection time by a parametrize
decorator, where no fixture exists yet.
"""

# system imports
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

# 3rd party imports
import pytest
import pytest_check as check

# Project imports
from tripsy_exim.models import Activity, Hosting, Transportation, Trip, mint
from tripsy_exim.sources import ACTIVITY, HOSTING, TRANSPORTATION, EventNote
from tripsy_exim.sources.ics import ParsedCalendar
from tripsy_exim.sync import DIVERGENCE_KM, enrich

# Places, named so a test reads as somewhere rather than as two floats.
# The last three are the pairs the divergence threshold was drawn
# between: an airport against the city a calendar names for it, and a
# mismatch no distance explains.
#
TOKYO = (35.6812, 139.7671)
KYOTO = (35.0116, 135.7681)
NARITA = (35.7647, 140.3864)
TOKYO_STATION = (35.6762, 139.6503)
VANCOUVER = (49.1947, -123.1792)

KIND_FOR = {
    Hosting: HOSTING,
    Activity: ACTIVITY,
    Transportation: TRANSPORTATION,
}

TripObject = Hosting | Activity | Transportation


####################################################################
#
def at(hour: int, day: int = 1) -> datetime:
    """One instant, written where an assertion can read it."""
    return datetime(2027, 6, day, hour, tzinfo=UTC)


####################################################################
#
def placed[T: TripObject](
    obj: T, where: tuple[float, float], prefix: str = ""
) -> T:
    """
    Put an object somewhere, and give it back.

    The position is set on the built object rather than passed to its
    constructor.  A `**` of latitude and longitude is opaque to mypy,
    which then stops checking the object's other fields too, and these
    models carry enough fields for that to matter.

    Args:
        obj: The object to place.  Returned, so a test builds and places
            in one expression.
        where: Latitude and longitude, from the constants above.
        prefix: Which end of a leg, e.g. 'arrival_'.  Empty for an
            object that keeps one position.

    Returns:
        The same object, now carrying a position.
    """
    setattr(obj, f"{prefix}latitude", where[0])
    setattr(obj, f"{prefix}longitude", where[1])
    return obj


####################################################################
#
def _token(name: str) -> str:
    """A stable uuid standing in for a source record's own."""
    return str(uuid5(NAMESPACE_URL, f"https://example.invalid/{name}"))


####################################################################
#
@pytest.fixture
def trip_of() -> Callable[..., ParsedCalendar]:
    """
    Build a parsed trip holding the objects a test names.

    Each object is filed into the collection its type belongs to, given
    a minted identifier, and paired with the `EventNote` the reader
    would have produced for it -- which is what enrichment matches on,
    so a trip without notes matches nothing.
    """

    def build(*objects: TripObject) -> ParsedCalendar:
        parsed = ParsedCalendar(
            trip=Trip(internal_identifier=mint("test", "trip")), join_key="k"
        )
        for obj in objects:
            name = obj.name or ""
            obj.internal_identifier = mint("test", name)
            if isinstance(obj, Hosting):
                parsed.hostings.append(obj)
            elif isinstance(obj, Transportation):
                parsed.transportations.append(obj)
            else:
                parsed.activities.append(obj)
            parsed.notes.append(
                EventNote(
                    uid=_token(name),
                    identifier=obj.internal_identifier,
                    kind=KIND_FOR[type(obj)],
                    summary=name,
                    confident=True,
                    reason="built for a test",
                    timezone=None,
                    timezone_source="none",
                )
            )
        return parsed

    return build


########################################################################
########################################################################
#
class TestMatching:
    """Tests for which export object a calendar event resolves to."""

    ####################################################################
    #
    def test_an_event_fills_a_place_the_export_left_blank(
        self, trip_of: Callable[..., ParsedCalendar]
    ) -> None:
        """
        GIVEN: an export activity with neither a position nor an address
        WHEN:  the calendar event at the same instant is matched to it
        THEN:  its coordinates are offered as a correction

        Nothing else can place this object: with no address there is
        nothing for Tripsy to geocode, so the calendar is the only
        source of a position.
        """
        target = trip_of(Activity(name="Yuushien Garden", starts_at=at(10)))
        source = trip_of(
            placed(Activity(name="Yuushien Garden", starts_at=at(10)), TOKYO)
        )

        result = enrich(target, source)

        entry = next(iter(result.overrides.entries.values()))
        check.equal(entry.fields["latitude"], TOKYO[0])
        check.equal(entry.fields["longitude"], TOKYO[1])
        check.equal(len(result.matched), 1)
        check.is_true(result.matched[0].named, "names agreed too")
        check.equal(
            result.divergences,
            [],
            "one side placing nothing is nothing to differ over",
        )

    ####################################################################
    #
    def test_an_object_with_an_address_is_left_for_tripsy_to_place(
        self, trip_of: Callable[..., ParsedCalendar]
    ) -> None:
        """
        GIVEN: an export activity carrying an address but no position
        WHEN:  a calendar event offers coordinates for it
        THEN:  nothing is offered

        Tripsy geocodes an address itself, and prefers a stored position
        over doing so.  Probed against the live app: an activity posted
        with Okayama's address and Tokyo's position drew its pin in
        Tokyo.  So handing a coordinate to something the app can already
        place trades an exact pin for a borrowed one.
        """
        target = trip_of(
            Activity(name="Yuushien Garden", starts_at=at(10), address="Matsue")
        )
        source = trip_of(
            placed(Activity(name="Yuushien Garden", starts_at=at(10)), TOKYO)
        )

        result = enrich(target, source)

        check.equal(result.overrides.entries, {}, "nothing offered")
        check.equal(len(result.matched), 1, "but still matched")

    ####################################################################
    #
    def test_a_leg_is_judged_per_end(
        self, trip_of: Callable[..., ParsedCalendar]
    ) -> None:
        """
        GIVEN: an export leg with a departure address and no arrival one
        WHEN:  a calendar event matches its arrival
        THEN:  the arrival is filled, because that end has no address

        A leg keeps an address per end, so one end being placed says
        nothing about the other.
        """
        target = trip_of(
            Transportation(
                name="Car",
                departure_at=at(9),
                arrival_at=at(17),
                departure_address="Collected here",
            )
        )
        source = trip_of(
            placed(Activity(name="Drop off", starts_at=at(17)), TOKYO)
        )

        result = enrich(target, source)

        assert "arrival_latitude" in result.matched[0].fields

    ####################################################################
    #
    def test_a_value_the_export_supplied_is_never_replaced(
        self, trip_of: Callable[..., ParsedCalendar]
    ) -> None:
        """
        GIVEN: an export object that already carries coordinates
        WHEN:  a calendar event offers different ones
        THEN:  nothing is corrected

        The export is the authority.  Enrichment fills gaps, so a wrong
        match can add a wrong value but can never destroy a right one.
        """
        target = trip_of(
            placed(Activity(name="Museum", starts_at=at(10)), KYOTO)
        )
        source = trip_of(
            placed(Activity(name="Museum", starts_at=at(10)), TOKYO)
        )

        result = enrich(target, source)

        assert result.overrides.entries == {}
        assert len(result.matched) == 1

    ####################################################################
    #
    def test_a_check_out_event_lands_on_the_stay_it_ends(
        self, trip_of: Callable[..., ParsedCalendar]
    ) -> None:
        """
        GIVEN: an export stay spanning two days
        WHEN:  the calendar's check-out event is matched
        THEN:  it resolves to that stay, on its end

        A calendar splits a stay into check-in and check-out events
        where the export keeps one object spanning both, so matching
        only on starts would lose every check-out.
        """
        target = trip_of(
            Hosting(name="Hotel", starts_at=at(15), ends_at=at(10, day=3))
        )
        source = trip_of(
            placed(
                Hosting(name="Check-out: Hotel", starts_at=at(10, day=3)),
                TOKYO,
            )
        )

        result = enrich(target, source)

        assert len(result.matched) == 1
        assert result.matched[0].endpoint == "end"
        assert result.overrides.entries

    ####################################################################
    #
    def test_an_event_matching_nothing_is_reported(
        self, trip_of: Callable[..., ParsedCalendar]
    ) -> None:
        """
        GIVEN: a calendar event at an instant the export does not use
        WHEN:  it is matched
        THEN:  it is reported unmatched rather than attached to something
        """
        target = trip_of(Activity(name="Museum", starts_at=at(10)))
        source = trip_of(Activity(name="Elsewhere", starts_at=at(18)))

        result = enrich(target, source)

        check.equal(result.unmatched, ["Elsewhere"])
        check.equal(result.matched, [])

    ####################################################################
    #
    def test_an_ambiguous_instant_is_refused_rather_than_guessed(
        self, trip_of: Callable[..., ParsedCalendar]
    ) -> None:
        """
        GIVEN: two export objects starting at one instant
        WHEN:  a calendar event shares it and matches neither by name
        THEN:  it is reported ambiguous and nothing is corrected

        Same rule as the trip-level join: where nothing separates two
        candidates, a person decides.
        """
        target = trip_of(
            Activity(name="Lunch", starts_at=at(12)),
            Activity(name="Talk", starts_at=at(12)),
        )
        source = trip_of(
            placed(Activity(name="Something Else", starts_at=at(12)), TOKYO)
        )

        result = enrich(target, source)

        check.equal(len(result.ambiguous), 1)
        check.equal(result.overrides.entries, {})

    ####################################################################
    #
    def test_a_name_breaks_a_tie_the_instant_cannot(
        self, trip_of: Callable[..., ParsedCalendar]
    ) -> None:
        """
        GIVEN: two export objects at one instant, one sharing the name
        WHEN:  a calendar event is matched
        THEN:  the name decides, and the match is made
        """
        target = trip_of(
            Activity(name="Lunch", starts_at=at(12)),
            Activity(name="Talk", starts_at=at(12)),
        )
        source = trip_of(placed(Activity(name="Talk", starts_at=at(12)), TOKYO))

        result = enrich(target, source)

        check.equal(result.ambiguous, [])
        check.equal(len(result.matched), 1)
        check.is_true(result.matched[0].named)


########################################################################
########################################################################
#
class TestWhereCoordinatesLand:
    """Tests for which field a coordinate is written to."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "moment,endpoint,expected",
        [
            (at(9), "start", "departure_latitude"),
            (at(17), "end", "arrival_latitude"),
        ],
        ids=["pick-up", "drop-off"],
    )
    def test_a_leg_takes_its_place_from_the_end_that_matched(
        self,
        trip_of: Callable[..., ParsedCalendar],
        moment: datetime,
        endpoint: str,
        expected: str,
    ) -> None:
        """
        GIVEN: an export car rental spanning a day
        WHEN:  the calendar's pick-up or drop-off event is matched
        THEN:  the coordinates land on the end that matched

        A rental is collected in one place and left in another, so which
        end matched is exactly what says which place this is.
        """
        target = trip_of(
            Transportation(name="Car", departure_at=at(9), arrival_at=at(17))
        )
        source = trip_of(
            placed(Activity(name="Rental", starts_at=moment), TOKYO)
        )

        result = enrich(target, source)

        assert result.matched[0].endpoint == endpoint
        assert expected in result.matched[0].fields

    ####################################################################
    #
    def test_a_legs_own_event_always_names_its_arrival(
        self, trip_of: Callable[..., ParsedCalendar]
    ) -> None:
        """
        GIVEN: a calendar leg, whose place is where it ends
        WHEN:  it matches an export leg on that leg's start
        THEN:  the coordinates still land on the arrival

        A calendar records a leg's destination, never its origin, so the
        end that matched must not be allowed to relabel it.  Nothing in
        the reference corpus exercises this -- every export flight
        already carries both airports -- but another account's rail or
        coach legs would.
        """
        target = trip_of(
            Transportation(name="Leg", departure_at=at(9), arrival_at=at(12))
        )
        source = trip_of(
            placed(
                Transportation(name="Leg", departure_at=at(9)),
                KYOTO,
                "arrival_",
            )
        )

        result = enrich(target, source)

        fields = result.matched[0].fields
        check.is_in("arrival_latitude", fields)
        check.is_not_in("departure_latitude", fields)


########################################################################
########################################################################
#
class TestConflicts:
    """Tests for two events wanting one field."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "checkout,conflicts,applied",
        [
            pytest.param(KYOTO, 2, {}, id="disagreeing"),
            pytest.param(
                TOKYO,
                0,
                {"latitude": TOKYO[0], "longitude": TOKYO[1]},
                id="agreeing",
            ),
        ],
    )
    def test_two_events_are_applied_only_where_they_agree(
        self,
        trip_of: Callable[..., ParsedCalendar],
        checkout: tuple[float, float],
        conflicts: int,
        applied: dict[str, float],
    ) -> None:
        """
        GIVEN: an export stay whose start and end both match an event
        WHEN:  the two events carry the same coordinates, or different
        THEN:  a value is applied only when they agree

        A hotel is in one place, so disagreement means a match is wrong
        and applying whichever came last would bury that.  Agreement is
        the ordinary case and must cost nothing.
        """
        target = trip_of(
            Hosting(name="Hotel", starts_at=at(15), ends_at=at(10, day=3))
        )
        source = trip_of(
            placed(Hosting(name="Check-in", starts_at=at(15)), TOKYO),
            placed(
                Hosting(name="Check-out", starts_at=at(10, day=3)), checkout
            ),
        )

        result = enrich(target, source)

        fields: dict[str, Any] = {}
        for entry in result.overrides.entries.values():
            fields.update(entry.fields)
        check.equal(len(result.conflicts), conflicts, "latitude and longitude")
        check.equal(fields, applied)


########################################################################
########################################################################
#
class TestDivergence:
    """
    Tests for the tripwire on two sources disagreeing about a place.

    The two routinely differ a little -- a calendar names a destination
    city where an export names its terminal -- so only a difference too
    large for that is worth reporting.
    """

    ####################################################################
    #
    def test_a_small_difference_is_not_reported(
        self, trip_of: Callable[..., ParsedCalendar]
    ) -> None:
        """
        GIVEN: two sources placing one thing a city's width apart
        WHEN:  they are matched
        THEN:  nothing is reported, because that is the normal case

        Tokyo Narita is 63km from the city the calendar names for it, and
        every such pair in the reference corpus is of that kind.
        """
        target = trip_of(
            placed(Activity(name="Arrival", starts_at=at(10)), NARITA)
        )
        source = trip_of(
            placed(Activity(name="Arrival", starts_at=at(10)), TOKYO_STATION)
        )

        result = enrich(target, source)

        check.equal(result.divergences, [])
        check.equal(len(result.matched), 1)

    ####################################################################
    #
    def test_a_difference_too_large_to_explain_is_reported(
        self, trip_of: Callable[..., ParsedCalendar]
    ) -> None:
        """
        GIVEN: two sources placing one thing continents apart
        WHEN:  they are matched
        THEN:  it is reported, because the match is probably wrong

        This is what two legs leaving at one instant look like when they
        are cross-matched.  Nothing is corrected either way -- the export
        keeps its own value -- so the report is the whole point.
        """
        target = trip_of(placed(Activity(name="Leg", starts_at=at(10)), TOKYO))
        source = trip_of(
            placed(Activity(name="Leg", starts_at=at(10)), VANCOUVER)
        )

        result = enrich(target, source)

        check.equal(len(result.divergences), 1, "reported")
        check.is_in("km apart", result.divergences[0])
        check.equal(result.overrides.entries, {}, "and nothing overwritten")

    ####################################################################
    #
    def test_the_threshold_sits_clear_of_city_sized_differences(
        self,
    ) -> None:
        """
        GIVEN: the threshold
        WHEN:  it is compared to what the corpus actually shows
        THEN:  it is above every explainable difference and far below a
               genuine mismatch

        63km is the worst city-against-terminal pair in the corpus and
        1269km the worst real mismatch found while developing this.  The
        threshold has to separate them and does.
        """
        assert 63.0 < DIVERGENCE_KM < 1269.0
