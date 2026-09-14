#!/usr/bin/env python
#
"""
Positions for the objects Tripsy will not place itself.

Tripsy geocodes an Activity's address when the app renders it, and never
geocodes a Transportation endpoint at all -- measured across five address
shapes and five endpoint categories, on create and on update.  So a leg
pins only if it is given coordinates, and this is where they come from.

The lookup goes through `geopy`, which is the point: the service is a
choice that may change, and every geocoder behind that interface answers
the same way.  Nominatim is the default because it needs no account and
its terms allow keeping what it returns.

Nominatim's usage policy sets the shape of this module:

  * one request a second, absolutely, from a single thread
  * a User-Agent that names the application -- a library's default will
    be refused
  * `Results must be cached on your side.  Clients sending repeatedly
    the same query may be classified as faulty and blocked.`

That last one makes the cache a condition of use rather than a
convenience, which is why it is durable storage rather than `~/.cache`,
and why a lookup that found nothing is recorded too: asking again for an
address that has no answer is the behaviour the policy warns about.
"""

# system imports
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

# 3rd party imports
from geopy.distance import geodesic
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import get_geocoder_for_service

# Project imports
from tripsy_exim import __version__

# Where the cache lives, and how to move it.
#
CACHE_ENV = "TRIPSY_EXIM_GEOCODE_CACHE"

# Named rather than defaulted: a library's stock User-Agent is refused
# outright by Nominatim, and the policy asks for something that
# identifies the application.  The repository stands as the contact, so
# that no address of the operator's goes out in a header.
#
USER_AGENT = (
    f"tripsy-exim/{__version__} (+https://github.com/scanner/tripsy-exim)"
)

# One request a second is the stated absolute maximum, so the delay is a
# little over it rather than exactly on it.
#
MIN_DELAY_SECONDS = 1.1

# How far a result may sit from everything else on its trip before it is
# refused.  'Kyoto Station' resolves to El Dorado County, California --
# 8,900 km out -- while a legitimate leg of a road trip can be several
# hundred.  The gap between those is wide enough that the threshold does
# not have to be clever.
#
FAR_KM = 2000.0

Position = tuple[float, float]

# A trailing telephone number.  A booking that carries one finds
# nothing; the same hotel spelled without it resolves.
#
_PHONE = re.compile(r"[\s,]*\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\s*$")

# A ZIP+4 written without its hyphen -- '968195299' -- which reads as
# neither a postcode nor anything else.  The five-digit part is what a
# geocoder wants either way, so the rest goes.
#
_ZIP_PLUS_FOUR = re.compile(r"\b(\d{5})-?\d{4}\b")

# Space before a comma, and runs of commas left by a field the export
# had nothing for: 'Honolulu , Hawaii , 96815 , USA'.
#
_LOOSE_COMMA = re.compile(r"\s*,(?:\s*,)*\s*")


####################################################################
#
def normalised(address: str) -> str:
    """
    One address in the form a geocoder has the best chance with.

    The export writes the same place several ways -- one booking per
    traveller, punctuated differently, sometimes with a phone number
    stuck on the end.  Three of thirteen lookups found nothing purely
    because of that, and duplicates cost a request each.

    Nothing is invented: this only removes what was never part of the
    address.

    Args:
        address: The address as the export or the app wrote it.

    Returns:
        The address to ask about, and to key the cache by.
    """
    text = " ".join(address.replace("\n", ", ").split())
    text = _PHONE.sub("", text)
    text = _ZIP_PLUS_FOUR.sub(r"\1", text)
    text = _LOOSE_COMMA.sub(", ", text)
    return text.strip(" ,")


########################################################################
########################################################################
#
# What a result was matched as, where the service says.  An answer for a
# town that comes back as an administrative boundary has matched the
# county the town is in: 'Elko, NV' answers with Elko County, 54 km from
# Elko, which no distance check against the rest of a trip will catch.
#
COARSE_KINDS = frozenset({"boundary", "administrative"})


@dataclass(frozen=True)
class Found:
    """What a lookup returned, or that it returned nothing."""

    address: str
    position: Position | None
    label: str | None = None
    service: str | None = None

    # 'class:type' as the service reported it, e.g. 'place:town' or
    # 'boundary:administrative'.  None when the service says nothing.
    #
    kind: str | None = None

    ####################################################################
    #
    @property
    def coarse(self) -> bool:
        """
        Whether this matched something larger than what was asked for.

        A county centroid is not a wrong answer so much as an answer to a
        different question, and it is the failure a distance check cannot
        see.  Reported rather than refused: somewhere in the right county
        beats nowhere, as long as nobody is misled about which it is.
        """
        parts = (self.kind or "").split(":")
        return any(part in COARSE_KINDS for part in parts)

    ####################################################################
    #
    @property
    def placed(self) -> bool:
        """Whether this lookup found somewhere."""
        return self.position is not None


####################################################################
#
def default_cache() -> Path:
    """
    Where lookups are remembered.

    Durable storage rather than a cache directory: keeping results is a
    condition of Nominatim's terms, and a directory whose contents a
    disk cleaner may remove is the wrong place for something compliance
    rests on.
    """
    named = os.environ.get(CACHE_ENV)
    if named:
        return Path(named).expanduser()
    root = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(root).expanduser() / "tripsy-exim" / "geocode.json"


########################################################################
########################################################################
#
class Lookup(Protocol):
    """Anything that can turn an address into a position."""

    def __call__(self, address: str) -> Found:
        """Look one address up."""
        ...


