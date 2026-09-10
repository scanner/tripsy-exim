#!/usr/bin/env python
#
"""
Test the .ics parser against synthetic TripIt-shaped calendars.

The export this reads carries no type information and no timezones, so
most of what is asserted here is inference rather than reading: what an
event was decided to be, and which zone its instants were read in.  Both
are judgement calls the parser has to make and a person has to be able to
check afterwards, which is why the report is tested as carefully as the
objects.
"""

# system imports
from datetime import UTC, date, datetime, timedelta

# 3rd party imports
import pytest
import pytest_check as check
from faker import Faker
from icalendar import Calendar
from pytest_mock import MockerFixture

# Project imports
from tests import ics_builder
from tripsy_exim.models import IDENTIFIER_PREFIX, mint
from tripsy_exim.sources.ics import (
    ACTIVITY,
    HOSTING,
    TRANSPORTATION,
    TRIPIT_UID_NAMESPACE,
    _zoneinfo,
    classify,
    parse,
    uuid_from_uid,
)
from tripsy_exim.sources.timezones import zone_for

TOKYO = ("Asia/Tokyo", 35.6812, 139.7671)
NEW_YORK = ("America/New_York", 40.7128, -74.0060)


####################################################################
#
def calendar_text(faker: Faker, **kwargs: object) -> str:
    """Synthetic .ics text with the defects a test asks for."""
    built = ics_builder.build_calendar(faker, **kwargs)  # type: ignore[arg-type]
    return ics_builder.to_ics(built)


