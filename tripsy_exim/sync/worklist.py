#!/usr/bin/env python
#
"""
The editable work-list, and reading it back as corrections.

`backfill report` says what is open; this is how a person answers it.
`draft` writes one row per gap, `apply` reads the rows back and turns
them into overrides.  In between, the file is edited by hand -- which is
what every decision here is shaped by.

Rows carry real model field names rather than logical ones, so applying
a row is mechanical and the file says plainly what it will set.  Every
row arrives pre-filled with what the object holds now, and `apply`
writes only what actually differs, which makes a second run over the
same file a no-op instead of a rewrite.

Three conventions the file relies on, all of them about telling "not
answered" apart from "answered with nothing":

    an empty string      a blank left deliberately blank; skipped
    an unchanged value   already correct; skipped
    a key ending _note   commentary for a reader; never written

Values are validated before they are stored.  The correction path lays
fields over an object with `model_copy`, which does not validate, so a
latitude typed into JSON as "35.7" would otherwise reach Tripsy as the
string it looks like.  Each row is merged into its object and the result
validated, so what gets stored is the coerced value or nothing at all.
"""

# system imports
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Project imports
from tripsy_exim.store import Archive
from tripsy_exim.sync.backfill import (
    GUESSED_TIMEZONE,
    SKIPPED,
    UNCLASSIFIED,
    Gap,
    gaps,
    places,
)
from tripsy_exim.sync.importer import Child, composed_children, staged_trip
from tripsy_exim.sync.overrides import (
    MODEL_FOR_COLLECTION,
    OverrideSet,
    load_overrides,
    read_index,
    save_overrides,
)
from tripsy_exim.sync.staging import REPORT_FILENAME

# What a row of each population offers to fill.  Prefixed by the
# endpoint when the object keeps its places apart, which every kind does
# the same way: 'departure_address' and 'address' are the same field on
# two shapes of object.
#
_PLACE_FIELDS = ("address", "latitude", "longitude")
_TIMEZONE_FIELDS = ("timezone",)

# The file's own version, so a reader written against a later shape can
# refuse an earlier one rather than misread it.
#
VERSION = 1

# A key a person adds to leave themselves a note.  Never written.
#
NOTE_SUFFIX = "_note"


########################################################################
########################################################################
#
class WorkListError(Exception):
    """The file is not a work-list this version can read."""


########################################################################
########################################################################
#
@dataclass
class Outcome:
    """What reading one work-list back did."""

    corrected: int = 0
    retyped: int = 0
    added: int = 0
    unchanged: int = 0
    left_blank: int = 0
    refused: list[tuple[str, str]] = field(default_factory=list)

    ####################################################################
    #
    @property
    def written(self) -> int:
        """How many rows would change something."""
        return self.corrected + self.retyped + self.added


####################################################################
#
def field_names(gap: Gap, obj: Child | None) -> tuple[str, ...]:
    """
    The model fields a row for this gap offers to fill.

    Args:
        gap: The gap the row is about.
        obj: The object it names, where there is one.  A guessed
            timezone needs it: the field is per endpoint on a leg and
            whole on anything else.

    Returns:
        Field names as the model spells them, empty for a gap answered
        by something other than fields.
    """
    if gap.population == UNCLASSIFIED:
        return ()

    if gap.population == SKIPPED:
        return ("name",)

    if gap.population == GUESSED_TIMEZONE:
        # The parser's note does not record which end it guessed, so a
        # leg offers both rather than picking one and being wrong half
        # the time.
        #
        # Which field holds the zone depends on the kind, so with no
        # object there is nothing honest to offer.
        #
        if obj is None:
            return ()
        prefixes = [f"{p.endpoint}_" if p.endpoint else "" for p in places(obj)]
        return tuple(
            f"{prefix}{base}"
            for prefix in prefixes
            for base in _TIMEZONE_FIELDS
        )

    prefix = f"{gap.endpoint}_" if gap.endpoint else ""
    return tuple(f"{prefix}{base}" for base in _PLACE_FIELDS)


####################################################################
#
def draft_rows(archive: Archive, trip_keys: list[str]) -> list[dict[str, Any]]:
    """
    One row per open gap, ready to be edited.

    Rows arrive pre-filled with what each object holds now.  For an
    unplaceable endpoint that is empty by definition, so filling it in
    is the whole action; for a guessed timezone it is the guess, which
    is what a person needs to see before overruling it.

    Args:
        archive: The archive holding the staged trips.
        trip_keys: Keys of the trips to write rows for.

    Returns:
        The rows, trips in the order given.
    """
    rows: list[dict[str, Any]] = []
    for trip_key in trip_keys:
        trip = staged_trip(archive, trip_key)
        name = str(getattr(trip, "name", "") or trip_key)
        by_identifier = {
            str(obj.internal_identifier or ""): obj
            for obj in composed_children(archive, trip_key)
        }

        for gap in gaps(archive, trip_key):
            rows.append(row_for(gap, name, by_identifier.get(gap.identifier)))
    return rows


