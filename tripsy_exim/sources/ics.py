#!/usr/bin/env python
#
"""
Turning a TripIt-exported .ics calendar into canonical trip objects.

Pure parsing: no network, no API knowledge, no writes.  What comes back is
a trip, its child objects, and a note on every event saying what was
decided about it and why.

Three properties of the real export shape everything here, all measured
over 60 files and 1339 events rather than assumed:

- *No type information whatsoever.*  No `CATEGORIES`, no `X-` properties,
  free-text summaries.  What an event *is* has to be inferred from prose.
- *No timezone anywhere.*  Instants are UTC, a few are date-only or
  floating, and nothing carries a `TZID`.  Zones come from `GEO`.
- *UIDs are `[item-]<uuid>@tripit.com`.*  Identifiers are minted from the
  uuid alone, so a later import keyed on the same uuid corrects these
  objects instead of duplicating them.

Both rules read `SUMMARY` and `DESCRIPTION` together, which is not
optional: measured over the real export, every one of the 150 flight
events matches in `DESCRIPTION` and none of them in `SUMMARY`, and 6
lodging events match in `DESCRIPTION` alone.  `LOCATION` never decides
either rule.  Reading only summaries would miss every flight.

Classification is deliberately small.  A wide keyword vocabulary was tried
during the survey and left over half the events ambiguous or unmatched,
and swung 25 points on a single added word -- unstable rather than merely
imperfect.  Two high-precision rules replace it, and everything else
becomes an Activity, which is a valid container rather than a wrong
answer.  They classify about a quarter of real events confidently and
say so about the rest, which is the honest version of the same number.  A rule set with no long tail cannot be destabilised by adding to
the tail.

The classification an event gets is write-once in practice.  Tripsy scopes
duplicate suppression to one collection of one trip -- verified 2026-09-10
-- so re-importing an event that has been reclassified does not move it.
It creates a second object in the new collection and leaves the first
where it was, and no later run can tidy that up, because deleting a trip
does not release its identifier either.  That is why every event carries a
note: the report is meant to be read before an import runs, not after.
"""

# system imports
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 3rd party imports
from icalendar import Calendar, Component

# Project imports
from tripsy_exim.models import Activity, Hosting, Transportation, Trip, mint
from tripsy_exim.sources.timezones import zone_for

# Names the key an identifier is minted from, not the file it arrived in.
# The same TripIt uuid could reach us by another route -- the GDPR JSON
# export most obviously -- and an import that keys on it must mint the
# same identifier in order to correct these objects rather than duplicate
# them.  Changing this after the first import orphans everything already
# written.
#
TRIPIT_UID_NAMESPACE = "tripit-uid"

# Item events prefix the uuid; the trip-level event is the bare one.
#
ITEM_PREFIX = "item-"

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# The two rules that survived the survey.  Both describe wording TripIt
# generates itself rather than wording a traveller typed, which is what
# makes them precise.
#
_LODGING = re.compile(r"check[\s-]?(in|out)", re.IGNORECASE)
_FLIGHT = re.compile(r"\bflight\b", re.IGNORECASE)

# Properties this parser consumes.  Anything else on a VEVENT is retained
# verbatim rather than dropped, because a field we did not think to read
# is exactly what a later importer may need.
#
_CONSUMED = frozenset(
    {"UID", "SUMMARY", "DESCRIPTION", "LOCATION", "GEO", "DTSTART", "DTEND"}
)

HOSTING = "hosting"
ACTIVITY = "activity"
TRANSPORTATION = "transportation"


########################################################################
########################################################################
#
@dataclass(frozen=True)
class EventNote:
    """What was decided about one event, and on what grounds."""

    uid: str
    identifier: str
    kind: str
    summary: str

    # False when nothing matched and the event fell through to Activity.
    # These are the ones worth a human's attention before an import.
    #
    confident: bool

    reason: str
    timezone: str | None

    # 'geo', 'inherited', 'unloadable' or 'none'.  An inherited zone is a
    # guess from a neighbouring event and may be wrong across a border or
    # a flight.  'unloadable' means a zone was derived but this machine
    # could not load it, so the instants were read as UTC and may be out
    # by as much as half a day.
    #
    timezone_source: str


