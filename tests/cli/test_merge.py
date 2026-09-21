#!/usr/bin/env python
#
"""
Test the `merge` subcommand.

The declaration is what upload reads to fold one trip into another, and
it is refused rather than recorded when it would not mean anything.  All
of that is reachable without credentials: merge never touches Tripsy.
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
from tripsy_exim.store import Archive
from tripsy_exim.sync.importer import in_travel_order, merged_into


########################################################################
########################################################################
#
class TestMergeCommand:
    """Tests for the `merge` subcommand."""

    ####################################################################
    #
    @pytest.fixture
    def two_keys(
        self,
        staged: Callable[..., Path],
        named_pair: tuple[dict, dict],
        opened: Callable[[Path], Archive],
    ) -> Callable[[], tuple[Path, str, str]]:
        """An archive of two trips, with their keys in travel order."""

        def build() -> tuple[Path, str, str]:
            archive_root = staged(*named_pair)
            archive = opened(archive_root)
            keys = in_travel_order(archive, archive.trip_keys())
            return archive_root, keys[0], keys[1]

        return build

    ####################################################################
    #
    def test_a_declaration_is_recorded_and_reported(
        self,
        runner: CliRunner,
        two_keys: Callable[[], tuple[Path, str, str]],
        opened: Callable[[Path], Archive],
    ) -> None:
        """
        GIVEN: two staged trips
        WHEN:  merge declares one into the other
        THEN:  the manifest records it and the plan is reported

        Nothing moves on disk, so the manifest is the only evidence the
        declaration was made at all.
        """
        archive_root, absorbed, target = two_keys()

        result = runner.invoke(
            main,
            ["merge", "--archive-root", str(archive_root), absorbed, target],
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in("uploads as part of", result.output)
        check.equal(merged_into(opened(archive_root)).get(absorbed), target)

    ####################################################################
    #
    def test_undo_releases_the_absorbed_trip(
        self,
        runner: CliRunner,
        two_keys: Callable[[], tuple[Path, str, str]],
        opened: Callable[[Path], Archive],
    ) -> None:
        """
        GIVEN: a merge already declared
        WHEN:  merge --undo names the absorbed trip
        THEN:  the declaration is gone and the trip stands alone again

        The declaration has to be reversible while nothing has been
        uploaded, which is the whole reason it lives in the manifest
        rather than in the staged files.
        """
        archive_root, absorbed, target = two_keys()
        runner.invoke(
            main,
            ["merge", "--archive-root", str(archive_root), absorbed, target],
        )

        result = runner.invoke(
            main,
            ["merge", "--archive-root", str(archive_root), "--undo", absorbed],
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in("uploads as its own trip again", result.output)
        check.is_not_in(absorbed, merged_into(opened(archive_root)))

    ####################################################################
    #
    def test_a_target_is_required_without_undo(
        self,
        runner: CliRunner,
        two_keys: Callable[[], tuple[Path, str, str]],
    ) -> None:
        """
        GIVEN: one trip named and no target
        WHEN:  merge is run without --undo
        THEN:  it is a usage error rather than a silent no-op

        TARGET is optional in the signature only so that --undo can take
        one argument, which makes this the one place it is checked.
        """
        archive_root, absorbed, _ = two_keys()

        result = runner.invoke(
            main, ["merge", "--archive-root", str(archive_root), absorbed]
        )

        check.equal(result.exit_code, 2, result.output)
        check.is_in("name the trip to merge into", result.output)

    ####################################################################
    #
    def test_a_trip_cannot_absorb_itself(
        self,
        runner: CliRunner,
        two_keys: Callable[[], tuple[Path, str, str]],
        opened: Callable[[Path], Archive],
    ) -> None:
        """
        GIVEN: one staged trip named as both halves
        WHEN:  merge is run
        THEN:  it is refused and nothing is recorded
        """
        archive_root, absorbed, _ = two_keys()

        result = runner.invoke(
            main,
            ["merge", "--archive-root", str(archive_root), absorbed, absorbed],
        )

        check.not_equal(result.exit_code, 0)
        check.is_in("cannot be merged into itself", result.output)
        check.equal(merged_into(opened(archive_root)), {})

    ####################################################################
    #
    def test_a_key_that_is_not_staged_is_refused(
        self,
        runner: CliRunner,
        two_keys: Callable[[], tuple[Path, str, str]],
    ) -> None:
        """
        GIVEN: a key naming no staged trip
        WHEN:  merge is run against it
        THEN:  it is refused, naming the key

        Trips are named by key here rather than by name, so a typo
        produces a key that simply is not there.
        """
        archive_root, _, target = two_keys()

        result = runner.invoke(
            main,
            [
                "merge",
                "--archive-root",
                str(archive_root),
                "txim-nonsense",
                target,
            ],
        )

        check.not_equal(result.exit_code, 0)
        check.is_in("txim-nonsense", result.output)
        check.is_in("not staged", result.output)

    ####################################################################
    #
    def test_merging_into_an_absorbed_trip_is_refused(
        self,
        runner: CliRunner,
        staged: Callable[..., Path],
        opened: Callable[[Path], Archive],
    ) -> None:
        """
        GIVEN: a trip already merged into another
        WHEN:  a third trip is merged into that absorbed one
        THEN:  it is refused and points at the end of the chain

        A chain would leave objects in a trip nothing uploads, which is
        not what anybody declaring it meant.

        Three trips built here rather than from the shared pairs: this
        needs a known order among three, and the pairs exist for name
        matching, so their dates are nobody's contract.
        """
        archive_root = staged(
            b.trip(
                name="One",
                start="2024-01-01",
                end="2024-01-04",
                objects=[b.flight()],
            ),
            b.trip(
                name="Two",
                start="2024-02-01",
                end="2024-02-04",
                objects=[b.lodging()],
            ),
            b.trip(
                name="Three",
                start="2024-03-01",
                end="2024-03-04",
                objects=[b.flight()],
            ),
        )
        archive = opened(archive_root)
        first, second, third = in_travel_order(archive, archive.trip_keys())

        runner.invoke(
            main, ["merge", "--archive-root", str(archive_root), second, first]
        )
        result = runner.invoke(
            main, ["merge", "--archive-root", str(archive_root), third, second]
        )

        check.not_equal(result.exit_code, 0)
        check.is_in("merge into that instead", result.output)
