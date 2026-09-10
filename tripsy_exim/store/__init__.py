#!/usr/bin/env python
#
"""
The local archive: canonical trips on disk, plus the sync manifest.

Writes merge on set fields rather than replacing whole objects, because any
Tripsy response can be partial -- `price` and `currency` are withheld
without expense permission, and `fields=` responses are partial by
construction.  A blind overwrite would let one restricted export erase data
an earlier one captured.

A payload that will not parse at all is kept verbatim under `quarantine/`
rather than dropped, so a change to Tripsy's own schema costs a run its
typed access to that object and nothing more.
"""

# 3rd party imports
from tripsy_exim.store.archive import (
    ARCHIVE_SCHEMA_VERSION,
    COLLECTIONS,
    Archive,
    local_key,
    quarantine_key,
)

__all__ = [
    "ARCHIVE_SCHEMA_VERSION",
    "COLLECTIONS",
    "Archive",
    "local_key",
    "quarantine_key",
]
