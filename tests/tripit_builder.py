#!/usr/bin/env python
#
"""
Generate synthetic TripIt-shaped GDPR export documents.

The reader is developed against these rather than against the real export
kept outside the repository, which is reference material only and
never committed.

The shape is copied from a structural survey of a real export; the content
is invented here.  What the real export actually contains, and therefore
what these builders reproduce:

- no identifier of any kind on a trip, an object or a segment
- objects with no type field: what a record is has to be read off the
  shape of its keys
- transport records holding one segment per leg
- `DateTime` mappings carrying a date, usually a time, an IANA zone and
  the offset that applied that day -- and sometimes only some of those
- mojibake: UTF-8 written out through cp1252

A generator that emitted only well-formed records would certify a reader
that falls over on the real data.
"""

# system imports
from datetime import date, timedelta
from typing import Any

# 3rd party imports
from faker import Faker

# The bytes cp1252 leaves undefined, which the export carries as raw
# latin-1.  Python's cp1252 codec raises on them, so encoding a
# has to be done a byte at a time.
#
_UNDEFINED = {0x81, 0x8D, 0x8F, 0x90, 0x9D}

# Place names carrying non-ASCII characters.  Faker's default locale is
# ASCII-only, and TripIt's encoding leaves ASCII alone -- so without
# these the generator would emit nothing needing recovery, and the
# untested.  The real export is full of them.
#
# Stamped into every generated export.  A real GDPR export cannot carry
# it, which is what lets the pre-commit hook tell a generated fixture
# from a real one -- the same job SYNTHETIC_PRODID does for .ics.
#
SYNTHETIC_MARKER = "x_synthetic"
SYNTHETIC_MARKER_VALUE = "tripsy-exim generated export"

NON_ASCII_PLACES = (
    "Kyōto",
    "München",
    "São Paulo",
    "Zürich",
    "Kraków",
    "Málaga",
    "日本",
)


####################################################################
#
def mojibake(text: str) -> str:
    """Encode text the way TripIt does: UTF-8 written through cp1252."""
    return "".join(
        chr(b) if b in _UNDEFINED else bytes([b]).decode("cp1252")
        for b in text.encode("utf-8")
    )


####################################################################
#
def moment(
    day: str,
    clock: str | None = "09:00:00",
    zone: str | None = "Asia/Tokyo",
    offset: str | None = "+09:00",
) -> dict[str, str]:
    """
    One `DateTime` mapping.

    Args:
        day: The `YYYY-MM-DD` date, the only part always present.
        clock: `HH:MM:SS`, or None for an all-day entry.
        zone: IANA name, or None where the export carried none.
        offset: `+09:00`, or None where the export carried none.

    Returns:
        The mapping, omitting whatever was passed as None.
    """
    built: dict[str, str] = {"date": day}
    if clock is not None:
        built["time"] = clock
    if zone is not None:
        built["timezone"] = zone
    if offset is not None:
        built["utc_offset"] = offset
    return built


####################################################################
#
def flight(
    *,
    name: str = "Flight",
    legs: int = 1,
    day: str = "2024-05-01",
    frm: str = "San Francisco",
    to: str = "Osaka",
) -> dict[str, Any]:
    """A flight record, with one segment per leg."""
    segments = [
        {
            "StartDateTime": moment(
                day, "11:20:00", "America/Los_Angeles", "-08:00"
            ),
            "EndDateTime": moment(day, "16:40:00"),
            "start_airport_code": frm[:3].upper(),
            "start_airport_name": f"{frm} International Airport",
            "start_airport_latitude": "37.615215",
            "start_airport_longitude": "-122.389881",
            "end_airport_code": to[:3].upper(),
            "end_airport_name": f"{to} International Airport",
            "end_airport_latitude": "34.435330",
            "end_airport_longitude": "135.243977",
            "marketing_airline": "Example Air",
            "marketing_flight_number": str(100 + index),
            "start_terminal": "I",
            "end_terminal": "1",
            "service_class": "Economy",
            "aircraft": "789",
        }
        for index in range(legs)
    ]
    return {"display_name": name, "is_purchased": "true", "Segment": segments}