########################################################################
########################################################################
#
class TestIdentifiers:
    """Tests the key every imported object is found by afterwards."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "uid,expected",
        [
            # A trip-level event carries the bare uuid.
            (
                "d1e2f3a4-b5c6-4778-89ab-cdef01234567@tripit.com",
                "d1e2f3a4-b5c6-4778-89ab-cdef01234567",
            ),
            # An item event prefixes it, and must yield the same uuid.
            (
                "item-d1e2f3a4-b5c6-4778-89ab-cdef01234567@tripit.com",
                "d1e2f3a4-b5c6-4778-89ab-cdef01234567",
            ),
            # The domain differs between a real export and a synthetic
            # one, so keying on it would mint differently for each.
            (
                "item-d1e2f3a4-b5c6-4778-89ab-cdef01234567@example.invalid",
                "d1e2f3a4-b5c6-4778-89ab-cdef01234567",
            ),
            # Case is normalised, or the same object would mint twice.
            (
                "ITEM-D1E2F3A4-B5C6-4778-89AB-CDEF01234567@tripit.com",
                "d1e2f3a4-b5c6-4778-89ab-cdef01234567",
            ),
            ("no-uuid-here@tripit.com", None),
            ("", None),
        ],
    )
    def test_the_uuid_is_taken_from_the_uid_and_nothing_else(
        self, uid: str, expected: str | None
    ) -> None:
        """
        GIVEN: a UID in any of the forms TripIt emits
        WHEN:  the uuid is extracted
        THEN:  the prefix, the domain and the case are all ignored, so
               the same object minted from another source keys the same
        """
        assert uuid_from_uid(uid) == expected

    ####################################################################
    #
    def test_an_identifier_is_derived_from_the_uuid_and_stays_put(
        self, faker: Faker
    ) -> None:
        """
        GIVEN: the same calendar parsed twice
        WHEN:  the identifiers are examined and rebuilt from the uuids
        THEN:  each is minted from its event's uuid alone and is the
               same on both passes -- derived rather than generated, so
               a re-run is a no-op and any other source keyed on that
               uuid corrects this object instead of duplicating it
        """
        text = calendar_text(faker, items=4)

        first = parse(text)
        second = parse(text)

        check.equal(
            [n.identifier for n in first.notes],
            [n.identifier for n in second.notes],
            "stable across parses",
        )
        check.equal(
            first.trip.internal_identifier,
            second.trip.internal_identifier,
            "the trip's too",
        )

        rebuilt = []
        for note in first.notes:
            token = uuid_from_uid(note.uid)
            assert token is not None
            rebuilt.append(mint(TRIPIT_UID_NAMESPACE, token))
        check.equal(
            [n.identifier for n in first.notes],
            rebuilt,
            "minted from the uuid and nothing else",
        )
        check.is_true(
            all(
                n.identifier.startswith(f"{IDENTIFIER_PREFIX}-")
                for n in first.notes
            ),
            "and marked as ours",
        )

    ####################################################################
    #
    def test_an_event_with_no_uuid_is_reported_and_left_out(
        self, faker: Faker
    ) -> None:
        """
        GIVEN: an event whose UID holds no uuid
        WHEN:  the calendar is parsed
        THEN:  it is not imported, because nothing stable could key it
               and every run would create another copy -- and it is
               reported rather than dropped in silence
        """
        calendar = ics_builder.build_calendar(faker, items=1)
        stray = ics_builder.build_event(
            faker,
            starts_at=datetime(2027, 6, 2, 9, 0, tzinfo=UTC),
            ends_at=datetime(2027, 6, 2, 11, 0, tzinfo=UTC),
            summary="Unkeyable",
        )
        stray["UID"] = "not-a-uuid@tripit.com"
        calendar.add_component(stray)

        parsed = parse(ics_builder.to_ics(calendar))

        check.equal(len(parsed.activities), 1, "only the keyable one")
        stranded = [n for n in parsed.notes if n.summary == "Unkeyable"]
        check.equal(len(stranded), 1, "the other is reported")
        check.is_in("uuid", stranded[0].reason, "and says why")


########################################################################
########################################################################
#
class TestClassification:
    """
    Tests the two rules, and the default everything else falls to.

    The rules are deliberately few.  A wide keyword vocabulary was
    measured during the survey and left over half the events ambiguous
    and swung 25 points on one added word; these two describe wording
    TripIt generates itself, and there is no tail to destabilise.
    """

    ####################################################################
    #
    @pytest.mark.parametrize(
        "summary,description,kind,confident",
        [
            # Lodging, in the spellings TripIt actually emits.
            ("Check in to Hotel Meridian", "", HOSTING, True),
            ("Check-in: Seaside Inn", "", HOSTING, True),
            ("Checkin", "", HOSTING, True),
            ("Check out of Hotel Meridian", "", HOSTING, True),
            ("Check-out", "", HOSTING, True),
            # Flights.
            ("Flight to Reykjavik", "", TRANSPORTATION, True),
            ("Connecting flight", "", TRANSPORTATION, True),
            ("", "Your flight departs at noon", TRANSPORTATION, True),
            # Lodging outranks a flight mentioned in the notes, because a
            # check-in event is what it says it is however it is annotated.
            ("Check in to Hotel Meridian", "after your flight", HOSTING, True),
            # Everything else is an activity: a valid container rather
            # than a wrong answer, and reported as a guess.
            ("Dinner at the tavern", "", ACTIVITY, False),
            ("Kyōto Nishiki Café", "", ACTIVITY, False),
            ("", "", ACTIVITY, False),
            # 'flights' as a substring of something else must not match,
            # which is what the word boundary is for.
            ("Preflight yoga", "", ACTIVITY, False),
        ],
    )
    def test_only_high_precision_wording_is_classified(
        self,
        summary: str,
        description: str,
        kind: str,
        confident: bool,
    ) -> None:
        """
        GIVEN: an event's prose
        WHEN:  it is classified
        THEN:  only wording TripIt generates itself is acted on, and
               anything else is an activity flagged as a guess
        """
        decided, was_confident, reason = classify(summary, description)

        check.equal(decided, kind, "kind")
        check.equal(was_confident, confident, "confidence")
        check.is_true(bool(reason), "a reason is always given")

    ####################################################################
    #
    def test_events_reach_the_right_collections_and_guesses_are_listed(
        self, faker: Faker
    ) -> None:
        """
        GIVEN: a calendar with lodging and flight events among others
        WHEN:  it is parsed
        THEN:  each matched kind lands in its own collection, and every
               unmatched event is both filed as an activity and listed
               -- Tripsy's duplicate suppression makes a misfiled event
               hard to take back, so the report is meant to be read
               before the import runs
        """
        parsed = parse(
            calendar_text(faker, items=6, lodging=2, flights=1, landmark=TOKYO)
        )

        check.equal(len(parsed.hostings), 2, "lodging")
        check.equal(len(parsed.transportations), 1, "flights")
        check.equal(len(parsed.activities), 3, "the rest")
        check.equal(len(parsed.unclassified), 3, "all of them flagged")
        check.is_true(
            all(n.kind == ACTIVITY for n in parsed.unclassified),
            "and all defaulted to activity",
        )

    ####################################################################
    #
    def test_a_flight_is_typed_and_fills_only_its_departure(
        self, faker: Faker
    ) -> None:
        """
        GIVEN: a flight event with one LOCATION and one GEO
        WHEN:  it becomes a transportation
        THEN:  the departure half is filled and the arrival half is left
               empty, because a VEVENT carries only one place and
               inventing the other end would be a fabrication
        """
        parsed = parse(calendar_text(faker, items=1, flights=1, landmark=TOKYO))
        leg = parsed.transportations[0]

        check.equal(leg.transportation_type, "airplane", "typed")
        check.is_not_none(leg.departure_at, "departure instant")
        check.equal(leg.departure_timezone, "Asia/Tokyo", "departure zone")
        check.is_not_none(leg.departure_address, "departure address")
        check.is_none(leg.arrival_address, "arrival left empty")
        check.is_none(leg.arrival_timezone, "and unzoned")


########################################################################
########################################################################
#
class TestTimezones:
    """
    Tests where a zone comes from when the file contains none.

    The export has no TZID anywhere, so GEO is the only candidate, and
    about one event in ten does not carry that either.
    """

    ####################################################################
    #
    @pytest.mark.parametrize(
        "kwargs,expected",
        [
            # Every event has coordinates: derived outright.
            (
                {"items": 3, "missing_geo": 0, "landmark": TOKYO},
                [("Asia/Tokyo", "geo")] * 3,
            ),
            # The first has none, as a real file's gaps do.  It inherits
            # from the event *after* it -- a forward-only search would
            # leave it unzoned and its instants read as UTC.
            (
                {"items": 3, "missing_geo": 1, "landmark": TOKYO},
                [
                    ("Asia/Tokyo", "inherited"),
                    ("Asia/Tokyo", "geo"),
                    ("Asia/Tokyo", "geo"),
                ],
            ),
            # Nothing anywhere in the file: no zone is invented, and the
            # report says so rather than implying one.
            (
                {"items": 2, "missing_geo": 2},
                [(None, "none")] * 2,
            ),
        ],
    )
    def test_a_zone_is_derived_inherited_or_honestly_absent(
        self,
        faker: Faker,
        kwargs: dict[str, object],
        expected: list[tuple[str | None, str]],
    ) -> None:
        """
        GIVEN: a calendar whose events carry coordinates, or do not
        WHEN:  it is parsed
        THEN:  a zone is derived where GEO allows, inherited from the
               nearest neighbour where it does not, and left absent when
               the file holds none -- each marked with which it was
        """
        parsed = parse(calendar_text(faker, **kwargs))

        check.equal(
            [(n.timezone, n.timezone_source) for n in parsed.notes],
            expected,
            "zone and where it came from",
        )

        # The report's own view of the same thing, since a caller reads
        # that rather than the notes: an inherited zone is a neighbour's
        # and is wrong for anything that crossed a border.
        #
        check.equal(
            len(parsed.guessed_timezones),
            sum(1 for _, source in expected if source == "inherited"),
            "guesses listed for review",
        )


########################################################################
########################################################################
#
class TestTimeForms:
    """
    Tests the three shapes an instant arrives in.

    The models reject naive datetimes, so all three have to land on an
    aware UTC instant.  Which zone a naive one is read in is the whole
    reason GEO is mined for a timezone: the first two rows below differ
    only in whether a zone could be derived, and land nine hours apart.
    """

    ####################################################################
    #
    @pytest.mark.parametrize(
        "kwargs,expected,all_day",
        [
            # Floating 09:00 with Tokyo coordinates is 00:00 UTC.
            (
                {"floating": 1, "landmark": TOKYO},
                datetime(2027, 6, 1, 0, 0, tzinfo=UTC),
                False,
            ),
            # The same 09:00 with no coordinates anywhere can only be
            # read as UTC -- a guess, and nine hours from the row above.
            (
                {"floating": 1, "missing_geo": 1},
                datetime(2027, 6, 1, 9, 0, tzinfo=UTC),
                False,
            ),
            # Date-only becomes local midnight, so the day it lands on is
            # the day it was written for.
            (
                {"date_only": 1, "landmark": TOKYO},
                datetime(2027, 5, 31, 15, 0, tzinfo=UTC),
                True,
            ),
            # An already-aware instant is untouched, whatever zone was
            # derived.  This is 1260 of the 1339 real events.
            (
                {"landmark": NEW_YORK},
                datetime(2027, 6, 1, 9, 0, tzinfo=UTC),
                False,
            ),
        ],
    )
    def test_every_date_form_lands_on_the_right_utc_instant(
        self,
        faker: Faker,
        kwargs: dict[str, object],
        expected: datetime,
        all_day: bool,
    ) -> None:
        """
        GIVEN: an event in one of the three forms the export uses
        WHEN:  it is parsed
        THEN:  it becomes the UTC instant it actually denotes, read in
               the zone derived for it, and is flagged all-day only when
               the source was date-only
        """
        parsed = parse(calendar_text(faker, items=1, **kwargs))
        activity = parsed.activities[0]

        check.equal(activity.starts_at, expected, "instant")
        check.equal(bool(activity.all_day), all_day, "all-day flag")


########################################################################
########################################################################
#
class TestTripEnvelope:
    """Tests the trip built from calendar metadata rather than an event."""

    ####################################################################
    #
    def test_the_trip_takes_its_name_and_span_from_the_calendar(
        self, faker: Faker
    ) -> None:
        """
        GIVEN: a calendar with X-WR- metadata and a trip-level event
        WHEN:  it is parsed
        THEN:  the trip is named from the calendar and dated from the
               trip-level event, as plain dates needing no zone
        """
        parsed = parse(calendar_text(faker, items=4, start=date(2027, 6, 1)))

        check.is_true(
            parsed.trip.name and parsed.trip.name.startswith("Trip to"),
            "named from X-WR-CALNAME",
        )
        check.is_not_none(parsed.trip.description, "and described")
        check.equal(parsed.trip.starts_at, date(2027, 6, 1), "first day")
        check.equal(
            parsed.trip.ends_at, date(2027, 6, 1) + timedelta(days=5), "last"
        )
        check.is_true(parsed.trip.has_dates, "and dated")

    ####################################################################
    #
    def test_the_trip_level_event_is_not_imported_as_a_child(
        self, faker: Faker
    ) -> None:
        """
        GIVEN: a calendar whose first event is the trip itself
        WHEN:  it is parsed
        THEN:  only the item events become child objects, or every trip
               would carry a duplicate of itself as an activity
        """
        parsed = parse(calendar_text(faker, items=3))

        total = (
            len(parsed.hostings)
            + len(parsed.activities)
            + len(parsed.transportations)
        )
        check.equal(total, 3, "the items only")
        check.equal(len(parsed.notes), 3, "and one note each")


########################################################################
########################################################################
#
class TestPassthrough:
    """Tests that nothing in the source file is silently discarded."""

    ####################################################################
    #
    def test_what_was_read_and_what_was_kept_are_complements(
        self, faker: Faker
    ) -> None:
        """
        GIVEN: an event carrying a property the parser does not map
        WHEN:  it is parsed
        THEN:  the unmapped value survives verbatim and the mapped ones
               do not appear alongside it -- dropping detail on import
               is the failure this project exists in response to, and
               keeping two copies of the rest is how an archive rots
        """
        calendar = ics_builder.build_calendar(faker, items=1)
        event = [c for c in calendar.walk() if c.name == "VEVENT"][-1]
        event.add("x-tripit-booking-ref", "ABC123")

        retained = (
            parse(ics_builder.to_ics(calendar)).activities[0].source_extras
        )

        check.equal(
            retained.get("X-TRIPIT-BOOKING-REF"), "ABC123", "kept verbatim"
        )
        for name in ("SUMMARY", "DESCRIPTION", "LOCATION", "GEO", "DTSTART"):
            check.is_not_in(name, retained, f"{name} was read, not kept")

    ####################################################################
    #
    def test_non_ascii_text_survives_a_round_trip(self, faker: Faker) -> None:
        """
        GIVEN: an event whose text is non-ASCII, as real ones are
        WHEN:  it is parsed
        THEN:  the characters arrive intact rather than mangled
        """
        parsed = parse(
            calendar_text(faker, items=1, non_ascii=1, landmark=TOKYO)
        )
        activity = parsed.activities[0]

        assert any(
            ord(character) > 127
            for character in f"{activity.name}{activity.address}"
        )


########################################################################
########################################################################
#
class TestMalformedInput:
    """Tests what happens to files that are not what was expected."""

    ####################################################################
    #
    def test_text_that_is_not_a_calendar_is_rejected(self) -> None:
        """
        GIVEN: something that is not an .ics file
        WHEN:  it is parsed
        THEN:  it raises rather than returning an empty trip that would
               import as a real one
        """
        with pytest.raises(ValueError):
            parse("this is not a calendar")

    ####################################################################
    #
    def test_a_calendar_with_no_events_yields_an_empty_trip(self) -> None:
        """
        GIVEN: a valid calendar carrying no events at all
        WHEN:  it is parsed
        THEN:  a trip comes back with no dates and nothing under it,
               rather than an exception -- an empty file is a legitimate
               thing to be handed
        """
        calendar = Calendar()
        calendar.add("prodid", ics_builder.SYNTHETIC_PRODID)
        calendar.add("version", "2.0")
        calendar.add("x-wr-calname", "Empty")

        parsed = parse(ics_builder.to_ics(calendar))

        check.equal(parsed.trip.name, "Empty", "named")
        check.is_none(parsed.trip.starts_at, "undated")
        check.is_false(parsed.trip.has_dates, "and says so")
        check.equal(parsed.notes, [], "nothing to report")


########################################################################
########################################################################
#
class TestFieldsWithNoHomeOnTheModel:
    """
    Tests what happens to source detail the target schema cannot hold.

    Only `Activity` has an `all_day` field.  A date-only lodging or
    flight event would otherwise lose that fact entirely on the way in,
    which is the class of loss this project exists to prevent.
    """

    ####################################################################
    #
    @pytest.mark.parametrize(
        "kwargs,collection",
        [
            ({"lodging": 1}, "hostings"),
            ({"flights": 1}, "transportations"),
        ],
    )
    def test_all_day_survives_on_a_model_that_cannot_express_it(
        self, faker: Faker, kwargs: dict[str, int], collection: str
    ) -> None:
        """
        GIVEN: a date-only event that classifies as lodging or a flight
        WHEN:  it is parsed
        THEN:  the all-day fact is kept in the passthrough, since
               neither model has a field for it and dropping it would be
               silent
        """
        parsed = parse(
            calendar_text(faker, items=1, date_only=1, landmark=TOKYO, **kwargs)
        )
        built = getattr(parsed, collection)[0]

        assert built.source_extras.get("x_all_day") == "TRUE"

    ####################################################################
    #
    @pytest.mark.parametrize(
        # One per way ZoneInfo can fail: not found, malformed, too
        # long for the filesystem.
        "name",
        ["Not/AZone", "../etc/passwd", "x" * 300],
    )
    def test_a_zone_that_will_not_load_is_not_quietly_utc(
        self, name: str
    ) -> None:
        """
        GIVEN: a zone name this machine cannot load, in each of the
               shapes that fail differently
        WHEN:  it is resolved
        THEN:  nothing comes back, rather than UTC -- an instant read in
               the wrong zone is out by up to half a day and would look
               like a clean derivation afterwards
        """
        assert _zoneinfo(name) is None

    ####################################################################
    #
    def test_an_unloadable_zone_is_reported_rather_than_hidden(
        self, faker: Faker, mocker: MockerFixture
    ) -> None:
        """
        GIVEN: coordinates that derive a zone the machine cannot load,
               as a container with trimmed tzdata produces
        WHEN:  the calendar is parsed
        THEN:  the import still completes, but the event is marked
               'unloadable' so the times are known to be suspect --
               `timezonefinder` only answers valid names, so this is
               forced rather than provoked
        """
        mocker.patch("tripsy_exim.sources.ics._zoneinfo", return_value=None)

        parsed = parse(
            calendar_text(faker, items=1, floating=1, landmark=TOKYO)
        )
        note = parsed.notes[0]

        check.equal(note.timezone, "Asia/Tokyo", "the name was derived")
        check.equal(note.timezone_source, "unloadable", "but could not be used")
        check.equal(
            parsed.activities[0].starts_at,
            datetime(2027, 6, 1, 9, 0, tzinfo=UTC),
            "and the instant fell back to UTC",
        )


########################################################################
########################################################################
#
class TestCoordinateLookup:
    """Tests the GEO to timezone step on its own."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "latitude,longitude,expected",
        [
            # Open ocean answers an Etc zone rather than nothing, which a
            # flight's coordinates can easily reach.
            (0.0, -30.0, "Etc/GMT+2"),
            # Out of range, as a malformed file can hold.  No zone, and
            # no exception -- one bad property is not worth failing an
            # import over.
            (999.0, 999.0, None),
        ],
    )
    def test_coordinates_resolve_or_politely_do_not(
        self, latitude: float, longitude: float, expected: str | None
    ) -> None:
        """
        GIVEN: coordinates from a GEO property
        WHEN:  a zone is looked up
        THEN:  a real point resolves, a point at sea gives an Etc zone,
               and an impossible one gives nothing without raising
        """
        assert zone_for(latitude, longitude) == expected
