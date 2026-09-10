#!/usr/bin/env python
#
"""The canonical activity -- something done at a time and place on a trip."""

# system imports
from typing import Any, ClassVar

# 3rd party imports
from tripsy_exim.models.base import CanonicalModel, Money, UtcDatetime
from tripsy_exim.models.common import ObjectOwner


########################################################################
########################################################################
#
class Activity(CanonicalModel):
    """An activity attached to a trip."""

    WRITABLE: ClassVar[frozenset[str]] = frozenset(
        {
            "internal_identifier",
            "hidden",
            "activity_type",
            "period",
            "starts_at",
            "ends_at",
            "all_day",
            "name",
            "description",
            "phone",
            "website",
            "checked",
            "address",
            "longitude",
            "latitude",
            "notes",
            "apple_maps_id",
            "timezone",
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

    # NOTE: the API documents no value set for `activity_type` or `period`,
    # and only 'sightseeing' has been observed.  Keeping them free strings
    # means an unanticipated value imports rather than failing validation.
    #
    activity_type: str | None = None
    period: str | None = None

    starts_at: UtcDatetime | None = None
    ends_at: UtcDatetime | None = None
    all_day: bool | None = None
    name: str | None = None
    description: str | None = None
    phone: str | None = None
    website: str | None = None
    checked: bool | None = None
    address: str | None = None
    longitude: float | None = None
    latitude: float | None = None
    notes: str | None = None
    apple_maps_id: str | None = None
    timezone: str | None = None
    provider_location_url: str | None = None
    provider_reservation_code: str | None = None
    provider_reservation_description: str | None = None
    google_places_id: str | None = None
    price: Money | None = None
    currency: str | None = None

    # Read-only.
    #
    owner: ObjectOwner | None = None
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None
    emails: list[dict[str, Any]] | None = None
