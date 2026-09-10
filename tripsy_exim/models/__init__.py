#!/usr/bin/env python
#
"""
Canonical trip models, and the schema of the local archive.

These are provider-neutral: Tripsy is the first service they are mapped to,
not the shape they are derived from.  Fields a source supplies that Tripsy
has nowhere to put are retained rather than dropped, so an import through
this model is lossless even where Tripsy's schema is narrower.
"""

# 3rd party imports
from tripsy_exim.models.activity import Activity
from tripsy_exim.models.base import (
    MONEY_FIELDS,
    SOURCE_KEY,
    CanonicalModel,
    Money,
    UtcDatetime,
)
from tripsy_exim.models.collaborator import Collaborator
from tripsy_exim.models.common import CollaboratorPermissions, ObjectOwner
from tripsy_exim.models.expense import Expense
from tripsy_exim.models.hosting import Hosting
from tripsy_exim.models.identifiers import IDENTIFIER_PREFIX, is_minted, mint
from tripsy_exim.models.transportation import Transportation
from tripsy_exim.models.trip import Trip

__all__ = [
    "IDENTIFIER_PREFIX",
    "MONEY_FIELDS",
    "SOURCE_KEY",
    "Activity",
    "CanonicalModel",
    "Collaborator",
    "CollaboratorPermissions",
    "Expense",
    "Hosting",
    "Money",
    "ObjectOwner",
    "Transportation",
    "Trip",
    "UtcDatetime",
    "is_minted",
    "mint",
]
