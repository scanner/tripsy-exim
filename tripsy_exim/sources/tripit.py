#!/usr/bin/env python
#
"""
Reading TripIt's GDPR JSON export into canonical trips.

The export is one document holding every trip on an account, so this
yields a list where the `.ics` parser yields one calendar.  Otherwise it
targets the same `ParsedCalendar`, and the archive stages either without
knowing which source produced it.

Two properties of the export shape everything here.

It carries no record identifiers -- no id, uid or record number on any
trip, object or segment.  Identity therefore has to be derived from
content, and the derived token is a uuid5 so that corrections key on it
exactly as they key on an `.ics` UID.  Content-derived identity is weaker
than a real id: editing a trip's name in TripIt and re-exporting mints a
new identifier, and Tripsy never releases the old one.

It types transport and little else.  Flights, rail, ferries, ground
transport, lodging, car rentals and restaurants are all recognisable from
the shape of a record, and everything else -- most of the corpus -- is an
undifferentiated activity that a person has to look at.  What the export
does settle is which journeys are rail, a question the `.ics` parser can
only answer by reading prose.
"""

# system imports
import json
from datetime import UTC, date, datetime, time, tzinfo
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

# Project imports
from tripsy_exim.models import Activity, Hosting, Transportation, Trip, mint
from tripsy_exim.sources.ics import (
    ACTIVITY,
    HOSTING,
    SKIPPED,
    TRANSPORTATION,
    EventNote,
    ParsedCalendar,
    _zoneinfo,
)
from tripsy_exim.sources.join import trip_key
from tripsy_exim.sources.text import repair_strings

# The namespace the JSON path mints into, kept apart from the `.ics`
# path's so the same trip reached by both routes cannot collide on one
# identifier.  Which of the two should win is an open decision; keeping
# them separable is what leaves it open.
#
TRIPIT_JSON_NAMESPACE = "tripit-json"

# Content tokens are uuid5 so that `uuid_from_uid` finds them and the
# overrides layer keys on them unchanged.  The seed is fixed forever:
# changing it re-keys every correction ever made against this source.
#
_RECORD_NAMESPACE = uuid5(NAMESPACE_URL, "https://tripsy-exim/tripit-json")

# TripIt's own type codes, on the segment or the object.  Only the ones
# observed in a real export are mapped; anything else is reported rather
# than guessed at.
#
_GROUND = "G"
_FERRY = "F"
_TOUR = "T"
_MEETING = "M"

# Tripsy's activity vocabulary is not extrapolable -- the app writes
# 'amusementPark' and 'roadtrip' alike -- so only values the app has been
# seen to write are used here.
#
_GENERAL = "general"
_RESTAURANT = "restaurant"
_TOUR_TYPE = "tour"

# Read off the app itself on 2026-09-12, by building one of each and
# reading the value back: 'airplane', 'bus', 'car', 'cruise', 'ferry',
# 'roadtrip', 'subway', 'train', 'transfer', 'walk'.
#
# Tripsy's own MCP server documents the set as airplane, bike, bus, car,
# roadtrip, cruise, ferry, motorcycle, train, walk -- which omits both
# 'subway' and 'transfer', and the app writes those, so the published
# list is not the whole of it.
#
_TRAIN = "train"
_CAR = "car"
_TRANSFER = "transfer"
_FERRY_TYPE = "ferry"
_ROADTRIP = "roadtrip"

# How TripIt names and shapes a map pin.  Observed across 66 of them in
# a real export, all alike.
#
_MAP_PREFIX = "Map of "
_MAP_SHAPE = frozenset({"Address", "DateTime", "display_name"})

# What TripIt calls a journey when it has nothing to call it.  These are
# replaced by the endpoints, which say more.
#
_TRIPIT_FLIGHT = "Flight"
_GENERIC_NAMES = frozenset(
    {
        "Flight",
        "Rail",
        "Train",
        "Transportation",
        "Ground Transportation",
        "Ferry",
    }
)

# Modes Tripsy has no type for.  The leg is filed as rail -- both run on
# a fixed guideway between two stations -- and the mode goes in the name,
# where the app shows it.  Swapping the type for a real one later leaves
# the name still true.
#
_MODE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("ropeway", "Ropeway"),
    ("cable car", "Funicular"),
)

