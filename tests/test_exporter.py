#!/usr/bin/env python
#
"""
Test the exporter: one dated directory per run, read on its own.

What matters here is not that objects come back -- the API client's own
tests cover that -- but what a run leaves on disk, since that is the
thing somebody opens a year later with the app long gone.  So the tests
read the written directory rather than the return value wherever the two
would say the same thing.

`exported` runs one export against the fake and hands back where it
landed.  A test then reads that directory, which is how anybody using
the backup will meet it.
"""

# system imports
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check

from tests.fake_tripsy import FakeTripsy
from tripsy_exim.api import TripsyClient

# Project imports
from tripsy_exim.sync.exporter import (
    EXPORT_SCHEMA_VERSION,
    ExportOutcome,
    document_filename,
    export,
    only_one_run,
    stamp_for,
    trip_directory,
)

# One instant, so a run's directory name is the same every time and a
# test can name it rather than discover it.
#
WHEN = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
STAMP = "2026-09-21T120000Z"


####################################################################
#
@pytest.fixture
def seeded(paced_tripsy: FakeTripsy) -> Callable[..., dict[str, Any]]:
    """
    Put one trip with a flight and a night in the fake.

    Every test here needs a trip that carries something, and none of
    them is about what it carries, so the making of one is groundwork.
    """

    def seed(**fields: Any) -> dict[str, Any]:
        """One trip, with whatever fields a test wants changed."""
        trip = paced_tripsy.seed_trip(
            **{
                "name": "Osaka, June 2012",
                "starts_at": "2012-06-01",
                "ends_at": "2012-06-04",
                "has_dates": True,
                "timezone": "Asia/Tokyo",
                **fields,
            }
        )
        paced_tripsy.seed_child(
            trip["id"],
            "transportations",
            name="NRT to ITM",
            transportation_type="airplane",
            starts_at="2012-06-01T09:00:00Z",
            ends_at="2012-06-01T10:30:00Z",
        )
        paced_tripsy.seed_child(
            trip["id"],
            "hostings",
            name="Hotel Granvia",
            starts_at="2012-06-01T15:00:00Z",
            ends_at="2012-06-04T10:00:00Z",
        )
        return trip

    return seed


####################################################################
#
@pytest.fixture
def exported(
    api_client: TripsyClient, paced_tripsy: FakeTripsy, tmp_path: Path
) -> Callable[..., tuple[Path, ExportOutcome]]:
    """
    Run one export against the fake and say where it landed.

    The fake serves the documents' bytes as well as the API, standing in
    for the bucket a pre-signed URL points at -- the suite refuses real
    sockets, and a download that escaped would say so.
    """

    def run(**kwargs: Any) -> tuple[Path, ExportOutcome]:
        """One export of every trip the fake holds."""
        kwargs.setdefault("scope", {"all": True})
        kwargs.setdefault("when", WHEN)
        outcome = export(
            api_client,
            tmp_path / "exports",
            list(api_client.iter_trips()),
            fetch=paced_tripsy.document_content,
            **kwargs,
        )
        return outcome.path, outcome

    return run


####################################################################
#
@pytest.fixture
def only_trip() -> Callable[[Path], Path]:
    """
    The one trip directory in an export.

    Tests that seed a single trip are about what is written inside its
    directory, not about what the directory is called -- naming has its
    own tests.  Finding it rather than naming it keeps that rule in one
    place.
    """

    def find(export_path: Path) -> Path:
        """The only trip directory under a finished export."""
        found = [
            p
            for p in export_path.iterdir()
            if p.is_dir() and p.name != "quarantine"
        ]
        assert len(found) == 1, f"expected one trip, got {found}"
        return found[0]

    return find


