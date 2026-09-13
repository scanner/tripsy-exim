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
from tripsy_exim.sync.enrich import (
    DIVERGENCE_KM,
    Enrichment,
    Match,
    enrich,
)
from tripsy_exim.sync.importer import (
    COLLECTION_ORDER,
    UPLOADED,
    PlannedObject,
    TripImport,
    TripPlan,
    child_ids_by_identifier,
    in_travel_order,
    mark_uploaded,
    numbered,
    plan_trip,
    resolve_trip_key,
    staged_children,
    staged_trip,
    upload_trip,
    uploaded_trips,
)
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
    TRIP_INDEX,
    StagedTrip,
    TripAlreadyArchived,
    archived_trips,
    stage,
    stage_export,
    stage_export_file,
    stage_file,
)

__all__ = [
    "COLLECTION_ORDER",
    "UPLOADED",
    "OVERRIDES_DIRNAME",
    "REPORT_FILENAME",
    "TRIP_INDEX",
    "DIVERGENCE_KM",
    "Applied",
    "Enrichment",
    "Match",
    "Override",
    "OverrideSet",
    "PlannedObject",
    "StagedTrip",
    "TripImport",
    "TripPlan",
    "TripAlreadyArchived",
    "archived_trips",
    "apply_overrides",
    "child_ids_by_identifier",
    "enrich",
    "in_travel_order",
    "mark_uploaded",
    "numbered",
    "plan_trip",
    "resolve_trip_key",
    "upload_trip",
    "uploaded_trips",
    "load_overrides",
    "overrides_path",
    "retyped",
    "save_overrides",
    "stage",
    "staged_children",
    "staged_trip",
    "stage_export",
    "stage_export_file",
    "stage_file",
]
