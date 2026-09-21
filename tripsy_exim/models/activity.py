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
            "sort_order",
        }
    )

    id: int | None = None
    internal_identifier: str | None = None
    trip: int | None = None
    hidden: bool | None = None

    # NOTE: `activity_type` holds a category slug.  Tripsy's MCP server
    # documents 47 built-in ones -- 'general' for uncategorised, plus
    # 'restaurant', 'cafe', 'museum', 'winery' and the rest -- and the
    # API filters on them.  The REST documentation lists none of this,
    # which is why it once read here as having no value set at all.
    #
    # The set is not closed.  A person defines their own categories and
    # those slugs are equally valid in this field.  A custom slug is
    # opaque: its name, icon and colour come from the categories
    # endpoint, which answers only for categories the caller can see --
    # so a slug stored here can stop resolving once its owner stops
    # sharing the trip it came from.
    #
    # Read off one trip on 2026-09-21: 'general', 'restaurant' and a
    # custom slug, all three in this one field.  That is the reason it
    # is a free string, over and above surviving a value Tripsy adds
    # later.
    #
    # `period` is a different matter: no documented value set, and none
    # observed.
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

    # One dense sequence across a whole trip rather than per collection:
    # activities, hostings and transportations share it, and the app
    # renders a trip in it.  The API stores what it is given and computes
    # nothing, so an object created without one holds 0 -- verified
    # 2026-09-12.  The importer numbers a trip chronologically.
    #
    sort_order: int | None = None

    # Read-only.
    #
    owner: ObjectOwner | None = None
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None
    emails: list[dict[str, Any]] | None = None
