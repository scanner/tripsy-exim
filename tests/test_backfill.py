#!/usr/bin/env python
#
"""
Test what a staged trip is still open about.

Every test here needs a staged trip built to be open about something in
particular, which is more setup than assertion, so the shapes arrive as
fixtures and a test names the one it is about.

The unplaceable cases all use `flight(placed=False)`.  An export that
carries an airport's code without its name is the case the app cannot
draw a pin for, and it is the only way to build one: the builder's
ordinary flight places both its ends, as real exports nearly always do.
"""

# system imports
import json
from collections.abc import Callable
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check

# Project imports
from tests import tripit_builder as b
from tripsy_exim.store import Archive
from tripsy_exim.sync import stage_export
from tripsy_exim.sync.backfill import (
    ARRIVAL,
    DEPARTURE,
    UNPLACEABLE,
    Gap,
    Place,
    by_population,
    by_recurrence,
    gaps,
    places,
    uuids_by_identifier,
)
from tripsy_exim.sync.importer import composed_children
from tripsy_exim.sync.overrides import OverrideSet, save_overrides


####################################################################
#
@pytest.fixture
def staged(archive: Archive) -> Callable[..., str]:
    """
    Stage one trip of the given objects and give back its key.

    A test says what the trip is made of and gets straight to asserting
    what the trip is open about.
    """

    def stage(*objects: dict[str, Any], name: str = "Kyoto, May 2011") -> str:
        stage_export(archive, b.export(b.trip(name=name, objects=[*objects])))
        keys = archive.trip_keys()
        assert len(keys) == 1
        return keys[0]

    return stage


####################################################################
#
@pytest.fixture
def unplaceable_trip(staged: Callable[..., str]) -> str:
    """
    A trip of two flights that both leave from one unplaced airport.

    Two, because a place seen twice is what the recurrence grouping is
    for, and one trip holding only one of each would never show it.
    """
    return staged(
        b.flight(frm="Narita", to="Vancouver", placed=False),
        b.flight(frm="Narita", to="Seattle", placed=False),
    )


########################################################################
########################################################################
#
class TestPlaces:
    """Tests for reading the places off an object."""

    ####################################################################
    #
    def test_a_leg_carries_two_places_and_a_stay_carries_one(
        self, staged: Callable[..., str], archive: Archive
    ) -> None:
        """
        GIVEN: a trip holding a flight and a hotel booking
        WHEN:  the places are read off each object
        THEN:  the leg has two ends and the stay has one

        A leg's ends are filled independently -- a rental collected at an
        airport and dropped downtown knows one and not the other -- so
        they cannot be one place between them.
        """
        key = staged(b.flight(), b.lodging())

        counted = {
            type(obj).__name__: [p.endpoint for p in places(obj)]
            for obj in composed_children(archive, key)
        }

        check.equal(counted["Transportation"], [DEPARTURE, ARRIVAL])
        check.equal(counted["Hosting"], [""])

    ####################################################################
    #
    @pytest.mark.parametrize(
        "address,latitude,longitude,expected",
        [
            ("Kyoto Station", None, None, True),
            (None, 35.0, 135.0, True),
            ("Kyoto Station", 35.0, 135.0, True),
            (None, None, None, False),
            ("", None, None, False),
        ],
    )
    def test_either_field_alone_is_enough_to_place_something(
        self,
        address: str | None,
        latitude: float | None,
        longitude: float | None,
        expected: bool,
    ) -> None:
        """
        GIVEN: a place carrying an address, a position, both or neither
        WHEN:  it is asked whether the app can place it
        THEN:  only having neither counts as unplaceable

        Tripsy geocodes an address when there is no position and prefers
        the position when there is one, so either field on its own is a
        pin.  Counting them separately would call an addressed object
        incomplete for lacking coordinates it does not need.
        """
        place = Place(DEPARTURE, address, latitude, longitude, "NRT")

        check.equal(place.placeable, expected)


