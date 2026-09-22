#!/usr/bin/env python
#
"""
Test the `export` command: what it takes, and what it refuses.

The writing itself is covered against the exporter, so these tests are
about the command's own job -- turning flags into a selection, holding
the lock, and exiting in a way a scheduler can act on.
"""

# system imports
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check
from click.testing import CliRunner
from pytest_mock import MockerFixture

# Project imports
from tests.clock import FakeClock
from tests.conftest import client_for
from tests.fake_tripsy import FakeTripsy
from tripsy_exim.api import BACKUP
from tripsy_exim.cli import main
from tripsy_exim.store import EXPORTS_DIR
from tripsy_exim.sync.exporter import only_one_run


####################################################################
#
@pytest.fixture
def account(
    paced_tripsy: FakeTripsy, clock: FakeClock, mocker: MockerFixture
) -> Callable[..., FakeTripsy]:
    """
    An account of named, dated trips, with the session patched onto it.

    The patch is here rather than in each test because every one of them
    needs it and none is about it: `export` is the first command whose
    whole job happens behind a session.

    The client is built through `client_for`, so the pacer's waiting is
    charged to the test clock.  `export` runs on the `backup` profile,
    which is the most patient of the three -- a run that really slept
    through it would make these the slowest tests in the suite.
    """

    def build(*trips: tuple[str, str, str]) -> FakeTripsy:
        """Seed one trip per (name, start, end) given."""
        for name, starts, ends in trips:
            paced_tripsy.seed_trip(
                name=name, starts_at=starts, ends_at=ends, has_dates=True
            )
        mocker.patch(
            "tripsy_exim.cli.open_session",
            return_value=mocker.MagicMock(
                __enter__=mocker.Mock(
                    return_value=client_for(paced_tripsy, clock, profile=BACKUP)
                ),
                __exit__=mocker.Mock(return_value=False),
            ),
        )
        return paced_tripsy

    return build


####################################################################
#
@pytest.fixture
def exported_names(tmp_path: Path) -> Callable[[], list[str]]:
    """The trip names one run wrote, read back from its manifest."""

    def names() -> list[str]:
        runs = sorted((tmp_path / EXPORTS_DIR).glob("2*"))
        assert len(runs) == 1, f"expected one run, got {runs}"
        manifest = json.loads((runs[0] / "manifest.json").read_text())
        return sorted(str(t["name"]) for t in manifest["trips"])

    return names


