#!/usr/bin/env python
#
"""
Filling what the archive can work out from itself.

An endpoint carrying an airport's code but no address and no position is
one the app has nothing to draw a pin from.  Usually the same airport
appears elsewhere in the same archive, already placed, because an export
places nearly everything it carries -- 518 of 522 endpoints across the
reference corpus.  The four it missed named codes the other segments
placed.  So the answer is already on disk, and no outside service has to
be asked for it.

Only exact keys.  An IATA code is three letters and nothing else, so two
endpoints naming one are naming one airport.  A station or a stop gives
its name instead -- 'Shin-Osaka Station' -- and free text is not a key:
two spellings of one station are two keys, and one spelling can be two
stations.  Matching on those would place things confidently and wrongly,
which is the failure this whole command is meant to avoid.

Two guards, and the first one is not this module's:

    the address guard   an endpoint with an address is left alone, and
                        the app geocodes it exactly.  Handing it a
                        borrowed coordinate replaces a precise pin with
                        an approximate one.  `backfill` only ever reports
                        endpoints with neither, so working from those is
                        the guard.

    the agreement guard one code observed in places far apart is a code
                        that names two places.  Taking the commonest
                        would answer confidently and wrongly, so the key
                        is refused and reported instead.

A refusal costs nothing: the endpoint stays open and a person sees it in
`backfill report`, which is where it was already.  A wrong placement is
silent and goes through a one-way upload.  So the guard errs tight, and
an archive holding both an export and calendars is expected to produce
some refusals -- a calendar's coordinates name a destination city rather
than its terminal, and can sit tens of kilometres from the export's.

The modal position, not the first: one odd record cannot move an
airport.  The agreement guard covers what the mode cannot, which is a
key that genuinely means two things.

What comes out is work-list rows, already filled in.  `infer` is the
same loop a person does by hand, so it writes the same rows and they are
applied by the same code -- which is what makes the two safe to run in
either order.
"""

# system imports
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

# 3rd party imports
from geopy.distance import geodesic

# Project imports
from tripsy_exim.store import Archive
from tripsy_exim.sync.backfill import UNPLACEABLE, Gap, gaps, places
from tripsy_exim.sync.importer import composed_children, staged_trip
from tripsy_exim.sync.worklist import row_for

# An exact key: three letters, nothing else.  Anything a person typed is
# not one, which is the whole of the rule.
#
_EXACT_KEY = re.compile(r"^[A-Z]{3}$")

# How far apart observations of one key may sit before the key is taken
# to name two places rather than one.
#
# Measured, against the two quantities it has to separate:
#
#   ~1.4km   the worst disagreement between published coordinates for
#            one airport -- terminal against centroid against runway
#   17.3km   SFO to OAK, the closest pair of airports that a reused
#            code could plausibly confuse
#
# Ten sits an order of magnitude above the first and well below the
# second.  An earlier draft used fifty, which was reasoned rather than
# measured and was wrong: every pair in the Bay Area -- SFO to OAK at
# 17km, OAK to SJC at 47km, SFO to SJC at 49km -- would have passed it.
#
# `--disagree-km` moves it, and a refusal says the distance it saw, so a
# threshold that is wrong for one archive reports itself rather than
# placing something quietly in the wrong city.
#
DISAGREE_KM = 10.0

# What a refusal says, so a reader can tell the two apart.  They are
# different problems: one wants a person, the other wants a look at the
# data.
#
NOTHING_PLACES_IT = "nothing else in the archive places this"
SEGMENTS_DISAGREE = "segments disagree about where this is"

Position = tuple[float, float]


########################################################################
########################################################################
#
@dataclass(frozen=True)
class Inference:
    """
    One unplaceable endpoint, and what the rest of the archive says.

    Either an answer or a reason there is none -- never both, and never
    neither.
    """

    gap: Gap
    key: str
    position: Position | None = None
    agreed: int = 0
    refusal: str = ""

    ####################################################################
    #
    @property
    def answered(self) -> bool:
        """Whether the archive could place this."""
        return self.position is not None


####################################################################
#
def exact_key(label: str | None) -> str | None:
    """
    The label, when it is a key two endpoints could share.

    Args:
        label: What the export called the place.

    Returns:
        The key, or None when the label is free text and matching on it
        would not be safe.
    """
    if not label:
        return None
    found = label.strip()
    return found if _EXACT_KEY.match(found) else None