# Carriers the export names but does not type.  Matched on the carrier,
# which is a field rather than a title somebody wrote, and listed as
# substrings because one operator runs several services.  Every one was
# read off a real export; nothing here is a guess about what might exist.
#
_CARRIER_TYPES: tuple[tuple[str, str], ...] = (
    ("ropeway", _TRAIN),
    ("cable car", _TRAIN),
    ("cruise", "cruise"),
    ("ferry", _FERRY_TYPE),
    ("bus", "bus"),
    ("shuttle", "bus"),
    ("express", _TRAIN),
    ("line", _TRAIN),
)

# Types whose endpoints are stations, stops, terminals or piers.  Tripsy
# calls that category publicTransport, and sets it on the endpoints of
# the legs its own app creates.
#
_PUBLIC_TRANSPORT = frozenset({_TRAIN, "subway", "bus", _FERRY_TYPE, "cruise"})

# Keys consumed off a trip record.  Everything else is retained.
#
_TRIP_CONSUMED = frozenset(
    {"start_date", "end_date", "display_name", "description"}
)


####################################################################
#
def load(text: str | bytes) -> dict[str, Any]:
    """
    Decode an export, repairing its text on the way in.

    This is the only sanctioned way to read the file.  The export is
    mojibake -- UTF-8 that was written out through cp1252 -- and a trip
    name is a join key, so loading it raw produces records that match
    nothing rather than records that merely look wrong.

    Args:
        text: The contents of the export.

    Returns:
        The decoded document with every string repaired.

    Raises:
        ValueError: The text is not JSON, or is not a JSON object.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"export is not valid JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise ValueError(
            f"export must be a JSON object, got {type(document).__name__}"
        )

    repaired = repair_strings(document)
    assert isinstance(repaired, dict)
    return repaired


####################################################################
#
def classify(obj: dict[str, Any]) -> tuple[str, str | None, bool, str]:
    """
    Decide what one exported object is.

    Ordered most specific first, and keyed on the shape of the record
    rather than on its prose: a record carrying `Segment` with an airport
    code is a flight whatever it calls itself.

    Args:
        obj: One entry from a trip's `Objects` list.

    Returns:
        The collection it belongs in, the Tripsy type value to set on it
        or None when that value is not known, whether the record was
        recognised at all, and a reason fit to show a person.
    """
    segments = _segments(obj)
    if segments:
        first = segments[0]
        code = obj.get("detail_type_code") or first.get("detail_type_code")

        if "start_airport_code" in first:
            return TRANSPORTATION, "airplane", True, "flight segment"
        if "start_station_name" in first or "StartStationAddress" in first:
            # Station endpoints are what make a leg rail.  Some records
            # name the stations and some only address them, and the
            # funiculars among them are rail the same way.
            #
            return TRANSPORTATION, _TRAIN, True, "rail segment"
        if code == _GROUND:
            # A booked point-to-point service, which is what the app calls
            # a transfer.  Its 'roadtrip' is a drive taken under one's own
            # steam, and these records carry a supplier.
            #
            return TRANSPORTATION, _TRANSFER, True, "ground transport (code G)"
        if code == _FERRY:
            return TRANSPORTATION, _FERRY_TYPE, True, "ferry (code F)"
        return (
            TRANSPORTATION,
            None,
            False,
            "transport segment carrying no type code",
        )

    if "Guest" in obj:
        return HOSTING, None, True, "lodging: carries a guest list"
    if "Driver" in obj:
        return TRANSPORTATION, _CAR, True, "car rental: carries a driver"
    if "cuisine" in obj:
        return ACTIVITY, _RESTAURANT, True, "restaurant: carries a cuisine"
    if "StartAddress" in obj and "EndAddress" in obj:
        # A planned way of getting between two places, which the app calls
        # a route and files as a transportation.  The record names no
        # travel mode, so the mode is assumed rather than read: 'roadtrip'
        # is the app's value for a drive, and these are TripIt's driving
        # directions.  Reported so the walks among them can be retyped.
        #
        return (
            TRANSPORTATION,
            _ROADTRIP,
            False,
            "directions between two addresses; travel mode assumed to be "
            "driving",
        )

    if _is_map(obj):
        # TripIt files a place you looked up as an object of its own,
        # named for you: "Map of <place> - <address>".  It is a pin, not
        # something done, and where it names lodging the trip already
        # carries that as a hosting.
        #
        # Named rather than shaped, which this module otherwise avoids.
        # The shape cannot separate them: 92 records carry exactly these
        # three keys and 26 are real plans somebody typed.  The prefix is
        # TripIt's own, written by the machine that made the record, so
        # it identifies as well as a type code would if there were one.
        #
        return (
            SKIPPED,
            None,
            True,
            "a map of a place rather than something done",
        )

    code = obj.get("detail_type_code")
    if code == _TOUR:
        return ACTIVITY, _TOUR_TYPE, True, "tour (code T)"
    if code == _MEETING:
        return ACTIVITY, _GENERAL, True, "meeting (code M)"

    return (
        ACTIVITY,
        _GENERAL,
        False,
        "no rule matched; defaulted to activity",
    )


####################################################################
#
def _is_fragment(source: dict[str, Any]) -> bool:
    """
    Whether a transport segment describes no journey at all.

    A leg has to happen somewhere or at some time; one naming neither is
    a fragment of its booking rather than a part of the travel.

    Args:
        source: One segment of a transport record.

    Returns:
        Whether the segment names neither a time nor a place.
    """
    timed = bool((source.get("StartDateTime") or {}).get("time"))
    placed = any(
        key in source
        for key in (
            "start_airport_code",
            "start_station_name",
            "StartStationAddress",
            "StartLocationAddress",
            "StartAddress",
        )
    )
    return not timed and not placed


####################################################################
#
def _is_map(obj: dict[str, Any]) -> bool:
    """
    Whether a record is one of TripIt's map pins.

    Both signals are required.  The name is how they are recognised and
    the shape is what keeps the rule narrow: a record carrying anything
    else is a booking of some kind, whatever it is called.

    Args:
        obj: One entry from a trip's `Objects` list.

    Returns:
        Whether this is a map pin.
    """
    name = str(obj.get("display_name") or "")
    return name.startswith(_MAP_PREFIX) and set(obj) == _MAP_SHAPE


####################################################################
#
def parse_export(
    document: dict[str, Any], namespace: str = TRIPIT_JSON_NAMESPACE
) -> list[ParsedCalendar]:
    """
    Parse a whole export into one `ParsedCalendar` per trip.

    Args:
        document: A decoded export.  `load` is the usual way to get one,
            but a document decoded any other way is repaired here too, so
            identity never depends on how the file was read.
        namespace: The identifier namespace to mint into.  A shaping run
            passes its own, so the objects it creates occupy a separate
            key space -- identifiers are never released once spent.

    Returns:
        One parsed trip per entry in `Trips`, in the order the export
        lists them.  Read `unclassified` on each before importing.

    Raises:
        ValueError: The document carries no `Trips` list, so it is not an
            export.
    """
    # Recovered here rather than trusted from the caller.  A document
    # loaded any other way still carries TripIt's encoding, and an
    # unrecovered trip name mints a different identifier -- which Tripsy
    # never releases, so the divergence cannot be undone afterwards.
    # Recovery is idempotent, so paying for it twice after `load` costs
    # only a walk.
    #
    repaired = repair_strings(document)
    assert isinstance(repaired, dict)

    # An export carries a `Trips` list whether or not the account has any
    # trips in it.  A document without one is a different JSON file, and
    # parsing it as an empty account reads as a run that imported nothing
    # successfully.
    #
    if "Trips" not in repaired:
        raise ValueError(
            "no 'Trips' list in the document, so it is not a TripIt export"
        )
    trips = repaired["Trips"] or []

    # Two trips can carry the same name and the same dates -- a journey
    # planned twice, or shared in by another traveller.  Without a
    # tiebreak they mint one identifier, the second POST answers an empty
    # 200, and the trip vanishes without an error.  Position in the
    # export is the tiebreak: stable across re-parses of one file.
    #
    seen: dict[tuple[str, ...], int] = {}

    parsed: list[ParsedCalendar] = []
    for record in trips:
        if not isinstance(record, dict):
            continue
        parsed.append(_parse_trip(record, namespace, seen))
    return parsed


####################################################################
#
def _parse_trip(
    record: dict[str, Any],
    namespace: str,
    seen: dict[tuple[str, ...], int],
) -> ParsedCalendar:
    """Parse one entry of the export's `Trips` list."""
    data = record.get("TripData") or {}
    name = str(data.get("display_name") or "")
    starts = _plain_date(data.get("start_date"))
    ends = _plain_date(data.get("end_date"))

    parts = ("trip", name, str(starts), str(ends))
    token = _token(parts, seen)
    identifier = mint(namespace, token)

    trip = Trip(
        internal_identifier=identifier,
        name=name or None,
        starts_at=starts,
        ends_at=ends,
        has_dates=starts is not None,
        description=data.get("description") or None,
    )
    extras = {k: v for k, v in data.items() if k not in _TRIP_CONSUMED}
    if extras:
        trip = trip.with_source(**extras)

    # The export is the authority for identity, so its own name and
    # dates are the key the calendar side has to match.
    #
    parsed = ParsedCalendar(trip=trip, join_key=trip_key(name, starts, ends))

    # Children are disambiguated within their trip, not across the whole
    # export: the same hotel on two trips is two records.
    #
    within: dict[tuple[str, ...], int] = {}
    for obj in record.get("Objects") or []:
        if isinstance(obj, dict):
            _parse_object(obj, token, namespace, within, parsed)
    return parsed


