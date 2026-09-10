#!/usr/bin/env python
#
"""Test the canonical models: partial data, passthrough, and money."""

# system imports
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

# 3rd party imports
import pytest
from pydantic import ValidationError

# Project imports
from tripsy_exim.models import (
    SOURCE_KEY,
    Activity,
    Collaborator,
    Expense,
    Hosting,
    Transportation,
    Trip,
)

# Every value here is invented.  No real trip, address, confirmation code,
# or name is allowed into the test suite.
#
HOSTING_PAYLOAD: dict[str, Any] = {
    "id": 101,
    "internal_identifier": "txim-ics-0123456789abcdef",
    "trip": 42,
    "name": "Example Lodging",
    "starts_at": "2027-06-01T14:00:00Z",
    "ends_at": "2027-06-05T11:00:00Z",
    "timezone": "Europe/Rome",
    "price": 78.5,
    "currency": "EUR",
    "created_at": "2027-03-17T14:30:00Z",
    "updated_at": "2027-03-17T14:30:00Z",
}


########################################################################
########################################################################
#
class TestPartialResponses:
    """Tests that unset is distinguishable from null."""

    ####################################################################
    #
    def test_unset_fields_are_not_dumped(self) -> None:
        """
        GIVEN: a response that omitted price and currency
        WHEN:  the model is dumped with exclude_unset
        THEN:  the omitted fields are absent rather than null
        """
        payload = {k: v for k, v in HOSTING_PAYLOAD.items() if k != "price"}
        hosting = Hosting.model_validate(payload)

        dumped = hosting.model_dump(exclude_unset=True)

        assert "price" not in dumped
        assert "price" not in hosting.model_fields_set
        assert hosting.price is None

    ####################################################################
    #
    def test_explicit_null_is_set(self) -> None:
        """
        GIVEN: a response that sent price as null
        WHEN:  the model is dumped with exclude_unset
        THEN:  price is present and null, because null is a value
        """
        hosting = Hosting.model_validate({**HOSTING_PAYLOAD, "price": None})

        dumped = hosting.model_dump(exclude_unset=True)

        assert "price" in dumped
        assert dumped["price"] is None

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
        self, incoming: dict[str, Any], expected: Decimal | None
    ) -> None:
        """
        GIVEN: a stored hosting with a price
        WHEN:  a newer hosting that omits, nulls, or replaces it is merged
        THEN:  only an explicitly set field overwrites the stored value
        """
        base = Hosting.model_validate(HOSTING_PAYLOAD)
        newer = Hosting.model_validate(
            {"id": 101, "name": "Renamed Lodging", **incoming}
        )

        merged = base.merged_with(newer)

        assert merged.price == expected
        assert merged.name == "Renamed Lodging"
        assert merged.currency == "EUR"

    ####################################################################
    #
    def test_merge_rejects_a_different_model(self) -> None:
        """
        GIVEN: a hosting and an activity
        WHEN:  the activity is merged into the hosting
        THEN:  a TypeError is raised rather than fields being mixed
        """
        hosting = Hosting.model_validate(HOSTING_PAYLOAD)

        with pytest.raises(TypeError):
            hosting.merged_with(Activity(id=1))  # type: ignore[arg-type]


########################################################################
########################################################################
#
class TestPassthrough:
    """Tests that unknown and source-only fields survive."""

    ####################################################################
    #
    def test_undocumented_fields_are_retained(self) -> None:
        """
        GIVEN: a payload carrying fields the docs do not list
        WHEN:  it is validated
        THEN:  the fields are kept rather than rejected
        """
        hosting = Hosting.model_validate(
            {**HOSTING_PAYLOAD, "sort_order": 3, "custom_icon": "bed"}
        )

        assert hosting.wire_extras == {"sort_order": 3, "custom_icon": "bed"}
        assert hosting.model_dump(exclude_unset=True)["sort_order"] == 3

    ####################################################################
    #
    def test_source_fields_stay_separable_from_wire_fields(self) -> None:
        """
        GIVEN: a hosting carrying both an undocumented Tripsy field and a
               source field Tripsy has nowhere to put
        WHEN:  each passthrough view is read
        THEN:  the two are distinguishable
        """
        hosting = Hosting.model_validate(
            {**HOSTING_PAYLOAD, "sort_order": 3}
        ).with_source(tripit_segment_id="abc-123")

        assert hosting.source_extras == {"tripit_segment_id": "abc-123"}
        assert hosting.wire_extras == {"sort_order": 3}

    ####################################################################
    #
    def test_source_fields_never_reach_a_write_payload(self) -> None:
        """
        GIVEN: a hosting carrying source-only and undocumented fields
        WHEN:  a write payload is built
        THEN:  neither appears, and read-only fields are dropped too
        """
        hosting = Hosting.model_validate(
            {**HOSTING_PAYLOAD, "sort_order": 3}
        ).with_source(tripit_segment_id="abc-123")

        payload = hosting.writable_payload()

        assert SOURCE_KEY not in payload
        assert "sort_order" not in payload
        for read_only in ("id", "trip", "created_at", "updated_at"):
            assert read_only not in payload

    ####################################################################
    #
    def test_source_fields_accumulate_across_merges(self) -> None:
        """
        GIVEN: two versions of a hosting carrying different source fields
        WHEN:  they are merged
        THEN:  both source fields survive
        """
        base = Hosting.model_validate(HOSTING_PAYLOAD).with_source(a="1")
        newer = Hosting.model_validate(HOSTING_PAYLOAD).with_source(b="2")

        merged = base.merged_with(newer)

        assert merged.source_extras == {"a": "1", "b": "2"}