########################################################################
########################################################################
#
class TestSelection:
    """Tests for turning flags into the trips a run takes."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "flags,expected",
        [
            (["--all"], ["Kyoto, May 2011", "Osaka, June 2012"]),
            (["--trip", "Kyoto"], ["Kyoto, May 2011"]),
            (["--glob", "Osaka*"], ["Osaka, June 2012"]),
            (["--from", "2012-01-01"], ["Osaka, June 2012"]),
            (["--to", "2011-12-31"], ["Kyoto, May 2011"]),
            (
                ["--trip", "Kyoto", "--trip", "Osaka"],
                ["Kyoto, May 2011", "Osaka, June 2012"],
            ),
            (
                ["--glob", "*2011", "--trip", "Osaka"],
                ["Kyoto, May 2011", "Osaka, June 2012"],
            ),
            (["--glob", "*", "--from", "2012-01-01"], ["Osaka, June 2012"]),
        ],
        ids=[
            "all",
            "by name",
            "by glob",
            "from",
            "to",
            "two names union",
            "glob and name union",
            "date narrows the union",
        ],
    )
    def test_what_each_way_of_asking_takes(
        self,
        runner: CliRunner,
        tmp_path: Path,
        account: Callable[..., FakeTripsy],
        exported_names: Callable[[], list[str]],
        flags: list[str],
        expected: list[str],
    ) -> None:
        """
        GIVEN: two trips a year apart
        WHEN:  a run asks for them in one of the documented ways
        THEN:  it takes exactly those

        Name selectors union and the date range narrows what they came
        to, which is the one rule that is easy to get backwards and
        impossible to notice afterwards -- a backup that quietly took
        less than asked looks exactly like one that took everything.
        """
        account(
            ("Kyoto, May 2011", "2011-05-01", "2011-05-08"),
            ("Osaka, June 2012", "2012-06-01", "2012-06-04"),
        )

        result = runner.invoke(
            main, ["export", "--archive-root", str(tmp_path), *flags]
        )

        check.equal(result.exit_code, 0, result.output)
        check.equal(exported_names(), expected)

    ####################################################################
    #
    @pytest.mark.parametrize(
        "flags,says",
        [
            ([], "nothing selected"),
            (["--all", "--trip", "Kyoto"], "cannot be combined"),
            (["--from", "2012-12-01", "--to", "2012-01-01"], "is after"),
        ],
        ids=["nothing asked for", "all with a selector", "backwards range"],
    )
    def test_a_selection_that_means_nothing_is_refused(
        self,
        runner: CliRunner,
        tmp_path: Path,
        flags: list[str],
        says: str,
    ) -> None:
        """
        GIVEN: flags that contradict each other or ask for nothing
        WHEN:  the command runs
        THEN:  it says so before reaching the network

        A backup command whose bare form silently meant "everything"
        would eventually be run by somebody who meant something
        narrower, and the cost of finding out is a full pass of
        requests.
        """
        result = runner.invoke(
            main, ["export", "--archive-root", str(tmp_path), *flags]
        )

        check.not_equal(result.exit_code, 0)
        check.is_in(says, result.output)

    ####################################################################
    #
    def test_an_undated_trip_is_outside_every_date_range(
        self,
        runner: CliRunner,
        tmp_path: Path,
        paced_tripsy: FakeTripsy,
        account: Callable[..., FakeTripsy],
        exported_names: Callable[[], list[str]],
    ) -> None:
        """
        GIVEN: a trip that says it has no dates
        WHEN:  a run asks for a range that its fields would fall inside
        THEN:  it is left out

        `has_dates` is authoritative, so the fields are not an answer.
        A range asks when somebody travelled and an undated trip has no
        answer, which is left out rather than guessed at.
        """
        account(("Kyoto, May 2011", "2011-05-01", "2011-05-08"))
        paced_tripsy.seed_trip(
            name="Someday",
            starts_at="2011-05-02",
            ends_at="2011-05-03",
            has_dates=False,
        )

        result = runner.invoke(
            main,
            [
                "export",
                "--archive-root",
                str(tmp_path),
                "--from",
                "2011-01-01",
            ],
        )

        check.equal(result.exit_code, 0, result.output)
        check.equal(exported_names(), ["Kyoto, May 2011"])

    ####################################################################
    #
    def test_a_selection_matching_nothing_writes_no_export(
        self,
        runner: CliRunner,
        tmp_path: Path,
        account: Callable[..., FakeTripsy],
    ) -> None:
        """
        GIVEN: a selection that matches no trip
        WHEN:  the command runs
        THEN:  it fails rather than writing an empty directory

        An empty export and an export of an empty account read the same
        on disk a year later.  Refusing the first keeps the second
        meaningful.
        """
        account(("Kyoto, May 2011", "2011-05-01", "2011-05-08"))

        result = runner.invoke(
            main,
            ["export", "--archive-root", str(tmp_path), "--trip", "Lisbon"],
        )

        check.not_equal(result.exit_code, 0)
        check.is_in("nothing matched", result.output)
        check.is_false(
            any((tmp_path / EXPORTS_DIR).glob("2*")), "nothing written"
        )


########################################################################
########################################################################
#
class TestReporting:
    """Tests for what a run says, and what it says nothing about."""

    ####################################################################
    #
    def test_quiet_by_default_and_named_when_verbose(
        self,
        runner: CliRunner,
        tmp_path: Path,
        account: Callable[..., FakeTripsy],
    ) -> None:
        """
        GIVEN: a run of two trips
        WHEN:  it runs with and without --verbose
        THEN:  both give the totals and only --verbose names the trips

        A nightly job that listed every trip would mail its whole
        output every night, and a person reading that mail would stop
        reading it.
        """
        account(
            ("Kyoto, May 2011", "2011-05-01", "2011-05-08"),
            ("Osaka, June 2012", "2012-06-01", "2012-06-04"),
        )

        # A root each: a stamp names one run, so two runs of the same
        # second cannot share one.
        #
        quiet = runner.invoke(
            main, ["export", "--archive-root", str(tmp_path / "a"), "--all"]
        )
        loud = runner.invoke(
            main,
            [
                "export",
                "--archive-root",
                str(tmp_path / "b"),
                "--all",
                "--verbose",
            ],
        )

        check.equal(quiet.exit_code, 0, quiet.output)
        check.is_in("2 trips", quiet.output)
        check.is_not_in("Kyoto", quiet.output)
        check.is_in("Kyoto", loud.output)


########################################################################
########################################################################
#
class TestTheLock:
    """Tests that two runs cannot write at once."""

    ####################################################################
    #
    def test_a_second_run_stands_down_rather_than_failing(
        self,
        runner: CliRunner,
        tmp_path: Path,
        account: Callable[..., FakeTripsy],
    ) -> None:
        """
        GIVEN: a run already holding the exports directory
        WHEN:  another starts
        THEN:  it exits 2, saying so, and writes nothing

        A nightly job starting while yesterday's is still going has
        nothing to do.  Exit 2 rather than 1 so a scheduler can tell
        that apart from a backup that actually failed -- one is worth
        waking somebody for and the other is not.
        """
        account(("Kyoto, May 2011", "2011-05-01", "2011-05-08"))

        with only_one_run(tmp_path / EXPORTS_DIR):
            result = runner.invoke(
                main, ["export", "--archive-root", str(tmp_path), "--all"]
            )

        check.equal(result.exit_code, 2)
        check.is_in("another export is running", result.output)
        check.is_false(
            any((tmp_path / EXPORTS_DIR).glob("2*")), "nothing written"
        )


########################################################################
########################################################################
#
class TestScope:
    """Tests that an export says what it was asked for."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "flags,expected",
        [
            (["--all"], {"all": True, "trips": [], "glob": None}),
            (
                ["--trip", "Kyoto"],
                {"all": False, "trips": ["Kyoto"], "glob": None},
            ),
            (
                ["--glob", "Kyoto*"],
                {"all": False, "trips": [], "glob": "Kyoto*"},
            ),
        ],
        ids=["all", "named", "glob"],
    )
    def test_the_manifest_says_how_much_was_asked_for(
        self,
        runner: CliRunner,
        tmp_path: Path,
        account: Callable[..., FakeTripsy],
        flags: list[str],
        expected: dict[str, Any],
    ) -> None:
        """
        GIVEN: a run of a subset, or of everything
        WHEN:  its manifest is read
        THEN:  it says which it was

        Without it a directory cannot tell a reader whether a trip is
        missing because the account had lost it or because nobody asked
        for it, and those call for opposite responses.
        """
        account(("Kyoto, May 2011", "2011-05-01", "2011-05-08"))

        result = runner.invoke(
            main, ["export", "--archive-root", str(tmp_path), *flags]
        )
        assert result.exit_code == 0, result.output

        run = next(iter(sorted((tmp_path / EXPORTS_DIR).glob("2*"))))
        scope = json.loads((run / "manifest.json").read_text())["scope"]

        for key, value in expected.items():
            check.equal(scope[key], value, key)