####################################################################
#
def rail(
    *,
    name: str = "Train",
    day: str = "2024-05-02",
    train_number: str | None = "125",
    frm: str = "Shin-Osaka",
    to: str = "Okayama",
) -> dict[str, Any]:
    """A rail record.  The export often omits the train number."""
    segment: dict[str, Any] = {
        "StartDateTime": moment(day, "08:00:00"),
        "EndDateTime": moment(day, "10:30:00"),
        "StartStationAddress": {"address": f"{frm} Station"},
        "EndStationAddress": {"address": f"{to} Station"},
        "start_station_name": f"{frm} Station",
        "end_station_name": f"{to} Station",
        "carrier_name": "Example Rail",
        "coach_number": "4",
        "seats": "11A, 11B",
    }
    if train_number is not None:
        segment["train_number"] = train_number
    return {"display_name": name, "Segment": [segment]}


####################################################################
#
def ground(
    *,
    name: str = "Shuttle",
    day: str = "2024-05-02",
    frm: str = "Hotel",
    to: str = "Station",
) -> dict[str, Any]:
    """A ground transport record, typed by its code rather than its shape."""
    return {
        "display_name": name,
        "detail_type_code": "G",
        "Segment": [
            {
                "StartDateTime": moment(day, "07:00:00"),
                "EndDateTime": moment(day, "07:40:00"),
                "StartLocationAddress": {"address": frm},
                "EndLocationAddress": {"address": to},
                "carrier_name": "Example Coach",
            }
        ],
    }


####################################################################
#
def ferry(
    *,
    name: str = "Ferry",
    day: str = "2024-05-03",
    frm: str = "Kagoshima",
    to: str = "Sakurajima",
) -> dict[str, Any]:
    """A ferry record, typed by its code."""
    return {
        "display_name": name,
        "detail_type_code": "F",
        "Segment": [
            {
                "StartDateTime": moment(day, "09:00:00"),
                "EndDateTime": moment(day, "09:15:00"),
                "StartLocationAddress": {"address": f"{frm} Port"},
                "EndLocationAddress": {"address": f"{to} Port"},
                "carrier_name": "Example Ferry",
                "ship_name": "Example Maru",
            }
        ],
    }


####################################################################
#
def car_rental(
    *,
    name: str = "Example Car Rental",
    day: str = "2024-05-02",
    place: str = "Osaka",
) -> dict[str, Any]:
    """A car rental, recognisable by its driver rather than a segment."""
    return {
        "display_name": name,
        "Driver": {"first_name": "Pat", "last_name": "Example"},
        "StartDateTime": moment(day, "09:00:00"),
        "EndDateTime": moment(day, "18:00:00"),
        "StartLocationAddress": {"address": f"{place} Station"},
        "EndLocationAddress": {"address": f"{place} Station"},
        "start_location_name": f"{place} Branch",
        "car_type": "Compact",
    }


####################################################################
#
def untyped_transport(
    *,
    name: str = "Transportation",
    day: str = "2024-05-02",
    carrier: str = "Example Operator",
) -> dict[str, Any]:
    """
    A transport segment carrying nothing that says what kind it is.

    No airport code, no station, no type code, and an operator whose
    name settles nothing either.  A real export's untyped legs are
    mostly recognisable from the carrier -- a cable car, a city bus, a
    lake cruise -- so one that is not takes a name matching no rule.
    """
    return {
        "display_name": name,
        "Segment": [
            {
                "StartDateTime": moment(day, "11:00:00"),
                "EndDateTime": moment(day, "11:20:00"),
                "StartLocationAddress": {"address": "Lower Station"},
                "EndLocationAddress": {"address": "Upper Station"},
                "start_location_name": "Lower Station",
                "end_location_name": "Upper Station",
                "carrier_name": carrier,
            }
        ],
    }


####################################################################
#
def map_pin(
    *,
    place: str = "Santa Barbara, CA",
    day: str = "2024-05-02",
) -> dict[str, Any]:
    """
    A map pin, which TripIt names for you and files as an object.

    Carries a place and a time and nothing else -- the same shape as a
    plan somebody typed, which is why the name is what tells them apart.
    """
    return {
        "display_name": f"Map of {place}",
        "Address": {"address": place},
        "DateTime": moment(day, "16:48:00"),
    }