####################################################################
#
def _parse_object(
    obj: dict[str, Any],
    trip_token: str,
    namespace: str,
    within: dict[tuple[str, ...], int],
    parsed: ParsedCalendar,
) -> None:
    """Build the canonical objects for one exported record."""
    kind, type_value, confident, reason = classify(obj)
    name = str(obj.get("display_name") or "")
    segments = _segments(obj)

    if kind == SKIPPED:
        parsed.notes.append(
            EventNote(
                uid=_token((trip_token, kind, name), within),
                identifier="",
                kind=SKIPPED,
                summary=name,
                confident=confident,
                reason=reason,
                timezone=None,
                timezone_source="none",
            )
        )
        return

    # A transport record holds a leg per segment, and each leg is its own
    # journey with its own endpoints.  Everything else is one object.
    #
    legs: list[dict[str, Any]] = segments if segments else [obj]
    for index, source in enumerate(legs):
        if segments and _is_fragment(source):
            # A booking can carry a segment that is not a journey: no
            # route, no times, only a seat already recorded on the leg it
            # belongs to.  Two of the corpus's 540 segments are these,
            # and both duplicate a seat the real leg already holds.
            #
            parsed.notes.append(
                EventNote(
                    uid=_token((trip_token, kind, name, str(index)), within),
                    identifier="",
                    kind=SKIPPED,
                    summary=f"{name} (segment {index})",
                    confident=True,
                    reason="transport segment naming neither a time nor a "
                    "place",
                    timezone=None,
                    timezone_source="none",
                )
            )
            continue

        starts, start_zone, all_day = _instant(
            source.get("StartDateTime") or source.get("DateTime")
        )
        ends, end_zone, _ = _instant(
            source.get("EndDateTime") or source.get("StartDateTime")
        )

        parts = (
            trip_token,
            kind,
            name,
            str(starts),
            str(index) if segments else "",
        )
        token = _token(parts, within)
        identifier = mint(namespace, token)

        built = _build(
            kind,
            type_value,
            obj,
            source,
            identifier,
            starts,
            ends,
            start_zone,
            end_zone,
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
                uid=token,
                identifier=identifier,
                kind=kind,
                summary=name,
                confident=confident,
                reason=reason,
                timezone=start_zone,
                timezone_source="source" if start_zone else "none",
            )
        )