########################################################################
########################################################################
#
class TestTripDirectory:
    """Tests for how a trip's directory is named."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "trip,expected",
        [
            (
                {"id": 7, "starts_at": "2026-11-21", "ends_at": "2026-12-07"},
                "2026-11-21--2026-12-07--7",
            ),
            (
                {
                    "id": 7,
                    "starts_at": "2026-11-21",
                    "ends_at": "2026-12-07",
                    "has_dates": False,
                },
                "undated--7",
            ),
            ({"id": 7, "starts_at": None, "ends_at": None}, "undated--7"),
            ({"id": 7}, "undated--7"),
        ],
        ids=["dated", "has_dates false", "null dates", "no dates at all"],
    )
    def test_a_trip_is_named_to_sort_by_travel_date(
        self, trip: dict[str, Any], expected: str
    ) -> None:
        """
        GIVEN: a trip payload
        WHEN:  its directory is named
        THEN:  the dates lead and the id makes it unique

        `has_dates` is authoritative even when the date fields are
        populated, which the API documents and the second case pins:
        a trip saying it has no dates is named as though it has none.
        """
        check.equal(trip_directory(trip), expected)

    ####################################################################
    #
    def test_two_trips_of_one_name_do_not_collide(self) -> None:
        """
        GIVEN: two trips with the same name and the same dates
        WHEN:  their directories are named
        THEN:  they are different directories

        Real accounts hold these: one journey recorded twice is what
        `merge` exists for, and a backup has to keep both rather than
        write one over the other.
        """
        dates = {"starts_at": "2012-04-11", "ends_at": "2012-04-26"}

        check.not_equal(
            trip_directory({"id": 41, **dates}),
            trip_directory({"id": 42, **dates}),
        )


########################################################################
########################################################################
#
class TestDocumentFilename:
    """Tests for what an attached file is saved as."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "title,expected",
        [
            (
                "Ryokan booking - as of June 2011.pdf",
                "88-Ryokan_booking_-_as_of_June_2011.pdf",
            ),
            ("plain.pdf", "88-plain.pdf"),
            ("../../etc/passwd", "88-etc_passwd"),
            ("", "88"),
            ("...", "88"),
        ],
        ids=["real title", "plain", "traversal", "empty", "dots only"],
    )
    def test_a_title_becomes_one_safe_filename(
        self, title: str, expected: str
    ) -> None:
        """
        GIVEN: a document title as somebody uploaded it
        WHEN:  it is turned into a filename
        THEN:  it is one component, keeping its extension

        The extension is what makes the saved file open in the right
        thing, so it survives; the id leads so two files of one name
        cannot collide.  A title is whatever a person typed, which is
        why the traversal case is here rather than hypothetical.
        """
        check.equal(document_filename({"id": 88, "title": title}), expected)


########################################################################
########################################################################
#
class TestExportLayout:
    """Tests for what one run leaves on disk."""

    ####################################################################
    #
    def test_a_run_writes_one_directory_per_trip_under_its_stamp(
        self,
        seeded: Callable[..., dict[str, Any]],
        exported: Callable[..., tuple[Path, ExportOutcome]],
    ) -> None:
        """
        GIVEN: two trips in the account
        WHEN:  an export runs
        THEN:  each has its own directory under the run's stamp
        """
        osaka = seeded()
        kyoto = seeded(
            name="Kyoto, May 2011", starts_at="2011-05-01", ends_at="2011-05-08"
        )

        path, outcome = exported()

        check.equal(path.name, STAMP)
        check.equal(outcome.trips, 2)
        check.equal(
            sorted(p.name for p in path.iterdir() if p.is_dir()),
            sorted(
                [
                    f"2012-06-01--2012-06-04--{osaka['id']}",
                    f"2011-05-01--2011-05-08--{kyoto['id']}",
                ]
            ),
        )

    ####################################################################
    #
    def test_a_trip_is_one_document_carrying_its_children(
        self,
        seeded: Callable[..., dict[str, Any]],
        exported: Callable[..., tuple[Path, ExportOutcome]],
        only_trip: Callable[[Path], Path],
    ) -> None:
        """
        GIVEN: a trip with a flight and a night
        WHEN:  an export runs
        THEN:  one trip.json holds the trip and both children

        One document rather than a file per object, because an export is
        read whole -- by a person or by a program loading it -- and never
        by this tool looking one object up.
        """
        seeded()

        path, _ = exported()

        document = json.loads((only_trip(path) / "trip.json").read_text())

        check.equal(document["schema_version"], EXPORT_SCHEMA_VERSION)
        check.equal(document["trip"]["name"], "Osaka, June 2012")
        check.equal(len(document["transportations"]), 1)
        check.equal(len(document["hostings"]), 1)
        check.equal(document["transportations"][0]["name"], "NRT to ITM")

    ####################################################################
    #
    def test_the_manifest_records_what_was_asked_for(
        self,
        seeded: Callable[..., dict[str, Any]],
        exported: Callable[..., tuple[Path, ExportOutcome]],
    ) -> None:
        """
        GIVEN: an export of a named subset
        WHEN:  its manifest is read
        THEN:  the scope it was given is there verbatim

        Without it a directory cannot say whether it means "everything
        Tripsy held" or "these trips", and the two are read differently
        by anybody restoring from it.
        """
        seeded()

        path, _ = exported(scope={"trips": ["Osaka"], "all": False})

        manifest = json.loads((path / "manifest.json").read_text())

        check.equal(manifest["scope"], {"trips": ["Osaka"], "all": False})
        check.equal(manifest["exported_at"], STAMP)
        check.equal(len(manifest["trips"]), 1)
        check.equal(manifest["trips"][0]["name"], "Osaka, June 2012")

    ####################################################################
    #
    def test_an_export_of_nothing_is_still_a_readable_export(
        self, exported: Callable[..., tuple[Path, ExportOutcome]]
    ) -> None:
        """
        GIVEN: an account with no trips
        WHEN:  an export runs
        THEN:  the directory exists and its manifest says it is empty

        An empty account and a failed run have to look different on
        disk, since one of them means the backup worked.
        """
        path, outcome = exported()

        check.equal(outcome.trips, 0)
        check.is_true((path / "manifest.json").is_file())
        check.equal(
            json.loads((path / "manifest.json").read_text())["trips"], []
        )


