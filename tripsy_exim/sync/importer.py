#!/usr/bin/env python
#
"""
Writing staged trips into Tripsy.

A run plans before it writes.  The plan says what would be created, what
already exists, and the order a trip's objects would take; the write path
then executes exactly that plan.  Both read the same archive, so a dry run
is the same computation as the real one with the POSTs left out.

Two properties of the API shape the design.

An identifier is never released.  A create whose `internal_identifier` is
already taken answers an empty 200 and writes nothing, which is what makes
a re-run a no-op and a resumed run free.  It also means a mistake cannot
be taken back, so the plan exists to be read first.

`sort_order` is one dense sequence across a whole trip -- activities,
hostings and transportations share it -- and the server computes nothing,
leaving every object on 0 when it is absent.  A trip is therefore numbered
here, chronologically, before anything is sent.  The number an object gets
on its first create is the number it keeps: a re-run resolves to the
existing object rather than updating it, so inserting an object later and
re-running leaves its siblings stale.  Renumbering after the fact is a
PATCH pass, not something a re-run does on its own.
"""

# system imports
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Project imports
from tripsy_exim.api import (
    BadRequest,
    Created,
    MethodNotAllowed,
    NotFound,
    TripsyClient,
)
from tripsy_exim.models import (
    Activity,
    CanonicalModel,
    Hosting,
    Transportation,
    Trip,
)
from tripsy_exim.store import Archive
from tripsy_exim.sync.overrides import MODEL_FOR_COLLECTION

# The order collections are written in.  Lodging and travel first, so a
# partial run leaves a trip with its skeleton rather than its trimmings.
#
# Where finished trips are recorded in the manifest.
#
UPLOADED = "uploaded_trips"

# Where one trip is recorded as uploading into another.
#
MERGED = "merged_trips"

COLLECTION_ORDER: tuple[str, ...] = (
    "transportations",
    "hostings",
    "activities",
)

Child = Activity | Hosting | Transportation


####################################################################
#
def instant_of(obj: CanonicalModel) -> datetime | None:
    """
    When an object begins, whichever field its kind uses for that.

    Args:
        obj: A staged child object.

    Returns:
        The start instant, or None for an object carrying no time.
    """
    if isinstance(obj, Transportation):
        return obj.departure_at
    return getattr(obj, "starts_at", None)


####################################################################
#
def zone_of(obj: CanonicalModel) -> str | None:
    """
    The zone an object's start was expressed in.

    Every instant is stored in UTC, so a plan rendered without this reads
    a Tokyo morning as the previous afternoon.  The plan is what a person
    checks before the one irreversible step, so it is rendered in the
    traveller's own time.

    Args:
        obj: A staged child object.

    Returns:
        An IANA zone name, or None when the object carries none.
    """
    if isinstance(obj, Transportation):
        return obj.departure_timezone
    return getattr(obj, "timezone", None)


########################################################################
########################################################################
#
@dataclass(frozen=True)
class PlannedObject:
    """One child object as the plan sees it."""

    collection: str
    identifier: str
    name: str
    sort_order: int
    starts_at: datetime | None
    type_value: str | None
    timezone: str | None = None

    ####################################################################
    #
    @property
    def when(self) -> str:
        """The instant in local time, for a plan a person reads."""
        if self.starts_at is None:
            return "no date"
        local = self.starts_at
        if self.timezone:
            try:
                local = local.astimezone(ZoneInfo(self.timezone))
            except ZoneInfoNotFoundError:
                pass
        return local.strftime("%Y-%m-%d %H:%M")


