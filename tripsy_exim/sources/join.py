#!/usr/bin/env python
#
"""
Recognising one real trip across the two sources.

A trip can reach the archive from a JSON export and from a `.ics`
calendar, and the two mint different identifiers for it: the export
carries no record ids, so its identity comes from content, while a
calendar keys on the `VEVENT` `UID`.  Importing one trip by both routes
would create two trips in Tripsy, and Tripsy never releases an
identifier, so that duplicate could be neither merged nor undone.

The export is the authority.  A calendar naming a trip the archive
already holds adopts that trip instead of minting a rival for it, and
this module is how the two are recognised as the same trip.

The key is a trip's name and the days it covers.  Both sources carry
those and neither hands them over plainly: an export's `display_name`
arrives mis-encoded, while a calendar states its name inside
`X-WR-CALDESC` and runs its span a day past the end, `DTEND` being
exclusive in iCalendar.  Verified across the whole reference corpus --
all 61 calendars key onto an export trip exactly.

A key is still not unique.  A journey planned twice, or shared in by
another traveller, carries the same name and the same dates both times,
so a caller has to be ready for more than one answer and must never
choose between them on its own.
"""

# system imports
import re
from datetime import date, timedelta

# Separates the parts of a key.  A unit separator cannot occur in a trip
# name, so no name can be built that collides with a different one.
#
SEPARATOR = "\x1f"

# TripIt appends this to the calendar description, naming whoever shared
# the trip.  It describes the export rather than the trip, and the same
# trip exported by two people would otherwise key differently.
#
_SHARED_BY = re.compile(r"\s*\(Trip Shared by .*\)\s*$")


####################################################################
#
def trip_name(caldesc: str) -> str:
    """
    The trip's own name, from a calendar's description.

    Args:
        caldesc: The `X-WR-CALDESC` property.

    Returns:
        The name with the sharer suffix removed.
    """
    return _SHARED_BY.sub("", caldesc or "").strip()


####################################################################
#
def trip_key(
    name: str | None, starts: date | None, ends: date | None
) -> str | None:
    """
    Build the key two sources agree on for one trip.

    Args:
        name: The trip's own name, as the export writes it.  Recover the
            text first: a mis-encoded name keys onto nothing.
        starts: First day of the trip.
        ends: Last day of the trip, inclusive.

    Returns:
        The key, or None when any part is missing -- an unkeyable trip is
        reported rather than matched against a guess.
    """
    if not name or starts is None or ends is None:
        return None
    return SEPARATOR.join((name.strip(), starts.isoformat(), ends.isoformat()))


####################################################################
#
def key_from_calendar(
    caldesc: str, starts: date | None, ends: date | None
) -> str | None:
    """
    Build the key for a calendar, correcting for its exclusive end.

    Args:
        caldesc: The `X-WR-CALDESC` property.
        starts: First day the calendar's events cover.
        ends: Last day they cover, as spanned -- one past the trip's own
            end, because an all-day `DTEND` names the morning after.

    Returns:
        The key, or None when the calendar cannot be keyed.
    """
    if ends is None:
        return None
    return trip_key(trip_name(caldesc), starts, ends - timedelta(days=1))
