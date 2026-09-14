#!/usr/bin/env python
#
"""
Checking an export trip against the calendar for the same trip.

The GDPR export owns a trip's identity, so a calendar for a trip the
archive already holds is refused rather than staged.  This is what the
calendar is good for instead.

Matching is by instant.  Both sources put the same events at the same
moments -- exactly, to the second -- so the instant is a far better key
here than any name, and names are used only to break ties.  A calendar
event matches an export object that starts *or* ends at that moment,
because a calendar splits a stay into check-in and check-out events where
an export keeps one object spanning both.  Of 1299 calendar events in the
reference corpus, 1294 match an export object.

What that is mostly worth is verification rather than data.  Probing the
live app settled why: Tripsy geocodes an address by itself, and draws the
pin from a stored position *in preference to* the address when one is
there.  An object that already carries a street address therefore needs
no coordinate from us, and giving it one replaces a pin the app placed
exactly with wherever the calendar's own geocoder landed.  So a
coordinate is offered only where the export left both the position and
the address empty -- which across the whole reference corpus is nowhere.

What is left is the comparison.  Where both sources place one thing, a
difference beyond `DIVERGENCE_KM` means the wrong event matched the wrong
object, and that is worth a person.

Nothing is overwritten in any case.  Enrichment fills a field the export
left empty and never replaces a value it supplied.
"""

# system imports
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Project imports
from tripsy_exim.models import Activity, Hosting, Transportation
from tripsy_exim.sources import ParsedCalendar, uuid_from_uid
from tripsy_exim.sync.overrides import OverrideSet

# The three kinds a trip's children can be.  They differ in which fields
# hold a place and a time, but every one carries an identifier and a
# name, which is what the matching needs.
#
type Child = Hosting | Activity | Transportation


# Which end of an export object a calendar event landed on.
#
START = "start"
END = "end"

# Where coordinates live, per kind.  A transportation splits them across
# the two ends of a leg; everything else holds one place.
#
# How far apart two sources may place one thing before it stops being
# explainable.  They routinely disagree a little: a calendar names a
# destination city where an export names its terminal, which runs to a
# median of 18km and reaches 63km for Tokyo Narita.  Beyond this a
# difference is no longer that -- it means the wrong event matched the
# wrong object, which is worth a person.
#
# Nothing in the reference corpus crosses it, and that is the point: it
# stays quiet until something is actually wrong.  The furthest genuine
# mismatch seen while developing this, two legs leaving at one instant
# and cross-matched, sat at 1269km.
#
DIVERGENCE_KM = 100.0

_WHOLE_COORDS = ("latitude", "longitude")
_DEPARTURE_COORDS = ("departure_latitude", "departure_longitude")
_ARRIVAL_COORDS = ("arrival_latitude", "arrival_longitude")


########################################################################
########################################################################
#
@dataclass(frozen=True)
class Match:
    """One calendar event resolved to one export object."""

    source_uuid: str
    target_uuid: str

    # Which end of the export object the instant hit.  A calendar's
    # check-out event lands on the end of the stay it belongs to.
    #
    endpoint: str

    # True when the two also agree on a name.  The instant does the
    # matching; this only says how much else lined up.
    #
    named: bool

    summary: str
    fields: dict[str, Any] = field(default_factory=dict)


########################################################################
########################################################################
#
@dataclass(frozen=True)
class _Resolution:
    """What looking for a calendar event's export object turned up."""

    target: Child | None = None
    endpoint: str = ""

    # True when the instant hit several objects and nothing separated
    # them.  Distinct from finding nothing: one wants a person, the
    # other wants nothing at all.
    #
    ambiguous: bool = False


########################################################################
########################################################################
#
@dataclass
class Enrichment:
    """What one calendar offers the export trip it describes."""

    overrides: OverrideSet
    matched: list[Match] = field(default_factory=list)

    # Calendar events that hit no export object at all.
    #
    unmatched: list[str] = field(default_factory=list)

    # Instants that hit more than one export object, with nothing to
    # separate them.  Left alone deliberately.
    #
    ambiguous: list[str] = field(default_factory=list)

    # Fields two calendar events would have filled differently.
    #
    conflicts: list[str] = field(default_factory=list)

    # Matches where the two sources place one thing further apart than a
    # city-against-terminal difference explains.  Nothing is corrected
    # either way -- the export's value stands -- but a match this far
    # out is likely the wrong one.
    #
    divergences: list[str] = field(default_factory=list)

    ####################################################################
    #
    @property
    def filled(self) -> int:
        """How many fields the export stands to gain."""
        return sum(
            len(entry.fields) for entry in self.overrides.entries.values()
        )