########################################################################
########################################################################
#
@dataclass
class ParsedCalendar:
    """One trip and everything parsed out of its calendar."""

    trip: Trip
    hostings: list[Hosting] = field(default_factory=list)
    activities: list[Activity] = field(default_factory=list)
    transportations: list[Transportation] = field(default_factory=list)
    notes: list[EventNote] = field(default_factory=list)

    ####################################################################
    #
    @property
    def unclassified(self) -> list[EventNote]:
        """Events that matched no rule and defaulted to an activity."""
        return [n for n in self.notes if not n.confident]

    ####################################################################
    #
    @property
    def guessed_timezones(self) -> list[EventNote]:
        """Events whose zone was inherited rather than derived from GEO."""
        return [n for n in self.notes if n.timezone_source == "inherited"]


####################################################################
#
def uuid_from_uid(uid: str) -> str | None:
    """
    Pull the uuid out of a TripIt UID.

    Both `<uuid>@tripit.com` and `item-<uuid>@tripit.com` yield the same
    uuid, and the domain is ignored -- keying on the whole string would
    make a synthetic fixture and a real export mint differently, and would
    break correction from any other source keyed on the same uuid.

    Args:
        uid: The raw UID property.

    Returns:
        The lowercased uuid, or None if the UID holds none.
    """
    found = _UUID.search(uid or "")
    return found.group(0).lower() if found else None


####################################################################
#
def is_trip_event(uid: str) -> bool:
    """True for the one calendar-level event rather than an itinerary item."""
    local = (uid or "").partition("@")[0]
    return not local.startswith(ITEM_PREFIX)


####################################################################
#
def classify(summary: str, description: str) -> tuple[str, bool, str]:
    """
    Decide what an event is from its prose.

    Ordered most specific first: a lodging event mentioning a flight
    number in its notes is still lodging.

    Args:
        summary: The SUMMARY property.
        description: The DESCRIPTION property.

    Returns:
        The kind, whether a rule actually matched, and a short reason
        fit to show a person reading the report.
    """
    text = f"{summary} {description}"

    if _LODGING.search(text):
        return HOSTING, True, "check-in/check-out wording"
    if _FLIGHT.search(text):
        return TRANSPORTATION, True, "flight wording"
    return ACTIVITY, False, "no rule matched; defaulted to activity"


####################################################################
#
def _text(event: Component, name: str) -> str:
    """One property as plain text, empty when absent."""
    value = event.get(name)
    return "" if value is None else str(value)


####################################################################
#
def _coordinates(event: Component) -> tuple[float, float] | None:
    """The GEO property as (latitude, longitude), if it carries one."""
    geo = event.get("GEO")
    latitude = getattr(geo, "latitude", None)
    longitude = getattr(geo, "longitude", None)
    if latitude is None or longitude is None:
        return None
    try:
        return float(latitude), float(longitude)
    except TypeError, ValueError:
        return None


####################################################################
#
def _as_utc(value: date | datetime, zone: ZoneInfo | None) -> datetime:
    """
    Turn any of the three date forms into a UTC instant.

    The canonical models reject naive datetimes, so every form has to
    land on an aware one.  Which zone a floating or date-only value is
    read in is the whole reason `GEO` is mined for a timezone: read as
    UTC, a floating morning in Tokyo lands on the previous evening.

    Args:
        value: A date for an all-day event, a datetime otherwise, and a
            naive datetime when the time floats.
        zone: The resolved zone to read a naive value in, or None to read
            it as UTC.  Resolving happens in `parse`, so that a zone that
            was named but could not be loaded is reported rather than
            quietly treated as UTC.

    Returns:
        An aware UTC datetime.
    """
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.combine(value, time.min)

    if moment.tzinfo is not None:
        return moment.astimezone(UTC)

    return moment.replace(tzinfo=zone or UTC).astimezone(UTC)


####################################################################
#
def _zoneinfo(zone: str | None) -> ZoneInfo | None:
    """
    Load a named zone.

    Args:
        zone: An IANA name, or None.

    Returns:
        The zone, or None when there was no name or the running system
        cannot load it -- a container with trimmed tzdata fails on names
        that are perfectly valid elsewhere.  The caller decides what to
        do about that; it is not turned into UTC here, because an instant
        read in the wrong zone is wrong by up to half a day and would
        otherwise look like a clean derivation in the report.
    """
    if not zone:
        return None
    try:
        return ZoneInfo(zone)
    except ZoneInfoNotFoundError, ValueError, OSError:
        return None


####################################################################
#
def _passthrough(event: Component) -> dict[str, Any]:
    """
    Everything on the event this parser did not read.

    Retained rather than dropped: TripIt records detail Tripsy has nowhere
    to put, and losing it on import is the exact failure this project
    exists in response to.
    """
    return {
        str(name): str(value)
        for name, value in event.property_items(recursive=False)
        if str(name).upper() not in _CONSUMED
        and str(name).upper() != "BEGIN"
        and str(name).upper() != "END"
    }


