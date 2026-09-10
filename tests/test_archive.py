#!/usr/bin/env python
#
"""Test the on-disk archive: keying, merge-writes, and round trips."""

# system imports
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest

# Project imports
from tripsy_exim.models import (
    Activity,
    Collaborator,
    Expense,
    Hosting,
    Trip,
    mint,
)
from tripsy_exim.store import ARCHIVE_SCHEMA_VERSION, Archive, local_key

# Synthetic throughout; nothing here corresponds to a real trip.
#
TRIP_PAYLOAD: dict[str, Any] = {
    "id": 42,
    "internal_identifier": mint("ics", "trip-uid-1"),
    "name": "Example Trip",
    "timezone": "Europe/Rome",
    "starts_at": "2027-06-01",
    "ends_at": "2027-06-15",
    "has_dates": True,
}

HOSTING_PAYLOAD: dict[str, Any] = {
    "id": 101,
    "internal_identifier": mint("ics", "hosting-uid-1"),
    "trip": 42,
    "name": "Example Lodging",
    "starts_at": "2027-06-01T14:00:00Z",
    "ends_at": "2027-06-05T11:00:00Z",
    "timezone": "Europe/Rome",
    "price": 78.5,
    "currency": "EUR",
}


####################################################################
#
@pytest.fixture
def archive(tmp_path: Path) -> Archive:
    """An empty archive rooted in a temporary directory."""
    return Archive(tmp_path / "archive")


########################################################################
########################################################################
#
class TestLocalKey:
    """Tests for how objects are named on disk."""

    ####################################################################
    #
    def test_minted_identifier_wins_over_the_tripsy_id(self) -> None:
        """
        GIVEN: a trip we imported, carrying an identifier we minted
        WHEN:  its archive key is derived
        THEN:  the identifier is used, so the key survives a provider move
        """
        trip = Trip.model_validate(TRIP_PAYLOAD)

        assert local_key(trip) == TRIP_PAYLOAD["internal_identifier"]

    ####################################################################
    #
    @pytest.mark.parametrize(
        "payload,expected",
        [
            pytest.param({"id": 42}, "tripsy-42", id="no-identifier"),
            pytest.param(
                {"id": 42, "internal_identifier": ""}, "tripsy-42", id="empty"
            ),
            pytest.param(
                {"id": 42, "internal_identifier": "app-made-this"},
                "tripsy-42",
                id="foreign-identifier",
            ),
        ],
    )
    def test_trips_we_did_not_mint_fall_back_to_the_tripsy_id(
        self, payload: dict[str, Any], expected: str
    ) -> None:
        """
        GIVEN: a trip created in the Tripsy app rather than imported
        WHEN:  its archive key is derived
        THEN:  the Tripsy id is used
        """
        assert local_key(Trip.model_validate(payload)) == expected

    ####################################################################
    #
    @pytest.mark.parametrize(
        "obj",
        [
            pytest.param(Trip(name="Example Trip"), id="nothing-to-key-on"),
            pytest.param(
                Hosting.model_validate({"internal_identifier": ".."}),
                id="identifier-sanitises-away",
            ),
        ],
    )
    def test_an_unidentifiable_object_is_refused(self, obj: Any) -> None:
        """
        GIVEN: an object with no usable identifier and no id
        WHEN:  its archive key is derived
        THEN:  a ValueError is raised rather than a colliding key invented
        """
        with pytest.raises(ValueError):
            local_key(obj)

    ####################################################################
    #
    @pytest.mark.parametrize(
        "identifier",
        [
            pytest.param("../../etc/passwd", id="traversal"),
            pytest.param(".hidden", id="leading-dot"),
        ],
    )
    def test_a_foreign_identifier_cannot_escape_the_archive(
        self, archive: Archive, identifier: str
    ) -> None:
        """
        GIVEN: an object whose only identifier is a path rather than a name
        WHEN:  it is written to the archive
        THEN:  the key is one plain component and the file stays inside
        """
        hosting = Hosting.model_validate({"internal_identifier": identifier})

        key = local_key(hosting)
        path = archive.write(hosting, trip_key="t")

        assert key not in (".", "..")
        assert not key.startswith(".")
        assert "/" not in key
        assert archive.root.resolve() in path.resolve().parents


