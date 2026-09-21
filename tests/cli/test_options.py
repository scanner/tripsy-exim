#!/usr/bin/env python
#
"""
Test the options every command resolves the same way.

Which directory the archive is, and how it is named, is settled once in
the command line layer rather than per command.  These tests drive it
through whichever command is cheapest to run, not because the behaviour
belongs to that command.
"""

# system imports
from collections.abc import Callable, MutableMapping
from pathlib import Path

# 3rd party imports
import pytest
import pytest_check as check
from click.testing import CliRunner
from pytest_mock import MockerFixture

# Project imports
from tests import tripit_builder as b
from tripsy_exim.cli import main
from tripsy_exim.store import (
    ARCHIVE_ENV,
    DEFAULT_ARCHIVE,
    Archive,
    staged_path,
)


########################################################################
########################################################################
#
class TestArchiveResolution:
    """Tests for settling which directory the archive is."""

    ####################################################################
    #
    @pytest.mark.uses_dotenv
    def test_dot_env_names_the_archive(
        self,
        runner: CliRunner,
        tmp_path: Path,
        environment: MutableMapping[str, str],
        in_directory: Callable[[Path], None],
        write_export: Callable[..., Path],
    ) -> None:
        """
        GIVEN: an archive named only in a .env file
        WHEN:  a command that reads the archive runs
        THEN:  it reads that archive

        .env has to be loaded before a default is resolved from the
        environment, not when credentials are first needed -- a dry run
        never asks for credentials at all.
        """
        export = write_export(b.trip(objects=[b.flight()]))
        archive_root = tmp_path / "named-by-dotenv"
        runner.invoke(
            main,
            ["stage-export", str(export), "--archive-root", str(archive_root)],
        )

        project = tmp_path / "project"
        project.mkdir()
        (project / ".env").write_text(f"{ARCHIVE_ENV}={archive_root}\n")
        in_directory(project)
        environment.pop(ARCHIVE_ENV, None)

        result = runner.invoke(main, ["list"])

        check.equal(result.exit_code, 0, result.output)
        check.is_in("1 trip", result.output)

    ####################################################################
    #
    def test_a_leading_tilde_is_expanded(
        self,
        runner: CliRunner,
        tmp_path: Path,
        environment: MutableMapping[str, str],
        write_export: Callable[..., Path],
    ) -> None:
        """
        GIVEN: an archive root written with a leading ~
        WHEN:  a command resolves it
        THEN:  it lands under the home directory

        A shell expands one on the command line, but nothing expands one
        written in .env or exported with quotes.
        """
        home = tmp_path / "home"
        home.mkdir()
        environment["HOME"] = str(home)
        environment[ARCHIVE_ENV] = "~/Documents/tripsy-archive"

        export = write_export(b.trip(objects=[b.flight()]))
        result = runner.invoke(main, ["stage-export", str(export)])

        check.equal(result.exit_code, 0, result.output)
        check.is_true(
            staged_path(home / "Documents" / "tripsy-archive", DEFAULT_ARCHIVE)
            .joinpath("trips")
            .is_dir(),
            "staged under the expanded path",
        )

    ####################################################################
    #
    def test_the_name_picks_which_archive_a_command_works_on(
        self, runner: CliRunner, staged: Callable[..., Path]
    ) -> None:
        """
        GIVEN: two staging archives under one root
        WHEN:  a command names one of them
        THEN:  it sees that one's trips and not the other's

        The whole point of naming archives: one root can hold the TripIt
        import and a separate scratch archive without either being able
        to see the other.
        """
        root = staged(b.trip(name="In the default one"))
        staged(b.trip(name="In the other one"), archive="scratch")

        listed = runner.invoke(
            main, ["list", "--archive-root", str(root), "--archive", "scratch"]
        )

        check.equal(listed.exit_code, 0, listed.output)
        check.is_in("In the other one", listed.output)
        check.is_not_in("In the default one", listed.output)

    ####################################################################
    #
    def test_a_name_that_is_not_a_directory_component_is_refused(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """
        GIVEN: an archive name the store will not accept
        WHEN:  a command resolves it
        THEN:  it is reported as an error, not raised

        Which names are refused is settled in the store and tested
        there.  What is this layer's is the translation: the store
        raises ValueError, and a person running a command has to see a
        message saying what a usable name looks like rather than a
        traceback.
        """
        result = runner.invoke(
            main,
            ["list", "--archive-root", str(tmp_path), "--archive", "../up"],
        )

        check.not_equal(result.exit_code, 0)
        check.is_in("not a usable archive name", result.output)
        check.is_not_instance(
            result.exception, ValueError, "reported, not raised"
        )

    ####################################################################
    #
    def test_a_missing_archive_names_the_resolved_path(
        self,
        runner: CliRunner,
        tmp_path: Path,
        environment: MutableMapping[str, str],
    ) -> None:
        """
        GIVEN: an archive directory that does not exist
        WHEN:  a command that reads the archive runs
        THEN:  the error names where it actually looked

        The path may have come from .env or from a default, so repeating
        it back is the only way to see which one was used.
        """
        environment[ARCHIVE_ENV] = str(tmp_path / "nowhere")

        result = runner.invoke(main, ["list"])

        check.not_equal(result.exit_code, 0)
        check.is_in("nowhere", result.output)

    ####################################################################
    #
    def test_an_unreadable_archive_is_reported_not_raised(
        self,
        runner: CliRunner,
        tmp_path: Path,
        environment: MutableMapping[str, str],
        mocker: MockerFixture,
    ) -> None:
        """
        GIVEN: an archive whose directory cannot be read
        WHEN:  a command that reads the archive runs
        THEN:  it says so rather than raising out of a directory walk

        macOS refuses a folder under Documents to a process it has not
        been told to trust, and a scheduled run has nobody to ask.

        The refusal is provoked rather than staged.  Making a real
        directory unreadable tests the operating system, not this code:
        root ignores the mode bits and so do some filesystems, so the
        same test means different things in different places.  What is
        ours is the one branch -- that an OSError out of the archive
        becomes a message.
        """
        root = tmp_path / "locked"
        staged_path(root, DEFAULT_ARCHIVE).joinpath("trips").mkdir(parents=True)
        environment[ARCHIVE_ENV] = str(root)
        mocker.patch.object(
            Archive,
            "trip_keys",
            side_effect=PermissionError(13, "Permission denied"),
        )

        result = runner.invoke(main, ["list"])

        check.not_equal(result.exit_code, 0)
        check.is_in("cannot read the archive", result.output)
        check.is_in("Permission denied", result.output)
        check.is_not_instance(result.exception, OSError, "reported, not raised")
