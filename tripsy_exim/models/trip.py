#!/usr/bin/env python
#
"""
The canonical trip.

`starts_at` and `ends_at` are plain dates here and UTC datetimes on every
child object -- the API uses both and they are not interchangeable.

Trips carry no `created_at` or `updated_at` in either API version, which is
why the exporter's watermark is the run's wall-clock time rather than
anything read out of the data.
"""

# system imports
from datetime import date
from typing import Any, ClassVar

# 3rd party imports
from tripsy_exim.models.base import CanonicalModel


########################################################################
########################################################################
#
class Trip(CanonicalModel):
    """A trip, the root of everything else in the archive."""

    WRITABLE: ClassVar[frozenset[str]] = frozenset(
        {
            "internal_identifier",
            "name",
            "timezone",
            "hidden",
            "description",
            "starts_at",
            "ends_at",
            "cover_gradient",
            "cover_image_url",
            "has_dates",
            "number_of_days",
        }
    )

    id: int | None = None
    internal_identifier: str | None = None
    name: str | None = None
    timezone: str | None = None
    hidden: bool | None = None
    description: str | None = None
    starts_at: date | None = None
    ends_at: date | None = None
    cover_gradient: int | None = None
    cover_image_url: str | None = None
    has_dates: bool | None = None
    number_of_days: int | None = None

    # Read-only. v1 calls the count `collaborators_count`, v2 calls it
    # `collaborators`; neither is the collaborator list, which comes from
    # its own endpoint.
    #
    owner: int | None = None
    collaborators: int | None = None
    collaborators_count: int | None = None
    emails: list[dict[str, Any]] | None = None

    # NOTE: real payloads also carry `documents` and `guests`, whose
    # contents have not been inspected.  They are left undeclared so they
    # land in the passthrough container with their structure intact.
