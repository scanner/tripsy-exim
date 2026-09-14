#!/usr/bin/env python
#
"""Test the command line entry point."""

# system imports
import json
from collections.abc import Callable, MutableMapping
from pathlib import Path

# 3rd party imports
import pytest
import pytest_check as check
from click.testing import CliRunner
from faker import Faker
from pytest_mock import MockerFixture

# Project imports
from tests import tripit_builder as b
from tests.ics_builder import build_calendar, to_ics
from tripsy_exim.cli import main
from tripsy_exim.geocode import Cache, Found, normalised
from tripsy_exim.secrets import SECRET_URL_ENV
from tripsy_exim.store import ARCHIVE_ENV, Archive
from tripsy_exim.sync.importer import (
    Unplaced,
    in_travel_order,
    mark_uploaded,
    staged_trip,
)


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


# Two trips whose names share a word and differ in another.  Both halves
# are load-bearing: 'kyoto' has to pick one trip and 'japan' has to pick
# both, which is what makes one of them a match and the other ambiguous.
#
OSAKA = "Osaka, Japan, May 2024"
KYOTO = "Kyoto, Japan, June 2024"

# Two trips eight months apart.  The names say which is which, so a test
# reads its own output without working the dates out.
#
EARLIER = "Earlier trip"
LATER = "Later trip"


####################################################################
#
def named_pair() -> tuple[dict, dict]:
    """Two trips sharing a word in their names."""
    return (
        b.trip(name=OSAKA, objects=[b.flight()]),
        b.trip(
            name=KYOTO,
            start="2024-06-01",
            end="2024-06-04",
            objects=[b.lodging()],
        ),
    )


####################################################################
#
def dated_pair() -> tuple[dict, dict]:
    """
    Two trips far apart in time, given later first.

    Always the wrong way round, so a test that gets them back in travel
    order has shown it ordered them rather than kept them as they came.
    """
    return (
        b.trip(
            name=LATER,
            start="2024-09-01",
            end="2024-09-04",
            objects=[b.flight()],
        ),
        b.trip(
            name=EARLIER,
            start="2024-01-01",
            end="2024-01-04",
            objects=[b.lodging()],
        ),
    )