########################################################################
########################################################################
#
class TestWritablePayload:
    """Tests the projection from model to API request body."""

    ####################################################################
    #
    def test_price_is_a_number_not_a_string(self) -> None:
        """
        GIVEN: a hosting whose price is a Decimal
        WHEN:  a write payload is built
        THEN:  price is a JSON number, since the API rejects a string
        """
        payload = Hosting.model_validate(HOSTING_PAYLOAD).writable_payload()

        assert payload["price"] == 78.5
        assert isinstance(payload["price"], float)

    ####################################################################
    #
    def test_datetimes_use_the_documented_format(self) -> None:
        """
        GIVEN: a hosting with UTC datetimes
        WHEN:  a write payload is built
        THEN:  they are rendered as YYYY-MM-DDTHH:MM:SSZ
        """
        payload = Hosting.model_validate(HOSTING_PAYLOAD).writable_payload()

        assert payload["starts_at"] == "2027-06-01T14:00:00Z"

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
        THEN:  update_trip is absent, since sending it moves the object
        """
        assert "update_trip" not in model.WRITABLE

    ####################################################################
    #
    def test_a_partial_model_yields_a_partial_payload(self) -> None:
        """
        GIVEN: a trip with only a name set
        WHEN:  a write payload is built for a PATCH
        THEN:  it contains that field alone
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
    def test_trip_dates_are_plain_dates(self) -> None:
        """
        GIVEN: a trip payload with date-only bounds
        WHEN:  it is validated
        THEN:  the values are dates, not datetimes
        """
        trip = Trip.model_validate(
            {"id": 42, "starts_at": "2027-06-01", "ends_at": "2027-06-15"}
        )

        assert trip.starts_at == date(2027, 6, 1)
        assert not isinstance(trip.starts_at, datetime)

    ####################################################################
    #
    def test_trip_has_no_timestamps(self) -> None:
        """
        GIVEN: the trip model
        WHEN:  its declared fields are inspected
        THEN:  created_at and updated_at are absent, since the API has none
        """
        assert "created_at" not in Trip.model_fields
        assert "updated_at" not in Trip.model_fields

    ####################################################################
    #
    @pytest.mark.parametrize(
        "raw,expected",
        [
            pytest.param(78.5, Decimal("78.5"), id="float"),
            pytest.param(100.0, Decimal("100.0"), id="whole-float"),
            pytest.param("78.5", Decimal("78.5"), id="string"),
            pytest.param(0.1, Decimal("0.1"), id="binary-unfriendly"),
        ],
    )
    def test_price_parses_to_an_exact_decimal(
        self, raw: Any, expected: Decimal
    ) -> None:
        """
        GIVEN: a price as the API sends it
        WHEN:  the model parses it
        THEN:  it is a Decimal with no binary float residue
        """
        hosting = Hosting.model_validate({**HOSTING_PAYLOAD, "price": raw})

        assert hosting.price == expected

    ####################################################################
    #
    def test_naive_child_datetimes_are_rejected(self) -> None:
        """
        GIVEN: a hosting datetime with no timezone
        WHEN:  it is validated
        THEN:  it is rejected rather than silently assumed to be UTC
        """
        with pytest.raises(ValidationError):
            Hosting.model_validate(
                {**HOSTING_PAYLOAD, "starts_at": "2027-06-01T14:00:00"}
            )

    ####################################################################
    #
    def test_offset_datetimes_normalise_to_utc(self) -> None:
        """
        GIVEN: a hosting datetime with a non-UTC offset
        WHEN:  it is validated
        THEN:  it is converted to the same instant in UTC
        """
        hosting = Hosting.model_validate(
            {**HOSTING_PAYLOAD, "starts_at": "2027-06-01T16:00:00+02:00"}
        )

        assert hosting.starts_at == datetime(2027, 6, 1, 14, 0, tzinfo=UTC)

    ####################################################################
    #
    def test_transportation_keeps_both_local_timezones(self) -> None:
        """
        GIVEN: a leg between two timezones
        WHEN:  it is validated
        THEN:  the UTC instants and both local zones are retained
        """
        leg = Transportation.model_validate(
            {
                "id": 303,
                "departure_at": "2027-05-31T22:30:00Z",
                "departure_timezone": "America/New_York",
                "arrival_at": "2027-06-01T10:30:00Z",
                "arrival_timezone": "Europe/Rome",
            }
        )

        assert leg.departure_timezone == "America/New_York"
        assert leg.arrival_timezone == "Europe/Rome"
        assert leg.departure_at == datetime(2027, 5, 31, 22, 30, tzinfo=UTC)

    ####################################################################
    #
    def test_owner_is_an_object_on_children_and_an_id_on_trips(self) -> None:
        """
        GIVEN: the two shapes the API uses for owner
        WHEN:  each is validated by its own model
        THEN:  both parse, because they were not forced into one type
        """
        trip = Trip.model_validate({"id": 42, "owner": 1})
        hosting = Hosting.model_validate(
            {
                **HOSTING_PAYLOAD,
                "owner": {
                    "id": 1,
                    "name": "Example Person",
                    "email": "person@example.invalid",
                    "photo_url": None,
                },
            }
        )

        assert trip.owner == 1
        assert hosting.owner is not None
        assert hosting.owner.id == 1

    ####################################################################
    #
    def test_collaborator_permissions_nest(self) -> None:
        """
        GIVEN: a collaborator payload
        WHEN:  it is validated
        THEN:  the permissions block is a model, not a bare dict
        """
        collaborator = Collaborator.model_validate(
            {
                "id": 2,
                "name": "Example Person",
                "email": "person@example.invalid",
                "permissions": {"is_owner": False, "can_edit": True},
                "joined": True,
            }
        )

        assert collaborator.permissions is not None
        assert collaborator.permissions.can_edit is True
        assert collaborator.permissions.is_owner is False