########################################################################
########################################################################
#
class TestExportIsWholeOrNothing:
    """Tests that a stamp never names a half-written run."""

    ####################################################################
    #
    def test_nothing_is_left_under_the_stamp_while_a_run_is_building(
        self,
        seeded: Callable[..., dict[str, Any]],
        api_client: TripsyClient,
        paced_tripsy: FakeTripsy,
        tmp_path: Path,
    ) -> None:
        """
        GIVEN: a run that fails partway through
        WHEN:  the exports directory is read afterwards
        THEN:  no directory under the stamp exists

        A reader cannot tell a half-written export from a finished one
        by looking inside it, so the guarantee has to be that a
        half-written one never has the name.
        """
        seeded()

        def refuse(url: str) -> bytes:
            raise RuntimeError("the bucket is down")

        paced_tripsy.seed_child(1, "documents", title="itinerary.pdf")

        with pytest.raises(RuntimeError):
            export(
                api_client,
                tmp_path / "exports",
                list(api_client.iter_trips()),
                scope={"all": True},
                when=WHEN,
                fetch=refuse,
            )

        check.is_false((tmp_path / "exports" / STAMP).exists())

    ####################################################################
    #
    def test_a_second_run_of_the_same_instant_is_refused(
        self,
        seeded: Callable[..., dict[str, Any]],
        exported: Callable[..., tuple[Path, ExportOutcome]],
    ) -> None:
        """
        GIVEN: an export already written under a stamp
        WHEN:  another run of the same instant is attempted
        THEN:  it is refused rather than merging into the first

        Merging would make one directory two instants, which is the one
        thing an export must never be.  A lock is what keeps a scheduled
        run and a manual one from reaching this at all.
        """
        seeded()
        exported()

        with pytest.raises(FileExistsError):
            exported()

    ####################################################################
    #
    def test_a_half_written_run_is_rebuilt_not_written_into(
        self,
        seeded: Callable[..., dict[str, Any]],
        exported: Callable[..., tuple[Path, ExportOutcome]],
        tmp_path: Path,
    ) -> None:
        """
        GIVEN: the leavings of an interrupted run
        WHEN:  a run of the same instant starts
        THEN:  what it left is gone, not carried into the new export

        An export is one instant.  Writing into what an earlier run
        abandoned would quietly make it two.
        """
        seeded()
        leftovers = tmp_path / "exports" / f".{STAMP}.partial"
        leftovers.mkdir(parents=True)
        (leftovers / "stale.json").write_text("{}")

        path, _ = exported()

        check.is_false((path / "stale.json").exists())


########################################################################
########################################################################
#
class TestDocuments:
    """Tests for the files attached to a trip."""

    ####################################################################
    #
    def test_an_attachment_is_downloaded_beside_its_trip(
        self,
        seeded: Callable[..., dict[str, Any]],
        paced_tripsy: FakeTripsy,
        exported: Callable[..., tuple[Path, ExportOutcome]],
        only_trip: Callable[[Path], Path],
    ) -> None:
        """
        GIVEN: a trip carrying a PDF
        WHEN:  an export runs
        THEN:  the bytes are on disk under the trip, named readably

        The whole reason a trip is a directory rather than a file.
        """
        trip = seeded()
        document = paced_tripsy.seed_child(
            trip["id"], "documents", title="Ryokan booking.pdf"
        )
        paced_tripsy.put_document_content(document["id"], b"%PDF-1.4 real")

        path, outcome = exported()

        saved = (
            only_trip(path)
            / "documents"
            / f"{document['id']}-Ryokan_booking.pdf"
        )

        check.equal(outcome.documents, 1)
        check.equal(saved.read_bytes(), b"%PDF-1.4 real")

    ####################################################################
    #
    def test_the_expiring_url_is_not_recorded(
        self,
        seeded: Callable[..., dict[str, Any]],
        paced_tripsy: FakeTripsy,
        exported: Callable[..., tuple[Path, ExportOutcome]],
        only_trip: Callable[[Path], Path],
    ) -> None:
        """
        GIVEN: a trip carrying a document
        WHEN:  its entry in trip.json is read
        THEN:  the download URL is gone and the saved file is named

        `temp_read_url` is pre-signed and expires.  Recording it would
        archive a link that is dead by the time anybody follows it,
        which is worse than recording nothing: it looks like it works.
        """
        trip = seeded()
        document = paced_tripsy.seed_child(
            trip["id"], "documents", title="itinerary.pdf"
        )

        path, _ = exported()

        entry = json.loads((only_trip(path) / "trip.json").read_text())[
            "documents"
        ][0]

        check.is_not_in("temp_read_url", entry)
        check.equal(entry["file"], f"documents/{document['id']}-itinerary.pdf")
        check.equal(entry["title"], "itinerary.pdf")

    ####################################################################
    #
    def test_what_a_document_is_attached_to_survives(
        self,
        seeded: Callable[..., dict[str, Any]],
        paced_tripsy: FakeTripsy,
        exported: Callable[..., tuple[Path, ExportOutcome]],
        only_trip: Callable[[Path], Path],
    ) -> None:
        """
        GIVEN: a document attached to one leg rather than to the trip
        WHEN:  an export runs
        THEN:  which leg it belongs to is still recorded

        A boarding pass belongs to a flight, not to a fortnight.  The
        link arrays are the only thing that says so, and a backup that
        dropped them would keep the file and lose its meaning.
        """
        trip = seeded()
        leg = paced_tripsy.seed_child(
            trip["id"], "transportations", name="ITM to NRT"
        )
        paced_tripsy.seed_child(
            trip["id"],
            "documents",
            title="boarding-pass.pdf",
            transportations=[leg["id"]],
        )

        path, _ = exported()

        entry = json.loads((only_trip(path) / "trip.json").read_text())[
            "documents"
        ][0]

        check.equal(entry["transportations"], [leg["id"]])


