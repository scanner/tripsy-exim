#!/usr/bin/env python
#
"""
Stage parsed trips into the local archive.

The first half of the import: source -> parse -> archive.  Nothing here
touches the network.  The archive is the staging area, so a trip exists as
canonical objects on disk -- and can be reviewed and corrected -- before
Tripsy is told anything about it.

Two sources reach it.  A `.ics` file is one trip, so `stage_file` returns
one result; a TripIt GDPR export is a whole account, so `stage_export`
returns one per trip.  Both land in the same archive under the same keys,
and nothing downstream needs to know which produced a trip.

The parser's report is written beside the trip as `report.json` rather
than left to be recovered by re-parsing.  It carries the records that
matched no rule and the zones that were guessed, which is what the
review has to act on, and keeping it here means the archive describes
itself without the source file still being around.
"""

# system imports
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tripsy_exim.models import is_scratch
from tripsy_exim.sources import (
    ACTIVITY,
    HOSTING,
    TRANSPORTATION,
    EventNote,
    ParsedCalendar,
    load,
    parse,
    parse_export,
    uuid_from_uid,
)

# Project imports
from tripsy_exim.sources.join import SEPARATOR
from tripsy_exim.store import Archive, local_key, write_json

REPORT_FILENAME = "report.json"

# Where the manifest records which trip a join key resolved to, so a
# calendar staged later can tell that the archive already holds its trip.
#
TRIP_INDEX = "trip_index"

# The parser reports a kind; the archive stores a collection.
#
COLLECTION_FOR_KIND: dict[str, str] = {
    HOSTING: "hostings",
    ACTIVITY: "activities",
    TRANSPORTATION: "transportations",
}


########################################################################
########################################################################
#
class TripAlreadyArchived(Exception):
    """
    Raised rather than staging a calendar for a trip already held.

    The GDPR export is the authority for identity.  A calendar keys onto
    the same real trip but mints a different identifier for it, so
    staging both would put two trips into Tripsy -- and Tripsy never
    releases an identifier, so that pair could be neither merged nor
    undone.  Refusing is the only reversible answer.

    What the calendar *does* carry that the export does not -- notably
    coordinates -- is enrichment, and applying it is not built yet.
    """

    ####################################################################
    #
    def __init__(self, join_key: str, trip_keys: list[str]) -> None:
        self.join_key = join_key
        self.trip_keys = trip_keys
        name = join_key.split(SEPARATOR)[0]
        if len(trip_keys) > 1:
            super().__init__(
                f"{name!r} matches {len(trip_keys)} archived trips, so "
                "which one this calendar belongs to cannot be decided "
                "here; resolve it by hand"
            )
        else:
            super().__init__(
                f"{name!r} is already archived as {trip_keys[0]}; the "
                "export is authoritative for it, and staging this "
                "calendar would create a second trip that cannot be "
                "merged or undone"
            )


########################################################################
########################################################################
#
@dataclass(frozen=True)
class StagedTrip:
    """What one calendar put on disk."""

    trip_key: str
    trip_path: Path
    report_path: Path
    counts: dict[str, int] = field(default_factory=dict)

    ####################################################################
    #
    @property
    def total(self) -> int:
        """How many child objects were written."""
        return sum(self.counts.values())


####################################################################
#
def stage(archive: Archive, parsed: ParsedCalendar) -> StagedTrip:
    """
    Write one parsed calendar into the archive.

    The trip key is derived once and reused for every child, so the whole
    calendar lands under one directory even if an object is unkeyable.

    Args:
        archive: The archive to write into.
        parsed: A calendar as the parser produced it.

    Returns:
        The paths written and a count per collection.
    """
    trip_key = local_key(parsed.trip)
    trip_path = archive.write(parsed.trip)

    counts: dict[str, int] = {}
    for name, objects in (
        ("hostings", parsed.hostings),
        ("activities", parsed.activities),
        ("transportations", parsed.transportations),
    ):
        for obj in objects:
            archive.write(obj, trip_key)
        counts[name] = len(objects)

    _record_join_key(archive, parsed, trip_key)
    report_path = _write_report(archive, trip_key, parsed)
    return StagedTrip(
        trip_key=trip_key,
        trip_path=trip_path,
        report_path=report_path,
        counts=counts,
    )


####################################################################
#
def stage_file(
    archive: Archive, path: Path, namespace: str | None = None
) -> StagedTrip:
    """
    Parse one `.ics` file and stage it.

    A calendar naming a trip the archive already holds is refused rather
    than staged.  The export is authoritative for identity, so a second
    copy of one trip is a duplicate that Tripsy could never release.

    Args:
        archive: The archive to write into.
        path: The `.ics` file to read.
        namespace: Identifier namespace to mint into, for a shaping run
            that must not spend the real identifiers.  The parser's own
            default is used when this is None.

    Returns:
        The paths written and a count per collection.

    Raises:
        ValueError: The file is not a parseable calendar.
        TripAlreadyArchived: The archive already holds this trip.
    """
    text = path.read_text()
    parsed = parse(text) if namespace is None else parse(text, namespace)

    # A shaping run is exempt.  Its whole purpose is to stage the same
    # trip again under a key space that will be thrown away, and nothing
    # it mints ever reaches the real account.
    #
    if not is_scratch(parsed.trip.internal_identifier):
        existing = archived_trips(archive, parsed.join_key)
        if existing:
            assert parsed.join_key is not None
            raise TripAlreadyArchived(parsed.join_key, existing)

    return stage(archive, parsed)