####################################################################
#
def lodging(
    *,
    name: str = "Example Hotel",
    arrive: str = "2024-05-01",
    depart: str = "2024-05-04",
    place: str = "Osaka",
) -> dict[str, Any]:
    """A lodging record, recognisable by its guest list."""
    return {
        "display_name": name,
        "StartDateTime": moment(arrive, "15:00:00"),
        "EndDateTime": moment(depart, "10:00:00"),
        "Address": {"address": f"1-1-1 Example, {place}"},
        "Guest": [{"first_name": "Pat", "last_name": "Example"}],
        "number_guests": "2",
        "room_type": "Twin",
        "supplier_phone": "+81 6-0000-0000",
        "supplier_url": "https://example.invalid/",
    }


####################################################################
#
def restaurant(
    *,
    name: str = "Example Kitchen",
    day: str = "2024-05-02",
    place: str = "Osaka",
) -> dict[str, Any]:
    """A restaurant record, recognisable by its cuisine."""
    return {
        "display_name": name,
        "DateTime": moment(day, "19:00:00"),
        "Address": {"address": f"2-2-2 Example, {place}"},
        "cuisine": "Japanese",
        "number_patrons": "2",
    }


####################################################################
#
def directions(*, day: str = "2024-05-02") -> dict[str, Any]:
    """A navigation artifact TripIt generated from its own map links."""
    return {
        "display_name": "Directions from A to B",
        "DateTime": moment(day, "12:00:00"),
        "StartAddress": {"address": "A"},
        "EndAddress": {"address": "B"},
    }


####################################################################
#
def activity(
    *,
    name: str = "Example Garden",
    day: str | None = "2024-05-03",
    code: str | None = None,
    all_day: bool = False,
    place: str = "Osaka",
) -> dict[str, Any]:
    """
    An untyped activity, which is most of a real export.

    Passing `day=None` builds a record carrying no date at all -- the
    export omits the containers rather than emptying them -- which is
    what sorts to the end of an imported trip.
    """
    built: dict[str, Any] = {
        "display_name": name,
        "Address": {"address": f"3-3-3 Example, {place}"},
        "supplier_url": "https://example.invalid/garden",
    }
    if day is not None:
        built["StartDateTime"] = moment(day, None if all_day else "10:00:00")
        built["EndDateTime"] = moment(day, None if all_day else "12:00:00")
    if code is not None:
        built["detail_type_code"] = code
    return built