########################################################################
########################################################################
#
class TestQuarantine:
    """Tests for payloads the models will not take."""

    ####################################################################
    #
    def test_a_payload_that_will_not_parse_is_kept_not_dropped(
        self,
        seeded: Callable[..., dict[str, Any]],
        paced_tripsy: FakeTripsy,
        exported: Callable[..., tuple[Path, ExportOutcome]],
    ) -> None:
        """
        GIVEN: a child object whose field has changed type
        WHEN:  an export runs
        THEN:  it is written verbatim under quarantine and the run goes on

        Tripsy can change the type of a field it already returns, and
        pydantic rejects the whole object when it does.  Dropping it
        would cost every other field on that object -- and abort the
        export that was supposed to be protecting the data.
        """
        trip = seeded()
        paced_tripsy.seed_child(
            trip["id"], "activities", name="Museum", latitude="not a number"
        )

        path, outcome = exported()

        kept = list((path / "quarantine").glob("*.json"))

        check.equal(len(outcome.quarantined), 1)
        check.equal(len(kept), 1)
        check.equal(
            json.loads(kept[0].read_text())["payload"]["latitude"],
            "not a number",
        )
        check.equal(outcome.trips, 1, "the run finished anyway")


########################################################################
########################################################################
#
class TestTheLock:
    """Tests that two runs cannot write at once."""

    ####################################################################
    #
    def test_one_holder_at_a_time(self, tmp_path: Path) -> None:
        """
        GIVEN: a run holding the exports directory
        WHEN:  another asks for it
        THEN:  it is refused rather than made to wait

        A scheduled export and one started by hand can land together.
        The second has nothing to do, so it is told at once instead of
        queueing behind a run that may take a long time.
        """
        with only_one_run(tmp_path):
            with pytest.raises(BlockingIOError):
                with only_one_run(tmp_path):
                    pass

    ####################################################################
    #
    def test_the_lock_goes_away_with_the_run(self, tmp_path: Path) -> None:
        """
        GIVEN: a run that has finished
        WHEN:  another asks for the same directory
        THEN:  it gets it

        The lock is held on an open descriptor rather than by the file
        existing, so it is released however the run ends -- crash
        included.  A lock file left behind cannot wedge every run after
        it, which is the failure nobody diagnoses at 3am.
        """
        with only_one_run(tmp_path):
            pass

        with only_one_run(tmp_path):
            pass


########################################################################
########################################################################
#
class TestStamp:
    """Tests for how a run is named."""

    ####################################################################
    #
    def test_a_stamp_is_utc_and_safe_as_a_directory_name(self) -> None:
        """
        GIVEN: an instant in a zone that is not UTC
        WHEN:  a run is named after it
        THEN:  it is the UTC time, carrying nothing a filesystem refuses

        Colon-free because a stamp is a directory name, and not every
        filesystem takes a colon in one.
        """
        local = datetime(
            2026, 9, 21, 5, 0, 0, tzinfo=timezone(timedelta(hours=-7))
        )

        stamp = stamp_for(local)

        check.equal(stamp, "2026-09-21T120000Z")
        check.is_not_in(":", stamp)