####################################################################
#
def _common(
    event: Component, identifier: str, zone: str | None
) -> dict[str, Any]:
    """Fields every kind of child object takes from a VEVENT alike."""
    fields: dict[str, Any] = {
        "internal_identifier": identifier,
        "name": _text(event, "SUMMARY") or None,
        "description": _text(event, "DESCRIPTION") or None,
        "address": _text(event, "LOCATION") or None,
        "timezone": zone,
    }
    point = _coordinates(event)
    if point is not None:
        fields["latitude"], fields["longitude"] = point
    return fields


####################################################################
#
def _build(
    kind: str,
    event: Component,
    identifier: str,
    zone: str | None,
    starts: datetime,
    ends: datetime,
    all_day: bool,
) -> Hosting | Activity | Transportation:
    """
    Build the canonical object for one classified event.

    Args:
        kind: One of the three module constants.
        event: The VEVENT.
        identifier: Its minted `internal_identifier`.
        zone: The IANA zone derived for it, if any.
        starts: DTSTART as a UTC instant.
        ends: DTEND as a UTC instant.
        all_day: Whether the source event was date-only.

    Returns:
        A canonical model carrying the unread properties in its
        passthrough container.
    """
    source = _passthrough(event)

    if kind == HOSTING:
        # `all_day` has no home on a Hosting, so a date-only lodging event
        # keeps only its midnight-to-midnight instants.  It is recorded in
        # the passthrough so nothing is silently lost.
        #
        if all_day:
            source["x_all_day"] = "TRUE"
        built: Hosting | Activity | Transportation = Hosting(
            starts_at=starts, ends_at=ends, **_common(event, identifier, zone)
        )
    elif kind == TRANSPORTATION:
        common = _common(event, identifier, zone)
        if all_day:
            source["x_all_day"] = "TRUE"

        # A VEVENT carries one LOCATION and one GEO, so only one end of a
        # leg can be filled.  Departure is the defensible half: it is where
        # the traveller is when the event begins, and it is what an
        # itinerary sorts by.
        #
        built = Transportation(
            internal_identifier=identifier,
            name=common["name"],
            description=common["description"],
            transportation_type="airplane",
            departure_at=starts,
            arrival_at=ends,
            departure_timezone=zone,
            departure_address=common["address"],
            departure_latitude=common.get("latitude"),
            departure_longitude=common.get("longitude"),
        )
    else:
        built = Activity(
            starts_at=starts,
            ends_at=ends,
            all_day=all_day,
            **_common(event, identifier, zone),
        )

    return built.with_source(**source) if source else built


####################################################################
#
def parse(text: str) -> ParsedCalendar:
    """
    Parse one TripIt-exported calendar into canonical objects.

    Args:
        text: The contents of a `.ics` file.

    Returns:
        The trip, its child objects, and a note per event.  Read
        `unclassified` before importing: those events matched no rule and
        were filed as activities, and Tripsy's duplicate suppression makes
        that hard to take back.

    Raises:
        ValueError: The text is not a parseable calendar.
    """
    calendar = Calendar.from_ical(text)
    events = [c for c in calendar.walk() if c.name == "VEVENT"]

    trip_event: Component | None = None
    items: list[Component] = []
    for event in events:
        if trip_event is None and is_trip_event(_text(event, "UID")):
            trip_event = event
        else:
            items.append(event)

    parsed = ParsedCalendar(trip=_build_trip(calendar, trip_event, items))

    zones = _zones_for(items)

    for event, (zone, zone_source) in zip(items, zones, strict=True):
        uid = _text(event, "UID")
        token = uuid_from_uid(uid)
        if token is None:
            # Nothing stable to key on, so importing it would duplicate on
            # every run.  Left out deliberately, and reported.
            #
            parsed.notes.append(
                EventNote(
                    uid=uid,
                    identifier="",
                    kind="",
                    summary=_text(event, "SUMMARY"),
                    confident=False,
                    reason="no uuid in UID; cannot mint a stable identifier",
                    timezone=None,
                    timezone_source="none",
                )
            )
            continue

        # Resolved here rather than inside the conversion, so a name
        # that will not load becomes a reported outcome instead of a
        # silent reading in UTC.
        #
        resolved = _zoneinfo(zone)
        if zone is not None and resolved is None:
            zone_source = "unloadable"

        starts_value = event.get("DTSTART").dt
        ends_property = event.get("DTEND")
        ends_value = (
            ends_property.dt if ends_property is not None else starts_value
        )
        all_day = not isinstance(starts_value, datetime)

        kind, confident, reason = classify(
            _text(event, "SUMMARY"), _text(event, "DESCRIPTION")
        )
        identifier = mint(TRIPIT_UID_NAMESPACE, token)
        built = _build(
            kind,
            event,
            identifier,
            zone,
            _as_utc(starts_value, resolved),
            _as_utc(ends_value, resolved),
            all_day,
        )

        if isinstance(built, Hosting):
            parsed.hostings.append(built)
        elif isinstance(built, Transportation):
            parsed.transportations.append(built)
        else:
            parsed.activities.append(built)

        parsed.notes.append(
            EventNote(
                uid=uid,
                identifier=identifier,
                kind=kind,
                summary=_text(event, "SUMMARY"),
                confident=confident,
                reason=reason,
                timezone=zone,
                timezone_source=zone_source,
            )
        )

    return parsed


