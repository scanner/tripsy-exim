#!/usr/bin/env python
#
"""
Small objects that appear nested inside the canonical models.

`owner` is not one shape across the API: trips and expenses return a bare
user id, while hostings, activities, and transportations return an object.
Both forms are kept as the endpoint returns them rather than normalised
into one, so the archive records what was actually received.
"""

# 3rd party imports
from tripsy_exim.models.base import CanonicalModel


########################################################################
########################################################################
#
class ObjectOwner(CanonicalModel):
    """The nested owner on a hosting, activity, or transportation."""

    id: int | None = None
    name: str | None = None
    email: str | None = None
    photo_url: str | None = None


########################################################################
########################################################################
#
class CollaboratorPermissions(CanonicalModel):
    """What a collaborator is allowed to do on a trip."""

    is_owner: bool | None = None
    can_edit: bool | None = None
    can_see_expenses: bool | None = None
    can_edit_expenses: bool | None = None
    is_travelling: bool | None = None
    receive_notifications: bool | None = None
