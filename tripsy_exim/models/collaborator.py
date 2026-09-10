#!/usr/bin/env python
#
"""
The canonical collaborator.

The API exposes no way to create or modify one, so `WRITABLE` stays empty
and the model exists to be read and archived.
"""

# 3rd party imports
from tripsy_exim.models.base import CanonicalModel
from tripsy_exim.models.common import CollaboratorPermissions


########################################################################
########################################################################
#
class Collaborator(CanonicalModel):
    """Someone with access to a trip, or invited to one."""

    id: int | None = None
    name: str | None = None
    email: str | None = None
    photo_url: str | None = None
    permissions: CollaboratorPermissions | None = None

    # False means the invitation is still pending.
    #
    joined: bool | None = None
