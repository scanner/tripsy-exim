#!/usr/bin/env python
#
"""Test the `upload` subcommand."""

# system imports
from collections.abc import Callable, MutableMapping
from pathlib import Path

# 3rd party imports
import pytest_check as check
from click.testing import CliRunner

# Project imports
from tests import tripit_builder as b
from tests.cli.conftest import EARLIER, KYOTO_TRIP, LATER, OSAKA_TRIP
from tripsy_exim.cli import main
from tripsy_exim.secrets import SECRET_URL_ENV
from tripsy_exim.store import DEFAULT_ARCHIVE, Archive, staged_path
from tripsy_exim.sync.importer import in_travel_order, mark_uploaded


########################################################################
########################################################################
#
class TestUploadCommand:
    """Tests for the `upload` subcommand."""

    ####################################################################
    #
    def test_a_dry_run_sends_nothing_and_needs_no_credentials(
        self,
        runner: CliRunner,
        staged: Callable[..., Path],
        environment: MutableMapping[str, str],
    ) -> None:
        """
        GIVEN: a staged archive and no credentials anywhere
        WHEN:  upload is run without --write
        THEN:  the plan is printed and nothing is sent

        The plan is what a person reads before the one step that cannot
        be undone, so it must not require an account to see.
        """
        for name in ("TRIPSY_USERNAME", "TRIPSY_PASSWORD", SECRET_URL_ENV):
            environment.pop(name, None)

        archive_root = staged()

        result = runner.invoke(
            main, ["upload", "--archive-root", str(archive_root)]
        )

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
        root = tmp_path / "archive"
        staged_path(root, DEFAULT_ARCHIVE).mkdir(parents=True)

        result = runner.invoke(main, ["upload", "--archive-root", str(root)])

        check.not_equal(result.exit_code, 0)
        check.is_in("no staged trips", result.output)

    ####################################################################
    #
    def test_trips_are_selected_by_name(
        self,
        runner: CliRunner,
        staged: Callable[..., Path],
        named_pair: tuple[dict, dict],
    ) -> None:
        """
        GIVEN: an archive of two trips
        WHEN:  upload names one by part of its name
        THEN:  only that trip is planned

        A trip key is a digest nobody can recognise or type.
        """
        archive_root = staged(*named_pair)

        result = runner.invoke(
            main,
            ["upload", "--archive-root", str(archive_root), "--trip", "kyoto"],
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in(KYOTO_TRIP, result.output)
        check.is_not_in(OSAKA_TRIP, result.output)
        check.is_in("1 trip", result.output)

    ####################################################################
    #
    def test_an_ambiguous_name_is_refused(
        self,
        runner: CliRunner,
        staged: Callable[..., Path],
        named_pair: tuple[dict, dict],
    ) -> None:
        """
        GIVEN: two trips whose names share a word
        WHEN:  upload names that word
        THEN:  it refuses and lists what matched

        Picking one would upload the wrong trip, and an identifier spent
        on the wrong trip cannot be taken back.
        """
        archive_root = staged(*named_pair)

        result = runner.invoke(
            main,
            ["upload", "--archive-root", str(archive_root), "--trip", "japan"],
        )

        check.not_equal(result.exit_code, 0)
        check.is_in("2 trips match", result.output)

    ####################################################################
    #
    def test_trips_are_planned_oldest_first(
        self,
        runner: CliRunner,
        staged: Callable[..., Path],
        dated_pair: tuple[dict, dict],
    ) -> None:
        """
        GIVEN: an archive of trips staged in no particular order
        WHEN:  upload plans them with a limit
        THEN:  the oldest is the one taken

        The archive orders trips by a digest, so without this --limit
        picks an arbitrary handful rather than working forward.
        """
        archive_root = staged(*dated_pair)

        result = runner.invoke(
            main,
            ["upload", "--archive-root", str(archive_root), "--limit", "1"],
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in(EARLIER, result.output)
        check.is_not_in(LATER, result.output)

    ####################################################################
    #
    def test_a_finished_trip_does_not_spend_the_limit(
        self,
        runner: CliRunner,
        staged: Callable[..., Path],
        dated_pair: tuple[dict, dict],
        opened: Callable[[Path], Archive],
    ) -> None:
        """
        GIVEN: an archive whose oldest trip a run already finished
        WHEN:  upload runs with --limit 1
        THEN:  the next trip is planned instead of nothing

        Otherwise running with --limit 1 twice would do the first trip
        and then nothing, rather than walking forward a trip at a time.
        """
        archive_root = staged(*dated_pair)

        archive = opened(archive_root)
        oldest = in_travel_order(archive, archive.trip_keys())[0]
        mark_uploaded(archive, oldest)

        result = runner.invoke(
            main,
            ["upload", "--archive-root", str(archive_root), "--limit", "1"],
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in(LATER, result.output)
        check.is_in("1 trips skipped", result.output)

    ####################################################################
    #
    def test_a_write_is_refused_while_a_leg_carries_no_type(
        self,
        runner: CliRunner,
        staged: Callable[..., Path],
        environment: MutableMapping[str, str],
    ) -> None:
        """
        GIVEN: a staged trip holding a leg the reader could not type
        WHEN:  upload is run with --write
        THEN:  it refuses before sending anything

        Tripsy requires a transportation to say what kind it is, so an
        untyped leg is refused one object at a time in the middle of a
        run and its trip goes up short.
        """
        environment["TRIPSY_USERNAME"] = "someone"
        environment["TRIPSY_PASSWORD"] = "secret"

        archive_root = staged(
            b.trip(objects=[b.flight(), b.untyped_transport()])
        )

        result = runner.invoke(
            main, ["upload", "--archive-root", str(archive_root), "--write"]
        )

        check.not_equal(result.exit_code, 0)
        check.is_in("no transportation_type", result.output)
