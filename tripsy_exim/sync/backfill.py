#!/usr/bin/env python
#
"""
What a staged trip still needs a person for.

The archive supports three kinds of correction and the parser's report
already records where it was unsure, but nothing reads the two together.
This does.  It walks a trip as that trip will be uploaded and names what
is still open, so corrections are authored against a list rather than
against a directory of JSON files.

Four populations.  Three are the parser's own doubts, read out of the
report it wrote beside the trip:

    unclassified      no rule matched and it defaulted to an activity,
                      which a retype moves
    guessed_timezone  the zone was guessed rather than read, which a
                      correction settles
    skipped           no object was produced at all, which an addition
                      puts back

The fourth the parser does not know it has, and is the one worth
explaining:

    unplaceable       neither an address nor a position, so the app has
                      nothing to draw a pin from

Tripsy geocodes an address by itself and draws the pin from a stored
position in preference to doing so.  An object carrying an address
therefore needs no coordinates, and one carrying coordinates needs no
address -- only an object with neither is actually unplaced.  Counting
the two fields separately would call thousands of perfectly placed
objects incomplete.  `enrich` is where that behaviour was established.

Not to be confused with `Unplaced`, which `fix-locations` works from.
That is the opposite case and it lives on the other side of an upload:
an object Tripsy holds an *address* for and has not placed, which a
geocoder can answer.  An unplaceable object has no address to ask
about, so nothing can look it up and the answer has to come from the
archive itself or from a person.

Everything here reads a trip through `composed_children`, which is the
trip with its corrections already laid over.  A gap reported here is one
that survives what has already been authored, so a run after a
correction reports one fewer rather than repeating itself.
"""

# system imports
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

# Project imports
from tripsy_exim.models import Transportation, mint, namespace_of
from tripsy_exim.sources import uuid_from_uid
from tripsy_exim.store import Archive
from tripsy_exim.sync.importer import (
    Child,
    composed_children,
    staged_trip,
)
from tripsy_exim.sync.overrides import (
    MODEL_FOR_COLLECTION,
    load_overrides,
    source_uuids,
)
from tripsy_exim.sync.staging import REPORT_FILENAME

# The four things a trip can still be open about.  The first three are
# report sections; the fourth is computed from the objects themselves.
#
UNCLASSIFIED = "unclassified"
GUESSED_TIMEZONE = "guessed_timezone"
SKIPPED = "skipped"
UNPLACEABLE = "unplaceable"

POPULATIONS = (UNCLASSIFIED, GUESSED_TIMEZONE, SKIPPED, UNPLACEABLE)

# The two ends of a leg, named as the app names them.  A stay or an
# activity holds one place and carries no endpoint at all.
#
DEPARTURE = "departure"
ARRIVAL = "arrival"

# What kind of correction closes each population.
#
RETYPE = "retype"
CORRECT = "correct"
ADD = "add"

REMEDY_FOR: dict[str, str] = {
    UNCLASSIFIED: RETYPE,
    GUESSED_TIMEZONE: CORRECT,
    SKIPPED: ADD,
    UNPLACEABLE: CORRECT,
}

# Which report section each of the first three populations reads.
#
_SECTION_FOR: dict[str, str] = {
    UNCLASSIFIED: "unclassified",
    GUESSED_TIMEZONE: "guessed_timezones",
    SKIPPED: "skipped",
}


########################################################################
########################################################################
#
@dataclass(frozen=True)
class Place:
    """
    One place an object carries: where it is, or one end of a leg.

    A stay or an activity is somewhere; a leg starts one place and
    finishes another, and the two are filled independently -- a rental
    collected at an airport and dropped downtown has an address for one
    end and nothing for the other.
    """

    endpoint: str
    address: str | None
    latitude: float | None
    longitude: float | None
    label: str | None

    ####################################################################
    #
    @property
    def placeable(self) -> bool:
        """
        Whether the app has anything to draw a pin from.

        Either field is enough on its own: Tripsy geocodes an address
        when there is no position, and prefers the position when there
        is one.
        """
        return bool(self.address) or (
            self.latitude is not None and self.longitude is not None
        )


########################################################################
########################################################################
#
@dataclass(frozen=True)
class Gap:
    """
    One open question about one object.

    `uuid` is what a correction is keyed by, and is empty when the
    object is one no correction can reach -- a skipped record names the
    uuid it would be added under instead.
    """

    trip_key: str
    population: str
    uuid: str
    identifier: str
    collection: str
    summary: str
    endpoint: str = ""
    label: str = ""

    ####################################################################
    #
    @property
    def remedy(self) -> str:
        """The kind of correction that closes this gap."""
        return REMEDY_FOR[self.population]

    ####################################################################
    #
    @property
    def where(self) -> str:
        """
        What to call the place this gap is about, for a reader.

        A leg's two ends are separate gaps and have to read as such, or
        a work-list shows the same leg twice with nothing to tell the
        rows apart.
        """
        if not self.label:
            return self.summary
        if not self.endpoint:
            return self.label
        return f"{self.label} ({self.endpoint})"


