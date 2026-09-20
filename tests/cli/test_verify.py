#!/usr/bin/env python
#
"""
Test the `verify` subcommand.

What verify *finds* is `sync.importer`'s work and is tested there.  What
is tested here is the command around it: which trips it selects, that it
refuses when there is nothing to check, and that a finding reaches the
terminal in a form a person can act on.
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
from tripsy_exim.cli import main
from tripsy_exim.store import Archive
from tripsy_exim.sync.importer import (
    Discrepancy,
    TripCheck,
    in_travel_order,
    mark_uploaded,
)


########################################################################
########################################################################
#
class TestVerifyCommand:
    """Tests for the `verify` subcommand."""

    ####################################################################
    #
    @pytest.fixture
    def checked(
        self,
        staged: Callable[..., Path],
        dated_pair: tuple[dict, dict],
        environment: MutableMapping[str, str],
        mocker: MockerFixture,
    ) -> Callable[..., tuple[Path, list[str]]]:
        """
        Two uploaded trips, with Tripsy stubbed out.

        Returns a factory taking what each trip should report, oldest
        first; a trip with nothing given for it reports agreement.  The
        session is mocked because what verify reads back is the
        importer's business, and this is about the command around it.

        The results do not need to carry real trip keys: they are bound
        to keys by position here, and the command prints a trip's name.

        A trip nobody gave a result for reports its own key as its name,
        deliberately: that is what lets a test assert *which* trips were
        checked, which is the whole of the --limit test below.
        """
        environment["TRIPSY_USERNAME"] = "someone"
        environment["TRIPSY_PASSWORD"] = "secret"

        def make(*results: TripCheck) -> tuple[Path, list[str]]:
            archive_root = staged(*dated_pair)
            archive = Archive(archive_root)
            keys = in_travel_order(archive, archive.trip_keys())
            for key in keys:
                mark_uploaded(archive, key)

            given = dict(zip(keys, results, strict=False))
            mocker.patch(
                "tripsy_exim.cli.open_session"
            ).return_value.__enter__.return_value = mocker.MagicMock()
            mocker.patch(
                "tripsy_exim.cli.verify_trip",
                side_effect=lambda _client, _archive, key: given.get(
                    key, TripCheck(trip_key=key, name=key, planned=1, matched=1)
                ),
            )
            return archive_root, keys

        return make

    ####################################################################
    #
    def test_an_archive_with_nothing_uploaded_is_refused(
        self, runner: CliRunner, staged: Callable[..., Path]
    ) -> None:
        """
        GIVEN: a staged archive no run has uploaded from
        WHEN:  verify is run
        THEN:  it fails saying so rather than checking nothing

        Verify's default is every trip an earlier run finished, and an
        empty default would otherwise read as success.
        """
        archive_root = staged()

        result = runner.invoke(main, ["verify", "--archive", str(archive_root)])

        check.not_equal(result.exit_code, 0)
        check.is_in("no trips have been uploaded", result.output)

    ####################################################################
    #
    def test_trips_that_agree_are_reported_ok(
        self,
        runner: CliRunner,
        checked: Callable[..., tuple[Path, list[str]]],
    ) -> None:
        """
        GIVEN: two uploaded trips the account matches
        WHEN:  verify runs
        THEN:  they are marked ok and the tally counts them both
        """
        archive_root, _ = checked()

        result = runner.invoke(main, ["verify", "--archive", str(archive_root)])

        check.equal(result.exit_code, 0, result.output)
        check.is_in("[ok]", result.output)
        check.is_in("2 of 2 trips match their plan", result.output)

    ####################################################################
    #
    def test_a_difference_is_reported_per_field(
        self,
        runner: CliRunner,
        checked: Callable[..., tuple[Path, list[str]]],
    ) -> None:
        """
        GIVEN: a trip missing an object and holding a wrong sort_order
        WHEN:  verify runs
        THEN:  it is marked DIFFERS, and the field is named with both
               values

        sort_order is assigned at create and never revisited, so a
        mismatch is permanent until someone edits it in the app -- which
        is why the report says which field and what it holds, rather
        than only that something differs.
        """
        archive_root, _ = checked(
            TripCheck(
                trip_key="txim-differing",
                name="A trip that did not",
                planned=2,
                matched=1,
                missing=["txim-missing-one"],
                differing=[
                    Discrepancy(
                        identifier="txim-out-of-order",
                        name="A hotel on the second night",
                        field="sort_order",
                        planned=12,
                        found=13,
                    )
                ],
            )
        )

        result = runner.invoke(main, ["verify", "--archive", str(archive_root)])

        check.equal(result.exit_code, 0, result.output)
        check.is_in("[DIFFERS]", result.output)
        check.is_in("sort_order", result.output)
        check.is_in("planned 12, found 13", result.output)
        check.is_in("missing   txim-missing-one", result.output)
        check.is_in("1 of 2 trips match their plan", result.output)

    ####################################################################
    #
    @pytest.mark.parametrize(
        "extra_args,listed",
        [([], False), (["--verbose"], True)],
        ids=["plain", "verbose"],
    )
    def test_extra_objects_are_listed_only_when_verbose(
        self,
        runner: CliRunner,
        checked: Callable[..., tuple[Path, list[str]]],
        extra_args: list[str],
        listed: bool,
    ) -> None:
        """
        GIVEN: a trip holding an object the plan does not know about
        WHEN:  verify runs with and without --verbose
        THEN:  the count is always reported and the identifiers only
               under --verbose

        An extra object is usually something added in the app, so it is
        counted rather than treated as a fault -- but the list is long
        enough to be worth asking for.
        """
        archive_root, _ = checked(
            TripCheck(
                trip_key="txim-with-extra",
                name="A trip with an addition",
                planned=1,
                matched=1,
                extra=["txim-added-in-the-app"],
            )
        )

        result = runner.invoke(
            main, ["verify", "--archive", str(archive_root), *extra_args]
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in("1 more in the account", result.output)
        check.equal("txim-added-in-the-app" in result.output, listed)

    ####################################################################
    #
    def test_unplaced_addresses_are_counted_and_explained(
        self,
        runner: CliRunner,
        checked: Callable[..., tuple[Path, list[str]]],
    ) -> None:
        """
        GIVEN: a trip carrying addresses Tripsy has not placed
        WHEN:  verify runs
        THEN:  they are counted and the closing note points at
               fix-locations

        Nothing on the server resolves these, so a count with no next
        step would leave a reader stuck.
        """
        archive_root, _ = checked(
            TripCheck(
                trip_key="txim-unplaced",
                name="A trip with nothing on the map",
                planned=1,
                matched=1,
                unplaced=["A station with an address"],
            )
        )

        result = runner.invoke(main, ["verify", "--archive", str(archive_root)])

        check.equal(result.exit_code, 0, result.output)
        check.is_in("1 addresses not yet placed", result.output)
        check.is_in("fix-locations", result.output)

    ####################################################################
    #
    def test_limit_takes_the_oldest_trips_first(
        self,
        runner: CliRunner,
        checked: Callable[..., tuple[Path, list[str]]],
    ) -> None:
        """
        GIVEN: two uploaded trips
        WHEN:  verify runs with --limit 1
        THEN:  one trip is checked, and it is the oldest

        Same order as upload, so `upload --limit 1` and `verify --limit
        1` are about the same trip.
        """
        archive_root, keys = checked()

        result = runner.invoke(
            main, ["verify", "--archive", str(archive_root), "--limit", "1"]
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in("of 1 trips match their plan", result.output)
        check.is_in(keys[0], result.output)
        check.is_not_in(keys[1], result.output)
