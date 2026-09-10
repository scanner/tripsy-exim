#!/usr/bin/env python
#
"""Test the on-disk archive: keying, merge-writes, and round trips."""

# system imports
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check
from faker import Faker

# Project imports
from tripsy_exim.models import Activity, Collaborator, Expense, Hosting, Trip
from tripsy_exim.store import ARCHIVE_SCHEMA_VERSION, Archive, local_key


########################################################################
########################################################################
#
class TestLocalKey:
    """Tests for how objects are named on disk."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "override,expected",
        [
            pytest.param(
                {},
                lambda p: p["internal_identifier"],
                id="minted-wins-over-id",
            ),
            pytest.param(
                {"internal_identifier": None},
                lambda p: f"tripsy-{p['id']}",
                id="no-identifier",
            ),
            pytest.param(
                {"internal_identifier": ""},
                lambda p: f"tripsy-{p['id']}",
                id="empty-identifier",
            ),
            pytest.param(
                {"internal_identifier": "app-made-this"},
                lambda p: f"tripsy-{p['id']}",
                id="foreign-identifier",
            ),
        ],
    )
    def test_a_minted_identifier_is_preferred_over_the_tripsy_id(
        self,
        trip_payload: dict[str, Any],
        override: dict[str, Any],
        expected: Callable[[dict[str, Any]], str],
    ) -> None:
        """
        GIVEN: a trip we imported, or one created in the Tripsy app
        WHEN:  its archive key is derived
        THEN:  an identifier we minted is used when there is one, since it
               survives a move to another provider; otherwise the Tripsy id
        """
        payload = {**trip_payload, **override}

        assert local_key(Trip.model_validate(payload)) == expected(payload)

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

        check.is_not_in(key, (".", ".."), "never a directory reference")
        check.is_false(key.startswith("."), "never a hidden file")
        check.is_not_in("/", key, "never a separator")
        check.is_in(
            archive.root.resolve(), path.resolve().parents, "stays inside"
        )


########################################################################
########################################################################
#
class TestArchiveWrites:
    """Tests for merge-on-write and the round trip back off disk."""

    ####################################################################
    #
    def test_a_trip_round_trips_and_its_file_is_self_describing(
        self, archive: Archive, trip: Trip
    ) -> None:
        """
        GIVEN: a canonical trip
        WHEN:  it is written to the archive and read back
        THEN:  the model is unchanged, and the file names its schema and
               model so it can be understood on its own
        """
        path = archive.write(trip)
        restored = archive.read(Trip, path)
        document = json.loads(path.read_text())

        check.equal(restored, trip, "round trips unchanged")
        check.equal(
            restored.model_fields_set if restored else None,
            trip.model_fields_set,
            "set fields preserved",
        )
        check.equal(
            document["schema_version"], ARCHIVE_SCHEMA_VERSION, "schema"
        )
        check.equal(document["kind"], "Trip", "model named")
        check.equal(
            archive.trip_keys(), [trip.internal_identifier], "discoverable"
        )

    ####################################################################
    #
    @pytest.mark.parametrize(
        "faker_locale,is_ascii",
        [
            pytest.param("ja_JP", False, id="japanese"),
            pytest.param("el_GR", False, id="greek"),
            pytest.param("en_US", True, id="english"),
        ],
    )
    def test_place_names_in_any_script_survive_the_archive(
        self,
        archive: Archive,
        trip_factory: Callable[..., Trip],
        faker: Faker,
        faker_locale: str,
        is_ascii: bool,
    ) -> None:
        """
        GIVEN: a trip named in a script the archive never anticipated
        WHEN:  it is written and read back
        THEN:  the text is unchanged and stored as UTF-8 rather than
               escaped, so an archive stays readable outside this tool
        """
        built = trip_factory(name=faker.city(), description=faker.paragraph())

        path = archive.write(built)
        restored = archive.read(Trip, path)
        raw = path.read_text(encoding="utf-8")

        check.equal(
            str(built.name).isascii(),
            is_ascii,
            f"the {faker_locale} locale really applied",
        )
        check.equal(restored, built, "round trips unchanged")
        check.is_in(str(built.name), raw, "written as UTF-8, not escaped")

    ####################################################################
    #
    @pytest.mark.parametrize(
        "model,payload_fixture,collection",
        [
            pytest.param(Hosting, "hosting_payload", "hostings", id="hosting"),
            pytest.param(
                Activity, "activity_payload", "activities", id="activity"
            ),
            pytest.param(Expense, "expense_payload", "expenses", id="expense"),
            pytest.param(
                Collaborator,
                "collaborator_payload",
                "collaborators",
                id="collaborator",
            ),
        ],
    )
    def test_child_objects_round_trip_under_their_trip(
        self,
        request: pytest.FixtureRequest,
        archive: Archive,
        model: type[Any],
        payload_fixture: str,
        collection: str,
    ) -> None:
        """
        GIVEN: a child object of a trip
        WHEN:  it is written and read back
        THEN:  it lands under the trip in its own collection, unchanged
        """
        obj = model.model_validate(request.getfixturevalue(payload_fixture))

        path = archive.write(obj, trip_key="trip-key")

        check.equal(path.parent.name, collection, "collection directory")
        check.equal(path.parent.parent.name, "trip-key", "under its trip")
        check.equal(archive.read(model, path), obj, "round trips unchanged")

    ####################################################################
    #
    def test_price_survives_the_json_round_trip_as_a_decimal(
        self, archive: Archive, hosting: Hosting
    ) -> None:
        """
        GIVEN: a hosting whose price is a Decimal
        WHEN:  it is written as JSON and read back
        THEN:  it is stored as a string and returns exact, not as a float
        """
        path = archive.write(hosting, trip_key="t")
        restored = archive.read(Hosting, path)

        check.equal(
            json.loads(path.read_text())["data"]["price"],
            "78.5",
            "stored as a string",
        )
        check.equal(
            restored.price if restored else None,
            Decimal("78.5"),
            "exact on the way back",
        )

    ####################################################################
    #
    def test_a_partial_write_does_not_erase_stored_fields(
        self, archive: Archive, hosting: Hosting
    ) -> None:
        """
        GIVEN: a hosting already archived with a price
        WHEN:  a later export without expense permission omits price
        THEN:  the stored price survives and the new name is applied
        """
        archive.write(hosting, trip_key="t")
        partial = Hosting.model_validate(
            {
                "id": hosting.id,
                "internal_identifier": hosting.internal_identifier,
                "name": "Renamed Lodging",
            }
        )

        path = archive.write(partial, trip_key="t")
        restored = archive.read(Hosting, path)
        assert restored is not None

        check.equal(restored.price, Decimal("78.5"), "price not erased")
        check.equal(restored.currency, "EUR", "currency not erased")
        check.equal(restored.name, "Renamed Lodging", "newer field applied")

    ####################################################################
    #
    def test_undocumented_and_source_fields_survive_the_archive(
        self,
        archive: Archive,
        hosting_payload_factory: Callable[..., dict[str, Any]],
    ) -> None:
        """
        GIVEN: a hosting carrying undocumented and source-only fields
        WHEN:  it is written and read back
        THEN:  both are still there and still distinguishable
        """
        hosting = Hosting.model_validate(
            hosting_payload_factory(sort_order=3, custom_icon="bed")
        ).with_source(tripit_segment_id="abc-123")

        path = archive.write(hosting, trip_key="t")
        restored = archive.read(Hosting, path)
        assert restored is not None

        check.equal(
            restored.wire_extras,
            {"sort_order": 3, "custom_icon": "bed"},
            "undocumented Tripsy fields",
        )
        check.equal(
            restored.source_extras,
            {"tripit_segment_id": "abc-123"},
            "source-only fields",
        )

    ####################################################################
    #
    def test_a_newer_schema_is_refused(
        self, archive: Archive, trip: Trip
    ) -> None:
        """
        GIVEN: a file written by a later version of this tool
        WHEN:  it is read
        THEN:  it is refused rather than silently misread
        """
        path = archive.write(trip)
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
        missing = archive.read(Trip, archive.root / "trips/none/trip.json")

        check.is_none(missing, "absent object reads as None")
        check.equal(archive.trip_keys(), [], "no trips listed")

    ####################################################################
    #
    def test_a_child_object_without_a_trip_key_is_refused(
        self, archive: Archive, hosting: Hosting
    ) -> None:
        """
        GIVEN: a hosting and no trip to put it under
        WHEN:  it is written
        THEN:  a ValueError is raised rather than a stray file created
        """
        with pytest.raises(ValueError, match="trip_key"):
            archive.write(hosting)


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
        THEN:  a usable empty manifest comes back rather than an error
        """
        manifest = archive.read_manifest()

        check.is_none(manifest["last_export_at"], "no watermark yet")
        check.equal(manifest["identifier_cache"], {}, "empty cache")

    ####################################################################
    #
    def test_recording_a_run_advances_the_watermark_and_keeps_the_cache(
        self, archive: Archive, trip: Trip
    ) -> None:
        """
        GIVEN: a manifest carrying a cached identifier mapping
        WHEN:  a run that began at a known time is recorded
        THEN:  the watermark is that time in the format updatedSince takes,
               and the cache is left alone
        """
        cache = {str(trip.internal_identifier): trip.id}
        manifest = archive.read_manifest()
        manifest["identifier_cache"] = cache
        archive.write_manifest(manifest)

        archive.record_export(datetime(2027, 3, 17, 14, 30, tzinfo=UTC))

        stored = archive.read_manifest()
        check.equal(
            stored["last_export_at"], "2027-03-17T14:30:00Z", "watermark"
        )
        check.equal(stored["identifier_cache"], cache, "cache kept")
