#!/usr/bin/env python
#
"""Test the `stage` and `stage-export` subcommands."""

# system imports
import json
from collections.abc import Callable
from pathlib import Path

# 3rd party imports
import pytest
import pytest_check as check
from click.testing import CliRunner
from faker import Faker

# Project imports
from tests import tripit_builder as b
from tests.ics_builder import build_calendar, to_ics
from tripsy_exim.cli import main
from tripsy_exim.store import Archive


########################################################################
########################################################################
#
class TestStageExport:
    """Tests for the `stage-export` subcommand."""

    ####################################################################
    #
    def test_stages_every_trip_in_the_export(
        self,
        runner: CliRunner,
        tmp_path: Path,
        write_export: Callable[..., Path],
        named_pair: tuple[dict, dict],
        opened: Callable[[Path], Archive],
    ) -> None:
        """
        GIVEN: an export carrying two trips
        WHEN:  stage-export is run against it
        THEN:  both trips are written to the archive and reported
        """
        export = write_export(*named_pair)
        archive_root = tmp_path / "archive"

        result = runner.invoke(
            main,
            ["stage-export", str(export), "--archive-root", str(archive_root)],
        )

        check.equal(result.exit_code, 0, result.output)
        check.equal(len(opened(archive_root).trip_keys()), 2)
        check.is_in("2 trips", result.output)

    ####################################################################
    #
    def test_scratch_mints_a_throwaway_namespace(
        self,
        runner: CliRunner,
        tmp_path: Path,
        write_export: Callable[..., Path],
        opened: Callable[[Path], Archive],
    ) -> None:
        """
        GIVEN: an export and the --scratch flag
        WHEN:  stage-export is run
        THEN:  the namespace is announced and the trip keys carry it
        """
        export = write_export(b.trip(objects=[b.flight()]))
        archive_root = tmp_path / "archive"

        result = runner.invoke(
            main,
            [
                "stage-export",
                str(export),
                "--archive-root",
                str(archive_root),
                "--scratch",
            ],
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in("scratch namespace:", result.output)
        keys = opened(archive_root).trip_keys()
        check.equal(len(keys), 1)
        check.is_in("scratch", keys[0])

    ####################################################################
    #
    @pytest.mark.parametrize(
        "command",
        ["stage", "stage-export"],
    )
    def test_namespace_and_scratch_together_are_refused(
        self,
        runner: CliRunner,
        tmp_path: Path,
        write_export: Callable[..., Path],
        command: str,
    ) -> None:
        """
        GIVEN: both --namespace and --scratch
        WHEN:  either staging command is run
        THEN:  it exits as a usage error, having written nothing
        """
        source = write_export(b.trip())
        archive_root = tmp_path / "archive"

        result = runner.invoke(
            main,
            [
                command,
                str(source),
                "--archive-root",
                str(archive_root),
                "--namespace",
                "chosen",
                "--scratch",
            ],
        )

        check.equal(result.exit_code, 2, result.output)
        check.is_in("one or the other", result.output)
        check.is_false(archive_root.exists())

    ####################################################################
    #
    def test_a_file_that_is_not_an_export_is_reported(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """
        GIVEN: a JSON file that is not a TripIt export
        WHEN:  stage-export is run against it
        THEN:  it fails with a message naming the file
        """
        path = tmp_path / "not-an-export.json"
        path.write_text(json.dumps({"something": "else"}))

        result = runner.invoke(
            main,
            [
                "stage-export",
                str(path),
                "--archive-root",
                str(tmp_path / "archive"),
            ],
        )

        check.not_equal(result.exit_code, 0)
        check.is_in("not-an-export.json", result.output)


########################################################################
########################################################################
#
class TestStage:
    """Tests for the `stage` subcommand, which reads calendars."""

    ####################################################################
    #
    def test_stages_a_calendar(
        self,
        runner: CliRunner,
        tmp_path: Path,
        faker: Faker,
        opened: Callable[[Path], Archive],
    ) -> None:
        """
        GIVEN: one synthetic .ics calendar
        WHEN:  stage is run against it
        THEN:  the trip is written to the archive
        """
        source = tmp_path / "trip.ics"
        source.write_text(to_ics(build_calendar(faker)))
        archive_root = tmp_path / "archive"

        result = runner.invoke(
            main,
            ["stage", str(source), "--archive-root", str(archive_root)],
        )

        check.equal(result.exit_code, 0, result.output)
        check.equal(len(opened(archive_root).trip_keys()), 1)