########################################################################
########################################################################
#
@dataclass
class TripPlan:
    """What a run would do to one trip, computed without writing."""

    trip_key: str
    name: str
    identifier: str
    objects: list[PlannedObject] = field(default_factory=list)

    # Objects an absorbed trip holds that this one already has.
    #
    duplicates: int = 0

    ####################################################################
    #
    @property
    def total(self) -> int:
        """How many child objects the trip would write."""
        return len(self.objects)

    ####################################################################
    #
    @property
    def untyped(self) -> list[PlannedObject]:
        """
        Transportations carrying no type.

        Tripsy draws an untyped leg without an icon, so these are worth a
        person's eye before a run rather than after.  A single field, and
        a PATCH fixes one afterwards.
        """
        return [
            obj
            for obj in self.objects
            if obj.collection == "transportations" and not obj.type_value
        ]

    ####################################################################
    #
    @property
    def undated(self) -> list[PlannedObject]:
        """Objects with no instant, which sort to the end of the trip."""
        return [obj for obj in self.objects if obj.starts_at is None]


########################################################################
########################################################################
#
@dataclass
class TripImport:
    """What a run actually did to one trip."""

    trip_key: str
    name: str
    trip_id: int | None = None
    trip_created: bool = False
    created: int = 0
    existing: int = 0
    failed: list[str] = field(default_factory=list)

    ####################################################################
    #
    @property
    def total(self) -> int:
        """Children accounted for, however they were accounted for."""
        return self.created + self.existing + len(self.failed)


####################################################################
#
def staged_children(archive: Archive, trip_key: str) -> Iterator[Child]:
    """
    Read every child object of a staged trip off disk.

    Args:
        archive: The archive holding the trip.
        trip_key: Key of the trip to read.

    Yields:
        One object at a time, collection by collection.
    """
    for collection in COLLECTION_ORDER:
        model = MODEL_FOR_COLLECTION[collection]
        directory = archive.trip_dir(trip_key) / collection
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            obj = archive.read(model, path)
            if obj is not None:
                yield obj  # type: ignore[misc]


####################################################################
#
def staged_trip(archive: Archive, trip_key: str) -> Trip | None:
    """Read the trip record itself, or None when there is none."""
    return archive.read(Trip, archive.trip_dir(trip_key) / "trip.json")


####################################################################
#
def numbered(objects: list[Child]) -> list[tuple[int, Child]]:
    """
    Put a trip's objects in the order the app shows them.

    One sequence covers the whole trip across all three collections,
    earliest first, and objects carrying no instant go to the end -- which
    is where the app puts its own.  Ties and undated objects fall back to
    the identifier, so numbering one trip twice gives one answer.

    Args:
        objects: Every child object of one trip.

    Returns:
        Pairs of `sort_order` and object, in that order, numbered from 1.
    """

    def key(obj: Child) -> tuple[int, str, str]:
        when = instant_of(obj)
        return (
            1 if when is None else 0,
            when.isoformat() if when else "",
            str(obj.internal_identifier or ""),
        )

    return list(enumerate(sorted(objects, key=key), start=1))


####################################################################
#
def merged_into(archive: Archive) -> dict[str, str]:
    """
    Which staged trips are to be uploaded as part of another.

    One journey can reach the archive as two trips: the export records a
    trip per traveller, so a holiday taken together arrives twice, each
    copy holding that traveller's own flights and room and one of them
    holding the itinerary they shared.  Neither is redundant and neither
    is the whole thing.

    Args:
        archive: The archive to read.

    Returns:
        Absorbed trip key to the key it is absorbed into.
    """
    record = archive.read_manifest().get(MERGED) or {}
    return {str(key): str(value) for key, value in record.items()}


####################################################################
#
def absorbed_by(archive: Archive, trip_key: str) -> list[str]:
    """The trips whose objects upload as part of this one."""
    return sorted(
        absorbed
        for absorbed, target in merged_into(archive).items()
        if target == trip_key
    )


