#!/usr/bin/env python
#
"""Test the canonical models: partial data, passthrough, and money."""

# system imports
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check
from pydantic import ValidationError

# Project imports
from tests.factories import SKIP
from tripsy_exim.models import (
    SOURCE_KEY,
    Activity,
    Collaborator,
    Expense,
    Hosting,
    Transportation,
    Trip,
)


########################################################################
########################################################################
#
class TestPartialResponses:
    """Tests that unset is distinguishable from null."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "hosting_payload__price,is_set,expected",
        [
            pytest.param(SKIP, False, None, id="omitted-stays-unset"),
            pytest.param(None, True, None, id="null-is-a-value"),
            pytest.param(78.5, True, Decimal("78.5"), id="value-is-set"),
        ],
    )
    def test_unset_is_distinguishable_from_null(
        self,
        hosting_payload: dict[str, Any],
        is_set: bool,
        expected: Decimal | None,
    ) -> None:
        """
        GIVEN: a response that omits price, nulls it, or gives a value
        WHEN:  the model is dumped with exclude_unset
        THEN:  only the omitted case is absent, and null survives as null
        """
        hosting = Hosting.model_validate(hosting_payload)

        dumped = hosting.model_dump(exclude_unset=True)

        check.equal("price" in dumped, is_set, "presence in dump")
        check.equal("price" in hosting.model_fields_set, is_set, "fields_set")
        check.equal(hosting.price, expected, "parsed value")

    ####################################################################
    #
    @pytest.mark.parametrize(
        "incoming,expected",
        [
            pytest.param({}, Decimal("78.5"), id="unset-keeps-base"),
            pytest.param({"price": None}, None, id="null-overwrites-base"),
            pytest.param(
                {"price": 12.25}, Decimal("12.25"), id="value-overwrites-base"
            ),
        ],
    )
    def test_merge_respects_set_fields(
        self,
        hosting: Hosting,
        incoming: dict[str, Any],
        expected: Decimal | None,
    ) -> None:
        """
        GIVEN: a stored hosting with a price
        WHEN:  a newer hosting that omits, nulls, or replaces it is merged
        THEN:  only an explicitly set field overwrites the stored value
        """
        newer = Hosting.model_validate(
            {"id": hosting.id, "name": "Renamed Lodging", **incoming}
        )

        merged = hosting.merged_with(newer)

        check.equal(merged.price, expected, "merged price")
        check.equal(merged.name, "Renamed Lodging", "newer field applied")
        check.equal(merged.currency, "EUR", "untouched field survives")

    ####################################################################
    #
    def test_merge_rejects_a_different_model(self, hosting: Hosting) -> None:
        """
        GIVEN: a hosting and an activity
        WHEN:  the activity is merged into the hosting
        THEN:  a TypeError is raised rather than fields being mixed
        """
        with pytest.raises(TypeError):
            hosting.merged_with(Activity(id=1))  # type: ignore[arg-type]


########################################################################
########################################################################
#
class TestPassthrough:
    """Tests that unknown and source-only fields survive and stay apart."""

    ####################################################################
    #
    def test_wire_and_source_extras_stay_separable(
        self, hosting_payload_factory: Callable[..., dict[str, Any]]
    ) -> None:
        """
        GIVEN: a hosting carrying both undocumented Tripsy fields and a
               source field Tripsy has nowhere to put
        WHEN:  its passthrough views are read
        THEN:  both are retained and the two remain distinguishable
        """
        hosting = Hosting.model_validate(
            hosting_payload_factory(sort_order=3, custom_icon="bed")
        ).with_source(tripit_segment_id="abc-123")

        check.equal(
            hosting.wire_extras,
            {"sort_order": 3, "custom_icon": "bed"},
            "undocumented Tripsy fields",
        )
        check.equal(
            hosting.source_extras,
            {"tripit_segment_id": "abc-123"},
            "source-only fields",
        )
        check.equal(
            hosting.model_dump(exclude_unset=True).get("sort_order"),
            3,
            "extras take part in a dump",
        )

    ####################################################################
    #
    def test_source_fields_accumulate_across_merges(
        self, hosting: Hosting
    ) -> None:
        """
        GIVEN: two versions of a hosting carrying different source fields
        WHEN:  they are merged
        THEN:  both survive, so a later import knowing fewer drops none
        """
        merged = hosting.with_source(a="1").merged_with(
            hosting.with_source(b="2")
        )

        assert merged.source_extras == {"a": "1", "b": "2"}


########################################################################
########################################################################
#
class TestWritablePayload:
    """Tests the projection from model to API request body."""

    ####################################################################
    #
    def test_payload_carries_only_writable_fields(
        self, hosting_payload_factory: Callable[..., dict[str, Any]]
    ) -> None:
        """
        GIVEN: a hosting carrying read-only, undocumented, and source fields
        WHEN:  a write payload is built
        THEN:  only writable fields survive, money is a number, and
               datetimes use the format the API documents
        """
        hosting = Hosting.model_validate(
            hosting_payload_factory(sort_order=3)
        ).with_source(tripit_segment_id="abc-123")

        payload = hosting.writable_payload()

        check.equal(payload["price"], 78.5, "money value")
        check.is_instance(payload["price"], float, "API rejects a string")
        check.equal(payload["starts_at"], "2027-06-01T14:00:00Z", "datetime")
        check.is_not_in(SOURCE_KEY, payload, "source fields withheld")
        check.is_not_in("sort_order", payload, "wire extras withheld")
        for read_only in ("id", "trip", "created_at", "updated_at"):
            check.is_not_in(read_only, payload, f"read-only {read_only}")

    ####################################################################
    #
    @pytest.mark.parametrize(
        "model",
        [Trip, Hosting, Activity, Transportation, Expense, Collaborator],
    )
    def test_update_trip_is_never_writable(self, model: type[Any]) -> None:
        """
        GIVEN: any canonical model
        WHEN:  its writable field set is inspected
        THEN:  update_trip is absent, since sending it moves the object to
               a different trip instead of updating this one
        """
        assert "update_trip" not in model.WRITABLE

    ####################################################################
    #
    def test_a_partial_model_yields_a_partial_payload(self) -> None:
        """
        GIVEN: a trip with only a name set
        WHEN:  a write payload is built for a PATCH
        THEN:  it contains that field alone, touching nothing else
        """
        payload = Trip(name="Example Trip").writable_payload()

        assert payload == {"name": "Example Trip"}


########################################################################
########################################################################
#
class TestFieldTypes:
    """Tests the type choices the API forced on us."""

    ####################################################################
    #
    def test_trip_dates_are_plain_and_trips_are_untimestamped(
        self, trip_payload: dict[str, Any]
    ) -> None:
        """
        GIVEN: the trip model and a payload with date-only bounds
        WHEN:  both are inspected
        THEN:  bounds are dates, and no timestamp field exists -- which is
               why the exporter's watermark is wall-clock time
        """
        parsed = Trip.model_validate(trip_payload)

        check.equal(parsed.starts_at, date(2027, 6, 1), "parsed as a date")
        check.is_not_instance(parsed.starts_at, datetime, "not a datetime")
        check.is_not_in("created_at", Trip.model_fields, "no created_at")
        check.is_not_in("updated_at", Trip.model_fields, "no updated_at")

    ####################################################################
    #
    @pytest.mark.parametrize(
        "hosting_payload__price,expected",
        [
            pytest.param(78.5, Decimal("78.5"), id="float"),
            pytest.param(100.0, Decimal("100"), id="whole-float"),
            pytest.param("78.5", Decimal("78.5"), id="string"),
            pytest.param(0.1, Decimal("0.1"), id="binary-unfriendly"),
        ],
    )
    def test_price_parses_to_an_exact_decimal(
        self, hosting_payload: dict[str, Any], expected: Decimal
    ) -> None:
        """
        GIVEN: a price as the API sends it
        WHEN:  the model parses it
        THEN:  it is a Decimal with no binary float residue
        """
        hosting = Hosting.model_validate(hosting_payload)

        assert hosting.price == expected

    ####################################################################
    #
    @pytest.mark.parametrize(
        "hosting_payload__starts_at",
        [
            pytest.param("2027-06-01T14:00:00Z", id="utc"),
            pytest.param("2027-06-01T16:00:00+02:00", id="positive-offset"),
            pytest.param("2027-06-01T09:00:00-05:00", id="negative-offset"),
        ],
    )
    def test_aware_datetimes_normalise_to_utc(
        self, hosting_payload: dict[str, Any]
    ) -> None:
        """
        GIVEN: a child datetime in any timezone
        WHEN:  it is validated
        THEN:  it becomes the same instant expressed in UTC
        """
        hosting = Hosting.model_validate(hosting_payload)

        assert hosting.starts_at == datetime(2027, 6, 1, 14, 0, tzinfo=UTC)

    ####################################################################
    #
    @pytest.mark.parametrize(
        "hosting_payload__starts_at",
        [pytest.param("2027-06-01T14:00:00", id="no-timezone")],
    )
    def test_naive_child_datetimes_are_rejected(
        self, hosting_payload: dict[str, Any]
    ) -> None:
        """
        GIVEN: a hosting datetime with no timezone
        WHEN:  it is validated
        THEN:  it is rejected rather than silently assumed to be UTC
        """
        with pytest.raises(ValidationError):
            Hosting.model_validate(hosting_payload)

    ####################################################################
    #
    def test_transportation_keeps_both_local_timezones(
        self, transportation_payload: dict[str, Any]
    ) -> None:
        """
        GIVEN: a leg between two timezones
        WHEN:  it is validated
        THEN:  the UTC instants and both local zones survive, since local
               wall-clock time is recoverable only from the pair
        """
        leg = Transportation.model_validate(transportation_payload)

        check.equal(leg.departure_timezone, "America/New_York", "departure tz")
        check.equal(leg.arrival_timezone, "Europe/Rome", "arrival tz")
        check.equal(
            leg.departure_at,
            datetime(2027, 5, 31, 22, 30, tzinfo=UTC),
            "departure instant",
        )

    ####################################################################
    #
    def test_owner_and_permissions_keep_their_own_shapes(
        self,
        trip_payload: dict[str, Any],
        hosting_payload_factory: Callable[..., dict[str, Any]],
        collaborator_payload: dict[str, Any],
    ) -> None:
        """
        GIVEN: the two shapes the API uses for owner, and a collaborator
        WHEN:  each is validated by its own model
        THEN:  all parse, because owner was not forced into one type
        """
        parsed_trip = Trip.model_validate({**trip_payload, "owner": 1})
        hosting = Hosting.model_validate(
            hosting_payload_factory(
                owner={
                    "id": 1,
                    "name": "Example Person",
                    "email": "person@example.invalid",
                    "photo_url": None,
                }
            )
        )
        collaborator = Collaborator.model_validate(collaborator_payload)

        owner = hosting.owner
        permissions = collaborator.permissions
        assert owner is not None and permissions is not None

        check.equal(parsed_trip.owner, 1, "trip owner is a bare id")
        check.equal(owner.id, 1, "child owner is an object")
        check.equal(owner.name, "Example Person", "child owner detail")
        check.is_false(permissions.is_owner, "nested permission")
        check.is_true(permissions.can_edit, "nested permission")