####################################################################
#
def trip(
    *,
    name: str = "Osaka, Japan, May 2024",
    start: str = "2024-05-01",
    end: str = "2024-05-04",
    objects: list[dict[str, Any]] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """One entry of the export's `Trips` list."""
    data: dict[str, Any] = {
        "start_date": start,
        "end_date": end,
        "display_name": name,
        "image_url": "https://example.invalid/osaka.jpg",
        "is_private": "false",
    }
    if description is not None:
        data["description"] = description
    return {"TripData": data, "Objects": objects if objects is not None else []}


####################################################################
#
def export(*trips: dict[str, Any]) -> dict[str, Any]:
    """A whole export document wrapped around these trips."""
    return {
        SYNTHETIC_MARKER: SYNTHETIC_MARKER_VALUE,
        "screen_name": "example",
        "first_name": "Pat",
        "last_name": "Example",
        "Trips": list(trips),
        "UnfiledItems": [],
    }


####################################################################
#
def as_exported(faker: Faker, text: str, odds: int = 3) -> str:
    """
    Mojibake roughly one string in `odds`, as the real export does.

    Generating some of it rather than none is what keeps the recovery
    honest: a suite built only from clean text would pass just as well
    recovery would pass just as well with the recovery removed.
    """
    return mojibake(text) if faker.random_int(1, odds) == 1 else text


####################################################################
#
def itinerary(
    faker: Faker,
    *,
    start: date | None = None,
    excursions: int | None = None,
) -> dict[str, Any]:
    """
    Generate one trip shaped like a real one.

    Out from a home city by plane, train or road; a hotel, some meals and
    some sightseeing; up to three excursions to other places and back;
    then the journey home.  The content is invented and only has to be
    coherent enough to parse -- what matters is that the *shape* matches
    what TripIt exports, its encoding included.

    Args:
        faker: Supplies the invented text and the choices.
        start: First day of the trip.  One is generated when not given.
        excursions: How many side trips to make, 0 to 3 when not given.

    Returns:
        One entry for the export's `Trips` list.
    """
    home = faker.city()
    away = _place(faker)
    first = start or faker.date_between(start_date="-8y", end_date="+1y")
    hops = faker.random_int(0, 3) if excursions is None else excursions

    def day(offset: int) -> str:
        return (first + timedelta(days=offset)).isoformat()

    # Out, stay, and the meals and sightseeing that fill the days.
    #
    objects: list[dict[str, Any]] = [
        _leg(faker, name="Outbound", day=day(0), frm=home, to=away),
        lodging(
            name=as_exported(faker, f"{faker.company()} Hotel"),
            arrive=day(0),
            depart=day(hops + 2),
            place=away,
        ),
        restaurant(
            name=as_exported(faker, faker.company()), day=day(1), place=away
        ),
        activity(
            name=as_exported(faker, f"{faker.last_name()} Museum"),
            day=day(1),
            place=away,
        ),
        activity(name=faker.catch_phrase(), day=day(2), place=away, code="T"),
    ]

    # Each excursion goes somewhere and comes back the same way.
    #
    for hop in range(hops):
        side = _place(faker)
        objects.append(
            _leg(faker, name="Excursion", day=day(2 + hop), frm=away, to=side)
        )
        objects.append(
            activity(
                name=as_exported(faker, f"{side} walking tour"),
                day=day(2 + hop),
                place=side,
            )
        )
        objects.append(
            _leg(faker, name="Return", day=day(2 + hop), frm=side, to=away)
        )

    objects.append(
        _leg(faker, name="Homebound", day=day(hops + 2), frm=away, to=home)
    )

    return trip(
        name=as_exported(faker, f"{away}, {first.strftime('%B %Y')}"),
        start=day(0),
        end=day(hops + 2),
        objects=objects,
    )


####################################################################
#
def _place(faker: Faker) -> str:
    """A destination, half of them carrying non-ASCII characters."""
    if faker.random_int(1, 2) == 1:
        return str(faker.random_element(NON_ASCII_PLACES))
    return str(faker.city())


####################################################################
#
def _leg(
    faker: Faker, *, name: str, day: str, frm: str, to: str
) -> dict[str, Any]:
    """One journey between two places, by whichever mode is drawn."""
    mode = faker.random_element(("plane", "train", "road"))
    if mode == "plane":
        return flight(name=name, day=day, frm=frm, to=to)
    if mode == "train":
        return rail(name=name, day=day, frm=frm, to=to)
    return ground(name=name, day=day, frm=frm, to=to)


####################################################################
#
def random_export(faker: Faker, *, trips: int = 3) -> dict[str, Any]:
    """
    Generate a whole export holding this many generated trips.

    The first trip's name always goes through TripIt's encoding.
    meant some seeds produced an export with no mojibake in it at all,
    and a suite that happens to draw one of those would pass just as well
    recovery deleted.  Everything else is left to chance, which is
    what varies the shape between runs.

    Args:
        faker: Supplies the invented text and the choices.
        trips: How many trips to generate.

    Returns:
        A whole export document.
    """
    built = [itinerary(faker) for _ in range(trips)]
    if built:
        data = built[0]["TripData"]

        # Composed from text that has not been through TripIt's
        # encoding, rather than putting the generated name through it
        # again.  That one may have been through already, and twice
        # gives a string one pass cannot finish: reading one round
        # back leaves recovered characters beside unrecovered ones,
        # and recovery then declines to touch it.  A real export went
        # through once, so this reproduces the real case rather than
        # a worse one.
        #
        data["display_name"] = mojibake(f"Kyōto -- {data['start_date']}")
    return export(*built)