####################################################################
#
def write_worklist(
    path: Path, archive: Archive, rows: list[dict[str, Any]]
) -> None:
    """
    Write a work-list to disk.

    The archive root travels with it so `apply` can refuse a file
    drafted somewhere else -- the uuids would mean nothing, and
    the failure would otherwise read as "no rows matched".

    Args:
        path: Where to write.
        archive: The archive the rows came from.
        rows: The rows to write.
    """
    document = {
        "version": VERSION,
        "archive": str(archive.root),
        "generated_at": datetime.now(UTC).isoformat(),
        "rows": rows,
    }
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


####################################################################
#
def read_worklist(path: Path, archive: Archive) -> list[dict[str, Any]]:
    """
    Read a work-list back, checking it belongs here.

    Args:
        path: The file to read.
        archive: The archive the rows will be applied to.

    Returns:
        The rows.

    Raises:
        WorkListError: The file is unreadable, of another version, or
            was drafted from a different archive.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkListError(f"cannot read {path}: {exc}") from exc

    if not isinstance(document, dict):
        raise WorkListError(f"{path} is not a work-list")

    version = document.get("version")
    if version != VERSION:
        raise WorkListError(
            f"{path} is version {version!r}, this reads version {VERSION}"
        )

    came_from = str(document.get("archive") or "")
    if came_from and Path(came_from) != archive.root:
        raise WorkListError(
            f"{path} was drafted from {came_from}, not {archive.root}"
        )

    rows = document.get("rows")
    if not isinstance(rows, list):
        raise WorkListError(f"{path} carries no rows")
    return rows


####################################################################
#
def apply_rows(
    archive: Archive, rows: list[dict[str, Any]], write: bool = False
) -> Outcome:
    """
    Turn edited rows back into corrections.

    Rows are grouped by trip before anything is written, because a trip
    keeps all of its corrections in one file and writing per row would
    rewrite that file once per row.

    Existing corrections are loaded and merged rather than replaced, so
    applying a second work-list does not discard the first.

    Args:
        archive: The archive holding the staged trips.
        rows: Rows read back from a work-list.
        write: Whether to save.  False reports what would happen.

    Returns:
        What was done, or would be.
    """
    outcome = Outcome()
    by_trip: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_trip[str(row.get("trip_key") or "")].append(row)

    for trip_key, in_trip in sorted(by_trip.items()):
        trip_uuid = _trip_uuid(archive, trip_key)
        if trip_uuid is None:
            for row in in_trip:
                outcome.refused.append(
                    (_label(row), f"no staged trip {trip_key!r}")
                )
            continue

        overrides = load_overrides(archive, trip_uuid)
        index = read_index(archive, trip_key)
        by_identifier = {
            str(obj.internal_identifier or ""): obj
            for obj in composed_children(archive, trip_key)
        }

        touched = False
        for row in in_trip:
            if _apply_one(row, overrides, index, by_identifier, outcome):
                touched = True

        if touched and write:
            save_overrides(archive, overrides)

    return outcome


####################################################################
#
def _apply_one(
    row: dict[str, Any],
    overrides: OverrideSet,
    index: dict[str, dict[str, str]],
    by_identifier: dict[str, Child],
    outcome: Outcome,
) -> bool:
    """One row, onto one trip's correction set.  True when it changed it."""
    uuid = str(row.get("uuid") or "")
    population = str(row.get("population") or "")
    if not uuid:
        outcome.refused.append((_label(row), "row names no uuid"))
        return False

    if population == UNCLASSIFIED:
        return _apply_retype(row, uuid, overrides, index, outcome)
    if population == SKIPPED:
        return _apply_addition(row, uuid, overrides, outcome)
    return _apply_correction(
        row, uuid, overrides, index, by_identifier, outcome
    )


####################################################################
#
def _apply_retype(
    row: dict[str, Any],
    uuid: str,
    overrides: OverrideSet,
    index: dict[str, dict[str, str]],
    outcome: Outcome,
) -> bool:
    """
    Move an object between collections, when the row asks for a move.

    The comparison is against the index rather than the object: a
    collection is where a file sits, not a field an object carries.
    """
    wanted = str(row.get("collection") or "")
    if not wanted:
        outcome.left_blank += 1
        return False

    located = index.get(uuid)
    if located is None:
        outcome.refused.append((_label(row), f"{uuid} is not in the index"))
        return False

    if wanted == located.get("collection"):
        outcome.unchanged += 1
        return False

    if wanted not in MODEL_FOR_COLLECTION:
        outcome.refused.append(
            (
                _label(row),
                f"{wanted!r} is not a collection; expected one of "
                f"{', '.join(sorted(MODEL_FOR_COLLECTION))}",
            )
        )
        return False

    overrides.retype(uuid, wanted)
    outcome.retyped += 1
    return True