####################################################################
#
def enrich(target: ParsedCalendar, source: ParsedCalendar) -> Enrichment:
    """
    Work out what a calendar can add to the export trip it describes.

    Nothing is written.  The result is an `OverrideSet` the caller saves
    and applies through the usual path, so enrichment is a correction
    like any other -- laid over what was staged, reversible, and stored
    outside the trip directory.

    Args:
        target: The trip as the export produced it.  Its values win.
        source: The same trip as its calendar produced it.

    Returns:
        The corrections to apply, and what could not be resolved.
    """
    trip_uuid = uuid_from_uid(target.trip.internal_identifier or "") or ""
    result = Enrichment(overrides=OverrideSet(trip_uuid=trip_uuid))

    targets = _by_uuid(target)
    starts, ends = _by_instant(target)

    # Remembers which calendar event filled a field, so a second one
    # filling it differently is caught rather than silently applied last.
    #
    claimed: dict[tuple[str, str], tuple[str, Any]] = {}

    for obj, uuid, summary in _objects(source):
        moment = _starts_at(obj)
        if moment is None:
            result.unmatched.append(summary)
            continue

        found = _resolve(obj, moment, starts, ends)
        if found.ambiguous:
            result.ambiguous.append(f"{summary} at {moment.isoformat()}")
            continue
        if found.target is None:
            result.unmatched.append(summary)
            continue

        target_obj, endpoint = found.target, found.endpoint
        target_uuid = targets.get(target_obj.internal_identifier or "")
        if target_uuid is None:
            result.unmatched.append(summary)
            continue

        apart = _apart(target_obj, obj)
        if apart is not None and apart > DIVERGENCE_KM:
            result.divergences.append(
                f"{summary}: the two sources place this {apart:.0f}km apart"
            )

        fields = _gaps(target_obj, obj, endpoint)
        for name, value in dict(fields).items():
            key = (target_uuid, name)
            if key in claimed and claimed[key][1] != value:
                result.conflicts.append(
                    f"{name} on {targets.get(target_obj.internal_identifier or '')}"
                    f": {claimed[key][0]} and {summary} disagree"
                )
                fields.pop(name)
                result.overrides.entries[target_uuid].fields.pop(name, None)
                continue
            claimed[key] = (summary, value)

        if fields:
            result.overrides.correct(target_uuid, **fields)

        # A conflict can empty an entry that an earlier event filled.
        # Leaving it behind would report an object as corrected when
        # nothing is going to change.
        #
        entry = result.overrides.entries.get(target_uuid)
        if entry is not None and not entry.fields and entry.collection is None:
            del result.overrides.entries[target_uuid]

        result.matched.append(
            Match(
                source_uuid=uuid,
                target_uuid=target_uuid,
                endpoint=endpoint,
                named=_same_name(target_obj, obj),
                summary=summary,
                fields=fields,
            )
        )

    return result


####################################################################
#
def _resolve(
    obj: Child,
    moment: datetime,
    starts: dict[datetime, list[Child]],
    ends: dict[datetime, list[Child]],
) -> _Resolution:
    """
    Find the one export object this calendar event belongs to.

    A start is preferred over an end: most events begin the thing they
    describe, and only a check-out or a drop-off lands on the far end of
    an object something else already matched.

    Returns:
        The object and which end matched, or a resolution saying the
        instant was ambiguous or matched nothing.
    """
    for endpoint, index in ((START, starts), (END, ends)):
        candidates = index.get(moment) or []
        if not candidates:
            continue
        if len(candidates) == 1:
            return _Resolution(target=candidates[0], endpoint=endpoint)

        named = [c for c in candidates if _same_name(c, obj)]
        if len(named) == 1:
            return _Resolution(target=named[0], endpoint=endpoint)
        return _Resolution(ambiguous=True)
    return _Resolution()


####################################################################
#
def _gaps(target: Child, source: Child, endpoint: str) -> dict[str, Any]:
    """
    The coordinates the export lacks and the calendar can supply.

    Where they land depends on the kind and on which end matched.  An
    activity or a stay holds one place whichever end was hit -- a hotel
    is in the same spot at check-out as at check-in -- while a leg keeps
    its two ends apart, so a pick-up fills the departure and a drop-off
    the arrival.
    """
    point = _coordinates(source)
    if point is None:
        return {}

    # Tripsy resolves an address into a pin on its own, and prefers a
    # stored position over doing so.  Handing it a coordinate for
    # something it can already place therefore trades an exact pin for a
    # borrowed one, so the address is what decides whether to offer
    # anything at all.
    #
    if _addressed(target, endpoint):
        return {}

    if isinstance(target, Transportation):
        # A leg's own event names where it *ends* -- that is what a
        # calendar records -- so its coordinates stay on the arrival
        # however the instants lined up.  They name the destination city
        # rather than its terminal, so they place a leg within a few tens
        # of kilometres and no closer.  An activity or a stay names where
        # it is, and then which end matched says whether that is the
        # leg's start or its finish: a rental collected in one place and
        # dropped in another is the case that needs it.
        #
        if isinstance(source, Transportation):
            names = _ARRIVAL_COORDS
        else:
            names = _DEPARTURE_COORDS if endpoint == START else _ARRIVAL_COORDS
    else:
        names = _WHOLE_COORDS

    filled: dict[str, Any] = {}
    for name, value in zip(names, point, strict=True):
        if getattr(target, name, None) is None:
            filled[name] = value
    return filled