####################################################################
#
def declare_merge(archive: Archive, absorbed: str, target: str) -> None:
    """
    Record that one staged trip uploads as part of another.

    Nothing is moved on disk.  Both trips stay as the parser produced
    them, which keeps staging lossless and the declaration reversible;
    it is read at upload time and nowhere else.

    Args:
        archive: The archive to record in.
        absorbed: Key of the trip that will not be created.
        target: Key of the trip its objects join.

    Raises:
        ValueError: The two are the same trip, either is not staged, or
            the target is itself absorbed -- a chain nobody intended.
    """
    if absorbed == target:
        raise ValueError(f"{absorbed} cannot be merged into itself")
    for key in (absorbed, target):
        if staged_trip(archive, key) is None:
            raise ValueError(f"{key} is not staged")

    existing = merged_into(archive)
    if target in existing:
        raise ValueError(
            f"{target} is itself merged into {existing[target]}; "
            f"merge into that instead"
        )

    manifest: dict[str, Any] = archive.read_manifest()
    record = manifest.setdefault(MERGED, {})
    record[absorbed] = target
    archive.write_manifest(manifest)


####################################################################
#
def in_travel_order(archive: Archive, keys: list[str]) -> list[str]:
    """
    Put trip keys in the order the trips were travelled, oldest first.

    A trip key is a digest, so the archive's own directory order is
    effectively random.  Uploading oldest first makes `--limit` a way of
    working forward through an account rather than a way of picking an
    arbitrary handful.

    Args:
        archive: The archive holding the staged trips.
        keys: The keys to order.

    Returns:
        The same keys, oldest trip first, with undated trips last.
    """

    def when(key: str) -> tuple[int, str, str]:
        trip = staged_trip(archive, key)
        starts = getattr(trip, "starts_at", None) if trip else None
        return (1 if starts is None else 0, str(starts or ""), key)

    return sorted(keys, key=when)


####################################################################
#
def resolve_trip_key(archive: Archive, needle: str) -> str:
    """
    Find one staged trip by its key or by its name.

    A key is a digest nobody can recognise, so a name is what a person
    actually has.  An exact key wins outright; otherwise the name is
    matched case-insensitively as a substring.

    Args:
        archive: The archive holding the staged trips.
        needle: A trip key, or part of a trip's name.

    Returns:
        The one key that matched.

    Raises:
        ValueError: Nothing matched, or more than one did.  An ambiguous
            match names the candidates rather than picking one.
    """
    keys = archive.trip_keys()
    if needle in keys:
        return needle

    wanted = needle.casefold()
    matched = []
    for key in keys:
        trip = staged_trip(archive, key)
        name = str(getattr(trip, "name", "") or "")
        if wanted in name.casefold():
            matched.append((key, name))

    if not matched:
        raise ValueError(f"no staged trip matches {needle!r}")
    if len(matched) > 1:
        listed = "\n".join(f"    {name}  [{key}]" for key, name in matched)
        raise ValueError(f"{len(matched)} trips match {needle!r}:\n{listed}")
    return matched[0][0]


####################################################################
#
def uploaded_trips(archive: Archive) -> dict[str, str]:
    """
    The trips an earlier run finished, and when it finished them.

    A finished trip is skipped outright rather than re-sent.  Re-sending
    is harmless -- every create would be suppressed -- but a hundred-object
    trip costs a hundred suppressed requests to learn nothing, and it
    would consume a `--limit` that was meant for work still to do.

    Args:
        archive: The archive to read.

    Returns:
        Trip keys to the instant each was finished.
    """
    record = archive.read_manifest().get(UPLOADED) or {}
    return {str(key): str(value) for key, value in record.items()}


####################################################################
#
def mark_uploaded(archive: Archive, trip_key: str) -> None:
    """Record that a trip finished, so a later run steps over it."""
    manifest: dict[str, Any] = archive.read_manifest()
    record = manifest.setdefault(UPLOADED, {})
    record[trip_key] = datetime.now(UTC).isoformat(timespec="seconds")
    archive.write_manifest(manifest)


