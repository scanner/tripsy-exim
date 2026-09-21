#!/usr/bin/env python
#
"""Test the `list` subcommand."""

# system imports
from collections.abc import Callable
from pathlib import Path

# 3rd party imports
import pytest_check as check
from click.testing import CliRunner

# Project imports
from tests.cli.conftest import EARLIER, LATER
from tripsy_exim.cli import main
from tripsy_exim.store import Archive
from tripsy_exim.sync.importer import in_travel_order, mark_uploaded


########################################################################
########################################################################
#
class TestListCommand:
    """Tests for the `list` subcommand."""

    ####################################################################
    #
    def test_what_is_listed_and_what_pending_hides(
        self,
        runner: CliRunner,
        staged: Callable[..., Path],
        dated_pair: tuple[dict, dict],
        opened: Callable[[Path], Archive],
    ) -> None:
        """
        GIVEN: an archive of two trips, the older already uploaded
        WHEN:  list is run plainly and again with --pending
        THEN:  the plain run shows both oldest first and marks the
               finished one, and --pending shows only what is left
        """
        archive_root = staged(*dated_pair)
        archive = opened(archive_root)
        mark_uploaded(archive, in_travel_order(archive, archive.trip_keys())[0])

        listed = runner.invoke(
            main, ["list", "--archive-root", str(archive_root)]
        )
        pending = runner.invoke(
            main, ["list", "--archive-root", str(archive_root), "--pending"]
        )

        check.equal(listed.exit_code, 0, listed.output)
        check.less(
            listed.output.index(EARLIER),
            listed.output.index(LATER),
            "oldest first",
        )
        check.is_in("1 already uploaded", listed.output)

        check.equal(pending.exit_code, 0, pending.output)
        check.is_in(LATER, pending.output)
        check.is_not_in(EARLIER, pending.output)
