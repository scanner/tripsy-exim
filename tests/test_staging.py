#!/usr/bin/env python
#
"""Test staging parsed calendars into the archive."""

# system imports
import json
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check
from faker import Faker

# Project imports
from tests.ics_builder import build_calendar, to_ics
from tripsy_exim.sources import parse
from tripsy_exim.store import Archive
from tripsy_exim.sync import REPORT_FILENAME, stage, stage_file


####################################################################
#
def calendar_text(faker: Faker, **kwargs: Any) -> str:
    """One synthetic calendar, as a .ics document."""
    return to_ics(build_calendar(faker, **kwargs))


########################################################################
########################################################################
#
class TestStage:
    """Tests for writing a parsed calendar to disk."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "kwargs,collection",
        [
            pytest.param({"items": 4, "lodging": 4}, "hostings", id="hostings"),
            pytest.param(
                {"items": 4, "flights": 4},
                "transportations",
                id="transportations",
            ),
            pytest.param({"items": 4}, "activities", id="activities"),
        ],
    )
    def test_children_land_in_their_collection(
        self,
        tmp_path: Path,
        faker: Faker,
        kwargs: dict[str, int],
        collection: str,
    ) -> None:
        """
        GIVEN: a calendar whose events are all of one kind
        WHEN:  it is staged
        THEN:  that collection holds a file per event
        """
        archive = Archive(tmp_path)
        parsed = parse(calendar_text(faker, **kwargs))

        staged = stage(archive, parsed)

        directory = archive.trip_dir(staged.trip_key) / collection
        check.equal(staged.counts[collection], 4)
        check.equal(len(list(directory.glob("*.json"))), 4)

    ####################################################################
    #
    def test_trip_is_written(self, tmp_path: Path, faker: Faker) -> None:
        """
        GIVEN: a parsed calendar
        WHEN:  it is staged
        THEN:  the trip lands at trip.json under its own key
        """
        archive = Archive(tmp_path)
        parsed = parse(calendar_text(faker, items=3))

        staged = stage(archive, parsed)

        check.is_true(staged.trip_path.exists())
        check.equal(staged.trip_path.name, "trip.json")
        check.equal(staged.trip_path.parent.name, staged.trip_key)

    ####################################################################
    #
    def test_children_share_the_trip_key(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a calendar with a mix of event kinds
        WHEN:  it is staged
        THEN:  every object sits under the one trip directory
        """
        archive = Archive(tmp_path)
        parsed = parse(calendar_text(faker, items=6, lodging=2, flights=2))

        staged = stage(archive, parsed)

        written = list(tmp_path.rglob("*.json"))
        trip_dir = archive.trip_dir(staged.trip_key)
        check.equal(len(archive.trip_keys()), 1)
        for path in written:
            check.is_true(
                trip_dir in path.parents or path.parent == trip_dir,
                f"{path} escaped {trip_dir}",
            )

    ####################################################################
    #
    def test_total_counts_the_children(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a calendar of six item events
        WHEN:  it is staged
        THEN:  the reported total is six, the trip not counted
        """
        archive = Archive(tmp_path)
        parsed = parse(calendar_text(faker, items=6, lodging=2, flights=1))

        staged = stage(archive, parsed)

        check.equal(staged.total, 6)

    ####################################################################
    #
    def test_staging_twice_does_not_duplicate(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a calendar already staged
        WHEN:  the same calendar is staged again
        THEN:  the same files are rewritten, none added

        Identifiers are derived from the source, so a second parse of the
        same text keys onto the same paths.
        """
        archive = Archive(tmp_path)
        text = calendar_text(faker, items=5, lodging=2)

        stage(archive, parse(text))
        before = sorted(p.name for p in tmp_path.rglob("*.json"))
        stage(archive, parse(text))
        after = sorted(p.name for p in tmp_path.rglob("*.json"))

        check.equal(before, after)

    ####################################################################
    #
    def test_report_records_what_the_parser_could_not_do(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a calendar whose events match no classification rule
        WHEN:  it is staged
        THEN:  report.json lists them as unclassified
        """
        archive = Archive(tmp_path)
        parsed = parse(calendar_text(faker, items=4))

        staged = stage(archive, parsed)

        document = json.loads(staged.report_path.read_text())
        check.equal(staged.report_path.name, REPORT_FILENAME)
        check.equal(len(document["unclassified"]), len(parsed.unclassified))
        check.equal(document["trip_key"], staged.trip_key)
        check.equal(document["counts"]["activities"], 4)


########################################################################
########################################################################
#
class TestStageFile:
    """Tests for staging straight from a .ics file."""

    ####################################################################
    #
    def test_reads_the_file(self, tmp_path: Path, faker: Faker) -> None:
        """
        GIVEN: a .ics file on disk
        WHEN:  it is staged
        THEN:  its trip and children are archived
        """
        source = tmp_path / "trip.ics"
        source.write_text(calendar_text(faker, items=3))
        archive = Archive(tmp_path / "archive")

        staged = stage_file(archive, source)

        check.is_true(staged.trip_path.exists())
        check.equal(staged.total, 3)

    ####################################################################
    #
    def test_namespace_gives_a_separate_key_space(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: one .ics file staged under the default namespace
        WHEN:  it is staged again under a scratch namespace
        THEN:  it lands beside the first, under different keys

        A shaping run must not spend the real identifiers, because the API
        never releases one once it has been used.
        """
        source = tmp_path / "trip.ics"
        source.write_text(calendar_text(faker, items=3))
        archive = Archive(tmp_path / "archive")

        real = stage_file(archive, source)
        scratch = stage_file(archive, source, namespace="scratch-run1")

        check.not_equal(real.trip_key, scratch.trip_key)
        check.equal(len(archive.trip_keys()), 2)
        check.is_in("scratch-run1", scratch.trip_key)
