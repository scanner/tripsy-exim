#!/usr/bin/env python
#
"""The canonical hosting -- a stay at one place within a trip."""

# system imports
from typing import Any, ClassVar

# 3rd party imports
from tripsy_exim.models.base import CanonicalModel, Money, UtcDatetime
from tripsy_exim.models.common import ObjectOwner


########################################################################
########################################################################
#
class Hosting(CanonicalModel):
    """Lodging attached to a trip."""

    WRITABLE: ClassVar[frozenset[str]] = frozenset(
        {
            "internal_identifier",
            "hidden",
            "starts_at",
            "ends_at",
            "timezone",
            "name",
            "description",
            "address",
            "longitude",
            "latitude",
            "phone",
            "apple_maps_id",
            "room_type",
            "room_number",
            "website",
            "notes",
            "provider_location_url",
            "provider_reservation_code",
            "provider_reservation_description",
            "google_places_id",
            "price",
            "currency",
        }
    )

    id: int | None = None
    internal_identifier: str | None = None
    trip: int | None = None
    hidden: bool | None = None
    starts_at: UtcDatetime | None = None
    ends_at: UtcDatetime | None = None
    timezone: str | None = None
    name: str | None = None
    description: str | None = None
    address: str | None = None
    longitude: float | None = None
    latitude: float | None = None
    phone: str | None = None
    apple_maps_id: str | None = None
    room_type: str | None = None
    room_number: str | None = None
    website: str | None = None
    notes: str | None = None
    provider_location_url: str | None = None
    provider_reservation_code: str | None = None
    provider_reservation_description: str | None = None
    google_places_id: str | None = None
    price: Money | None = None
    currency: str | None = None

    # Read-only.  `price` and `currency` above are withheld entirely when
    # the caller cannot see expenses, which is what merge-on-set protects.
    #
    owner: ObjectOwner | None = None
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None
    emails: list[dict[str, Any]] | None = None
