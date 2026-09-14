#!/usr/bin/env python
#
"""
Corrections laid over what the parser inferred.

The parser guesses, and 74% of the corpus falls through to an activity
because nothing matched.  Those guesses have to be correctable, but a
correction must not be an edit in place: re-staging would clobber it, and
'what the parser inferred' has to stay separable from 'what I decided'.

So corrections live outside the trip directories entirely, keyed by the
source uuid.  The uuid is the one identity that survives both a re-export
and a change of identifier namespace -- a shaping run mints different
identifiers from the same uuids, and corrections made while shaping are
meant to carry into the real run.

Overrides are applied on the way out, never during staging.  Staging
writes what the parser produced and nothing else, so re-staging is
lossless and the parser's output stays inspectable.
"""

# system imports
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Project imports
from tripsy_exim.models import Activity, CanonicalModel, Hosting, Transportation
from tripsy_exim.store import Archive, local_key, write_json
from tripsy_exim.sync.staging import REPORT_FILENAME

OVERRIDES_DIRNAME = "overrides"

MODEL_FOR_COLLECTION: dict[str, type[CanonicalModel]] = {
    "hostings": Hosting,
    "activities": Activity,
    "transportations": Transportation,
}

# A transportation splits one event's place and time across a departure
# and an arrival; an activity and a hosting keep them whole.  Retyping
# between the two shapes reads this correspondence in whichever direction
# the retype runs.
#
# The place goes to the arrival half.  A source event carries one place,
# and where that place is has been measured: TripIt's calendars put the
# destination there.  The parser files a transport event the same way, so
# a retyped activity and a directly parsed leg end up alike rather than
# a field apart.  A GDPR export offers no evidence either way, and one
# consistent rule beats two.
#
_SPLIT_FIELDS: dict[str, str] = {
    "starts_at": "departure_at",
    "ends_at": "arrival_at",
    "timezone": "arrival_timezone",
    "address": "arrival_address",
    "latitude": "arrival_latitude",
    "longitude": "arrival_longitude",
}

# Carried across any retype, because every kind holds them alike.
#
_SHARED_FIELDS = ("internal_identifier", "name", "description")


########################################################################
########################################################################
#
@dataclass
class Override:
    """One correction, against one source uuid."""

    collection: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    note: str | None = None

    ####################################################################
    #
    def as_document(self) -> dict[str, Any]:
        """This override, as it is stored."""
        out: dict[str, Any] = {}
        if self.collection is not None:
            out["collection"] = self.collection
        if self.fields:
            out["fields"] = dict(self.fields)
        if self.note is not None:
            out["note"] = self.note
        return out


########################################################################
########################################################################
#
@dataclass
class Addition:
    """One object the source never held, against one trip."""

    collection: str
    fields: dict[str, Any] = field(default_factory=dict)
    note: str | None = None

    ####################################################################
    #
    def as_document(self) -> dict[str, Any]:
        """This addition, as it is stored."""
        out: dict[str, Any] = {"collection": self.collection}
        if self.fields:
            out["fields"] = dict(self.fields)
        if self.note is not None:
            out["note"] = self.note
        return out


########################################################################
########################################################################
#
@dataclass
class OverrideSet:
    """Every correction against one trip, keyed by source uuid."""

    trip_uuid: str
    entries: dict[str, Override] = field(default_factory=dict)

    # Objects the source never held.  A correction names something the
    # parser produced; an addition is something only a person knows --
    # the shuttle from the terminal to the rental counter that nobody
    # writes down because all you had to do was find it.  They live here
    # rather than in the trip directory so re-staging cannot prune them.
    #
    additions: dict[str, Addition] = field(default_factory=dict)

    ####################################################################
    #
    def retype(self, uuid: str, collection: str) -> None:
        """Record that this uuid belongs in another collection."""
        if collection not in MODEL_FOR_COLLECTION:
            raise ValueError(
                f"unknown collection {collection!r}; expected one of "
                f"{', '.join(sorted(MODEL_FOR_COLLECTION))}"
            )
        self.entries.setdefault(uuid, Override()).collection = collection

    ####################################################################
    #
    def correct(self, uuid: str, **fields: Any) -> None:
        """Record field corrections against this uuid."""
        self.entries.setdefault(uuid, Override()).fields.update(fields)

    ####################################################################
    #
    def add(self, uuid: str, collection: str, **fields: Any) -> None:
        """
        Record an object the source never held.

        Args:
            uuid: A uuid for the addition, minted by whoever adds it.
                Re-adding under the same uuid replaces the fields.
            collection: Which collection the object belongs to.
            fields: The object's own fields.  Every one is the adder's:
                nothing is inferred from a source record, because there
                is none.

        Raises:
            ValueError: The collection is not one Tripsy has, or a field
                holds a value that cannot be stored.
        """
        if collection not in MODEL_FOR_COLLECTION:
            raise ValueError(
                f"unknown collection {collection!r}; expected one of "
                f"{', '.join(sorted(MODEL_FOR_COLLECTION))}"
            )
        for name, value in sorted(fields.items()):
            try:
                json.dumps(value)
            except TypeError as exc:
                raise ValueError(
                    f"{name} holds a {type(value).__name__}, which cannot "
                    f"be stored; give instants as ISO 8601 strings"
                ) from exc
        self.additions[uuid] = Addition(
            collection=collection, fields=dict(fields)
        )

    ####################################################################
    #
    def as_document(self) -> dict[str, Any]:
        """The whole set, as it is stored."""
        document: dict[str, Any] = {
            "trip_uuid": self.trip_uuid,
            "entries": {
                uuid: entry.as_document()
                for uuid, entry in sorted(self.entries.items())
            },
        }
        if self.additions:
            document["additions"] = {
                uuid: addition.as_document()
                for uuid, addition in sorted(self.additions.items())
            }
        return document