####################################################################
#
def _apply_addition(
    row: dict[str, Any],
    uuid: str,
    overrides: OverrideSet,
    outcome: Outcome,
) -> bool:
    """
    Put back an object the parser produced nothing for.

    A row with no collection is one nobody filled in.  That is the
    ordinary state of an addition row, not a mistake, so it is counted
    and passed over rather than refused.
    """
    collection = str(row.get("collection") or "")
    if not collection:
        outcome.left_blank += 1
        return False

    filled = _filled(row)
    if not filled:
        outcome.left_blank += 1
        return False

    try:
        overrides.add(uuid, collection, **filled)
    except ValueError as exc:
        outcome.refused.append((_label(row), str(exc)))
        return False

    outcome.added += 1
    return True


####################################################################
#
def _apply_correction(
    row: dict[str, Any],
    uuid: str,
    overrides: OverrideSet,
    index: dict[str, dict[str, str]],
    by_identifier: dict[str, Child],
    outcome: Outcome,
) -> bool:
    """
    Lay a row's fields over the object it names.

    The merged object is validated before anything is stored, and what
    is stored is the validated form -- so a number typed as text is
    kept as a number, and a value the model will not accept is refused
    here rather than discovered at upload.
    """
    filled = _filled(row)
    if not filled:
        outcome.left_blank += 1
        return False

    # The row carries an identifier, but the file has been edited by
    # hand since it was written, so the index is what is trusted.
    #
    located = index.get(uuid) or {}
    identifier = str(located.get("identifier") or row.get("identifier") or "")
    obj = by_identifier.get(identifier)
    if obj is None:
        outcome.refused.append(
            (_label(row), f"{uuid} names no object staged under this trip")
        )
        return False

    model = type(obj)
    try:
        merged = model.model_validate({**obj.model_dump(), **filled})
    except ValueError as exc:
        outcome.refused.append((_label(row), _first_line(exc)))
        return False

    # Compare against what the object holds now, and store the validated
    # form rather than what was typed.
    #
    was = obj.model_dump(mode="json")
    now = merged.model_dump(mode="json")
    changed = {
        name: now[name]
        for name in filled
        if name in now and now[name] != was.get(name)
    }
    if not changed:
        outcome.unchanged += 1
        return False

    overrides.correct(uuid, **changed)
    outcome.corrected += 1
    return True


####################################################################
#
def row_for(gap: Gap, trip_name: str, obj: Child | None) -> dict[str, Any]:
    """
    One gap, as an editable row.

    Shared with `infer`, which fills rows in rather than leaving them
    for a person -- so both produce the same shape and `apply_rows`
    reads them the same way.
    """
    row: dict[str, Any] = {
        "trip": trip_name,
        "trip_key": gap.trip_key,
        "uuid": gap.uuid,
        "identifier": gap.identifier,
        "population": gap.population,
        "object": gap.where,
    }

    if gap.population == UNCLASSIFIED:
        row["collection"] = gap.collection
        return row

    if gap.population == SKIPPED:
        row["collection"] = ""
        row["fields"] = {"name": gap.summary}
        return row

    current = obj.model_dump(mode="json") if obj is not None else {}
    row["fields"] = {
        name: current.get(name) if current.get(name) is not None else ""
        for name in field_names(gap, obj)
    }
    return row


####################################################################
#
def _filled(row: dict[str, Any]) -> dict[str, Any]:
    """
    The fields a row actually asks to set.

    An empty string is a blank somebody chose to leave blank, and a
    `_note` key is commentary they left for themselves.  Neither is a
    value, so neither is written.
    """
    fields = row.get("fields")
    if not isinstance(fields, dict):
        return {}
    return {
        name: value
        for name, value in fields.items()
        if not name.endswith(NOTE_SUFFIX) and value not in ("", None)
    }


####################################################################
#
def _trip_uuid(archive: Archive, trip_key: str) -> str | None:
    """The uuid a trip's corrections are filed under, or None."""
    path = archive.trip_dir(trip_key) / REPORT_FILENAME
    if not path.is_file():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    found = document.get("trip_uuid")
    return str(found) if found else None


####################################################################
#
def _label(row: dict[str, Any]) -> str:
    """How one row is named when something is wrong with it."""
    return str(row.get("object") or row.get("uuid") or "a row")


####################################################################
#
def _first_line(exc: Exception) -> str:
    """The first line of a validation complaint, which names the field."""
    text = str(exc).strip().splitlines()
    return text[1].strip() if len(text) > 1 else text[0]
