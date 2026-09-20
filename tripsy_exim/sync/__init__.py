#!/usr/bin/env python
#
"""
Trip-level orchestration: what to upload, what to download, and when.

Staging parses a source calendar into the archive, where a trip can be
reviewed and corrected before anything is posted.  The importer then writes
canonical trips to Tripsy idempotently.  All policy lives here, so `api`
stays a transport and `sources` stay parsers.

Pulling changes back into the archive incrementally is the half that is
not written yet, and it belongs here when it is.
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
    Discrepancy,
    PlannedObject,
    TripCheck,
    TripImport,
    TripPlan,
    Unplaced,
    child_ids_by_identifier,
    in_travel_order,
    mark_uploaded,
    numbered,
    plan_trip,
    positions_in,
    resolve_trip_key,
    staged_children,
    staged_trip,
    unplaced_in,
    upload_trip,
    uploaded_trips,
    verify_trip,
)
from tripsy_exim.sync.overrides import (
    OVERRIDES_DIRNAME,
    Addition,
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
    "Addition",
    "Applied",
    "Enrichment",
    "Match",
    "Override",
    "OverrideSet",
    "PlannedObject",
    "StagedTrip",
    "Discrepancy",
    "TripCheck",
    "TripImport",
    "TripPlan",
    "Unplaced",
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
    "positions_in",
    "unplaced_in",
    "verify_trip",
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
