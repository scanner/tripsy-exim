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
import json
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
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
    mint,
    namespace_of,
)
from tripsy_exim.store import Archive
from tripsy_exim.sync.overrides import (
    MODEL_FOR_COLLECTION,
    Override,
    load_overrides,
)
from tripsy_exim.sync.staging import REPORT_FILENAME

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
def undo_merge(archive: Archive, absorbed: str) -> None:
    """
    Stop uploading one trip as part of another.

    Args:
        archive: The archive to record in.
        absorbed: Key of the trip to release.

    Raises:
        ValueError: That trip is not declared as part of anything.
    """
    manifest: dict[str, Any] = archive.read_manifest()
    record = manifest.get(MERGED) or {}
    if absorbed not in record:
        raise ValueError(f"{absorbed} is not merged into anything")
    del record[absorbed]
    manifest[MERGED] = record
    archive.write_manifest(manifest)


####################################################################
#
def spanning(archive: Archive, trip: Trip, trip_key: str) -> Trip:
    """
    Widen a merged trip's dates to cover everything it absorbs.

    A trip's declared range comes from the record it was parsed from, and
    the record absorbing the others need not be the one that ran longest:
    a weekend in San Francisco reached the archive as a hotel booking and
    a day on Angel Island, and the day is the record with more objects.
    Left alone, the trip would say it lasted an afternoon.

    Args:
        archive: The archive holding the staged trips.
        trip: The trip record as parsed.
        trip_key: Its key.

    Returns:
        The trip, its dates widened when it absorbs anything that runs
        outside them.
    """
    absorbed = absorbed_by(archive, trip_key)
    if not absorbed:
        return trip

    starts = [trip.starts_at] if trip.starts_at else []
    ends = [trip.ends_at] if trip.ends_at else []
    for key in absorbed:
        other = staged_trip(archive, key)
        if other is None:
            continue
        if other.starts_at:
            starts.append(other.starts_at)
        if other.ends_at:
            ends.append(other.ends_at)

    if not starts or not ends:
        return trip
    return trip.model_copy(
        update={"starts_at": min(starts), "ends_at": max(ends)}
    )


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

    outcome = client.create_trip(
        spanning(archive, trip, trip_key).writable_payload()
    )
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
def corrections(archive: Archive, trip_key: str) -> dict[str, Override]:
    """
    The corrections to lay over one trip's objects, by identifier.

    Corrections are keyed by the source uuid, because that is the one
    identity surviving a change of namespace -- a shaping run and the
    real run mint different identifiers from the same uuid.  The objects
    on disk are keyed by identifier, so the trip's own index is what
    joins the two.

    Args:
        archive: The archive holding the staged trip.
        trip_key: Key of the trip to correct.

    Returns:
        Identifier to the correction against it, empty when the trip has
        none or its report predates the uuid being recorded.
    """
    report = archive.trip_dir(trip_key) / REPORT_FILENAME
    if not report.is_file():
        return {}

    document = json.loads(report.read_text(encoding="utf-8"))
    trip_uuid = document.get("trip_uuid")
    if not trip_uuid:
        return {}

    overrides = load_overrides(archive, str(trip_uuid))
    if not overrides.entries:
        return {}

    index = document.get("index") or {}
    out: dict[str, Override] = {}
    for uuid, entry in overrides.entries.items():
        located = index.get(uuid)
        if located and located.get("identifier"):
            out[str(located["identifier"])] = entry
    return out


####################################################################
#
def added(archive: Archive, trip_key: str) -> list[Child]:
    """
    The objects a person added to one trip, built from its corrections.

    An addition carries no source record, so its identifier is minted
    from the trip's uuid and its own -- the two identities that survive a
    re-export -- in the namespace the trip itself was staged under.  A
    re-stage therefore reproduces the same identifier, and re-uploading
    an addition is a no-op the same way re-uploading a parsed object is.

    Args:
        archive: The archive holding the staged trip.
        trip_key: Key of the trip to read additions for.

    Returns:
        The added objects, empty when the trip has none.
    """
    report = archive.trip_dir(trip_key) / REPORT_FILENAME
    if not report.is_file():
        return []

    document = json.loads(report.read_text(encoding="utf-8"))
    trip_uuid = document.get("trip_uuid")
    if not trip_uuid:
        return []

    overrides = load_overrides(archive, str(trip_uuid))
    if not overrides.additions:
        return []

    trip = staged_trip(archive, trip_key)
    namespace = namespace_of(trip.internal_identifier if trip else None)
    if namespace is None:
        return []

    built: list[Child] = []
    for uuid, addition in sorted(overrides.additions.items()):
        model = MODEL_FOR_COLLECTION.get(addition.collection)
        if model is None:
            continue
        fields = dict(addition.fields)
        fields["internal_identifier"] = mint(namespace, str(trip_uuid), uuid)
        built.append(cast(Child, model.model_validate(fields)))
    return built


