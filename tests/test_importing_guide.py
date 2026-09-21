#!/usr/bin/env python
#
"""
Walk `docs/importing.md` end to end, against the in-memory fake.

A runbook fails quietly.  Rename a flag or change what a command prints
and every man page still passes its own tests while the guide that
strings them together goes wrong -- and the person following it is a
newcomer, who has no way to tell a stale instruction from their own
mistake.

So the guide's commands are run here, in the documented order, against
`FakeTripsy`.  Steps 1 and 6 are the two that cannot be: one is an email
to TripIt, the other is real credentials, and the credential step is
what the patched session stands in for.

`walkthrough` runs the whole sequence once and records what each step
printed.  Each test then reads one step, because the sequence is the
thing under test and running it per assertion would prove less.

The transcript is also the worked example in the guide.  Set
SHOW_WALKTHROUGH=1 to print it -- not TRIPSY_-prefixed, because
the suite scrubs those to keep itself away from a real account.
"""

# system imports
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# 3rd party imports
import pytest
import pytest_check as check
from click.testing import CliRunner
from pytest_mock import MockerFixture

# Project imports
from tests import tripit_builder as b
from tests.places import NRT, SEA
from tripsy_exim.api import TripsyClient
from tripsy_exim.cli import main


########################################################################
########################################################################
#
@dataclass
class Walkthrough:
    """What each step of the guide printed, in order."""

    archive: Path
    work_list: Path

    # Name, command as a reader would type it, and what it printed.  The
    # name drops every path, because a lookup by substring against a
    # command carrying pytest's tmp_path matches whatever that directory
    # happens to be called.
    #
    steps: list[tuple[str, str, str]] = field(default_factory=list)

    ####################################################################
    #
    def said(self, step: str) -> str:
        """What one step printed, by the command that produced it."""
        for name, _, output in self.steps:
            if name == step or name.startswith(f"{step} "):
                return output
        raise AssertionError(f"no step matching {step!r}")

    ####################################################################
    #
    @property
    def transcript(self) -> str:
        """The whole run, as a reader would see it."""
        return "\n".join(
            f"$ tripsy-exim {command}\n{output.rstrip()}\n"
            for _, command, output in self.steps
        )


####################################################################
#
@pytest.fixture
def two_travellers() -> dict:
    """
    An export shaped like the guide's own examples.

    One trip places Narita and another names it with nothing to place
    it by, which is the case `backfill infer` closes; a second endpoint
    nothing places is what it leaves for a person.
    """
    return b.export(
        b.trip(
            name="Kyoto, May 2011",
            objects=[
                b.flight(frm="Narita", to="Seattle", frm_at=NRT, to_at=SEA),
                b.lodging(),
            ],
        ),
        b.trip(
            name="Osaka, June 2012",
            start="2012-06-01",
            end="2012-06-05",
            objects=[
                b.flight(frm="Narita", to="Vancouver", placed=False),
                b.restaurant(),
            ],
        ),
    )


####################################################################
#
@pytest.fixture
def walkthrough(
    runner: CliRunner,
    tmp_path: Path,
    mocker: MockerFixture,
    api_client: TripsyClient,
    two_travellers: dict,
) -> Iterator[Walkthrough]:
    """
    Run the guide's commands in order, recording what each printed.

    The session is patched to the fake rather than the credentials being
    faked further down, because step 6 of the guide is exactly where a
    real run stops being local -- so that is the seam.
    """
    archive = tmp_path / "archive"
    export = tmp_path / "export.json"
    export.write_text(json.dumps(two_travellers))
    work_list = tmp_path / "work.json"

    mocker.patch(
        "tripsy_exim.cli.open_session",
        return_value=mocker.MagicMock(
            __enter__=mocker.Mock(return_value=api_client),
            __exit__=mocker.Mock(return_value=False),
        ),
    )

    run = Walkthrough(archive=archive, work_list=work_list)

    def readable(text: str) -> str:
        """The same text with this run's scratch paths taken out."""
        return text.replace(f"{tmp_path}/", "").replace(str(tmp_path), ".")

    def step(*argv: str) -> str:
        """One documented command, recorded as a reader would see it."""
        result = runner.invoke(main, [*argv, "--archive", str(archive)])
        assert result.exit_code == 0, f"{argv}\n{result.output}"
        run.steps.append(
            (
                " ".join(word for word in argv if "/" not in word),
                readable(" ".join(argv)),
                readable(result.output),
            )
        )
        return result.output

    # 2. Stage it.
    step("stage-export", str(export))
    step("list")

    # 3. Let the archive fix what it can.
    step("backfill", "infer")
    step("backfill", "infer", "--write")

    # 4. See what still needs you.
    step("backfill", "report")

    # 5. Answer it.
    step("backfill", "export", str(work_list))
    _answer(work_list)
    step("backfill", "apply", str(work_list))
    step("backfill", "apply", str(work_list), "--write")
    step("backfill", "report")

    # 7. Upload, a trip at a time.
    step("upload", "--limit", "1")
    step("upload", "--limit", "1", "--write")
    step("verify", "--limit", "1")

    yield run

    if os.environ.get("SHOW_WALKTHROUGH"):
        print("\n" + run.transcript)