########################################################################
########################################################################
#
class TestGaps:
    """Tests for what one staged trip is open about."""

    ####################################################################
    #
    def test_a_placed_trip_is_open_about_nothing(
        self, staged: Callable[..., str], archive: Archive
    ) -> None:
        """
        GIVEN: a trip whose flight places both of its ends
        WHEN:  its gaps are read
        THEN:  none of them is about placing anything

        The ordinary export places what it carries.  A report that said
        otherwise would bury the few real gaps under hundreds of objects
        that are perfectly fine.
        """
        key = staged(b.flight(), b.lodging())

        found = gaps(archive, key)

        check.equal([g for g in found if g.population == UNPLACEABLE], [])

    ####################################################################
    #
    def test_an_endpoint_with_only_a_code_is_unplaceable(
        self, unplaceable_trip: str, archive: Archive
    ) -> None:
        """
        GIVEN: a trip of two flights carrying airport codes and no names
        WHEN:  its gaps are read
        THEN:  every end of every leg is reported as unplaceable

        The code and the name are separate export fields: the label
        reads the code and the address falls back to the name, so an
        endpoint can be named and still have nothing to place it by.
        """
        found = [g for g in gaps(archive, unplaceable_trip) if g.endpoint]

        check.equal(len(found), 4, "two legs, two ends each")
        check.equal({g.population for g in found}, {UNPLACEABLE})
        check.equal({g.remedy for g in found}, {"correct"})
        check.equal(
            {g.label for g in found}, {"NAR", "VAN", "SEA"}, "the codes"
        )

    ####################################################################
    #
    def test_a_gap_carries_the_uuid_a_correction_is_keyed_by(
        self, unplaceable_trip: str, archive: Archive
    ) -> None:
        """
        GIVEN: a trip with unplaceable endpoints
        WHEN:  its gaps are read
        THEN:  each names the source uuid, not just the identifier

        A correction is keyed by the source uuid, which survives both a
        re-export and a change of namespace.  A gap that named only the
        identifier would have to be joined back before it could be
        answered.
        """
        found = gaps(archive, unplaceable_trip)

        uuids = uuids_by_identifier(archive, unplaceable_trip)
        for gap in found:
            check.is_true(gap.uuid, f"{gap.identifier} carries no uuid")
            check.equal(gap.uuid, uuids[gap.identifier])

    ####################################################################
    #
    def test_a_correction_closes_the_gap_it_answers(
        self, unplaceable_trip: str, archive: Archive
    ) -> None:
        """
        GIVEN: a trip with unplaceable endpoints
        WHEN:  one of them is given an address and the gaps are read
               again
        THEN:  that gap is gone and the others remain

        Corrections are laid over on the way out rather than written
        back to the staged files, so a report that read the disk would
        keep reporting a gap after it had been answered.
        """
        before = gaps(archive, unplaceable_trip)
        answered = next(g for g in before if g.endpoint == DEPARTURE)

        overrides = OverrideSet(trip_uuid=_trip_uuid(archive, unplaceable_trip))
        overrides.correct(answered.uuid, departure_address="Narita Airport")
        save_overrides(archive, overrides)

        after = gaps(archive, unplaceable_trip)

        check.equal(len(after), len(before) - 1)
        check.is_not_in(
            (answered.identifier, answered.endpoint),
            {(g.identifier, g.endpoint) for g in after},
        )

    ####################################################################
    #
    def test_an_unstaged_trip_is_open_about_nothing(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a trip key nothing was ever staged under
        WHEN:  its gaps are read
        THEN:  there are none, rather than an error

        A report walks whatever the archive holds, and a directory with
        no report in it is not a failure worth stopping the walk for.
        """
        check.equal(gaps(archive, "never-staged"), [])


########################################################################
########################################################################
#
class TestGrouping:
    """Tests for the two ways a work-list is ordered."""

    ####################################################################
    #
    def test_recurrence_puts_the_repeated_place_first(
        self, unplaceable_trip: str, archive: Archive
    ) -> None:
        """
        GIVEN: two flights that both leave from one unplaced airport
        WHEN:  the gaps are grouped by the place they name
        THEN:  the airport both legs share leads, naming both

        This is the order that finishes soonest: answering the one place
        that recurs closes two gaps, and the places seen once are what
        is left.
        """
        found = gaps(archive, unplaceable_trip)

        grouped = by_recurrence(found)

        check.equal(grouped[0][0], "NAR")
        check.equal(len(grouped[0][1]), 2)
        check.equal([len(rows) for _, rows in grouped], [2, 1, 1])

    ####################################################################
    #
    def test_ties_are_broken_alphabetically(
        self, unplaceable_trip: str, archive: Archive
    ) -> None:
        """
        GIVEN: two places each named by exactly one gap
        WHEN:  the gaps are grouped by place
        THEN:  the two are ordered by name

        Equal counts would otherwise come back in whatever order the
        objects were read, so the same archive would print a different
        work-list each run.
        """
        grouped = by_recurrence(gaps(archive, unplaceable_trip))

        check.equal([label for label, _ in grouped[1:]], ["SEA", "VAN"])

    ####################################################################
    #
    def test_populations_are_grouped_and_empty_ones_left_out(
        self, unplaceable_trip: str, archive: Archive
    ) -> None:
        """
        GIVEN: a trip open about one kind of thing only
        WHEN:  its gaps are grouped by population
        THEN:  that population is there and the others are absent

        A report listing every population it found nothing in reads as
        though it found something in each.
        """
        grouped = by_population(gaps(archive, unplaceable_trip))

        check.equal(list(grouped), [UNPLACEABLE])
        check.equal(len(grouped[UNPLACEABLE]), 4)

    ####################################################################
    #
    @pytest.mark.parametrize(
        "endpoint,label,summary,expected",
        [
            (DEPARTURE, "NRT", "Flight 101", "NRT (departure)"),
            ("", "Kyoto Station", "Train", "Kyoto Station"),
            (ARRIVAL, "", "Flight 101", "Flight 101"),
        ],
    )
    def test_a_gap_reads_as_the_place_and_the_end_it_is_about(
        self, endpoint: str, label: str, summary: str, expected: str
    ) -> None:
        """
        GIVEN: a gap about one end of a leg, a whole object, or an
               endpoint with no label
        WHEN:  it is rendered for a reader
        THEN:  the end is named only when there is a label to qualify

        A leg's two ends are separate gaps, so without the end the same
        leg shows twice with nothing to tell the rows apart.
        """
        gap = Gap(
            trip_key="k",
            population=UNPLACEABLE,
            uuid="u",
            identifier="i",
            collection="transportations",
            summary=summary,
            endpoint=endpoint,
            label=label,
        )

        check.equal(gap.where, expected)


####################################################################
#
def _trip_uuid(archive: Archive, trip_key: str) -> str:
    """The uuid a trip's corrections are filed under."""
    document = json.loads(
        (archive.trip_dir(trip_key) / "report.json").read_text()
    )
    return str(document["trip_uuid"])