####################################################################
#
def _zones_for(
    events: list[Component],
) -> list[tuple[str | None, str]]:
    """
    Derive a timezone for every event, filling the gaps from neighbours.

    About one event in ten carries no GEO, and in the real export those
    gaps cluster inside a trip rather than scattering across it -- so the
    nearest event that does have one is usually in the same place.  The
    nearest is searched in both directions, because a gap at the start of
    a file has no predecessor to inherit from and would otherwise be read
    as UTC.

    An inherited zone is a guess and is marked as one: it is wrong for an
    event that crossed a border, which is exactly what a flight does.

    Args:
        events: The itinerary events, in file order.

    Returns:
        One (zone, source) pair per event, with source 'geo', 'inherited'
        or 'none'.
    """
    derived: list[str | None] = []
    for event in events:
        point = _coordinates(event)
        derived.append(zone_for(*point) if point is not None else None)

    filled: list[tuple[str | None, str]] = []
    for index, zone in enumerate(derived):
        if zone is not None:
            filled.append((zone, "geo"))
            continue

        neighbour = _nearest(derived, index)
        filled.append((neighbour, "inherited") if neighbour else (None, "none"))
    return filled


####################################################################
#
def _nearest(zones: list[str | None], index: int) -> str | None:
    """The closest known zone to a position, looking behind then ahead."""
    for offset in range(1, len(zones) + 1):
        before = index - offset
        if before >= 0 and zones[before] is not None:
            return zones[before]
        after = index + offset
        if after < len(zones) and zones[after] is not None:
            return zones[after]
    return None


####################################################################
#
def _build_trip(
    calendar: Component,
    trip_event: Component | None,
    items: list[Component],
) -> Trip:
    """
    Build the trip envelope from calendar metadata and the event span.

    `starts_at` and `ends_at` are plain dates on a trip, so no timezone is
    needed for them -- which is just as well, since the trip-level event
    carries no GEO.

    Args:
        calendar: The VCALENDAR, for its X-WR- properties.
        trip_event: The one non-item event, when the file has one.
        items: The itinerary events, used for the span as a fallback.

    Returns:
        A trip, identified by the trip-level event's uuid when there is
        one.
    """
    name = _text(calendar, "X-WR-CALNAME") or None
    description = _text(calendar, "X-WR-CALDESC") or None

    identifier = None
    if trip_event is not None:
        token = uuid_from_uid(_text(trip_event, "UID"))
        if token is not None:
            identifier = mint(TRIPIT_UID_NAMESPACE, token)

    # The trip-level event usually encloses its items, but nothing in the
    # format guarantees it.  Spanning the union means an item outside a
    # short envelope widens the trip rather than being silently left
    # outside its own dates.
    #
    starts, ends = _span(
        ([trip_event] if trip_event is not None else []) + items
    )

    return Trip(
        internal_identifier=identifier,
        name=name,
        description=description,
        starts_at=starts,
        ends_at=ends,
        has_dates=starts is not None,
    )


####################################################################
#
def _span(events: list[Component]) -> tuple[date | None, date | None]:
    """The first and last calendar day a group of events covers."""
    days: list[date] = []
    for event in events:
        for name in ("DTSTART", "DTEND"):
            prop = event.get(name)
            if prop is None:
                continue
            value = prop.dt
            days.append(value.date() if isinstance(value, datetime) else value)
    if not days:
        return None, None
    return min(days), max(days)
