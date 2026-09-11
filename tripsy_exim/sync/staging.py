#!/usr/bin/env python
#
"""
Stage parsed calendars into the local archive.

The first half of the import: `.ics` -> parse -> archive.  Nothing here
touches the network.  The archive is the staging area, so a trip exists as
canonical objects on disk -- and can be reviewed and corrected -- before
Tripsy is told anything about it.

The parser's report is written beside the trip as `report.json` rather
than left to be recovered by re-parsing.  It carries the events that
matched no rule and the zones that were inherited, which is what the
review has to act on, and keeping it here means the archive describes
itself without the source `.ics` still being around.
"""

# system imports
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Project imports
from tripsy_exim.sources import EventNote, ParsedCalendar, parse
from tripsy_exim.store import Archive, local_key, write_json

REPORT_FILENAME = "report.json"


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
    """
    text = path.read_text()
    parsed = parse(text) if namespace is None else parse(text, namespace)
    return stage(archive, parsed)


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
    }
    path = archive.trip_dir(trip_key) / REPORT_FILENAME
    write_json(path, document)
    return path


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