####################################################################
#
def _answer(work_list: Path) -> None:
    """Fill the work-list in, as a person would in an editor."""
    document = json.loads(work_list.read_text())
    for row in document["rows"]:
        for name in row.get("fields", {}):
            if name.endswith("_address"):
                row["fields"][name] = "Vancouver International Airport"
    work_list.write_text(json.dumps(document, indent=2))


########################################################################
########################################################################
#
class TestImportingGuide:
    """Tests that the documented sequence does what the guide says."""

    ####################################################################
    #
    def test_staging_lists_every_trip_not_yet_uploaded(
        self, walkthrough: Walkthrough
    ) -> None:
        """
        GIVEN: the guide's step 2
        WHEN:  the export is staged and the trips listed
        THEN:  both are there, oldest first, marked as not uploaded

        The guide promises exactly this as the sign that step 2 worked.
        """
        listed = walkthrough.said("list")

        check.is_in("Kyoto, May 2011", listed)
        check.is_in("Osaka, June 2012", listed)
        check.is_in("2 trips, 0 already uploaded", listed)
        check.less(listed.index("Osaka"), listed.index("Kyoto"), "oldest first")

    ####################################################################
    #
    def test_infer_places_what_another_trip_already_placed(
        self, walkthrough: Walkthrough
    ) -> None:
        """
        GIVEN: the guide's step 3
        WHEN:  infer runs
        THEN:  Narita is placed from the trip that placed it, and the
               endpoint nothing places is refused

        The guide says it fills what your own history already answers
        and refuses the rest.  Both halves are load-bearing: a reader
        who sees only the first will not understand step 4.
        """
        planned = walkthrough.said("backfill infer")

        check.is_in("NAR", planned)
        check.is_in("1 would be placed", planned)
        check.is_in("refused", planned)
        check.is_in("VAN", planned)

    ####################################################################
    #
    def test_report_names_only_what_is_left(
        self, walkthrough: Walkthrough
    ) -> None:
        """
        GIVEN: the guide's step 4, run after step 3
        WHEN:  the report is read
        THEN:  what infer placed is gone and what it refused remains

        The ordering claim the guide makes -- run infer first so the
        list is shorter -- is only true if this holds.
        """
        reported = walkthrough.said("backfill report")

        check.is_in("VAN", reported)
        check.is_not_in("NAR", reported)
        check.is_in("1 gap", reported)

    ####################################################################
    #
    def test_answering_the_work_list_closes_the_gap(
        self, walkthrough: Walkthrough
    ) -> None:
        """
        GIVEN: the guide's step 5
        WHEN:  the work-list is filled in and applied
        THEN:  it is written, and a second report finds nothing open

        The guide tells a reader to go round steps 4 and 5 until this
        is what they see, so this is the promised end of the local half.
        """
        applied = walkthrough.said("backfill apply")
        final = walkthrough.steps[-4][2]

        check.is_in("1 would be written", applied)
        check.is_in("nothing open", final)

    ####################################################################
    #
    def test_a_dry_run_precedes_every_write(
        self, walkthrough: Walkthrough
    ) -> None:
        """
        GIVEN: the whole documented sequence
        WHEN:  the steps that write are found
        THEN:  each was preceded by the same command without --write

        Every example in the guide is a pair, and that is the habit it
        is trying to teach.  A command that stopped being dry by default
        would make the guide teach the wrong one.
        """
        commands = [name for name, _, _ in walkthrough.steps]

        for index, command in enumerate(commands):
            if "--write" not in command:
                continue
            dry = command.replace(" --write", "")
            check.is_in(
                dry,
                commands[:index],
                f"{command} ran with no dry run before it",
            )

    ####################################################################
    #
    def test_upload_sends_one_trip_and_verify_finds_it(
        self, walkthrough: Walkthrough
    ) -> None:
        """
        GIVEN: the guide's step 7
        WHEN:  one trip is uploaded and read back
        THEN:  verify reports it as matching

        The guide's advice to take one trip, look at it, then take the
        rest rests on --limit meaning what it says.
        """
        planned = walkthrough.said("upload --limit 1")
        checked = walkthrough.said("verify")

        check.is_in("1 trip", planned)
        check.is_not_in("error", checked.lower())

    ####################################################################
    #
    def test_the_transcript_reads_as_a_worked_example(
        self, walkthrough: Walkthrough
    ) -> None:
        """
        GIVEN: the recorded run
        WHEN:  it is rendered as a transcript
        THEN:  every documented step is in it, in order

        This is what the guide's worked example is generated from, so a
        step that stopped running would take the example with it rather
        than leaving it silently wrong.
        """
        transcript = walkthrough.transcript

        for command in (
            "stage-export",
            "backfill infer",
            "backfill report",
            "backfill export",
            "backfill apply",
            "upload",
            "verify",
        ):
            check.is_in(f"$ tripsy-exim {command}", transcript)