####################################################################
#
def corrected(obj: Child, override: Override) -> Child:
    """
    Lay one correction over one object.

    Only fields are applied here.  A retype moves an object between
    collections, which is a change to the archive rather than to what is
    sent, and `apply_overrides` is what does that.

    Args:
        obj: The object as it was staged.
        override: The correction against it.

    Returns:
        A corrected copy, or the object itself when nothing applies.
    """
    if not override.fields:
        return obj
    return obj.model_copy(update=dict(override.fields))


####################################################################
#
def composed_children(archive: Archive, trip_key: str) -> list[Child]:
    """
    One trip's objects as they will be uploaded, corrections laid over.

    Staging owns the files under a trip and a re-stage rewrites them, so
    a correction is never written back to them: it lives in the trip's
    override file and is laid over on the way out.  The staged object on
    disk is therefore the *uncorrected* one, and anything asking what a
    trip still lacks has to ask this rather than the disk -- otherwise a
    field an earlier correction already filled reads as a gap again.

    Additions are included.  An addition is as much a part of the trip as
    a parsed record and carries the same gaps.

    Absorption is not.  An absorbed trip uploads as part of the trip
    absorbing it, but it remains its own staged trip with its own report
    and its own overrides, and a correction against it belongs to it.

    Args:
        archive: The archive holding the staged trip.
        trip_key: Key of the trip to read.

    Returns:
        The trip's own objects and its additions, corrected.
    """
    fixes = corrections(archive, trip_key)
    objects = [
        corrected(obj, fixes[str(obj.internal_identifier)])
        if str(obj.internal_identifier) in fixes
        else obj
        for obj in staged_children(archive, trip_key)
    ]
    objects.extend(added(archive, trip_key))
    return objects


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
    the target already has is left out, and so is one the target holds
    twice itself.  Identifiers cannot catch either: they are derived per
    record, so one journey written down twice mints two of them and both
    would be created.

    Args:
        archive: The archive holding the staged trips.
        trip_key: Key of the trip being uploaded.

    Returns:
        The objects to upload, and how many duplicates were left out.
    """
    own = composed_children(archive, trip_key)

    duplicates = 0
    seen: set[tuple[str, str, str]] = set()
    objects: list[Child] = []

    # A trip is deduplicated against itself as well as against what it
    # absorbs.  One source can hold the same record twice -- the same
    # train, the same minute, the same label, booked twice or shared in
    # twice -- and two objects alike in all three are alike to a reader
    # of the app too, so carrying both puts a duplicate on the itinerary
    # rather than recording anything the first does not.
    #
    for obj in own:
        mark = _fingerprint(obj)
        if mark in seen:
            duplicates += 1
            continue
        seen.add(mark)
        objects.append(obj)

    for absorbed in absorbed_by(archive, trip_key):
        others = corrections(archive, absorbed)
        for obj in [
            *staged_children(archive, absorbed),
            *added(archive, absorbed),
        ]:
            key = str(obj.internal_identifier)
            if key in others:
                obj = corrected(obj, others[key])
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


########################################################################
########################################################################
#
@dataclass(frozen=True)
class Discrepancy:
    """One way an uploaded object differs from what was planned."""

    identifier: str
    name: str
    field: str
    planned: Any
    found: Any


########################################################################
########################################################################
#
@dataclass
class TripCheck:
    """What reading one uploaded trip back found."""

    trip_key: str
    name: str
    trip_id: int | None = None
    planned: int = 0

    # Objects the plan asked for and the account holds.  Counted apart
    # from `extra`, because an object added in the app is not one of the
    # planned ones arriving: reading '2 of 1 objects' helps nobody.
    #
    matched: int = 0

    # Planned objects the account does not hold.  Either the run stopped
    # part way or a create was refused.
    #
    missing: list[str] = field(default_factory=list)

    # Objects the account holds that the plan does not.  Added in the app
    # after the upload, which is a normal thing to find.
    #
    extra: list[str] = field(default_factory=list)

    differing: list[Discrepancy] = field(default_factory=list)

    # Objects carrying an address that Tripsy has not resolved to a
    # position.  Geocoding is asynchronous, so a fresh upload reads as
    # unplaced for a while and is worth checking again rather than
    # correcting.
    #
    unplaced: list[str] = field(default_factory=list)

    ####################################################################
    #
    @property
    def agrees(self) -> bool:
        """Whether the account matches the plan in every checked respect."""
        return not (self.missing or self.differing)


####################################################################
#
def verify_trip(
    client: TripsyClient, archive: Archive, trip_key: str
) -> TripCheck:
    """
    Read one uploaded trip back and compare it against its plan.

    The plan is the same computation the upload ran, so a difference is
    either something the API refused, something the app changed, or
    something a person edited afterwards.  Nothing is written: this says
    what is there, and correcting it is a separate decision.

    `sort_order` is worth the comparison on its own.  It is assigned on
    first create and a re-run does not revisit it, so a trip that gained
    an object later reads in the wrong order until it is repaired.

    Args:
        client: An authenticated client.
        archive: The archive holding the staged trip.
        trip_key: Key of the trip to check.

    Returns:
        What the comparison found.
    """
    plan = plan_trip(archive, trip_key)
    check = TripCheck(trip_key=trip_key, name=plan.name, planned=plan.total)

    trip_id = client.trip_ids_by_identifier().get(plan.identifier)
    if trip_id is None:
        check.missing = [obj.identifier for obj in plan.objects]
        return check
    check.trip_id = trip_id

    wanted = {obj.identifier: obj for obj in plan.objects}
    seen: set[str] = set()
    for collection in COLLECTION_ORDER:
        for found in client.iter_children(trip_id, collection):
            identifier = str(found.get("internal_identifier") or "")
            if not identifier or identifier not in wanted:
                check.extra.append(identifier or f"<unnamed {collection}>")
                continue

            seen.add(identifier)
            check.matched += 1
            planned = wanted[identifier]
            _compare(check, planned, found, collection)

    check.missing = sorted(set(wanted) - seen)
    return check


####################################################################
#
def _compare(
    check: TripCheck,
    planned: PlannedObject,
    found: dict[str, Any],
    collection: str,
) -> None:
    """Note every way one object differs from how it was planned."""
    if collection != planned.collection:
        check.differing.append(
            Discrepancy(
                planned.identifier,
                planned.name,
                "collection",
                planned.collection,
                collection,
            )
        )

    order = found.get("sort_order")
    if order != planned.sort_order:
        check.differing.append(
            Discrepancy(
                planned.identifier,
                planned.name,
                "sort_order",
                planned.sort_order,
                order,
            )
        )

    # Tripsy resolves an address to a position on its own, after the
    # object is created, so an address with no position is a thing to
    # look at again rather than a thing to correct.
    #
    for end in ("", "departure_", "arrival_"):
        if found.get(f"{end}address") and found.get(f"{end}latitude") is None:
            check.unplaced.append(f"{planned.name} ({end or 'the '}address)")


########################################################################
########################################################################
#
@dataclass(frozen=True)
class Unplaced:
    """One endpoint Tripsy holds an address for and no position."""

    collection: str
    child_id: int
    name: str
    address: str

    # '' for an activity or a hosting, which hold one place; 'departure_'
    # or 'arrival_' for the two ends of a leg.
    #
    prefix: str = ""

    ####################################################################
    #
    @property
    def where(self) -> str:
        """How this endpoint reads in a report."""
        end = self.prefix.rstrip("_")
        return f"{self.name} ({end})" if end else self.name


####################################################################
#
def unplaced_in(
    client: TripsyClient,
    trip_id: int,
    redo: Collection[str] = (),
) -> list[Unplaced]:
    """
    Every object on one uploaded trip carrying an address and no position.

    This asks Tripsy rather than the archive on purpose: what wants
    placing is whatever the app has not placed, which only Tripsy knows.
    An activity resolves itself once the app renders the trip, so running
    this after a look in the app leaves only the legs.

    An address in `redo` is returned even where it already has a
    position.  A geocoder that answered the wrong question answered
    confidently, and nothing would ever replace what it said: saying an
    address was wrong is the only way back.

    Args:
        client: An authenticated client.
        trip_id: The trip to look at.
        redo: Addresses to return whether or not they are placed.

    Returns:
        What wants placing, in no particular order.
    """
    again = set(redo)
    found: list[Unplaced] = []
    for collection in COLLECTION_ORDER:
        prefixes = (
            ("departure_", "arrival_")
            if collection == "transportations"
            else ("",)
        )
        for obj in client.iter_children(trip_id, collection):
            for prefix in prefixes:
                address = obj.get(f"{prefix}address")
                if not address:
                    continue
                if (
                    obj.get(f"{prefix}latitude") is not None
                    and str(address).strip() not in again
                ):
                    continue
                found.append(
                    Unplaced(
                        collection=collection,
                        child_id=int(obj["id"]),
                        name=str(obj.get("name") or "")
                        or str(obj.get(f"{prefix}description") or "")
                        or f"<{collection[:-1]} {obj['id']}>",
                        address=str(address).strip(),
                        prefix=prefix,
                    )
                )
    return found


####################################################################
#
def positions_in(
    client: TripsyClient, trip_id: int
) -> list[tuple[float, float]]:
    """
    Every position one uploaded trip already holds.

    These are what a new result is measured against: a geocoder does not
    fail by answering nothing, it fails by answering somewhere, and only
    the rest of the trip says whether somewhere is credible.

    Args:
        client: An authenticated client.
        trip_id: The trip to look at.

    Returns:
        Latitude and longitude pairs, in no particular order.
    """
    out: list[tuple[float, float]] = []
    for collection in COLLECTION_ORDER:
        prefixes = (
            ("departure_", "arrival_")
            if collection == "transportations"
            else ("",)
        )
        for obj in client.iter_children(trip_id, collection):
            for prefix in prefixes:
                lat = obj.get(f"{prefix}latitude")
                lon = obj.get(f"{prefix}longitude")
                if lat is not None and lon is not None:
                    out.append((float(lat), float(lon)))
    return out