####################################################################
#
def _rekeyed(held: dict[str, Any]) -> dict[str, Any]:
    """
    Bring a cache written before normalisation into line with it.

    An entry already under its normalised key is kept as it stands,
    misses included: remembering that an address has no answer is what
    stops it being asked about again, which the terms require.

    An entry whose key changes is a different matter.  A place it found
    has not moved, so that answer carries over.  A failure does not: the
    normalised form is precisely the one that might resolve, and three
    of the first thirteen real lookups failed on punctuation alone.

    Args:
        held: The cache as it was written.

    Returns:
        The cache keyed the way lookups are now made.
    """
    out: dict[str, Any] = {}
    for address, entry in held.items():
        key = normalised(address)
        if key != address and entry.get("position") is None:
            continue
        out.setdefault(key, entry)
    return out


########################################################################
########################################################################
#
class Cache:
    """Every lookup ever made, kept between runs."""

    ####################################################################
    #
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_cache()
        self.entries: dict[str, dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0
        if self.path.is_file():
            held = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = _rekeyed(held)

    ####################################################################
    #
    def get(self, address: str) -> Found | None:
        """
        What was learned about this address before, if anything.

        A lookup that found nothing is remembered as such and answered
        from here, rather than asked again.
        """
        held = self.entries.get(address)
        if held is None:
            return None
        self.hits += 1
        position = held.get("position")
        return Found(
            address=address,
            position=(position[0], position[1]) if position else None,
            label=held.get("label"),
            service=held.get("service"),
            kind=held.get("kind"),
        )

    ####################################################################
    #
    def put(self, found: Found) -> None:
        """Remember what a lookup returned, including that it was nothing."""
        self.misses += 1
        self.entries[found.address] = {
            "position": list(found.position) if found.position else None,
            "label": found.label,
            "kind": found.kind,
            "service": found.service,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    ####################################################################
    #
    def forget(self, address: str) -> bool:
        """
        Drop what is remembered about one address, so it is asked again.

        The way to correct a result that was believed and should not have
        been: the answer is not wrong in the cache's terms, so nothing
        else would ever replace it.
        """
        return self.entries.pop(address, None) is not None

    ####################################################################
    #
    def save(self) -> Path:
        """Write the cache out, making its directory if it is not there."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                self.entries, indent=2, ensure_ascii=False, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )
        return self.path


####################################################################
#
def lookup_for(service: str = "nominatim", **kwargs: Any) -> Lookup:
    """
    Build a rate-limited lookup against one geocoding service.

    Args:
        service: A name `geopy` knows, e.g. 'nominatim' or 'opencage'.
        kwargs: Passed to the geocoder, e.g. `api_key`.

    Returns:
        A callable taking an address and answering what was found.
    """
    # geopy logs a full traceback at WARNING for every retry it rides
    # out.  A recovered timeout is not worth forty lines of stack, and
    # burying the run's own output under one is worse than saying
    # nothing: a failure that exhausts the retries still raises.
    #
    logging.getLogger("geopy").setLevel(logging.ERROR)

    builder = get_geocoder_for_service(service)
    if service == "nominatim":
        kwargs.setdefault("user_agent", USER_AGENT)
    geocoder = builder(**kwargs)

    # One thread, one request a second.  Both are the policy's words.
    #
    limited = RateLimiter(
        geocoder.geocode,
        min_delay_seconds=MIN_DELAY_SECONDS,
        max_retries=2,
        swallow_exceptions=False,
    )

    def look(address: str) -> Found:
        """Ask the service about one address."""
        answer = limited(address)
        if answer is None:
            return Found(address=address, position=None, service=service)
        raw = getattr(answer, "raw", None) or {}
        kind = ":".join(
            str(raw[name]) for name in ("class", "type") if raw.get(name)
        )
        return Found(
            address=address,
            position=(float(answer.latitude), float(answer.longitude)),
            label=str(answer.address),
            service=service,
            kind=kind or None,
        )

    return look


####################################################################
#
def placed(
    addresses: list[str], look: Lookup, cache: Cache
) -> dict[str, Found]:
    """
    Resolve every address, asking the service only about new ones.

    Args:
        addresses: The addresses wanted, in any order.
        look: What to ask when the cache does not know.
        cache: Where answers are kept.

    Returns:
        Address to what was found, including the ones found nowhere.
    """
    # Keyed by the normalised form, so two spellings of one hotel are one
    # request rather than two, and answered back under both.
    #
    out: dict[str, Found] = {}
    answers: dict[str, Found] = {}
    for address in sorted(set(addresses)):
        key = normalised(address)
        held = answers.get(key) or cache.get(key)
        if held is None:
            held = look(key)
            cache.put(held)
        answers[key] = held
        out[address] = held
    return out


####################################################################
#
def far_from(position: Position, references: list[Position]) -> float | None:
    """
    How far a position sits from the nearest thing already on its trip.

    Args:
        position: The position in question.
        references: Positions the trip already holds.

    Returns:
        Kilometres to the nearest reference, or None when there are none
        to compare against.
    """
    if not references:
        return None
    return min(float(geodesic(position, other).km) for other in references)


####################################################################
#
def plausible(
    position: Position, references: list[Position], limit: float = FAR_KM
) -> bool:
    """
    Whether a result is close enough to its trip to be believed.

    A geocoder does not fail by returning nothing; it fails by returning
    somewhere.  'Kyoto Station' answers with a point in California, which
    is indistinguishable from a good answer unless it is measured against
    what the trip already knows.

    Args:
        position: The position in question.
        references: Positions the trip already holds.
        limit: How many kilometres away is too far.

    Returns:
        Whether to believe it.  A trip with nothing placed has nothing to
        measure against, so its results are believed.
    """
    distance = far_from(position, references)
    return distance is None or distance <= limit