####################################################################
#
def plan_trip(archive: Archive, trip_key: str) -> TripPlan:
    """
    Work out what importing one staged trip would do.

    Nothing is written and nothing is sent.  The `sort_order` values in
    the plan are the ones a real run would send, because both come from
    this function.

    Args:
        archive: The archive holding the staged trip.
        trip_key: Key of the trip to plan.

    Returns:
        The plan for that trip.

    Raises:
        ValueError: The trip has no `trip.json`, so it is not staged.
    """
    trip = staged_trip(archive, trip_key)
    if trip is None:
        raise ValueError(f"{trip_key} carries no trip record")

    plan = TripPlan(
        trip_key=trip_key,
        name=str(trip.name or ""),
        identifier=str(trip.internal_identifier or ""),
    )

    children, plan.duplicates = _all_children(archive, trip_key)
    for order, obj in numbered(children):
        collection = _collection_of(obj)
        plan.objects.append(
            PlannedObject(
                collection=collection,
                identifier=str(obj.internal_identifier or ""),
                name=_label(obj),
                sort_order=order,
                starts_at=instant_of(obj),
                type_value=_type_of(obj),
                timezone=zone_of(obj),
            )
        )
    return plan


####################################################################
#
def upload_trip(
    client: TripsyClient, archive: Archive, trip_key: str
) -> TripImport:
    """
    Upload one staged trip to Tripsy.

    The trip is created first and its id resolved, then its children in
    `sort_order`.  An object whose identifier is already taken is counted
    as existing rather than retried: the API suppressed it, which is what
    makes a second run of this function a no-op.

    Args:
        client: An authenticated client.
        archive: The archive holding the staged trip.
        trip_key: Key of the trip to write.

    Returns:
        What was created, what was already there, and what failed.

    Raises:
        ValueError: The trip is not staged, or Tripsy accepted the trip
            but no id could be resolved for it -- without one there is
            nothing to hang children off.
    """
    trip = staged_trip(archive, trip_key)
    if trip is None:
        raise ValueError(f"{trip_key} carries no trip record")

    identifier = str(trip.internal_identifier or "")
    result = TripImport(trip_key=trip_key, name=str(trip.name or ""))

    outcome = client.create_trip(trip.writable_payload())
    if isinstance(outcome, Created):
        result.trip_created = True
        result.trip_id = outcome.payload.get("id")

    if result.trip_id is None:
        # The trip was already there, so the create answered an empty 200
        # carrying no id.  A run that reached this trip before recorded
        # the id, which saves listing the whole account once per trip on
        # a resumed run of a hundred.
        #
        result.trip_id = _recall(archive, identifier)

    if result.trip_id is None:
        result.trip_id = client.trip_ids_by_identifier().get(identifier)

    if result.trip_id is None:
        raise ValueError(
            f"{trip_key}: no trip id for {identifier!r} after the create"
        )

    _remember(archive, identifier, result.trip_id)

    children, _ = _all_children(archive, trip_key)
    for order, obj in numbered(children):
        collection = _collection_of(obj)
        payload = obj.writable_payload()
        payload["sort_order"] = order
        try:
            written = client.create_child(result.trip_id, collection, payload)
        except (BadRequest, NotFound, MethodNotAllowed) as exc:
            # One object the server would not take.  The rest of the trip
            # is unaffected, so it is recorded and the run goes on.
            # Anything else -- a refused token, a service that stopped
            # answering, a throttle the pacer could not ride out -- is
            # about the run rather than this object, and propagates:
            # collecting it per object would spend a trip's identifiers
            # while reporting a completed run.
            #
            result.failed.append(
                f"{collection}/{obj.internal_identifier}: {exc!r}"
            )
            continue

        if isinstance(written, Created):
            result.created += 1
        else:
            result.existing += 1

    if not result.failed:
        mark_uploaded(archive, trip_key)

    return result


####################################################################
#
def child_ids_by_identifier(
    client: TripsyClient, trip_id: int, collection: str
) -> dict[str, int]:
    """
    Map a collection's `internal_identifier` values to their ids.

    A duplicate create answers an empty 200 carrying no id, and there is
    no child equivalent of `trip_ids_by_identifier`, so resolving one
    means reading the collection back.  Needed to correct an object that
    is already there -- a retype, or a `sort_order` repair -- rather than
    to create it.

    Args:
        client: An authenticated client.
        trip_id: The trip the collection belongs to.
        collection: One of the plural collection names.

    Returns:
        Identifiers to ids, skipping objects carrying no identifier.
    """
    found: dict[str, int] = {}
    for obj in client.iter_children(
        trip_id, collection, fields=["id", "internal_identifier"]
    ):
        key = obj.get("internal_identifier")
        if key and obj.get("id") is not None:
            found[str(key)] = int(obj["id"])
    return found


