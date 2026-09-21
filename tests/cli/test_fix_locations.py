#!/usr/bin/env python
#
"""Test the `fix-locations` subcommand."""

# system imports
from collections.abc import Callable, MutableMapping
from pathlib import Path

# 3rd party imports
import pytest
import pytest_check as check
from click.testing import CliRunner
from faker import Faker
from pytest_mock import MockerFixture

# Project imports
from tripsy_exim.cli import main
from tripsy_exim.geocode import Cache, Found, normalised
from tripsy_exim.store import Archive
from tripsy_exim.sync.importer import Unplaced, mark_uploaded, staged_trip


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
        opened: Callable[[Path], Archive],
    ) -> Callable[..., tuple[Path, list[tuple]]]:
        """
        One uploaded trip holding one endpoint with an address and no
        position, with Tripsy stubbed out.

        Returns a factory taking the cached answer, if any, and giving
        back the archive root and the list every update lands in.
        """

        def make(cached: tuple[float, float] | None) -> tuple[Path, list]:
            archive_root = staged()
            archive = opened(archive_root)
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
                "--archive-root",
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
                "--archive-root",
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
                "--archive-root",
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
                "--archive-root",
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
                "--archive-root",
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
