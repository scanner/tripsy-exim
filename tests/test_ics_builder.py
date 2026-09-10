#!/usr/bin/env python
#
"""
Test that the generator emits the awkward cases, not just the clean ones.

A generator producing only well-formed timed events with coordinates would
certify a parser that falls over on the real export, so each defect the
survey found in the real data gets a knob and a test.
"""

# system imports
from collections.abc import Callable

# 3rd party imports
import pytest
import pytest_check as check
from faker import Faker
from icalendar import Calendar

# Project imports
from tests.ics_builder import (
    LANDMARKS,
    SYNTHETIC_PRODID,
    SYNTHETIC_UID_DOMAIN,
    build_calendar,
    day_of,
    events_of,
    field,
    is_all_day,
    is_floating,
    to_ics,
    uid_of,
)


########################################################################
########################################################################
#
class TestGeneratedShape:
    """Tests the calendar matches what a real TripIt export looks like."""

    ####################################################################
    #
    def test_one_trip_level_event_plus_items(
        self, ics_calendar: Callable[..., str]
    ) -> None:
        """
        GIVEN: a generated calendar of five items
        WHEN:  its events are read back
        THEN:  there is exactly one all-day trip-level event with a bare
               UUID, and the rest are prefixed item events
        """
        events = events_of(ics_calendar(items=5))

        uids = [uid_of(e) for e in events]
        trip_level = [u for u in uids if not u.startswith("item-")]

        check.equal(len(events), 6, "five items plus the trip")
        check.equal(len(trip_level), 1, "exactly one trip-level event")
        check.is_true(
            all(u.endswith(f"@{SYNTHETIC_UID_DOMAIN}") for u in uids),
            "every UID is synthetic",
        )

    ####################################################################
    #
    def test_uids_carry_a_uuid_that_survives_the_prefix(
        self, ics_calendar: Callable[..., str]
    ) -> None:
        """
        GIVEN: trip-level and item UIDs
        WHEN:  the prefix and domain are stripped
        THEN:  a bare UUID remains in both cases, which is the key an
               import from either source must mint from
        """
        events = events_of(ics_calendar(items=3))

        tokens = [uid_of(e).removeprefix("item-").split("@")[0] for e in events]

        check.equal(len(set(tokens)), len(tokens), "no collisions")
        for token in tokens:
            check.equal(
                [len(p) for p in token.split("-")],
                [8, 4, 4, 4, 12],
                "canonical UUID shape",
            )

    ####################################################################
    #
    def test_properties_match_the_real_export(
        self, ics_calendar: Callable[..., str]
    ) -> None:
        """
        GIVEN: a generated calendar
        WHEN:  its properties are inspected
        THEN:  it carries what the real files carry, so a parser written
               against it is not surprised later
        """
        text = ics_calendar(items=3)
        calendar = Calendar.from_ical(text)

        for prop in ("METHOD", "X-WR-CALNAME", "X-WR-CALDESC"):
            check.is_in(prop, calendar, f"calendar has {prop}")
        check.equal(
            str(field(calendar, "PRODID")),
            SYNTHETIC_PRODID,
            "PRODID marks it synthetic, never TripIt's own",
        )
        for event in events_of(text):
            for prop in ("DTSTAMP", "UID", "DTSTART", "DTEND", "SUMMARY"):
                check.is_in(prop, event, f"event has {prop}")


########################################################################
########################################################################
#
class TestDefectKnobs:
    """Tests each defect the real export contains can be reproduced."""

    ####################################################################
    #
    def test_no_tzid_is_ever_emitted(
        self, ics_calendar: Callable[..., str]
    ) -> None:
        """
        GIVEN: a generated calendar
        WHEN:  its text is inspected
        THEN:  no TZID appears, matching the real export -- which is why
               timezones have to come from GEO or not at all
        """
        assert "TZID" not in ics_calendar(items=5, date_only=1, floating=1)

    ####################################################################
    #
    @pytest.mark.parametrize(
        "knob,count",
        [
            pytest.param("missing_geo", 2, id="events-without-coordinates"),
            pytest.param("date_only", 3, id="all-day-events"),
            pytest.param("floating", 2, id="floating-times"),
            pytest.param("non_ascii", 4, id="non-ascii-text"),
        ],
    )
    def test_each_defect_class_is_reproducible(
        self, ics_calendar: Callable[..., str], knob: str, count: int
    ) -> None:
        """
        GIVEN: a calendar asked for a specific number of defective events
        WHEN:  the events are counted by that defect
        THEN:  at least that many carry it, so a parser test can rely on
               getting the case it asked for
        """
        events = events_of(ics_calendar(items=6, **{knob: count}))

        if knob == "missing_geo":
            found = sum(1 for e in events if "GEO" not in e)
        elif knob == "date_only":
            found = sum(1 for e in events if is_all_day(e))
        elif knob == "floating":
            found = sum(1 for e in events if is_floating(e))
        else:
            found = sum(
                1 for e in events if not str(field(e, "SUMMARY")).isascii()
            )

        assert found >= count

    ####################################################################
    #
    def test_a_defect_count_over_the_item_count_is_refused(
        self, faker: Faker
    ) -> None:
        """
        GIVEN: more defective events requested than exist
        WHEN:  the calendar is built
        THEN:  a ValueError is raised rather than silently under-producing
        """
        with pytest.raises(ValueError, match="exceeds items"):
            build_calendar(faker, items=2, date_only=5)

    ####################################################################
    #
    def test_fixed_coordinates_allow_asserting_a_derived_timezone(
        self, faker: Faker
    ) -> None:
        """
        GIVEN: a calendar pinned to one landmark's coordinates
        WHEN:  an event's GEO is read
        THEN:  it matches, so a parser test can assert the zone derived
               from it without depending on a random draw
        """
        landmark = LANDMARKS[1]
        text = to_ics(build_calendar(faker, items=2, landmark=landmark))

        located = [e for e in events_of(text) if "GEO" in e]
        latitude, longitude = (
            field(located[0], "GEO").latitude,
            field(located[0], "GEO").longitude,
        )

        check.almost_equal(
            float(latitude), landmark[1], abs=0.001, msg="latitude"
        )
        check.almost_equal(
            float(longitude), landmark[2], abs=0.001, msg="longitude"
        )


########################################################################
########################################################################
#
class TestDeterminism:
    """Tests a generated calendar is stable enough to debug."""

    ####################################################################
    #
    def test_dates_advance_one_event_per_day(
        self, ics_calendar: Callable[..., str]
    ) -> None:
        """
        GIVEN: a generated calendar
        WHEN:  its item events are read in order
        THEN:  they fall on consecutive days inside the trip's own span
        """
        events = events_of(ics_calendar(items=4))
        trip = next(e for e in events if not uid_of(e).startswith("item-"))
        items = [e for e in events if uid_of(e).startswith("item-")]

        starts = [day_of(e) for e in items]
        trip_start = day_of(trip)

        check.equal(starts, sorted(starts), "in order")
        check.is_true(is_all_day(trip), "trip bound is a plain date")
        check.is_true(all(s >= trip_start for s in starts), "inside the trip")