####################################################################
#
def _carrier_of(source: dict[str, Any], obj: dict[str, Any]) -> str:
    """Who runs this leg, however the record names them."""
    return str(
        source.get("carrier_name")
        or obj.get("supplier_name")
        or source.get("marketing_airline")
        or ""
    )


####################################################################
#
def _type_from_carrier(carrier: str) -> str | None:
    """
    Work out a leg's type from who runs it.

    The shape settles most of the corpus; what is left is a cable car, a
    lake cruise, a city bus -- journeys the export records without ever
    saying what they are.  The operator's name is the only thing that
    does say, and it is a field of its own rather than a title somebody
    typed, so it is read here and nowhere else.

    Args:
        carrier: The operator's name.

    Returns:
        A Tripsy type, or None when the name settles nothing.
    """
    lowered = carrier.lower()
    for needle, value in _CARRIER_TYPES:
        if needle in lowered:
            return value
    return None


####################################################################
#
def _mode_prefix(carrier: str) -> str:
    """
    The mode to put in a leg's name, for modes Tripsy cannot draw.

    Args:
        carrier: The operator's name.

    Returns:
        'Ropeway', 'Funicular', or an empty string.
    """
    lowered = carrier.lower()
    for needle, mode in _MODE_PREFIXES:
        if needle in lowered:
            return mode
    return ""