####################################################################
#
def overrides_path(archive: Archive, trip_uuid: str) -> Path:
    """
    Where one trip's corrections live.

    Outside `trips/`, because a trip directory is named by an identifier
    and identifiers carry the namespace.  Corrections keyed there would
    not reach the same trip staged under a different namespace, which is
    exactly what a shaping run does.
    """
    return archive.root / OVERRIDES_DIRNAME / f"{trip_uuid}.json"


####################################################################
#
def save_overrides(archive: Archive, overrides: OverrideSet) -> Path:
    """Write one trip's corrections."""
    path = overrides_path(archive, overrides.trip_uuid)
    write_json(path, overrides.as_document())
    return path


####################################################################
#
def load_overrides(archive: Archive, trip_uuid: str) -> OverrideSet:
    """Read one trip's corrections, empty when there are none."""
    path = overrides_path(archive, trip_uuid)
    if not path.exists():
        return OverrideSet(trip_uuid=trip_uuid)

    document = json.loads(path.read_text())
    entries = {
        uuid: Override(
            collection=body.get("collection"),
            fields=dict(body.get("fields") or {}),
            note=body.get("note"),
        )
        for uuid, body in (document.get("entries") or {}).items()
    }
    additions = {
        uuid: Addition(
            collection=str(body.get("collection") or ""),
            fields=dict(body.get("fields") or {}),
            note=body.get("note"),
        )
        for uuid, body in (document.get("additions") or {}).items()
    }
    return OverrideSet(
        trip_uuid=trip_uuid, entries=entries, additions=additions
    )


####################################################################
#
def retyped[M: CanonicalModel](obj: CanonicalModel, target: type[M]) -> M:
    """
    Rebuild an object as another kind, carrying what both kinds hold.

    A transportation splits place and time across departure and arrival,
    so the correspondence is applied in whichever direction the retype
    runs.  Fields the target has no home for are dropped, and fields it
    has but the source did not fill are left unset for a correction to
    supply.

    Args:
        obj: The object as it was parsed.
        target: The model it should become.

    Returns:
        A new object of the target kind.
    """
    values: dict[str, Any] = {
        name: getattr(obj, name, None) for name in _SHARED_FIELDS
    }

    from_split = isinstance(obj, Transportation)
    to_split = target is Transportation
    if from_split and to_split:
        for split in _SPLIT_FIELDS.values():
            values[split] = getattr(obj, split, None)
    elif not from_split and not to_split:
        # Activity to hosting: the field names already agree.
        #
        for whole in _SPLIT_FIELDS:
            values[whole] = getattr(obj, whole, None)
    elif to_split:
        for whole, split in _SPLIT_FIELDS.items():
            values[split] = getattr(obj, whole, None)
    else:
        for whole, split in _SPLIT_FIELDS.items():
            values[whole] = getattr(obj, split, None)

    return target(**{k: v for k, v in values.items() if v is not None})


########################################################################
########################################################################
#
@dataclass
class Applied:
    """What applying a set of corrections did."""

    retyped: int = 0
    corrected: int = 0
    unknown: list[str] = field(default_factory=list)

    ####################################################################
    #
    @property
    def total(self) -> int:
        """How many objects were touched."""
        return self.retyped + self.corrected


####################################################################
#
def read_index(archive: Archive, trip_key: str) -> dict[str, dict[str, str]]:
    """
    Read the uuid map staging wrote beside a trip.

    Raises:
        FileNotFoundError: The trip has not been staged.
    """
    path = archive.trip_dir(trip_key) / REPORT_FILENAME
    document = json.loads(path.read_text())
    index: dict[str, dict[str, str]] = document.get("index") or {}
    return index


####################################################################
#
def apply_overrides(
    archive: Archive, trip_key: str, overrides: OverrideSet
) -> Applied:
    """
    Lay a trip's corrections over what the parser staged.

    A retype is a move between collections: the object is rebuilt as the
    target kind, written to its new collection, and the old file removed.
    The key is unchanged by that -- it comes from the identifier, which a
    retype does not touch -- so the filename moves directories intact.

    Args:
        archive: The archive holding the staged trip.
        trip_key: Key of the staged trip to correct.
        overrides: The corrections to lay over it.

    Returns:
        Counts of what was retyped and corrected, and any uuid the trip's
        index does not name.
    """
    index = read_index(archive, trip_key)
    applied = Applied()

    for uuid, entry in sorted(overrides.entries.items()):
        located = index.get(uuid)
        if located is None:
            applied.unknown.append(uuid)
            continue

        collection = located["collection"]
        model = MODEL_FOR_COLLECTION[collection]
        path = (
            archive.trip_dir(trip_key)
            / collection
            / f"{located['identifier']}.json"
        )
        obj = archive.read(model, path)
        if obj is None:
            applied.unknown.append(uuid)
            continue

        target_collection = entry.collection or collection
        if target_collection != collection:
            obj = retyped(obj, MODEL_FOR_COLLECTION[target_collection])
            path.unlink()
            applied.retyped += 1

        if entry.fields:
            obj = obj.model_copy(update=dict(entry.fields))
            applied.corrected += 1

        # The key comes from the identifier, which a retype leaves alone,
        # so the file moves directories under the same name.  A key that
        # changed would strand the old object instead of replacing it.
        #
        if local_key(obj) != located["identifier"]:
            raise ValueError(
                f"retyping {uuid} changed its key from "
                f"{located['identifier']} to {local_key(obj)}"
            )
        archive.write(obj, trip_key)

    return applied
