#!/usr/bin/env python
#
"""
The on-disk archive: one JSON file per canonical object.

Every write merges onto what is already on disk rather than replacing it,
because any Tripsy response can be partial.  A file therefore accumulates
the union of everything ever seen about an object, and a restricted export
cannot erase what a fuller one captured.

Layout under the archive root:

    manifest.json
    trips/<trip key>/trip.json
    trips/<trip key>/hostings/<key>.json
    trips/<trip key>/activities/<key>.json
    trips/<trip key>/transportations/<key>.json
    trips/<trip key>/expenses/<key>.json
    trips/<trip key>/collaborators/<key>.json

Keys come from `local_key`, and files are written whole through a
temporary file and a rename, so an interrupted run leaves the previous
version rather than a truncated one.
"""

# system imports
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

# 3rd party imports
from tripsy_exim.models.activity import Activity
from tripsy_exim.models.base import CanonicalModel
from tripsy_exim.models.collaborator import Collaborator
from tripsy_exim.models.expense import Expense
from tripsy_exim.models.hosting import Hosting
from tripsy_exim.models.identifiers import is_minted
from tripsy_exim.models.transportation import Transportation
from tripsy_exim.models.trip import Trip

# Bumped when the on-disk shape changes in a way a reader must know about.
# It is written into the manifest and into every object file, so a file
# found on its own is still self-describing.
#
ARCHIVE_SCHEMA_VERSION = 1

# Subdirectory under a trip for each kind of child object.
#
COLLECTIONS: dict[type[CanonicalModel], str] = {
    Hosting: "hostings",
    Activity: "activities",
    Transportation: "transportations",
    Expense: "expenses",
    Collaborator: "collaborators",
}

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

M = TypeVar("M", bound=CanonicalModel)


####################################################################
#
def local_key(obj: CanonicalModel) -> str:
    """
    Derive the archive's filename key for an object.

    An identifier we minted is preferred, because it is derived from the
    source data and so survives a move to another provider.  Objects
    created in the Tripsy app have no such identifier and fall back to
    their Tripsy id.

    Args:
        obj: Any canonical object.

    Returns:
        A key safe to use as a directory or file name.

    Raises:
        ValueError: If the object has neither an identifier nor an id.
    """
    identifier = getattr(obj, "internal_identifier", None)
    if identifier and is_minted(identifier):
        return _sanitise(identifier)

    obj_id = getattr(obj, "id", None)
    if obj_id is not None:
        return f"tripsy-{obj_id}"

    if identifier:
        return _sanitise(identifier)

    raise ValueError(
        f"{type(obj).__name__} has no internal_identifier and no id; "
        "it cannot be keyed in the archive"
    )


####################################################################
#
def _sanitise(value: str) -> str:
    """
    Reduce an identifier to one safe filename component.

    Identifiers can come from a source we do not control, so separators are
    replaced and leading dots stripped -- the key is then never '.', '..',
    or a hidden file, and cannot reach outside the archive.
    """
    cleaned = _UNSAFE.sub("-", value).strip("-.")
    if not cleaned:
        raise ValueError(f"identifier {value!r} has no usable characters")
    return cleaned