####################################################################
#
def _label(obj: Child) -> str:
    """
    What to call an object in a plan a person reads.

    A flight carries no name -- the app titles one from its endpoints and
    TripIt's own word for them all is "Flight" -- so the plan says what
    the app will say rather than leaving the row blank.

    Args:
        obj: A staged child object.

    Returns:
        The object's name, or its endpoints, or an empty string.
    """
    name = str(obj.name or "")
    if name or not isinstance(obj, Transportation):
        return name

    ends = [obj.departure_description, obj.arrival_description]
    if any(ends):
        return " to ".join(str(end or "?") for end in ends)
    return ""


####################################################################
#
def _all_children(archive: Archive, trip_key: str) -> tuple[list[Child], int]:
    """
    Every object this trip uploads, its own and any it absorbs.

    Numbering runs over the whole of it, so a merged journey reads as one
    itinerary in time order rather than one traveller's followed by the
    other's.

    An absorbed trip can hold the same object as the trip absorbing it --
    two travellers on one flight each carry that flight -- so an object
    the target already has is left out.  Identifiers cannot catch this:
    they are derived per trip, so the same flight in two records mints
    two of them and both would be created.

    Args:
        archive: The archive holding the staged trips.
        trip_key: Key of the trip being uploaded.

    Returns:
        The objects to upload, and how many duplicates were left out.
    """
    objects = list(staged_children(archive, trip_key))
    seen = {_fingerprint(obj) for obj in objects}

    duplicates = 0
    for absorbed in absorbed_by(archive, trip_key):
        for obj in staged_children(archive, absorbed):
            mark = _fingerprint(obj)
            if mark in seen:
                duplicates += 1
                continue
            seen.add(mark)
            objects.append(obj)
    return objects, duplicates


####################################################################
#
def _fingerprint(obj: Child) -> tuple[str, str, str]:
    """
    What makes two staged objects the same thing.

    Kind, instant and label: a flight two travellers were both on is one
    flight, and a trip should carry it once.  Two rooms in one hotel are
    not caught by this, and should not be -- they differ by the minute
    each was booked for, which is what tells them apart.

    Args:
        obj: A staged child object.

    Returns:
        A value equal for two objects describing the same thing.
    """
    when = instant_of(obj)
    return (
        type(obj).__name__,
        when.isoformat() if when else "",
        _label(obj),
    )


####################################################################
#
def _collection_of(obj: CanonicalModel) -> str:
    """The plural collection name an object belongs in."""
    for name, model in MODEL_FOR_COLLECTION.items():
        if type(obj) is model:
            return name
    raise ValueError(f"no collection for {type(obj).__name__}")


####################################################################
#
def _type_of(obj: CanonicalModel) -> str | None:
    """The Tripsy type string an object carries, whichever field holds it."""
    for name in ("transportation_type", "activity_type", "room_type"):
        value = getattr(obj, name, None)
        if value:
            return str(value)
    return None


####################################################################
#
def _recall(archive: Archive, identifier: str) -> int | None:
    """The trip id an earlier run recorded for this identifier, if any."""
    cache = archive.read_manifest().get("identifier_cache") or {}
    found = cache.get(identifier)
    return int(found) if found is not None else None


####################################################################
#
def _remember(archive: Archive, identifier: str, trip_id: int) -> None:
    """Record a trip's id in the manifest, so a later run need not ask."""
    manifest: dict[str, Any] = archive.read_manifest()
    cache = manifest.setdefault("identifier_cache", {})
    cache[identifier] = trip_id
    archive.write_manifest(manifest)