####################################################################
#
def archived_trips(archive: Archive, join_key: str | None) -> list[str]:
    """
    Which archived trips this key resolves to.

    Args:
        archive: The archive to look in.
        join_key: A parsed trip's key, or None when it has none.

    Returns:
        The trip keys it matches -- empty when the archive holds no such
        trip, and more than one where a journey was planned twice under
        the same name and dates.  Throwaway trips never count: they are a
        separate key space by construction and block nothing.
    """
    if not join_key:
        return []
    index = archive.read_manifest().get(TRIP_INDEX) or {}
    found: list[str] = index.get(join_key) or []
    return [key for key in found if not is_scratch(key)]


####################################################################
#
def _record_join_key(
    archive: Archive, parsed: ParsedCalendar, trip_key: str
) -> None:
    """
    Note which trip this key resolved to, for a later calendar to find.

    Keys are held as lists rather than single values: two real trips can
    share a name and dates, and flattening that to one would silently
    lose a trip instead of reporting an ambiguity nobody can resolve
    automatically.
    """
    if not parsed.join_key:
        return

    manifest = archive.read_manifest()
    index = dict(manifest.get(TRIP_INDEX) or {})
    keys = list(index.get(parsed.join_key) or [])
    if trip_key not in keys:
        keys.append(trip_key)
    index[parsed.join_key] = keys
    manifest[TRIP_INDEX] = index
    archive.write_manifest(manifest)


####################################################################
#
def stage_export(
    archive: Archive,
    document: dict[str, Any],
    namespace: str | None = None,
) -> list[StagedTrip]:
    """
    Stage every trip in a TripIt GDPR export.

    An export holds a whole account, so this returns one `StagedTrip` per
    trip where `stage_file` returns one per calendar.  Each is written
    independently: the archive is keyed per trip, so a document carrying
    a trip the reader cannot key still stages the rest.

    Args:
        archive: The archive to write into.
        document: A decoded export.  The reader repairs its text, so it
            does not matter how the document was read.
        namespace: Identifier namespace to mint into, for a shaping run
            that must not spend the real identifiers.  The reader's own
            default is used when this is None.

    Returns:
        What was written, in the order the export lists its trips.
    """
    parsed = (
        parse_export(document)
        if namespace is None
        else parse_export(document, namespace)
    )
    return [stage(archive, trip) for trip in parsed]


####################################################################
#
def stage_export_file(
    archive: Archive, path: Path, namespace: str | None = None
) -> list[StagedTrip]:
    """
    Read one TripIt GDPR export and stage every trip in it.

    Args:
        archive: The archive to write into.
        path: The export to read.
        namespace: Identifier namespace to mint into, or None for the
            reader's default.

    Returns:
        What was written, one entry per trip.

    Raises:
        ValueError: The file is not a parseable export.
    """
    return stage_export(archive, load(path.read_bytes()), namespace)


####################################################################
#
def _write_report(
    archive: Archive, trip_key: str, parsed: ParsedCalendar
) -> Path:
    """Record what the parser could not decide, beside the trip."""
    document: dict[str, Any] = {
        "trip_key": trip_key,
        "counts": {
            "hostings": len(parsed.hostings),
            "activities": len(parsed.activities),
            "transportations": len(parsed.transportations),
        },
        "unclassified": [_note(n) for n in parsed.unclassified],
        "guessed_timezones": [_note(n) for n in parsed.guessed_timezones],
        "index": _index(parsed),
    }
    path = archive.trip_dir(trip_key) / REPORT_FILENAME
    write_json(path, document)
    return path


####################################################################
#
def _index(parsed: ParsedCalendar) -> dict[str, dict[str, str]]:
    """
    Map each source uuid to the object it produced.

    Corrections are keyed by uuid because that is the one identity that
    survives both a re-export and a change of identifier namespace -- a
    shaping run and the real run mint different identifiers from the same
    uuid.  Resolving a correction back to an object needs this map, and
    the collection belongs in it because a retype is what changes it.
    """
    index: dict[str, dict[str, str]] = {}
    for note in parsed.notes:
        token = uuid_from_uid(note.uid)
        if token is None:
            continue
        index[token] = {
            "identifier": note.identifier,
            "collection": COLLECTION_FOR_KIND[note.kind],
        }
    return index


####################################################################
#
def _note(note: EventNote) -> dict[str, Any]:
    """One parser note, as it is stored."""
    return {
        "uid": note.uid,
        "identifier": note.identifier,
        "kind": note.kind,
        "summary": note.summary,
        "reason": note.reason,
        "timezone": note.timezone,
        "timezone_source": note.timezone_source,
    }