####################################################################
#
@pytest.fixture
def staged(runner: CliRunner, tmp_path: Path) -> Callable[..., Path]:
    """
    Stage trips into a fresh archive and give back its root.

    Every command but the staging ones needs an archive to work on
    rather than an export, so building one is groundwork rather than
    part of any test.
    """

    def stage(*trips: dict) -> Path:
        """Stage these trips, or one ordinary one when none are named."""
        if not trips:
            trips = (b.trip(objects=[b.flight()]),)
        export = write_export(tmp_path, *trips)
        archive_root = tmp_path / "archive"
        result = runner.invoke(
            main,
            ["stage-export", str(export), "--archive", str(archive_root)],
        )
        assert result.exit_code == 0, result.output
        return archive_root

    return stage


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
        export = write_export(tmp_path, *named_pair())
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
        self, runner: CliRunner, staged: Callable[..., Path]
    ) -> None:
        """
        GIVEN: an archive of two trips
        WHEN:  upload names one by part of its name
        THEN:  only that trip is planned

        A trip key is a digest nobody can recognise or type.
        """
        archive_root = staged(*named_pair())

        result = runner.invoke(
            main,
            ["upload", "--archive", str(archive_root), "--trip", "kyoto"],
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in(KYOTO, result.output)
        check.is_not_in(OSAKA, result.output)
        check.is_in("1 trips", result.output)

    ####################################################################
    #
    def test_an_ambiguous_name_is_refused(
        self, runner: CliRunner, staged: Callable[..., Path]
    ) -> None:
        """
        GIVEN: two trips whose names share a word
        WHEN:  upload names that word
        THEN:  it refuses and lists what matched

        Picking one would upload the wrong trip, and an identifier spent
        on the wrong trip cannot be taken back.
        """
        archive_root = staged(*named_pair())

        result = runner.invoke(
            main,
            ["upload", "--archive", str(archive_root), "--trip", "japan"],
        )

        check.not_equal(result.exit_code, 0)
        check.is_in("2 trips match", result.output)

    ####################################################################
    #
    def test_trips_are_planned_oldest_first(
        self, runner: CliRunner, staged: Callable[..., Path]
    ) -> None:
        """
        GIVEN: an archive of trips staged in no particular order
        WHEN:  upload plans them with a limit
        THEN:  the oldest is the one taken

        The archive orders trips by a digest, so without this --limit
        picks an arbitrary handful rather than working forward.
        """
        archive_root = staged(*dated_pair())

        result = runner.invoke(
            main, ["upload", "--archive", str(archive_root), "--limit", "1"]
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in(EARLIER, result.output)
        check.is_not_in(LATER, result.output)

    ####################################################################
    #
    def test_a_finished_trip_does_not_spend_the_limit(
        self, runner: CliRunner, staged: Callable[..., Path]
    ) -> None:
        """
        GIVEN: an archive whose oldest trip a run already finished
        WHEN:  upload runs with --limit 1
        THEN:  the next trip is planned instead of nothing

        Otherwise running with --limit 1 twice would do the first trip
        and then nothing, rather than walking forward a trip at a time.
        """
        archive_root = staged(*dated_pair())

        archive = Archive(archive_root)
        oldest = in_travel_order(archive, archive.trip_keys())[0]
        mark_uploaded(archive, oldest)

        result = runner.invoke(
            main, ["upload", "--archive", str(archive_root), "--limit", "1"]
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in(LATER, result.output)
        check.is_in("1 trips skipped", result.output)


########################################################################
########################################################################
#
class TestListCommand:
    """Tests for the `list` subcommand."""

    ####################################################################
    #
    def test_trips_are_listed_oldest_first_with_their_state(
        self, runner: CliRunner, staged: Callable[..., Path]
    ) -> None:
        """
        GIVEN: an archive of two trips, one already uploaded
        WHEN:  list is run
        THEN:  both appear oldest first, and the finished one is marked
        """
        archive_root = staged(*dated_pair())
        archive = Archive(archive_root)
        mark_uploaded(archive, in_travel_order(archive, archive.trip_keys())[0])

        result = runner.invoke(main, ["list", "--archive", str(archive_root)])

        check.equal(result.exit_code, 0, result.output)
        check.less(
            result.output.index(EARLIER),
            result.output.index(LATER),
            "oldest first",
        )
        check.is_in("1 already uploaded", result.output)

    ####################################################################
    #
    def test_pending_hides_what_is_done(
        self, runner: CliRunner, staged: Callable[..., Path]
    ) -> None:
        """
        GIVEN: an archive whose oldest trip is uploaded
        WHEN:  list is run with --pending
        THEN:  only the trip still to do is shown
        """
        archive_root = staged(*dated_pair())
        archive = Archive(archive_root)
        mark_uploaded(archive, in_travel_order(archive, archive.trip_keys())[0])

        result = runner.invoke(
            main, ["list", "--archive", str(archive_root), "--pending"]
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in(LATER, result.output)
        check.is_not_in(EARLIER, result.output)


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
    ) -> None:
        """
        GIVEN: an archive named only in a .env file
        WHEN:  a command that reads the archive runs
        THEN:  it reads that archive

        .env has to be loaded before a default is resolved from the
        environment, not when credentials are first needed -- a dry run
        never asks for credentials at all.
        """
        export = write_export(tmp_path, b.trip(objects=[b.flight()]))
        archive_root = tmp_path / "named-by-dotenv"
        runner.invoke(
            main, ["stage-export", str(export), "--archive", str(archive_root)]
        )

        project = tmp_path / "project"
        project.mkdir()
        (project / ".env").write_text(f"{ARCHIVE_ENV}={archive_root}\n")
        in_directory(project)
        environment.pop(ARCHIVE_ENV, None)

        result = runner.invoke(main, ["list"])

        check.equal(result.exit_code, 0, result.output)
        check.is_in("1 trips", result.output)

    ####################################################################
    #
    def test_a_leading_tilde_is_expanded(
        self,
        runner: CliRunner,
        tmp_path: Path,
        environment: MutableMapping[str, str],
    ) -> None:
        """
        GIVEN: an archive path written with a leading ~
        WHEN:  a command resolves it
        THEN:  it lands under the home directory

        A shell expands one on the command line, but nothing expands one
        written in .env or exported with quotes.
        """
        home = tmp_path / "home"
        home.mkdir()
        environment["HOME"] = str(home)
        environment[ARCHIVE_ENV] = "~/Documents/tripsy-archive"

        export = write_export(tmp_path, b.trip(objects=[b.flight()]))
        result = runner.invoke(main, ["stage-export", str(export)])

        check.equal(result.exit_code, 0, result.output)
        check.is_true(
            (home / "Documents" / "tripsy-archive" / "trips").is_dir(),
            "staged under the expanded path",
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
        (root / "trips").mkdir(parents=True)
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
            main, ["upload", "--archive", str(archive_root), "--write"]
        )

        check.not_equal(result.exit_code, 0)
        check.is_in("no transportation_type", result.output)


########################################################################
########################################################################
#
class TestFixLocationsCommand:
    """Tests for the fix-locations command."""

    ####################################################################
    #
    @pytest.fixture
    def uploaded(
        self,
        staged: Callable[..., Path],
        tmp_path: Path,
        mocker: MockerFixture,
        faker: Faker,
    ) -> Callable[..., tuple[Path, list[tuple]]]:
        """
        One uploaded trip holding one endpoint with an address and no
        position, with Tripsy stubbed out.

        Returns a factory taking the cached answer, if any, and giving
        back the archive root and the list every update lands in.
        """

        def make(cached: tuple[float, float] | None) -> tuple[Path, list]:
            archive_root = staged()
            archive = Archive(archive_root)
            key = next(iter(archive.trip_keys()))
            mark_uploaded(archive, key)
            trip = staged_trip(archive, key)
            assert trip is not None

            address = faker.street_address()
            updates: list[tuple] = []
            client = mocker.MagicMock()
            client.trip_ids_by_identifier.return_value = {
                str(trip.internal_identifier): 99
            }
            client.update_child.side_effect = lambda *args, **kwargs: (
                updates.append(args)
            )
            mocker.patch(
                "tripsy_exim.cli.open_session"
            ).return_value.__enter__.return_value = client
            mocker.patch(
                "tripsy_exim.cli.unplaced_in",
                return_value=[
                    Unplaced(
                        collection="transportations",
                        child_id=7,
                        name=faker.company(),
                        address=address,
                        prefix="departure_",
                    )
                ],
            )
            mocker.patch(
                "tripsy_exim.cli.positions_in", return_value=[(37.0, -122.0)]
            )

            cache_path = tmp_path / "geocode.json"
            if cached is not None:
                cache = Cache(cache_path)
                cache.put(
                    Found(
                        address=normalised(address),
                        position=cached,
                        label=faker.city(),
                        service="nominatim",
                    )
                )
                cache.save()
            return archive_root, updates

        return make

    ####################################################################
    #
    @pytest.mark.parametrize(
        "cached,expected",
        [
            (None, "would look up"),
            ((37.1, -122.1), "would place"),
        ],
        ids=["nothing cached", "answer already cached"],
    )
    def test_a_dry_run_sends_nothing(
        self,
        runner: CliRunner,
        tmp_path: Path,
        uploaded: Callable[..., tuple[Path, list]],
        cached: tuple[float, float] | None,
        expected: str,
    ) -> None:
        """
        GIVEN: a trip with an endpoint carrying an address and no position
        WHEN:  fix-locations runs without --write
        THEN:  nothing is sent to Tripsy, whether or not the cache
               already holds the answer

        A warm cache is the case that matters: the whole plan-first
        habit rests on a dry run being dry no matter what is already
        known.
        """
        archive_root, updates = uploaded(cached)

        result = runner.invoke(
            main,
            [
                "fix-locations",
                "--archive",
                str(archive_root),
                "--cache",
                str(tmp_path / "geocode.json"),
            ],
        )

        check.equal(result.exit_code, 0, result.output)
        check.equal(updates, [], "a dry run writes nothing")
        check.is_in(expected, result.output)

    ####################################################################
    #
    def test_a_dry_run_asks_the_geocoder_nothing(
        self,
        runner: CliRunner,
        tmp_path: Path,
        mocker: MockerFixture,
        uploaded: Callable[..., tuple[Path, list]],
    ) -> None:
        """
        GIVEN: an endpoint whose address the cache does not hold
        WHEN:  fix-locations runs without --write
        THEN:  no geocoder is built and no lookup is made

        A lookup is a request to an outside service under a policy that
        counts them, so it is an outward act in its own right.
        """
        archive_root, _ = uploaded(None)
        build = mocker.patch("tripsy_exim.cli.lookup_for")

        runner.invoke(
            main,
            [
                "fix-locations",
                "--archive",
                str(archive_root),
                "--cache",
                str(tmp_path / "geocode.json"),
            ],
        )

        build.assert_not_called()

    ####################################################################
    #
    def test_a_write_places_what_the_cache_holds(
        self,
        runner: CliRunner,
        tmp_path: Path,
        environment: MutableMapping[str, str],
        uploaded: Callable[..., tuple[Path, list]],
    ) -> None:
        """
        GIVEN: an endpoint whose address the cache already answers
        WHEN:  fix-locations runs with --write
        THEN:  the position is sent to Tripsy under the endpoint's prefix
        """
        environment["TRIPSY_USERNAME"] = "someone"
        environment["TRIPSY_PASSWORD"] = "secret"
        archive_root, updates = uploaded((37.1, -122.1))

        result = runner.invoke(
            main,
            [
                "fix-locations",
                "--archive",
                str(archive_root),
                "--cache",
                str(tmp_path / "geocode.json"),
                "--write",
            ],
        )

        check.equal(result.exit_code, 0, result.output)
        check.equal(len(updates), 1)
        if updates:
            check.equal(
                updates[0][3],
                {"departure_latitude": 37.1, "departure_longitude": -122.1},
            )

    ####################################################################
    #
    def test_a_result_far_from_the_trip_is_refused(
        self,
        runner: CliRunner,
        tmp_path: Path,
        environment: MutableMapping[str, str],
        uploaded: Callable[..., tuple[Path, list]],
    ) -> None:
        """
        GIVEN: a cached answer thousands of kilometres from the trip
        WHEN:  fix-locations runs with --write
        THEN:  it is reported as refused and nothing is sent

        A geocoder does not fail by answering nothing; it fails by
        answering somewhere.
        """
        environment["TRIPSY_USERNAME"] = "someone"
        environment["TRIPSY_PASSWORD"] = "secret"
        archive_root, updates = uploaded((35.0, 135.0))

        result = runner.invoke(
            main,
            [
                "fix-locations",
                "--archive",
                str(archive_root),
                "--cache",
                str(tmp_path / "geocode.json"),
                "--write",
            ],
        )

        check.equal(result.exit_code, 0, result.output)
        check.equal(updates, [])
        check.is_in("REFUSED", result.output)

    ####################################################################
    #
    @pytest.mark.parametrize(
        "extra,changes,expected",
        [
            ([], False, "would be forgotten"),
            (["--write"], True, "forgotten"),
        ],
        ids=["dry run", "write"],
    )
    def test_forget_changes_the_cache_file_only_on_a_write(
        self,
        runner: CliRunner,
        tmp_path: Path,
        environment: MutableMapping[str, str],
        uploaded: Callable[..., tuple[Path, list]],
        mocker: MockerFixture,
        extra: list[str],
        changes: bool,
        expected: str,
    ) -> None:
        """
        GIVEN: an address the cache holds
        WHEN:  fix-locations --forget names it
        THEN:  the cache file on disk is left alone unless the run
               writes

        Keeping answers is a condition of Nominatim's terms, so a plan
        does not get to shorten the cache.
        """
        environment["TRIPSY_USERNAME"] = "someone"
        environment["TRIPSY_PASSWORD"] = "secret"
        archive_root, _ = uploaded((37.1, -122.1))
        cache_path = tmp_path / "geocode.json"
        address = next(iter(Cache(cache_path).entries))
        before = cache_path.read_text()

        # A forgotten address is asked about again, so a write would
        # reach the service.  What it answers is beside the point here.
        #
        mocker.patch(
            "tripsy_exim.cli.lookup_for",
            return_value=lambda asked: Found(
                address=asked, position=(37.5, -122.5), service="stub"
            ),
        )

        result = runner.invoke(
            main,
            [
                "fix-locations",
                "--archive",
                str(archive_root),
                "--cache",
                str(cache_path),
                "--forget",
                address,
                *extra,
            ],
        )

        check.equal(result.exit_code, 0, result.output)
        check.is_in(expected, result.output)
        check.equal(cache_path.read_text() != before, changes)
