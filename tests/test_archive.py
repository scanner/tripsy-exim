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


########################################################################
########################################################################
#
class TestSchemaDrift:
    """
    Tests for surviving a change to Tripsy's own payload shape.

    Three things can drift.  A new field is absorbed, a withdrawn field
    leaves what was already captured alone, and a field whose type changed
    defeats the model entirely -- only the last one needs the raw payload
    kept for a later look.
    """

    ####################################################################
    #
    def test_a_new_field_is_absorbed_and_reported(
        self,
        archive: Archive,
        hosting_payload_factory: Callable[..., dict[str, Any]],
    ) -> None:
        """
        GIVEN: a payload carrying a field Tripsy never used to send
        WHEN:  it is ingested
        THEN:  it archives normally, and the new field is reported rather
               than only silently absorbed
        """
        payload = hosting_payload_factory(loyalty_tier="gold")

        obj = archive.ingest(Hosting, payload, trip_key="t")

        check.is_not_none(obj, "still parses")
        check.equal(
            archive.unknown_fields["Hosting"], {"loyalty_tier"}, "drift seen"
        )
        check.equal(archive.quarantined(), [], "nothing quarantined")

    ####################################################################
    #
    @pytest.mark.parametrize(
        "override,field",
        [
            pytest.param({"price": "78.50 EUR"}, "price", id="money-to-string"),
            pytest.param({"id": "9f8e-7d6c"}, "id", id="int-id-to-uuid"),
            pytest.param(
                {"starts_at": "not a date"}, "starts_at", id="bad-date"
            ),
        ],
    )
    def test_a_changed_type_is_quarantined_not_lost(
        self,
        archive: Archive,
        hosting_payload_factory: Callable[..., dict[str, Any]],
        override: dict[str, Any],
        field: str,
    ) -> None:
        """
        GIVEN: a payload whose field changed to a type the model refuses
        WHEN:  it is ingested
        THEN:  the run continues, and the payload is kept verbatim with
               the reason, so it can be re-read once the model catches up
        """
        payload = hosting_payload_factory(**override)

        obj = archive.ingest(Hosting, payload, trip_key="t")

        assert obj is None, "the object cannot be parsed"
        quarantined = archive.quarantined()
        assert len(quarantined) == 1
        document = json.loads(quarantined[0].read_text(encoding="utf-8"))

        check.equal(document["payload"], payload, "kept byte for byte")
        check.equal(document["kind"], "Hosting", "model named")
        check.equal(document["trip_key"], "t", "owning trip recorded")
        check.is_in(
            field,
            [error["field"] for error in document["errors"]],
            "offending field named",
        )

    ####################################################################
    #
    def test_a_payload_with_nothing_to_key_on_is_quarantined(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a payload whose identifying fields have both gone
        WHEN:  it is ingested
        THEN:  it is quarantined rather than raising, since a renamed id
               field is drift like any other
        """
        obj = archive.ingest(Hosting, {"name": "Example Lodging"}, trip_key="t")

        check.is_none(obj, "cannot be keyed")
        check.equal(len(archive.quarantined()), 1, "kept anyway")

    ####################################################################
    #
    def test_a_withdrawn_field_leaves_the_archive_alone(
        self,
        archive: Archive,
        hosting_payload_factory: Callable[..., dict[str, Any]],
    ) -> None:
        """
        GIVEN: a hosting archived with a price
        WHEN:  a later payload no longer carries that field at all
        THEN:  it still parses and the captured value is left in place
        """
        first = hosting_payload_factory()
        archive.ingest(Hosting, first, trip_key="t")
        without_price = {k: v for k, v in first.items() if k != "price"}

        obj = archive.ingest(Hosting, without_price, trip_key="t")

        assert obj is not None
        stored = archive.read(Hosting, archive.path_for(obj, "t"))
        check.equal(stored.price if stored else None, Decimal("78.5"), "kept")
        check.equal(archive.quarantined(), [], "not a failure")

    ####################################################################
    #
    def test_a_clean_run_reports_no_drift(
        self, archive: Archive, hosting_payload: dict[str, Any]
    ) -> None:
        """
        GIVEN: payloads matching the models exactly
        WHEN:  they are ingested
        THEN:  nothing is reported, so a report means something changed
        """
        archive.ingest(Hosting, hosting_payload, trip_key="t")

        check.equal(dict(archive.unknown_fields), {}, "no drift")
        check.equal(archive.quarantined(), [], "no quarantine")
