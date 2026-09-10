#!/usr/bin/env python
#
"""The canonical transportation -- a leg between two places."""

# system imports
from typing import Any, ClassVar

# 3rd party imports
from tripsy_exim.models.base import CanonicalModel, Money, UtcDatetime
from tripsy_exim.models.common import ObjectOwner


########################################################################
########################################################################
#
class Transportation(CanonicalModel):
    """A transportation leg attached to a trip."""

    WRITABLE: ClassVar[frozenset[str]] = frozenset(
        {
            "internal_identifier",
            "hidden",
            "name",
            "description",
            "notes",
            "transportation_type",
            "phone",
            "website",
            "departure_description",
            "departure_at",
            "departure_timezone",
            "departure_address",
            "departure_longitude",
            "departure_latitude",
            "arrival_description",
            "arrival_at",
            "arrival_timezone",
            "arrival_address",
            "arrival_longitude",
            "arrival_latitude",
            "company",
            "seat_number",
            "seat_class",
            "transport_number",
            "actual_transport_number",
            "coach_number",
            "vehicle_description",
            "departure_terminal",
            "departure_gate",
            "arrival_terminal",
            "arrival_gate",
            "arrival_bags",
            "flight_aware_identifier",
            "automatic_updates",
            "provider_url",
            "provider_reservation_code",
            "provider_reservation_description",
            "distance_meters",
            "price",
            "currency",
            "departure_apple_maps_id",
        }
    )

    id: int | None = None
    internal_identifier: str | None = None
    trip: int | None = None
    hidden: bool | None = None
    name: str | None = None
    description: str | None = None
    notes: str | None = None

    # NOTE: as with `activity_type`, the documented value set is empty and
    # only 'airplane' and the seat class 'economy' have been observed.
    #
    transportation_type: str | None = None
    seat_class: str | None = None

    phone: str | None = None
    website: str | None = None

    # Departure and arrival each carry their own timezone.  The instants
    # are UTC, so the local wall-clock time a traveller actually reads is
    # recoverable only in combination with these.
    #
    departure_description: str | None = None
    departure_at: UtcDatetime | None = None
    departure_timezone: str | None = None
    departure_address: str | None = None
    departure_longitude: float | None = None
    departure_latitude: float | None = None
    departure_terminal: str | None = None
    departure_gate: str | None = None
    departure_apple_maps_id: str | None = None

    arrival_description: str | None = None
    arrival_at: UtcDatetime | None = None
    arrival_timezone: str | None = None
    arrival_address: str | None = None
    arrival_longitude: float | None = None
    arrival_latitude: float | None = None
    arrival_terminal: str | None = None
    arrival_gate: str | None = None
    arrival_bags: int | None = None

    company: str | None = None
    seat_number: str | None = None
    transport_number: str | None = None
    actual_transport_number: str | None = None
    coach_number: str | None = None
    vehicle_description: str | None = None
    flight_aware_identifier: str | None = None
    automatic_updates: bool | None = None
    provider_url: str | None = None
    provider_reservation_code: str | None = None
    provider_reservation_description: str | None = None
    distance_meters: int | None = None
    price: Money | None = None
    currency: str | None = None

    # Read-only.  `arrival_apple_maps_id` is returned by v2 but is not in
    # the documented writable set, unlike its departure counterpart.
    #
    arrival_apple_maps_id: str | None = None
    owner: ObjectOwner | None = None
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None
    emails: list[dict[str, Any]] | None = None