########################################################################
########################################################################
#
class TestArchiveWrites:
    """Tests for merge-on-write and the round trip back off disk."""

    ####################################################################
    #
    def test_a_trip_round_trips_unchanged(self, archive: Archive) -> None:
        """
        GIVEN: a canonical trip
        WHEN:  it is written to the archive and read back
        THEN:  the model that comes back equals the one that went in
        """
        trip = Trip.model_validate(TRIP_PAYLOAD)

        path = archive.write(trip)
        restored = archive.read(Trip, path)

        assert restored == trip
        assert restored is not None
        assert restored.model_fields_set == trip.model_fields_set

    ####################################################################
    #
    @pytest.mark.parametrize(
        "model,payload,collection",
        [
            pytest.param(Hosting, HOSTING_PAYLOAD, "hostings", id="hosting"),
            pytest.param(
                Activity,
                {"id": 202, "name": "Example Activity", "price": 45.0},
                "activities",
                id="activity",
            ),
            pytest.param(
                Expense,
                {"id": 404, "title": "Example Expense", "price": 12.25},
                "expenses",
                id="expense",
            ),
            pytest.param(
                Collaborator,
                {"id": 2, "name": "Example Person", "joined": True},
                "collaborators",
                id="collaborator",
            ),
        ],
    )
    def test_child_objects_round_trip_under_their_trip(
        self,
        archive: Archive,
        model: type[Any],
        payload: dict[str, Any],
        collection: str,
    ) -> None:
        """
        GIVEN: a child object of a trip
        WHEN:  it is written and read back
        THEN:  it lands under the trip in its own collection, unchanged
        """
        obj = model.model_validate(payload)

        path = archive.write(obj, trip_key="trip-key")

        assert path.parent.name == collection
        assert path.parent.parent.name == "trip-key"
        assert archive.read(model, path) == obj

    ####################################################################
    #
    def test_price_survives_the_json_round_trip_as_a_decimal(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a hosting whose price is a Decimal
        WHEN:  it is written as JSON and read back
        THEN:  it is still an exact Decimal, not a float
        """
        hosting = Hosting.model_validate(HOSTING_PAYLOAD)

        path = archive.write(hosting, trip_key="trip-key")
        restored = archive.read(Hosting, path)

        assert json.loads(path.read_text())["data"]["price"] == "78.5"
        assert restored is not None
        assert restored.price == Decimal("78.5")

    ####################################################################
    #
    def test_a_partial_write_does_not_erase_stored_fields(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a hosting already archived with a price
        WHEN:  a later export without expense permission omits price
        THEN:  the stored price survives and the new name is applied
        """
        archive.write(Hosting.model_validate(HOSTING_PAYLOAD), trip_key="t")
        partial = Hosting.model_validate(
            {
                "id": 101,
                "internal_identifier": HOSTING_PAYLOAD["internal_identifier"],
                "name": "Renamed Lodging",
            }
        )

        path = archive.write(partial, trip_key="t")
        restored = archive.read(Hosting, path)

        assert restored is not None
        assert restored.price == Decimal("78.5")
        assert restored.currency == "EUR"
        assert restored.name == "Renamed Lodging"

    ####################################################################
    #
    def test_undocumented_fields_survive_the_archive(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a hosting carrying fields the docs do not list
        WHEN:  it is written and read back
        THEN:  the fields are still there
        """
        hosting = Hosting.model_validate(
            {**HOSTING_PAYLOAD, "sort_order": 3, "custom_icon": "bed"}
        ).with_source(tripit_segment_id="abc-123")

        path = archive.write(hosting, trip_key="t")
        restored = archive.read(Hosting, path)

        assert restored is not None
        assert restored.wire_extras == {"sort_order": 3, "custom_icon": "bed"}
        assert restored.source_extras == {"tripit_segment_id": "abc-123"}

    ####################################################################
    #
    def test_files_are_self_describing(self, archive: Archive) -> None:
        """
        GIVEN: any archived object
        WHEN:  its file is inspected on its own
        THEN:  it names its schema version and the model it holds
        """
        path = archive.write(Trip.model_validate(TRIP_PAYLOAD))

        document = json.loads(path.read_text())

        assert document["schema_version"] == ARCHIVE_SCHEMA_VERSION
        assert document["kind"] == "Trip"

    ####################################################################
    #
    def test_a_newer_schema_is_refused(self, archive: Archive) -> None:
        """
        GIVEN: a file written by a later version of this tool
        WHEN:  it is read
        THEN:  it is refused rather than silently misread
        """
        path = archive.write(Trip.model_validate(TRIP_PAYLOAD))
        document = json.loads(path.read_text())
        document["schema_version"] = ARCHIVE_SCHEMA_VERSION + 1
        path.write_text(json.dumps(document))

        with pytest.raises(ValueError, match="archive schema"):
            archive.read(Trip, path)

    ####################################################################
    #
    def test_reading_a_missing_object_is_not_an_error(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: an empty archive
        WHEN:  an object that was never written is read
        THEN:  None comes back, so the first write has nothing to merge
        """
        assert archive.read(Trip, archive.root / "trips/none/trip.json") is None
        assert archive.trip_keys() == []

    ####################################################################
    #
    def test_a_child_object_without_a_trip_key_is_refused(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a hosting and no trip to put it under
        WHEN:  it is written
        THEN:  a ValueError is raised rather than a stray file created
        """
        with pytest.raises(ValueError, match="trip_key"):
            archive.write(Hosting.model_validate(HOSTING_PAYLOAD))


########################################################################
########################################################################
#
class TestManifest:
    """Tests for the sync manifest and the export watermark."""

    ####################################################################
    #
    def test_a_missing_manifest_reads_as_a_fresh_one(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: an archive that has never been exported to
        WHEN:  the manifest is read
        THEN:  a usable empty manifest comes back
        """
        manifest = archive.read_manifest()

        assert manifest["last_export_at"] is None
        assert manifest["identifier_cache"] == {}

    ####################################################################
    #
    def test_the_watermark_is_the_run_start_time(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: an export that began at a known time
        WHEN:  the run is recorded
        THEN:  the watermark is that time, in the format updatedSince takes
        """
        started = datetime(2027, 3, 17, 14, 30, tzinfo=UTC)

        archive.record_export(started)

        assert (
            archive.read_manifest()["last_export_at"] == "2027-03-17T14:30:00Z"
        )

    ####################################################################
    #
    def test_the_identifier_cache_survives_a_watermark_update(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a manifest carrying a cached identifier mapping
        WHEN:  a later run advances the watermark
        THEN:  the cache is still there
        """
        manifest = archive.read_manifest()
        manifest["identifier_cache"] = {"txim-ics-0123456789abcdef": 42}
        archive.write_manifest(manifest)

        archive.record_export(datetime(2027, 3, 17, 14, 30, tzinfo=UTC))

        cache = archive.read_manifest()["identifier_cache"]
        assert cache == {"txim-ics-0123456789abcdef": 42}