####################################################################
#
def _leg_name(
    name: str | None,
    type_value: str | None,
    carrier: str,
    departure: str | None,
    arrival: str | None,
) -> str | None:
    """
    Settle what a leg is called.

    A flight is left unnamed: Tripsy resolves its airport codes and
    titles the row itself, better than anything written here -- "San Jose
    to Santa Barbara" from SJC and SBA.  It does that for flights only,
    so every other leg says where it ran, since TripIt's own word for one
    is "Rail" or "Transportation".

    Where the mode is one Tripsy cannot draw, the name carries it: a
    ropeway files as rail and reads "Ropeway: Owakudani to Sounzan", so
    the icon is honest and the row still says what it was.

    Args:
        name: What TripIt called it.
        type_value: The Tripsy type settled for it.
        carrier: The operator's name.
        departure: The departure endpoint's label, if any.
        arrival: The arrival endpoint's label, if any.

    Returns:
        The name to give the leg, or None to leave it unnamed.
    """
    if type_value == "airplane":
        return None

    mode = _mode_prefix(carrier)
    if name and name not in _GENERIC_NAMES and not mode:
        return name

    if departure and arrival:
        route = f"{departure} to {arrival}"
        return f"{mode}: {route}" if mode else route

    # No endpoints to name it by.  The operator says more than "Rail"
    # does, and for a ropeway it already says the mode.
    #
    return carrier or name


####################################################################
#
def _build(
    kind: str,
    type_value: str | None,
    obj: dict[str, Any],
    source: dict[str, Any],
    identifier: str,
    starts: datetime | None,
    ends: datetime | None,
    start_zone: str | None,
    end_zone: str | None,
    all_day: bool,
) -> Hosting | Activity | Transportation:
    """
    Build the canonical object for one classified record or leg.

    Args:
        kind: One of the three collection constants.
        type_value: The Tripsy type to set, or None when unknown.
        obj: The whole exported record, which carries the booking fields.
        source: The segment for a leg, or the record again otherwise.
        identifier: The minted `internal_identifier`.
        starts: Start instant in UTC, if the record carries one.
        ends: End instant in UTC.
        start_zone: IANA zone the start was expressed in.
        end_zone: IANA zone the end was expressed in.
        all_day: Whether the source carried a date with no time.

    Returns:
        A canonical model carrying everything unread in its passthrough.
    """
    extras = _unread(obj, source)
    name = str(obj.get("display_name") or "") or None

    notes = obj.get("notes") or obj.get("text") or None

    if kind == HOSTING:
        # Hosting has no `all_day`, so a date-only stay keeps its
        # midnight instants and records the fact where nothing is lost.
        # `ics.py` uses the same key; a second convention would mean the
        # review had to know which source it was reading.
        #
        if all_day:
            extras["x_all_day"] = "TRUE"
        built: Hosting | Activity | Transportation = Hosting(
            internal_identifier=identifier,
            name=name,
            starts_at=starts,
            ends_at=ends,
            timezone=start_zone,
            address=_address(obj, "Address"),
            phone=obj.get("supplier_phone") or None,
            website=obj.get("supplier_url") or None,
            room_type=obj.get("room_type") or None,
            notes=notes,
        )

    elif kind == TRANSPORTATION:
        if all_day:
            extras["x_all_day"] = "TRUE"

        carrier = _carrier_of(source, obj)
        type_value = type_value or _type_from_carrier(carrier)
        departure = _endpoint_label(source, "start")
        arrival = _endpoint_label(source, "end")

        # A station, stop, terminal or pier is public transport, which is
        # the category the app sets on the legs it creates itself.  A
        # flight is left alone -- its airports render without one -- and a
        # transfer's ends are a judgement the export cannot make.
        #
        place = "publicTransport" if type_value in _PUBLIC_TRANSPORT else None

        built = Transportation(
            internal_identifier=identifier,
            name=_leg_name(name, type_value, carrier, departure, arrival),
            transportation_type=type_value,
            departure_location_type=place,
            arrival_location_type=place,
            departure_at=starts,
            arrival_at=ends,
            departure_timezone=start_zone,
            arrival_timezone=end_zone,
            departure_address=_departure_address(source, obj),
            arrival_address=_arrival_address(source, obj),
            departure_latitude=_number(source.get("start_airport_latitude")),
            departure_longitude=_number(source.get("start_airport_longitude")),
            arrival_latitude=_number(source.get("end_airport_latitude")),
            arrival_longitude=_number(source.get("end_airport_longitude")),
            departure_description=departure,
            arrival_description=arrival,
            departure_terminal=source.get("start_terminal") or None,
            arrival_terminal=source.get("end_terminal") or None,
            departure_gate=source.get("start_gate") or None,
            arrival_gate=source.get("end_gate") or None,
            company=_company(source, obj),
            transport_number=_transport_number(source),
            coach_number=source.get("coach_number") or None,
            seat_number=source.get("seats") or None,
            seat_class=source.get("service_class") or None,
            vehicle_description=source.get("vehicle_description") or None,
            provider_url=obj.get("booking_site_url") or None,
            notes=notes,
        )

    else:
        built = Activity(
            internal_identifier=identifier,
            name=name,
            activity_type=type_value,
            starts_at=starts,
            ends_at=ends,
            all_day=all_day,
            timezone=start_zone,
            address=_address(obj, "Address", "StartAddress"),
            phone=obj.get("supplier_phone") or None,
            website=obj.get("supplier_url") or None,
            notes=notes,
        )

    return built.with_source(**extras) if extras else built


