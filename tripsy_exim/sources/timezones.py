#!/usr/bin/env python
#
"""
Deriving an IANA timezone from coordinates.

The TripIt export carries no timezone at all: every instant is UTC, a
handful are date-only or floating, and no event carries a `TZID`.  Tripsy
wants a zone beside each instant, so it has to come from somewhere, and
`GEO` is the only candidate in the file -- present on about 91% of real
events.

This is why a zone derived here is worth more than a default.  Without
one, a floating time is guessed at as UTC and can be a whole working day
out; with one, it is read in the place it happened.

The lookup table is large and slow to build, so one finder is made on
first use and kept.
"""

# system imports
from functools import lru_cache

# 3rd party imports
from timezonefinder import TimezoneFinder


####################################################################
#
@lru_cache(maxsize=1)
def _finder() -> TimezoneFinder:
    """The one finder, built on first use."""
    return TimezoneFinder()


####################################################################
#
def zone_for(latitude: float, longitude: float) -> str | None:
    """
    Name the timezone containing a point.

    Args:
        latitude: Degrees north, as `GEO` gives it.
        longitude: Degrees east.

    Returns:
        An IANA name such as 'Asia/Tokyo', or None when the coordinates
        are unusable.  A point at sea answers an `Etc/GMT+N` zone rather
        than None, which is correct and worth expecting: a flight's
        coordinates can easily land in open ocean.
    """
    try:
        # Narrowed rather than returned straight through: the library
        # ships no stubs, so its answer is Any wherever it is not
        # installed alongside the type checker.
        #
        found = _finder().timezone_at(lat=latitude, lng=longitude)
        return str(found) if found is not None else None
    except ValueError:
        # Out of range coordinates.  A file this malformed is not worth
        # failing a whole import over.
        #
        return None