####################################################################
#
def _apart(target: Child, source: Child) -> float | None:
    """
    How far apart the two sources put one thing, in kilometres.

    Returns:
        The distance, or None when either side has no place to compare.
        Equirectangular rather than great-circle: at these distances the
        difference is far below what the answer is used for.
    """
    here = _coordinates(target)
    there = _coordinates(source)
    if here is None or there is None:
        return None

    mean_latitude = math.radians((here[0] + there[0]) / 2)
    return 111.0 * math.hypot(
        here[0] - there[0], (here[1] - there[1]) * math.cos(mean_latitude)
    )


####################################################################
#
def _addressed(target: Child, endpoint: str) -> bool:
    """
    Whether the export already said where this is, in words.

    A leg keeps an address per end, so only the end being filled counts:
    a rental with a collection address and no return address still wants
    the return one.

    Args:
        target: The export object being considered.
        endpoint: Which of its ends matched.

    Returns:
        True when the app has something to geocode.
    """
    if isinstance(target, Transportation):
        if endpoint == START:
            return bool(target.departure_address)
        return bool(target.arrival_address)
    return bool(target.address)


####################################################################
#
def _coordinates(obj: Child) -> tuple[float, float] | None:
    """The one point a source object carries, wherever it keeps it."""
    for names in (_WHOLE_COORDS, _ARRIVAL_COORDS, _DEPARTURE_COORDS):
        latitude = getattr(obj, names[0], None)
        longitude = getattr(obj, names[1], None)
        if latitude is not None and longitude is not None:
            return float(latitude), float(longitude)
    return None


####################################################################
#
def _starts_at(obj: Child) -> datetime | None:
    """When an object begins, whichever field its kind uses."""
    value = getattr(obj, "starts_at", None) or getattr(
        obj, "departure_at", None
    )
    return value if isinstance(value, datetime) else None


####################################################################
#
def _ends_at(obj: Child) -> datetime | None:
    """When an object finishes, whichever field its kind uses."""
    value = getattr(obj, "ends_at", None) or getattr(obj, "arrival_at", None)
    return value if isinstance(value, datetime) else None


####################################################################
#
def _same_name(left: Child, right: Child) -> bool:
    """Whether two objects agree on a name, ignoring surrounding space."""
    one = (getattr(left, "name", None) or "").strip()
    two = (getattr(right, "name", None) or "").strip()
    return bool(one) and one == two


####################################################################
#
def _objects(
    parsed: ParsedCalendar,
) -> list[tuple[Child, str, str]]:
    """Every child object, with its source uuid and a readable summary."""
    uuids: dict[str, tuple[str, str]] = {}
    for note in parsed.notes:
        token = uuid_from_uid(note.uid) or note.uid
        if note.identifier:
            uuids[note.identifier] = (token, note.summary)

    out: list[tuple[Child, str, str]] = []
    for obj in children(parsed):
        token, summary = uuids.get(
            obj.internal_identifier or "", ("", obj.name or "")
        )
        out.append((obj, token, summary or obj.name or ""))
    return out


####################################################################
#
def children(parsed: ParsedCalendar) -> list[Child]:
    """Every child object of a parsed trip, in one list."""
    out: list[Child] = []
    out.extend(parsed.hostings)
    out.extend(parsed.activities)
    out.extend(parsed.transportations)
    return out


####################################################################
#
def _by_uuid(parsed: ParsedCalendar) -> dict[str, str]:
    """Map each object's identifier to the source uuid it was minted from."""
    return {
        note.identifier: uuid_from_uid(note.uid) or note.uid
        for note in parsed.notes
        if note.identifier
    }


####################################################################
#
def _by_instant(
    parsed: ParsedCalendar,
) -> tuple[dict[datetime, list[Child]], dict[datetime, list[Child]]]:
    """Index a trip's objects by the instants they begin and end at."""
    starts: dict[datetime, list[Child]] = defaultdict(list)
    ends: dict[datetime, list[Child]] = defaultdict(list)
    for obj in children(parsed):
        moment = _starts_at(obj)
        if moment is not None:
            starts[moment].append(obj)
        closing = _ends_at(obj)
        if closing is not None:
            ends[closing].append(obj)
    return starts, ends
