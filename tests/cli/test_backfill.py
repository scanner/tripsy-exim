#!/usr/bin/env python
#
"""
Test the backfill commands.

`backfill report` writes nothing, so what is asserted here is what a
reader is told: that the counts are right, that the places are ordered
so the work finishes soonest, and that a trip with nothing open says so
rather than printing an empty table.
"""

# system imports
from collections.abc import Callable
from pathlib import Path

# 3rd party imports
import pytest
import pytest_check as check
from click.testing import CliRunner

# Project imports
from tests import tripit_builder as b
from tripsy_exim.cli import main

# The trip the unplaceable fixtures stage, named so a test reading the
# output can recognise it.
#
NARITA_TRIP = "Narita, May 2024"


####################################################################
#
@pytest.fixture
def unplaceable(staged: Callable[..., Path]) -> Path:
    """
    An archive of one trip whose two flights share an unplaced airport.

    `placed=False` is what makes an endpoint unplaceable: the export
    carries the airport's code without its name, so there is neither an
    address to geocode nor a position to draw a pin from.  Both flights
    leave from the same one, which is what the recurrence ordering is
    there to surface.
    """
    return staged(
        b.trip(
            name=NARITA_TRIP,
            objects=[
                b.flight(frm="Narita", to="Vancouver", placed=False),
                b.flight(frm="Narita", to="Seattle", placed=False),
            ],
        )
    )


####################################################################
#
@pytest.fixture
def report(runner: CliRunner) -> Callable[..., str]:
    """Run `backfill report` against an archive and give back its output."""

    def run(archive_root: Path, *args: str) -> str:
        result = runner.invoke(
            main,
            ["backfill", "report", "--archive", str(archive_root), *args],
        )
        assert result.exit_code == 0, result.output
        return result.output

    return run


########################################################################
########################################################################
#
class TestBackfillReport:
    """Tests for saying what still needs a person."""

    ####################################################################
    #
    def test_unplaceable_endpoints_are_counted_against_their_trip(
        self, unplaceable: Path, report: Callable[..., str]
    ) -> None:
        """
        GIVEN: an archive of one trip with four unplaced endpoints
        WHEN:  the report is run
        THEN:  the trip is named with its count and the remedy
        """
        output = report(unplaceable)

        check.is_in(NARITA_TRIP, output)
        check.is_in("unplaceable", output)
        check.is_in("4", output)
        check.is_in("correct", output, "the kind of correction to write")

    ####################################################################
    #
    def test_the_repeated_place_is_listed_first(
        self, unplaceable: Path, report: Callable[..., str]
    ) -> None:
        """
        GIVEN: two flights that both leave from one unplaced airport
        WHEN:  the report is run
        THEN:  that airport heads the list of places

        Answering the place that recurs closes two gaps at once, so it
        is what a person should be shown first.
        """
        output = report(unplaceable)

        listed = output.split("by place, commonest first:")[1]
        places = [
            line.split()[1]
            for line in listed.splitlines()
            if line.startswith("  ") and "x " in line
        ]

        check.equal(places[0], "NAR")
        check.equal(sorted(places[1:]), ["SEA", "VAN"])

    ####################################################################
    #
    def test_a_trip_that_places_everything_says_nothing_is_open(
        self, staged: Callable[..., Path], report: Callable[..., str]
    ) -> None:
        """
        GIVEN: an archive whose trip places both ends of its flight
        WHEN:  the report is run
        THEN:  it says so rather than printing an empty table

        The ordinary export places what it carries, so this is the
        common case and it has to read as success.
        """
        output = report(staged())

        check.is_in("nothing open", output)
        check.is_not_in("by place", output)

    ####################################################################
    #
    def test_a_population_can_be_asked_for_on_its_own(
        self, unplaceable: Path, report: Callable[..., str]
    ) -> None:
        """
        GIVEN: an archive whose only open question is about placing
        WHEN:  the report is asked for retypes instead
        THEN:  nothing is reported

        Filtering is what makes the report usable one kind at a time,
        and a filter that quietly reported everything would be worse
        than none.
        """
        output = report(unplaceable, "--population", "unclassified")

        check.is_in("nothing open", output)
        check.is_not_in("NAR", output)

    ####################################################################
    #
    def test_one_trip_can_be_named(
        self,
        staged: Callable[..., Path],
        report: Callable[..., str],
    ) -> None:
        """
        GIVEN: an archive of two trips, one of which places nothing
        WHEN:  the report names that trip by part of its name
        THEN:  only that trip is reported on
        """
        archive_root = staged(
            b.trip(
                name=NARITA_TRIP,
                objects=[b.flight(frm="Narita", to="Osaka", placed=False)],
            ),
            b.trip(
                name="Kyoto, June 2024",
                start="2024-06-01",
                end="2024-06-04",
                objects=[b.lodging()],
            ),
        )

        output = report(archive_root, "narita")

        check.is_in(NARITA_TRIP, output)
        check.is_not_in("Kyoto", output)
        check.is_in("gaps across 1 trips", output)

    ####################################################################
    #
    def test_an_empty_archive_is_refused(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """
        GIVEN: an archive directory holding no trips
        WHEN:  the report is run against it
        THEN:  it fails and names the directory

        Nothing to report and nothing staged are different answers, and
        the second is usually the wrong --archive.
        """
        empty = tmp_path / "empty"
        empty.mkdir()

        result = runner.invoke(
            main, ["backfill", "report", "--archive", str(empty)]
        )

        check.equal(result.exit_code, 1)
        check.is_in(str(empty), result.output)
