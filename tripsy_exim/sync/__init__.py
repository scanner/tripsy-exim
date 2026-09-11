#!/usr/bin/env python
#
"""
Trip-level orchestration: what to upload, what to download, and when.

Staging parses a source calendar into the archive, where a trip can be
reviewed and corrected before anything is posted.  The importer then writes
canonical trips to Tripsy idempotently; the exporter pulls changes back into
the archive incrementally.  All policy lives here, so `api` stays a transport
and `sources` stay parsers.
"""

# 3rd party imports
from tripsy_exim.sync.overrides import (
    OVERRIDES_DIRNAME,
    Applied,
    Override,
    OverrideSet,
    apply_overrides,
    load_overrides,
    overrides_path,
    retyped,
    save_overrides,
)
from tripsy_exim.sync.staging import (
    REPORT_FILENAME,
    StagedTrip,
    stage,
    stage_file,
)

__all__ = [
    "OVERRIDES_DIRNAME",
    "REPORT_FILENAME",
    "Applied",
    "Override",
    "OverrideSet",
    "StagedTrip",
    "apply_overrides",
    "load_overrides",
    "overrides_path",
    "retyped",
    "save_overrides",
    "stage",
    "stage_file",
]
