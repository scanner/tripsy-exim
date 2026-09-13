#!/usr/bin/env python
#
"""Test staging parsed trips into the archive."""

# system imports
import json
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check
from faker import Faker

# Project imports
from tests import tripit_builder
from tests.ics_builder import build_calendar, to_ics
from tripsy_exim.sources import parse
from tripsy_exim.sources.join import SEPARATOR
from tripsy_exim.store import Archive
from tripsy_exim.sync import (
    REPORT_FILENAME,
    TripAlreadyArchived,
    archived_trips,
    stage,
    stage_export,
    stage_export_file,
    stage_file,
)


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

        # The manifest belongs at the archive root rather than inside a
        # trip, so it is not one of the files under test here.
        #
        written = [
            p for p in tmp_path.rglob("*.json") if p != archive.manifest_path
        ]
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


########################################################################
########################################################################
#
class TestStageExport:
    """Tests for staging a whole TripIt GDPR export."""

    ####################################################################
    #
    def test_every_trip_is_staged(self, tmp_path: Path, faker: Faker) -> None:
        """
        GIVEN: an export holding several trips
        WHEN:  it is staged
        THEN:  one result comes back per trip, each with its own directory

        An export is a whole account, where a .ics file is one trip.
        """
        archive = Archive(tmp_path)
        document = tripit_builder.random_export(faker, trips=4)

        staged = stage_export(archive, document)

        check.equal(len(staged), 4)
        check.equal(len({s.trip_key for s in staged}), 4)
        for one in staged:
            check.is_true(one.trip_path.exists())
            check.is_true(one.report_path.exists())
            check.greater(one.total, 0)

    ####################################################################
    #
    def test_children_land_under_their_own_trip(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: an export of several trips
        WHEN:  it is staged
        THEN:  no trip's objects are written under another trip's key
        """
        archive = Archive(tmp_path)

        staged = stage_export(archive, tripit_builder.random_export(faker))

        for one in staged:
            written = sum(
                1
                for p in archive.trip_dir(one.trip_key).rglob("*.json")
                if p.name != REPORT_FILENAME
            )
            # The trip's own file, plus one per child.
            #
            check.equal(written, one.total + 1)

    ####################################################################
    #
    def test_staging_twice_does_not_duplicate(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: an export staged once
        WHEN:  it is staged again
        THEN:  the same keys come back and nothing new is written

        Identity is derived from content, so a re-run has to land on the
        same objects rather than a second copy of them.
        """
        archive = Archive(tmp_path)
        document = tripit_builder.random_export(faker, trips=2)

        first = stage_export(archive, document)
        before = sorted(p.name for p in tmp_path.rglob("*.json"))
        second = stage_export(archive, document)

        assert [s.trip_key for s in first] == [s.trip_key for s in second]
        assert sorted(p.name for p in tmp_path.rglob("*.json")) == before

    ####################################################################
    #
    def test_namespace_gives_a_separate_key_space(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: one export staged into the real and a scratch namespace
        WHEN:  the keys are compared
        THEN:  they share none, so a shaping run spends no real identifier
        """
        archive = Archive(tmp_path)
        document = tripit_builder.random_export(faker, trips=2)

        real = stage_export(archive, document)
        scratch = stage_export(archive, document, "scratch-xyz")

        assert not {s.trip_key for s in real} & {s.trip_key for s in scratch}

    ####################################################################
    #
    def test_reads_the_file(self, tmp_path: Path, faker: Faker) -> None:
        """
        GIVEN: an export written to disk, TripIt's encoding and all
        WHEN:  it is staged from its path
        THEN:  the trips land and their names are recovered on the way in
        """
        archive = Archive(tmp_path / "archive")
        path = tmp_path / "export.json"
        path.write_text(
            json.dumps(tripit_builder.random_export(faker, trips=2)),
            encoding="utf-8",
        )

        staged = stage_export_file(archive, path)

        assert len(staged) == 2
        for one in staged:
            written = json.loads(one.trip_path.read_text())
            check.is_not_in("Ã", written.get("name") or "")
            check.is_not_in("â", written.get("name") or "")


########################################################################
########################################################################
#
class TestTheExportIsAuthoritative:
    """
    Tests that a calendar never rivals a trip the export already defined.

    The two sources mint different identifiers for one real trip, and
    Tripsy never releases an identifier, so staging both would leave a
    duplicate that could be neither merged nor undone.
    """

    ####################################################################
    #
    @staticmethod
    def matching_export(calendar_path: Path) -> dict[str, Any]:
        """An export describing the same trip as this calendar."""
        parsed = parse(calendar_path.read_text())
        assert parsed.join_key is not None
        name, starts, ends = parsed.join_key.split(SEPARATOR)
        return tripit_builder.export(
            tripit_builder.trip(name=name, start=starts, end=ends, objects=[])
        )

    ####################################################################
    #
    def test_a_calendar_for_an_archived_trip_is_refused(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a trip staged from the export
        WHEN:  the calendar for that same trip is staged
        THEN:  it is refused, naming the trip already held
        """
        archive = Archive(tmp_path / "archive")
        source = tmp_path / "trip.ics"
        source.write_text(calendar_text(faker, items=3, name="Osaka 2027"))
        stage_export(archive, self.matching_export(source))

        with pytest.raises(TripAlreadyArchived) as raised:
            stage_file(archive, source)

        check.is_in("Osaka 2027", str(raised.value))
        check.equal(len(archive.trip_keys()), 1, "no second trip written")

    ####################################################################
    #
    def test_a_calendar_for_an_unknown_trip_still_stages(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: an archive holding a different trip
        WHEN:  a calendar for a trip it does not hold is staged
        THEN:  it lands normally

        Most people will have no export at all, and the calendar path has
        to keep working for them.
        """
        archive = Archive(tmp_path / "archive")
        source = tmp_path / "trip.ics"
        source.write_text(calendar_text(faker, items=3, name="Osaka 2027"))
        stage_export(archive, tripit_builder.random_export(faker, trips=1))

        staged = stage_file(archive, source)

        assert staged.total > 0
        assert len(archive.trip_keys()) == 2

    ####################################################################
    #
    def test_an_ambiguous_match_is_refused_rather_than_guessed(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: two archived trips sharing one name and one span
        WHEN:  a calendar keys onto both
        THEN:  it is refused, saying which way it could not decide

        A journey planned twice looks identical from the outside.  The
        corpus holds one such pair, and nothing in either source can tell
        them apart, so a person has to.
        """
        archive = Archive(tmp_path / "archive")
        source = tmp_path / "trip.ics"
        source.write_text(calendar_text(faker, items=3, name="Twice Over"))
        document = self.matching_export(source)

        # The same trip twice over, as the export really does carry it.
        #
        document["Trips"].append(document["Trips"][0])
        stage_export(archive, document)

        with pytest.raises(TripAlreadyArchived) as raised:
            stage_file(archive, source)

        check.is_in("2 archived trips", str(raised.value))

    ####################################################################
    #
    def test_a_shaping_run_is_exempt(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a trip already staged from the export
        WHEN:  the calendar is staged into a throwaway namespace
        THEN:  it is allowed, because that key space is discarded

        A shaping run exists to restage a trip it will throw away, and
        nothing it mints ever reaches the real account.
        """
        archive = Archive(tmp_path / "archive")
        source = tmp_path / "trip.ics"
        source.write_text(calendar_text(faker, items=3, name="Osaka 2027"))
        stage_export(archive, self.matching_export(source))

        staged = stage_file(archive, source, namespace="scratch-run1")

        assert "scratch-run1" in staged.trip_key

    ####################################################################
    #
    def test_a_throwaway_trip_blocks_nothing(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a trip staged only under a throwaway namespace
        WHEN:  the same calendar is staged for real
        THEN:  it is allowed, since nothing real holds that trip yet
        """
        archive = Archive(tmp_path / "archive")
        source = tmp_path / "trip.ics"
        source.write_text(calendar_text(faker, items=3, name="Osaka 2027"))
        stage_file(archive, source, namespace="scratch-run1")

        staged = stage_file(archive, source)

        assert "scratch" not in staged.trip_key

    ####################################################################
    #
    def test_an_unkeyable_trip_matches_nothing(self, tmp_path: Path) -> None:
        """
        GIVEN: a trip that could not be keyed
        WHEN:  the archive is searched for it
        THEN:  nothing matches, rather than everything

        A missing key must never be treated as a wildcard.
        """
        assert archived_trips(Archive(tmp_path), None) == []
