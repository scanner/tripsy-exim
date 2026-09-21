#!/usr/bin/env python
#
"""
Test recognising one trip across the two sources.

The rules asserted here were derived from the whole reference corpus --
every one of its 61 calendars keys onto exactly one export trip -- and
the values below are the shapes that made that work.
"""

# system imports
from datetime import date

# 3rd party imports
import pytest

# Project imports
from tripsy_exim.sources.join import (
    SEPARATOR,
    key_from_calendar,
    trip_key,
    trip_name,
)


########################################################################
########################################################################
#
class TestTripName:
    """Tests for pulling a trip's own name out of a calendar."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "caldesc,expected",
        [
            (
                "Kyoto, May 2011 (Trip Shared by Another Traveller)",
                "Kyoto, May 2011",
            ),
            (
                "日本の旅行 (Trip Shared by Someone Else)",
                "日本の旅行",
            ),
            ("Osaka, June 2012", "Osaka, June 2012"),
            ("", ""),
        ],
        ids=["shared", "non-ascii", "no-suffix", "empty"],
    )
    def test_the_sharer_suffix_is_removed(
        self, caldesc: str, expected: str
    ) -> None:
        """
        GIVEN: a calendar description as TripIt writes it
        WHEN:  the trip's own name is taken from it
        THEN:  the sharer suffix is gone

        The suffix names whoever exported the file, not the trip, so two
        people sharing one trip would otherwise key it differently.
        """
        assert trip_name(caldesc) == expected


########################################################################
########################################################################
#
class TestTripKey:
    """Tests for the key itself."""

    ####################################################################
    #
    def test_a_key_is_name_and_span(self) -> None:
        """
        GIVEN: a name and the days a trip covers
        WHEN:  a key is built
        THEN:  it carries all three, separated unambiguously
        """
        key = trip_key("Osaka", date(2026, 11, 21), date(2026, 12, 7))

        assert key == SEPARATOR.join(("Osaka", "2026-11-21", "2026-12-07"))

    ####################################################################
    #
    @pytest.mark.parametrize(
        "name,starts,ends",
        [
            (None, date(2026, 1, 1), date(2026, 1, 2)),
            ("", date(2026, 1, 1), date(2026, 1, 2)),
            ("Osaka", None, date(2026, 1, 2)),
            ("Osaka", date(2026, 1, 1), None),
        ],
        ids=["no-name", "empty-name", "no-start", "no-end"],
    )
    def test_an_incomplete_trip_has_no_key(
        self, name: str | None, starts: date | None, ends: date | None
    ) -> None:
        """
        GIVEN: a trip missing a part of its identity
        WHEN:  a key is asked for
        THEN:  None comes back rather than a partial key

        A partial key would match the wrong trip, and adopting the wrong
        trip is worse than failing to adopt at all.
        """
        assert trip_key(name, starts, ends) is None

    ####################################################################
    #
    def test_names_differing_only_by_spacing_do_not_collide(self) -> None:
        """
        GIVEN: two trips whose names differ beyond surrounding space
        WHEN:  keys are built
        THEN:  the keys differ, while surrounding space is ignored
        """
        padded = trip_key("  Osaka  ", date(2026, 1, 1), date(2026, 1, 2))
        plain = trip_key("Osaka", date(2026, 1, 1), date(2026, 1, 2))
        other = trip_key("Osaka 2", date(2026, 1, 1), date(2026, 1, 2))

        assert padded == plain
        assert plain != other


########################################################################
########################################################################
#
class TestKeyFromCalendar:
    """Tests for the calendar side, which ends a day late."""

    ####################################################################
    #
    def test_the_exclusive_end_is_corrected(self) -> None:
        """
        GIVEN: a calendar spanning up to the morning after the trip
        WHEN:  its key is built
        THEN:  the key names the trip's own last day

        An all-day DTEND names the day after the event, so a calendar's
        span runs one day past what the export calls the end.  Verified
        across the whole reference corpus.
        """
        key = key_from_calendar(
            "Osaka (Trip Shared by Someone)",
            date(2026, 11, 21),
            date(2026, 12, 8),
        )

        assert key == trip_key("Osaka", date(2026, 11, 21), date(2026, 12, 7))

    ####################################################################
    #
    @pytest.mark.parametrize(
        "starts,ends",
        [(None, date(2026, 1, 2)), (date(2026, 1, 1), None)],
        ids=["no-start", "no-end"],
    )
    def test_a_calendar_without_a_span_has_no_key(
        self, starts: date | None, ends: date | None
    ) -> None:
        """
        GIVEN: a calendar whose span could not be determined
        WHEN:  its key is asked for
        THEN:  None comes back
        """
        assert key_from_calendar("Osaka (Trip Shared by X)", starts, ends) is (
            None
        )