####################################################################
#
def positions_by_key(
    archive: Archive, trip_keys: list[str]
) -> dict[str, list[Position]]:
    """
    Every position the archive holds, gathered by the key naming it.

    Placed endpoints are what this reads, across every trip given rather
    than only the one being filled: an airport a person flew through
    once unplaced was very likely placed on another trip.

    Args:
        archive: The archive holding the staged trips.
        trip_keys: Keys of the trips to read.

    Returns:
        Key to the positions observed for it, in no order.
    """
    found: dict[str, list[Position]] = defaultdict(list)
    for trip_key in trip_keys:
        for obj in composed_children(archive, trip_key):
            for place in places(obj):
                key = exact_key(place.label)
                if key is None:
                    continue
                if place.latitude is None or place.longitude is None:
                    continue
                found[key].append(
                    (float(place.latitude), float(place.longitude))
                )
    return dict(found)


####################################################################
#
def settle(
    observed: list[Position], disagree_km: float = DISAGREE_KM
) -> tuple[Position | None, int, str]:
    """
    What one key's observations amount to.

    Args:
        observed: Every position recorded for this key.
        disagree_km: How far apart they may sit and still be one place.

    Returns:
        The position, how many observations agreed on it, and a refusal
        -- which is empty when there is a position.
    """
    if not observed:
        return None, 0, NOTHING_PLACES_IT

    # The mode rather than the first, so one odd record cannot move an
    # airport.
    #
    ranked = Counter(observed).most_common()
    position, agreed = ranked[0]

    furthest = max(
        (float(geodesic(position, other).km) for other in observed),
        default=0.0,
    )
    if furthest > disagree_km:
        return None, 0, f"{SEGMENTS_DISAGREE} ({furthest:.0f}km apart)"

    return position, agreed, ""


####################################################################
#
def inferences(
    archive: Archive,
    trip_keys: list[str],
    disagree_km: float = DISAGREE_KM,
) -> list[Inference]:
    """
    What the archive can say about each endpoint it cannot place.

    Args:
        archive: The archive holding the staged trips.
        trip_keys: Keys of the trips to work over.
        disagree_km: How far apart one key's observations may sit.

    Returns:
        One entry per unplaceable endpoint carrying an exact key, each
        either answered or refused.  An endpoint whose label is free
        text is left out entirely -- there is nothing safe to say.
    """
    known = positions_by_key(archive, trip_keys)

    found: list[Inference] = []
    for trip_key in trip_keys:
        for gap in gaps(archive, trip_key):
            if gap.population != UNPLACEABLE:
                continue
            key = exact_key(gap.label)
            if key is None:
                continue

            position, agreed, refusal = settle(known.get(key, []), disagree_km)
            found.append(
                Inference(
                    gap=gap,
                    key=key,
                    position=position,
                    agreed=agreed,
                    refusal=refusal,
                )
            )
    return found


####################################################################
#
def filled_rows(
    archive: Archive, found: list[Inference]
) -> list[dict[str, Any]]:
    """
    The answered inferences, as work-list rows already filled in.

    The same rows `export` writes for a person, so the same code applies
    them -- which is what keeps `infer` and a hand-edited work-list safe
    to run in either order, and gives these rows the same validation and
    the same diff against what the object already holds.

    Args:
        archive: The archive holding the staged trips.
        found: Inferences, answered or not.  Refusals are left out.

    Returns:
        Rows ready for `apply_rows`.
    """
    names: dict[str, str] = {}
    objects: dict[str, dict[str, Any]] = {}

    rows: list[dict[str, Any]] = []
    for inference in found:
        if not inference.answered or inference.position is None:
            continue

        gap = inference.gap
        if gap.trip_key not in names:
            trip = staged_trip(archive, gap.trip_key)
            names[gap.trip_key] = str(getattr(trip, "name", "") or gap.trip_key)
            objects[gap.trip_key] = {
                str(obj.internal_identifier or ""): obj
                for obj in composed_children(archive, gap.trip_key)
            }

        row = row_for(
            gap, names[gap.trip_key], objects[gap.trip_key].get(gap.identifier)
        )
        prefix = f"{gap.endpoint}_" if gap.endpoint else ""
        row["fields"][f"{prefix}latitude"] = inference.position[0]
        row["fields"][f"{prefix}longitude"] = inference.position[1]
        rows.append(row)
    return rows