########################################################################
########################################################################
#
class Archive:
    """A directory of canonical trips, written by merge."""

    ####################################################################
    #
    def __init__(self, root: Path) -> None:
        """
        Args:
            root: Directory holding the archive.  Created on first write.
        """
        self.root = Path(root)

    ####################################################################
    #
    @property
    def manifest_path(self) -> Path:
        """Where the sync manifest lives."""
        return self.root / "manifest.json"

    ####################################################################
    #
    def trip_dir(self, trip_key: str) -> Path:
        """The directory holding one trip and its children."""
        return self.root / "trips" / trip_key

    ####################################################################
    #
    def trip_keys(self) -> list[str]:
        """Every trip key present in the archive, sorted."""
        trips = self.root / "trips"
        if not trips.is_dir():
            return []
        return sorted(p.name for p in trips.iterdir() if p.is_dir())

    ####################################################################
    #
    def path_for(
        self, obj: CanonicalModel, trip_key: str | None = None
    ) -> Path:
        """
        Locate the file an object belongs in.

        Args:
            obj: The object to place.
            trip_key: Key of the owning trip.  Required for child objects,
                ignored for a trip.

        Returns:
            The path the object is stored at, which need not exist yet.

        Raises:
            ValueError: If a child object is given without a trip key, or
                the model has no place in the archive.
        """
        if isinstance(obj, Trip):
            return self.trip_dir(local_key(obj)) / "trip.json"

        collection = COLLECTIONS.get(type(obj))
        if collection is None:
            raise ValueError(f"no archive location for {type(obj).__name__}")
        if trip_key is None:
            raise ValueError(
                f"{type(obj).__name__} is a child object and needs a trip_key"
            )
        return self.trip_dir(trip_key) / collection / f"{local_key(obj)}.json"

    ####################################################################
    #
    def read(self, model: type[M], path: Path) -> M | None:
        """
        Load one object, or None when the file does not exist.

        Args:
            model: The canonical model the file holds.
            path: The file to read.

        Returns:
            The stored object, or None.

        Raises:
            ValueError: If the file was written by a newer schema.
        """
        if not path.is_file():
            return None

        document = json.loads(path.read_text(encoding="utf-8"))
        version = document.get("schema_version")
        if version is not None and version > ARCHIVE_SCHEMA_VERSION:
            raise ValueError(
                f"{path} uses archive schema {version}, but this version "
                f"only understands {ARCHIVE_SCHEMA_VERSION}"
            )
        return model.model_validate(document["data"])

    ####################################################################
    #
    def write(self, obj: M, trip_key: str | None = None) -> Path:
        """
        Merge an object into the archive.

        Fields the incoming object did not set are taken from whatever is
        already stored, so writing a partial response adds to the record
        instead of narrowing it.

        Args:
            obj: The object to store.
            trip_key: Key of the owning trip, for child objects.

        Returns:
            The path written.
        """
        path = self.path_for(obj, trip_key)
        existing = self.read(type(obj), path)
        merged = existing.merged_with(obj) if existing is not None else obj

        document = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "kind": type(obj).__name__,
            "data": merged.model_dump(mode="json", exclude_unset=True),
        }
        _write_json(path, document)
        return path

    ####################################################################
    #
    def read_manifest(self) -> dict[str, Any]:
        """Load the sync manifest, or a fresh one if there is none yet."""
        if not self.manifest_path.is_file():
            return {
                "schema_version": ARCHIVE_SCHEMA_VERSION,
                "last_export_at": None,
                "identifier_cache": {},
            }
        result: dict[str, Any] = json.loads(
            self.manifest_path.read_text(encoding="utf-8")
        )
        return result

    ####################################################################
    #
    def write_manifest(self, manifest: dict[str, Any]) -> Path:
        """Store the sync manifest."""
        manifest = {**manifest, "schema_version": ARCHIVE_SCHEMA_VERSION}
        _write_json(self.manifest_path, manifest)
        return self.manifest_path

    ####################################################################
    #
    def record_export(self, started_at: datetime | None = None) -> Path:
        """
        Advance the export watermark.

        The watermark is wall-clock time because trips carry no
        `updated_at` to compute it from.  It is the time the run *started*,
        so a change made while the run was in flight is caught by the next
        one rather than missed.

        Args:
            started_at: When the run began.  Defaults to now.

        Returns:
            The manifest path.
        """
        when = started_at or datetime.now(UTC)
        manifest = self.read_manifest()
        manifest["last_export_at"] = when.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return self.write_manifest(manifest)


####################################################################
#
def _write_json(path: Path, document: dict[str, Any]) -> None:
    """Write JSON through a temporary file and an atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
