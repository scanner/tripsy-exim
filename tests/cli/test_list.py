#!/usr/bin/env python
#
"""Test the `list` subcommand."""

# system imports
from collections.abc import Callable
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check
from click.testing import CliRunner
from pytest_mock import MockerFixture

# Project imports
from tests.cli.conftest import EARLIER, LATER
from tripsy_exim.cli import main
from tripsy_exim.store import Archive
from tripsy_exim.sync.importer import (
    in_travel_order,
    mark_uploaded,
    staged_trip,
)


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


########################################################################
########################################################################
#
class TestListUploaded:
    """
    Tests for `list --uploaded`, which asks Tripsy rather than the archive.

    The archive is optional: with one, a trip that came from it shows its
    key; without one, the account is listed alone.
    """

    ####################################################################
    #
    @pytest.fixture
    def tripsy_account(self, mocker: MockerFixture) -> Callable[..., Any]:
        """
        Stand in for a signed-in session onto an account.

        Call it with the trips the account holds, as Tripsy returns them.
        Returns the client mock, so a test can see what was asked for.
        """

        def holding(*trips: dict[str, Any]) -> Any:
            client = mocker.MagicMock()
            client.iter_trips.return_value = iter(trips)
            mocker.patch(
                "tripsy_exim.cli.open_session"
            ).return_value.__enter__.return_value = client
            return client

        return holding

    ####################################################################
    #
    def test_the_account_is_listed_with_keys_from_the_archive(
        self,
        runner: CliRunner,
        staged: Callable[..., Path],
        dated_pair: tuple[dict, dict],
        opened: Callable[[Path], Archive],
        tripsy_account: Callable[..., Any],
    ) -> None:
        """
        GIVEN: an archive of two trips, one uploaded as Tripsy trip 1 --
               which has since come back from Tripsy carrying a different
               internal_identifier, as a trip saved again in the app does
               -- and a trip in the account made in the app
        WHEN:  list --uploaded is run
        THEN:  both account trips are listed oldest first, the one from
               the archive carries its key, found through the id its
               upload recorded, and the summary counts both
        """
        archive_root = staged(*dated_pair)
        archive = opened(archive_root)
        later = in_travel_order(archive, archive.trip_keys())[1]
        trip = staged_trip(archive, later)
        assert trip is not None
        manifest = archive.read_manifest()
        manifest["identifier_cache"] = {str(trip.internal_identifier): 1}
        archive.write_manifest(manifest)
        tripsy_account(
            {"id": 2, "name": "Made in the app", "starts_at": "2030-01-01"},
            {
                "id": 1,
                "name": LATER,
                "starts_at": "2024-09-01",
                "internal_identifier": "ReassignedInTheApp01",
            },
        )

        result = runner.invoke(
            main, ["list", "--uploaded", "--archive-root", str(archive_root)]
        )

        check.equal(result.exit_code, 0, result.output)
        check.less(
            result.output.index(LATER),
            result.output.index("Made in the app"),
            "oldest first",
        )
        check.is_in(later, result.output, "key of the archive's trip")
        check.is_not_in(EARLIER, result.output, "not in the account")
        check.is_in("2 trips in Tripsy, 1 from this archive", result.output)

    ####################################################################
    #
    def test_no_archive_is_needed(
        self,
        runner: CliRunner,
        tmp_path: Path,
        tripsy_account: Callable[..., Any],
    ) -> None:
        """
        GIVEN: an account holding a trip, and no staged archive at all
        WHEN:  list --uploaded is run
        THEN:  the account's trips are listed, with no archive keys
        """
        tripsy_account(
            {"id": 1, "name": "Made in the app", "starts_at": "2030-01-01"}
        )

        result = runner.invoke(
            main, ["list", "--uploaded", "--archive-root", str(tmp_path)]
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in("Made in the app", result.output)
        check.is_in("1 trip in Tripsy", result.output)
        check.is_not_in("from this archive", result.output)