####################################################################
#
def _segments(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """The record's segments, as a list however the export shaped it."""
    segments = obj.get("Segment")
    if isinstance(segments, dict):
        return [segments]
    if isinstance(segments, list):
        return [s for s in segments if isinstance(s, dict)]
    return []


####################################################################
#
def _instant(
    value: Any,
) -> tuple[datetime | None, str | None, bool]:
    """
    Turn one exported DateTime into a UTC instant.

    The export gives a date, usually a time, and usually both an IANA
    zone and the offset that applied on the day.  The offset is preferred
    where it exists: it is what TripIt recorded at the time, so it stays
    correct even where a zone's rules have since changed.

    A record with no time is an all-day entry and is read as midnight in
    its own zone, which is the same reading `.ics` date-only events get.

    Args:
        value: A `DateTime` mapping, or anything else.

    Returns:
        The instant in UTC, the zone it was expressed in, and whether the
        source carried a date with no time.  The instant is None when
        there was no usable date.
    """
    if not isinstance(value, dict):
        return None, None, False

    day = _plain_date(value.get("date"))
    if day is None:
        return None, None, False

    zone = str(value.get("timezone") or "") or None
    clock = str(value.get("time") or "")
    all_day = not clock

    moment = datetime.combine(day, _clock(clock))

    offset = _offset(str(value.get("utc_offset") or ""))
    if offset is not None:
        return moment.replace(tzinfo=offset).astimezone(UTC), zone, all_day

    loaded: ZoneInfo | None = _zoneinfo(zone)
    return moment.replace(tzinfo=loaded or UTC).astimezone(UTC), zone, all_day


####################################################################
#
def _clock(value: str) -> time:
    """A `HH:MM:SS` time, midnight when the export carried none."""
    try:
        return time.fromisoformat(value) if value else time.min
    except ValueError:
        return time.min


####################################################################
#
def _offset(value: str) -> tzinfo | None:
    """The `+09:00` offset as a timezone, or None when unreadable."""
    if not value:
        return None
    try:
        return datetime.strptime(value.replace(":", ""), "%z").tzinfo
    except ValueError:
        return None


####################################################################
#
def _plain_date(value: Any) -> date | None:
    """A `YYYY-MM-DD` string as a date, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


####################################################################
#
def _token(parts: tuple[str, ...], seen: dict[tuple[str, ...], int]) -> str:
    """
    Mint the uuid5 that stands in for a record identifier.

    The export carries no ids, so identity is derived from content, and
    content is not guaranteed unique -- a journey planned twice carries
    the same name and dates both times.  An occurrence number
    disambiguates, counted in the order the export lists records so that
    re-parsing one file always assigns the same numbers.

    Args:
        parts: The values that identify this record.
        seen: Occurrence counts so far, mutated as a side effect.

    Returns:
        A uuid5 string, shaped so that `uuid_from_uid` finds it and
        corrections key on it exactly as on an `.ics` UID.
    """
    occurrence = seen.get(parts, 0)
    seen[parts] = occurrence + 1
    return str(uuid5(_RECORD_NAMESPACE, "\0".join((*parts, str(occurrence)))))


# Keys read off a record.  Everything else is retained verbatim: a field
# nobody thought to map is exactly what a later importer may want.
#
_OBJECT_CONSUMED = frozenset(
    {
        "display_name",
        "notes",
        "text",
        "Address",
        "StartAddress",
        "EndAddress",
        "Segment",
        "DateTime",
        "StartDateTime",
        "EndDateTime",
        "supplier_phone",
        "supplier_url",
        "room_type",
        "booking_site_url",
        "detail_type_code",
    }
)

_SEGMENT_CONSUMED = frozenset(
    {
        "DateTime",
        "StartDateTime",
        "EndDateTime",
        "StartStationAddress",
        "EndStationAddress",
        "StartLocationAddress",
        "EndLocationAddress",
        "start_airport_name",
        "end_airport_name",
        "start_airport_latitude",
        "start_airport_longitude",
        "end_airport_latitude",
        "end_airport_longitude",
        "start_station_name",
        "end_station_name",
        "start_terminal",
        "end_terminal",
        "start_gate",
        "end_gate",
        "coach_number",
        "seats",
        "service_class",
        "vehicle_description",
        "marketing_airline",
        "marketing_flight_number",
        "carrier_name",
        "train_number",
        "detail_type_code",
    }
)


####################################################################
#
def _unread(obj: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """
    Everything the mapping did not consume, kept for the archive.

    Segment fields are nested under one key rather than merged, so a
    record's own `display_name` is never shadowed by a leg's and the two
    stay tellable apart when a person reads the archive.
    """
    extras = {k: v for k, v in obj.items() if k not in _OBJECT_CONSUMED}
    if source is not obj:
        leg = {k: v for k, v in source.items() if k not in _SEGMENT_CONSUMED}
        if leg:
            extras["segment"] = leg
    return extras


####################################################################
#
def _address(obj: dict[str, Any], *names: str) -> str | None:
    """The first of these address containers that carries a string."""
    for name in names:
        value = obj.get(name)
        if isinstance(value, dict) and value.get("address"):
            return str(value["address"])
    return None


####################################################################
#
def _departure_address(
    source: dict[str, Any], obj: dict[str, Any]
) -> str | None:
    """Where a leg starts, however the export named the place."""
    return (
        _address(
            source,
            "StartStationAddress",
            "StartLocationAddress",
            "StartAddress",
        )
        or source.get("start_airport_name")
        or source.get("start_station_name")
        or _address(obj, "Address")
    )


####################################################################
#
def _arrival_address(source: dict[str, Any], obj: dict[str, Any]) -> str | None:
    """Where a leg ends, however the export named the place."""
    return (
        _address(
            source, "EndStationAddress", "EndLocationAddress", "EndAddress"
        )
        or source.get("end_airport_name")
        or source.get("end_station_name")
        or None
    )


####################################################################
#
def _endpoint_label(source: dict[str, Any], end: str) -> str | None:
    """
    What one end of a leg is called, as against where it is.

    The app titles a leg from these, so an airport wants its IATA code
    rather than its full name -- Tripsy's own guidance asks for the code
    outright.  A station or a stop has no code, so it gives its name.
    The address is a separate field and stays the address.

    Args:
        source: The segment, or the record when there are no segments.
        end: 'start' or 'end'.

    Returns:
        A short label for that end, or None when the source names none.
    """
    for key in (
        f"{end}_airport_code",
        f"{end}_station_name",
        f"{end}_location_name",
    ):
        value = source.get(key)
        if value:
            return str(value)
    return None


####################################################################
#
def _company(source: dict[str, Any], obj: dict[str, Any]) -> str | None:
    """Who operates the leg: the airline, the rail carrier, the agency."""
    return (
        source.get("marketing_airline")
        or source.get("carrier_name")
        or obj.get("supplier_name")
        or obj.get("booking_site_name")
        or None
    )


####################################################################
#
def _transport_number(source: dict[str, Any]) -> str | None:
    """The flight or train number, as text."""
    value = source.get("marketing_flight_number") or source.get("train_number")
    return str(value) if value else None


####################################################################
#
def _number(value: Any) -> float | None:
    """A coordinate the export wrote as a string."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None
