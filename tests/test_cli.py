#!/usr/bin/env python
#
"""Test the command line entry point."""

# system imports
import json
from pathlib import Path
from typing import Any

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
from tripsy_exim.sync.importer import in_travel_order, mark_uploaded


####################################################################
#
@pytest.fixture
def runner() -> CliRunner:
    """A Click runner for the command group."""
    return CliRunner()


####################################################################
#
def write_export(tmp_path: Path, *trips: dict) -> Path:
    """One synthetic GDPR export on disk."""
    path = tmp_path / "export.json"
    path.write_text(json.dumps(b.export(*trips)))
    return path


########################################################################
########################################################################
#
class TestStageExport:
    """Tests for the `stage-export` subcommand."""

    ####################################################################
    #
    def test_stages_every_trip_in_the_export(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """
        GIVEN: an export carrying two trips
        WHEN:  stage-export is run against it
        THEN:  both trips are written to the archive and reported
        """
        export = write_export(
            tmp_path,
            b.trip(name="Osaka, Japan, May 2024", objects=[b.flight()]),
            b.trip(
                name="Kyoto, Japan, June 2024",
                start="2024-06-01",
                end="2024-06-04",
                objects=[b.lodging()],
            ),
        )
        archive_root = tmp_path / "archive"

        result = runner.invoke(
            main,
            ["stage-export", str(export), "--archive", str(archive_root)],
        )

        check.equal(result.exit_code, 0, result.output)
        check.equal(len(Archive(archive_root).trip_keys()), 2)
        check.is_in("2 trips", result.output)

    ####################################################################
    #
    def test_scratch_mints_a_throwaway_namespace(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """
        GIVEN: an export and the --scratch flag
        WHEN:  stage-export is run
        THEN:  the namespace is announced and the trip keys carry it
        """
        export = write_export(tmp_path, b.trip(objects=[b.flight()]))
        archive_root = tmp_path / "archive"

        result = runner.invoke(
            main,
            [
                "stage-export",
                str(export),
                "--archive",
                str(archive_root),
                "--scratch",
            ],
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in("scratch namespace:", result.output)
        keys = Archive(archive_root).trip_keys()
        check.equal(len(keys), 1)
        check.is_in("scratch", keys[0])

    ####################################################################
    #
    @pytest.mark.parametrize(
        "command",
        ["stage", "stage-export"],
    )
    def test_namespace_and_scratch_together_are_refused(
        self, runner: CliRunner, tmp_path: Path, command: str
    ) -> None:
        """
        GIVEN: both --namespace and --scratch
        WHEN:  either staging command is run
        THEN:  it exits as a usage error, having written nothing
        """
        source = write_export(tmp_path, b.trip())
        archive_root = tmp_path / "archive"

        result = runner.invoke(
            main,
            [
                command,
                str(source),
                "--archive",
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
                "--archive",
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
        self, runner: CliRunner, tmp_path: Path, faker: Faker
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
            ["stage", str(source), "--archive", str(archive_root)],
        )

        check.equal(result.exit_code, 0, result.output)
        check.equal(len(Archive(archive_root).trip_keys()), 1)


########################################################################
########################################################################
#
class TestUploadCommand:
    """Tests for the `upload` subcommand."""

    ####################################################################
    #
    def test_a_dry_run_sends_nothing_and_needs_no_credentials(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """
        GIVEN: a staged archive and no credentials anywhere
        WHEN:  upload is run without --write
        THEN:  the plan is printed and nothing is sent

        The plan is what a person reads before the one step that cannot
        be undone, so it must not require an account to see.
        """
        for name in (
            "TRIPSY_USERNAME",
            "TRIPSY_PASSWORD",
            "TRIPSY_ONEPASSWORD_URL",
        ):
            monkeypatch.delenv(name, raising=False)

        export = write_export(tmp_path, b.trip(objects=[b.flight()]))
        archive_root = tmp_path / "archive"
        runner.invoke(
            main,
            ["stage-export", str(export), "--archive", str(archive_root)],
        )

        result = runner.invoke(main, ["upload", "--archive", str(archive_root)])

        check.equal(result.exit_code, 0, result.output)
        check.is_in("Dry run", result.output)
        check.is_in("Nothing was sent", result.output)

    ####################################################################
    #
    def test_an_empty_archive_is_refused(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """
        GIVEN: an archive holding no staged trips
        WHEN:  upload is run
        THEN:  it fails saying so, rather than reporting a run of nothing
        """
        empty = tmp_path / "archive"
        empty.mkdir()

        result = runner.invoke(main, ["upload", "--archive", str(empty)])

        check.not_equal(result.exit_code, 0)
        check.is_in("no staged trips", result.output)

    ####################################################################
    #
    def test_trips_are_selected_by_name(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """
        GIVEN: an archive of two trips
        WHEN:  upload names one by part of its name
        THEN:  only that trip is planned

        A trip key is a digest nobody can recognise or type.
        """
        export = write_export(
            tmp_path,
            b.trip(name="Osaka, Japan, May 2024", objects=[b.flight()]),
            b.trip(
                name="Kyoto, Japan, June 2024",
                start="2024-06-01",
                end="2024-06-04",
                objects=[b.lodging()],
            ),
        )
        archive_root = tmp_path / "archive"
        runner.invoke(
            main, ["stage-export", str(export), "--archive", str(archive_root)]
        )

        result = runner.invoke(
            main,
            ["upload", "--archive", str(archive_root), "--trip", "kyoto"],
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in("Kyoto", result.output)
        check.is_not_in("Osaka", result.output)
        check.is_in("1 trips", result.output)

    ####################################################################
    #
    def test_an_ambiguous_name_is_refused(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """
        GIVEN: two trips whose names share a word
        WHEN:  upload names that word
        THEN:  it refuses and lists what matched

        Picking one would upload the wrong trip, and an identifier spent
        on the wrong trip cannot be taken back.
        """
        export = write_export(
            tmp_path,
            b.trip(name="Osaka, Japan, May 2024", objects=[b.flight()]),
            b.trip(
                name="Kyoto, Japan, June 2024",
                start="2024-06-01",
                end="2024-06-04",
                objects=[b.lodging()],
            ),
        )
        archive_root = tmp_path / "archive"
        runner.invoke(
            main, ["stage-export", str(export), "--archive", str(archive_root)]
        )

        result = runner.invoke(
            main,
            ["upload", "--archive", str(archive_root), "--trip", "japan"],
        )

        check.not_equal(result.exit_code, 0)
        check.is_in("2 trips match", result.output)

    ####################################################################
    #
    def test_trips_are_planned_oldest_first(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """
        GIVEN: an archive of trips staged in no particular order
        WHEN:  upload plans them with a limit
        THEN:  the oldest is the one taken

        The archive orders trips by a digest, so without this --limit
        picks an arbitrary handful rather than working forward.
        """
        export = write_export(
            tmp_path,
            b.trip(
                name="Later trip",
                start="2024-09-01",
                end="2024-09-04",
                objects=[b.flight()],
            ),
            b.trip(
                name="Earlier trip",
                start="2024-01-01",
                end="2024-01-04",
                objects=[b.lodging()],
            ),
        )
        archive_root = tmp_path / "archive"
        runner.invoke(
            main, ["stage-export", str(export), "--archive", str(archive_root)]
        )

        result = runner.invoke(
            main, ["upload", "--archive", str(archive_root), "--limit", "1"]
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in("Earlier trip", result.output)
        check.is_not_in("Later trip", result.output)

    ####################################################################
    #
    def test_a_finished_trip_does_not_spend_the_limit(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """
        GIVEN: an archive whose oldest trip a run already finished
        WHEN:  upload runs with --limit 1
        THEN:  the next trip is planned instead of nothing

        Otherwise running with --limit 1 twice would do the first trip
        and then nothing, rather than walking forward a trip at a time.
        """
        export = write_export(
            tmp_path,
            b.trip(
                name="Earlier trip",
                start="2024-01-01",
                end="2024-01-04",
                objects=[b.flight()],
            ),
            b.trip(
                name="Later trip",
                start="2024-09-01",
                end="2024-09-04",
                objects=[b.lodging()],
            ),
        )
        archive_root = tmp_path / "archive"
        runner.invoke(
            main, ["stage-export", str(export), "--archive", str(archive_root)]
        )

        archive = Archive(archive_root)
        oldest = in_travel_order(archive, archive.trip_keys())[0]
        mark_uploaded(archive, oldest)

        result = runner.invoke(
            main, ["upload", "--archive", str(archive_root), "--limit", "1"]
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in("Later trip", result.output)
        check.is_in("1 trips skipped", result.output)


########################################################################
########################################################################
#
class TestListCommand:
    """Tests for the `list` subcommand."""

    ####################################################################
    #
    def test_trips_are_listed_oldest_first_with_their_state(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """
        GIVEN: an archive of two trips, one already uploaded
        WHEN:  list is run
        THEN:  both appear oldest first, and the finished one is marked
        """
        export = write_export(
            tmp_path,
            b.trip(
                name="Later trip",
                start="2024-09-01",
                end="2024-09-04",
                objects=[b.flight()],
            ),
            b.trip(
                name="Earlier trip",
                start="2024-01-01",
                end="2024-01-04",
                objects=[b.lodging()],
            ),
        )
        archive_root = tmp_path / "archive"
        runner.invoke(
            main, ["stage-export", str(export), "--archive", str(archive_root)]
        )
        archive = Archive(archive_root)
        mark_uploaded(archive, in_travel_order(archive, archive.trip_keys())[0])

        result = runner.invoke(main, ["list", "--archive", str(archive_root)])

        check.equal(result.exit_code, 0, result.output)
        check.less(
            result.output.index("Earlier trip"),
            result.output.index("Later trip"),
            "oldest first",
        )
        check.is_in("1 already uploaded", result.output)

    ####################################################################
    #
    def test_pending_hides_what_is_done(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """
        GIVEN: an archive whose oldest trip is uploaded
        WHEN:  list is run with --pending
        THEN:  only the trip still to do is shown
        """
        export = write_export(
            tmp_path,
            b.trip(
                name="Earlier trip",
                start="2024-01-01",
                end="2024-01-04",
                objects=[b.flight()],
            ),
            b.trip(
                name="Later trip",
                start="2024-09-01",
                end="2024-09-04",
                objects=[b.lodging()],
            ),
        )
        archive_root = tmp_path / "archive"
        runner.invoke(
            main, ["stage-export", str(export), "--archive", str(archive_root)]
        )
        archive = Archive(archive_root)
        mark_uploaded(archive, in_travel_order(archive, archive.trip_keys())[0])

        result = runner.invoke(
            main, ["list", "--archive", str(archive_root), "--pending"]
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in("Later trip", result.output)
        check.is_not_in("Earlier trip", result.output)