####################################################################
#
def places(obj: Child) -> list[Place]:
    """
    Every place one object carries.

    Args:
        obj: A staged child object of any kind.

    Returns:
        One place for a stay or an activity, two for a leg.  The label
        is what the export called the place -- an airport's IATA code,
        a station's name -- which is what groups gaps that are really
        the same place seen several times.
    """
    if isinstance(obj, Transportation):
        return [
            Place(
                DEPARTURE,
                obj.departure_address,
                obj.departure_latitude,
                obj.departure_longitude,
                obj.departure_description,
            ),
            Place(
                ARRIVAL,
                obj.arrival_address,
                obj.arrival_latitude,
                obj.arrival_longitude,
                obj.arrival_description,
            ),
        ]
    return [Place("", obj.address, obj.latitude, obj.longitude, obj.name)]


####################################################################
#
def uuids_by_identifier(archive: Archive, trip_key: str) -> dict[str, str]:
    """
    Identifier to source uuid, for parsed objects and additions alike.

    `source_uuids` covers what the parser staged.  An addition is no
    file under the trip and no entry in its index, so it is absent from
    that map -- but it is minted from the trip's uuid and its own, which
    is enough to put it back.

    Args:
        archive: The archive holding the staged trip.
        trip_key: Key of the staged trip.

    Returns:
        Identifier to source uuid.  Empty when the trip has no report.
    """
    document = _report(archive, trip_key)
    if not document:
        return {}

    found = source_uuids(archive, trip_key)

    trip_uuid = document.get("trip_uuid")
    trip = staged_trip(archive, trip_key)
    namespace = namespace_of(trip.internal_identifier if trip else None)
    if not trip_uuid or namespace is None:
        return found

    for uuid in load_overrides(archive, str(trip_uuid)).additions:
        found[mint(namespace, str(trip_uuid), uuid)] = uuid
    return found


####################################################################
#
def gaps(archive: Archive, trip_key: str) -> list[Gap]:
    """
    Everything one staged trip is still open about.

    Args:
        archive: The archive holding the staged trip.
        trip_key: Key of the trip to survey.

    Returns:
        The trip's gaps, in population order.  A trip the parser was
        sure about and placed completely has none.
    """
    document = _report(archive, trip_key)
    if not document:
        return []

    index: dict[str, dict[str, str]] = document.get("index") or {}
    collections = {
        str(entry.get("identifier")): str(entry.get("collection", ""))
        for entry in index.values()
    }

    found: list[Gap] = [
        *_noted(trip_key, document, collections),
        *_unplaceable(archive, trip_key),
    ]
    return sorted(found, key=lambda g: (POPULATIONS.index(g.population),))


####################################################################
#
def by_population(found: list[Gap]) -> dict[str, list[Gap]]:
    """Gaps grouped by what kind of question they are, in report order."""
    grouped: dict[str, list[Gap]] = {name: [] for name in POPULATIONS}
    for gap in found:
        grouped[gap.population].append(gap)
    return {name: rows for name, rows in grouped.items() if rows}


####################################################################
#
def by_recurrence(found: list[Gap]) -> list[tuple[str, list[Gap]]]:
    """
    Gaps grouped by the place they name, commonest first.

    One airport or station appears across a whole corpus, so answering
    it once closes every gap that names it.  Ordering the work this way
    is what turns a list of hundreds into an afternoon: the handful of
    repeated places account for most of it, and the long tail of places
    seen once is what is left.

    Args:
        found: The gaps to group.  Any whose place has no label are
            grouped under their own summary, since nothing else joins
            them.

    Returns:
        Pairs of place and the gaps naming it, most-repeated first and
        alphabetical within a count so the order is stable.
    """
    grouped: dict[str, list[Gap]] = defaultdict(list)
    for gap in found:
        grouped[gap.label or gap.summary].append(gap)
    return sorted(grouped.items(), key=lambda row: (-len(row[1]), row[0]))


####################################################################
#
def _report(archive: Archive, trip_key: str) -> dict[str, Any]:
    """The report staging wrote beside a trip, empty when there is none."""
    path = archive.trip_dir(trip_key) / REPORT_FILENAME
    if not path.is_file():
        return {}
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


####################################################################
#
def _noted(
    trip_key: str, document: dict[str, Any], collections: dict[str, str]
) -> list[Gap]:
    """The three populations the parser recorded its own doubts as."""
    found: list[Gap] = []
    for population, section in _SECTION_FOR.items():
        for note in document.get(section) or []:
            identifier = str(note.get("identifier") or "")
            found.append(
                Gap(
                    trip_key=trip_key,
                    population=population,
                    uuid=str(uuid_from_uid(note.get("uid")) or ""),
                    identifier=identifier,
                    collection=collections.get(identifier, ""),
                    summary=str(note.get("summary") or ""),
                )
            )
    return found


####################################################################
#
def _unplaceable(archive: Archive, trip_key: str) -> list[Gap]:
    """Every end of every object the app has no way to put on a map."""
    uuids = uuids_by_identifier(archive, trip_key)
    collection_for = {
        model: name for name, model in MODEL_FOR_COLLECTION.items()
    }

    found: list[Gap] = []
    for obj in composed_children(archive, trip_key):
        identifier = str(obj.internal_identifier or "")
        for place in places(obj):
            if place.placeable:
                continue
            found.append(
                Gap(
                    trip_key=trip_key,
                    population=UNPLACEABLE,
                    uuid=uuids.get(identifier, ""),
                    identifier=identifier,
                    collection=collection_for.get(type(obj), ""),
                    summary=str(obj.name or ""),
                    endpoint=place.endpoint,
                    label=str(place.label or ""),
                )
            )
    return found
