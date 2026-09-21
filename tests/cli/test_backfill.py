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
import json
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


########################################################################
########################################################################
#
class TestBackfillRoundTrip:
    """Tests for writing the work-list out and reading it back."""

    ####################################################################
    #
    def test_export_then_apply_closes_the_gap(
        self,
        unplaceable: Path,
        runner: CliRunner,
        tmp_path: Path,
        report: Callable[..., str],
    ) -> None:
        """
        GIVEN: an archive with unplaceable endpoints
        WHEN:  the work-list is exported, filled in and applied
        THEN:  the report no longer lists what was answered

        The whole point of the pair, end to end: nothing else proves
        that what `export` writes is what `apply` can read.
        """
        work = tmp_path / "work.json"
        written = runner.invoke(
            main,
            [
                "backfill",
                "export",
                str(work),
                "--archive",
                str(unplaceable),
            ],
        )
        assert written.exit_code == 0, written.output

        document = json.loads(work.read_text())
        for row in document["rows"]:
            if row["object"].startswith("NAR"):
                row["fields"]["departure_address"] = "Narita Airport"
        work.write_text(json.dumps(document))

        applied = runner.invoke(
            main,
            [
                "backfill",
                "apply",
                str(work),
                "--archive",
                str(unplaceable),
                "--write",
            ],
        )

        check.equal(applied.exit_code, 0, applied.output)
        check.is_in("2 written", applied.output)
        check.is_not_in("NAR", report(unplaceable))

    ####################################################################
    #
    def test_apply_is_a_dry_run_by_default(
        self,
        unplaceable: Path,
        runner: CliRunner,
        tmp_path: Path,
        report: Callable[..., str],
    ) -> None:
        """
        GIVEN: a filled-in work-list
        WHEN:  apply runs without --write
        THEN:  it says what it would do and the gaps stay open

        Matching `upload` and `fix-locations`, which are also dry by
        default.  A command that writes corrections unasked would be
        the odd one out.
        """
        work = tmp_path / "work.json"
        runner.invoke(
            main,
            ["backfill", "export", str(work), "--archive", str(unplaceable)],
        )
        document = json.loads(work.read_text())
        for row in document["rows"]:
            row["fields"]["departure_address"] = "Somewhere"
        work.write_text(json.dumps(document))

        result = runner.invoke(
            main,
            ["backfill", "apply", str(work), "--archive", str(unplaceable)],
        )

        check.is_in("would be written", result.output)
        check.is_in("Nothing was saved", result.output)
        check.is_in("NAR", report(unplaceable), "still open")

    ####################################################################
    #
    def test_export_refuses_when_nothing_is_open(
        self, staged: Callable[..., Path], runner: CliRunner, tmp_path: Path
    ) -> None:
        """
        GIVEN: an archive whose trip places everything
        WHEN:  a work-list is exported
        THEN:  it fails rather than writing an empty file

        An empty work-list is a file somebody would then edit and apply
        to no effect.  Saying so up front is the better answer.
        """
        work = tmp_path / "work.json"

        result = runner.invoke(
            main,
            ["backfill", "export", str(work), "--archive", str(staged())],
        )

        check.equal(result.exit_code, 1)
        check.is_in("nothing is open", result.output)
        check.is_false(work.exists())

    ####################################################################
    #
    def test_a_work_list_from_another_archive_is_refused(
        self,
        unplaceable: Path,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """
        GIVEN: a work-list exported from one archive
        WHEN:  it is applied to another
        THEN:  it is refused rather than matching nothing

        Applied anyway it would report that nothing needed doing, which
        is a true statement and the wrong answer.
        """
        work = tmp_path / "work.json"
        runner.invoke(
            main,
            ["backfill", "export", str(work), "--archive", str(unplaceable)],
        )

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        result = runner.invoke(
            main,
            [
                "backfill",
                "apply",
                str(work),
                "--archive",
                str(elsewhere),
                "--write",
            ],
        )

        check.equal(result.exit_code, 1)
        check.is_in("was exported from", result.output)
