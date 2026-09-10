#!/usr/bin/env python
#
"""
Generate synthetic TripIt-shaped .ics calendars.

The parser is developed against these rather than against the real export
in from_tripit/, which is reference material only and never enters the
repository.

The shape is copied from a structural survey of a real export
(probe_scripts/probe_ics.py); the content is invented here.  Two fields
differ from the real thing on purpose -- PRODID and the UID domain -- so
that a real calendar can never pass as a generated one.  That is what the
guard test in test_no_personal_data.py checks, and it is why the parser
must key off the UUID rather than either of those.

What the real export actually contains, and therefore what the knobs
reproduce:

- exactly one all-day trip-level VEVENT whose UID is a bare UUID, plus
  item events prefixed `item-`
- no TZID anywhere: instants are UTC, with a handful date-only or floating
- GEO on about 91% of events, absent on the rest
- non-ASCII text in descriptions, locations and summaries

A generator that emitted only well-formed timed events with coordinates
would certify a parser that falls over on the real data.
"""

# system imports
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

# 3rd party imports
from faker import Faker
from icalendar import Calendar, Component, Event

# Deliberately not TripIt's own values.  A real export cannot match these,
# which is what makes the guard test able to tell them apart.
#
SYNTHETIC_PRODID = "-//tripsy-exim//synthetic test calendar//EN"
SYNTHETIC_UID_DOMAIN = "example.invalid"

# Public landmark coordinates, so a test can assert the timezone a parser
# derives from GEO.  Nothing here comes from anyone's itinerary.
#
LANDMARKS: tuple[tuple[str, float, float], ...] = (
    ("America/Los_Angeles", 37.6213, -122.3790),
    ("Asia/Tokyo", 35.6812, 139.7671),
    ("Asia/Hong_Kong", 22.3080, 113.9185),
    ("Europe/Rome", 41.9028, 12.4964),
    ("America/New_York", 40.7128, -74.0060),
)

# Invented strings that exercise the non-ASCII path.
#
NON_ASCII_SAMPLES: tuple[str, ...] = (
    "Kyōto Nishiki Café",
    "Ωμέγα Ταβέρνα",
    "Cañada Súper Mercado",
    "Zürich Hauptbahnhof Kiosk",
)


####################################################################
#
def synthetic_uid(*, item: bool = True) -> str:
    """
    Build a UID shaped like TripIt's.

    Trip-level events carry a bare UUID; item events prefix it with
    `item-`.  Both matter: the parser is expected to mint identifiers from
    the UUID alone, so that an import from the GDPR JSON later produces
    the same key and corrects these objects rather than duplicating them.

    Args:
        item: True for an item event, False for the trip-level event.

    Returns:
        A UID of the form `[item-]<uuid>@example.invalid`.
    """
    token = uuid.uuid4()
    prefix = "item-" if item else ""
    return f"{prefix}{token}@{SYNTHETIC_UID_DOMAIN}"


####################################################################
#
def build_event(
    faker: Faker,
    *,
    starts_at: datetime | date,
    ends_at: datetime | date,
    item: bool = True,
    with_geo: bool = True,
    non_ascii: bool = False,
    landmark: tuple[str, float, float] | None = None,
) -> Event:
    """
    Build one VEVENT carrying the properties the real export carries.

    Args:
        faker: Supplies the invented text.
        starts_at: A date for an all-day event, a datetime otherwise.  A
            naive datetime produces a floating time, as 4 real events have.
        ends_at: As starts_at.
        item: False for the trip-level event.
        with_geo: False to omit GEO, as 120 real events do.
        non_ascii: True to draw text from the non-ASCII samples.
        landmark: Coordinates to use, so a test can assert the derived
            timezone.  Chosen from LANDMARKS when not given.

    Returns:
        An icalendar Event.
    """
    event = Event()
    event.add("uid", synthetic_uid(item=item))
    event.add("dtstamp", datetime(2027, 1, 1, tzinfo=UTC))
    event.add("dtstart", starts_at)
    event.add("dtend", ends_at)

    if non_ascii:
        summary = faker.random_element(NON_ASCII_SAMPLES)
        location = faker.random_element(NON_ASCII_SAMPLES)
        description = (
            f"{faker.random_element(NON_ASCII_SAMPLES)} -- {faker.sentence()}"
        )
    else:
        summary = faker.catch_phrase()
        location = faker.street_address()
        description = faker.sentence()

    event.add("summary", summary)
    event.add("location", location)
    event.add("description", description)

    if with_geo:
        _, latitude, longitude = landmark or faker.random_element(LANDMARKS)
        event.add("geo", (latitude, longitude))
    return event


####################################################################
#
def build_calendar(
    faker: Faker,
    *,
    items: int = 5,
    missing_geo: int = 0,
    date_only: int = 0,
    floating: int = 0,
    non_ascii: int = 0,
    landmark: tuple[str, float, float] | None = None,
    start: date | None = None,
) -> Calendar:
    """
    Build a whole trip calendar, defects included.

    The counts are how many of the `items` events carry each defect, so a
    caller asks for what it wants to exercise rather than hoping a random
    draw produces it.

    Args:
        faker: Supplies the invented text.
        items: Item events, not counting the trip-level one.
        missing_geo: How many item events omit GEO.
        date_only: How many are all-day rather than timed.
        floating: How many carry a naive, floating time.
        non_ascii: How many draw non-ASCII text.
        landmark: Fix the coordinates, to assert a derived timezone.
        start: First day of the trip.  Defaults to a fixed date.

    Returns:
        An icalendar Calendar with one trip-level event and `items`
        item events.

    Raises:
        ValueError: If a defect count exceeds the number of items.
    """
    for name, count in (
        ("missing_geo", missing_geo),
        ("date_only", date_only),
        ("floating", floating),
        ("non_ascii", non_ascii),
    ):
        if count > items:
            raise ValueError(f"{name}={count} exceeds items={items}")

    first = start or date(2027, 6, 1)
    calendar = Calendar()
    calendar.add("prodid", SYNTHETIC_PRODID)
    calendar.add("version", "2.0")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", f"Trip to {faker.city()}")
    calendar.add("x-wr-caldesc", faker.sentence())

    # Exactly one all-day trip-level event, as every real file has.
    #
    calendar.add_component(
        build_event(
            faker,
            starts_at=first,
            ends_at=first + timedelta(days=items + 1),
            item=False,
            with_geo=False,
            landmark=landmark,
        )
    )

    for index in range(items):
        day = first + timedelta(days=index)
        if index < date_only:
            starts: Any = day
            ends: Any = day + timedelta(days=1)
        elif index < date_only + floating:
            starts = datetime(day.year, day.month, day.day, 9, 0)
            ends = datetime(day.year, day.month, day.day, 11, 0)
        else:
            starts = datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC)
            ends = datetime(day.year, day.month, day.day, 11, 0, tzinfo=UTC)

        calendar.add_component(
            build_event(
                faker,
                starts_at=starts,
                ends_at=ends,
                with_geo=index >= missing_geo,
                non_ascii=index < non_ascii,
                landmark=landmark,
            )
        )
    return calendar


####################################################################
#
def to_ics(calendar: Calendar) -> str:
    """Render a calendar to .ics text, as a file on disk would hold it."""
    rendered: bytes = calendar.to_ical()
    return rendered.decode("utf-8")


####################################################################
#
def events_of(text: str) -> list[Component]:
    """Parse .ics text and return its VEVENT components."""
    return [c for c in Calendar.from_ical(text).walk() if c.name == "VEVENT"]


####################################################################
#
def field(component: Component, name: str) -> Any:
    """
    Read one property, past icalendar's very wide value union.

    `__getitem__` is typed as a union of every vType the library knows,
    which mypy cannot narrow usefully at a call site.  Narrowing once here
    keeps the reading code readable.
    """
    return cast(Any, component[name])


####################################################################
#
def uid_of(event: Component) -> str:
    """The event's UID as text."""
    return str(field(event, "UID"))


####################################################################
#
def start_of(event: Component) -> date | datetime:
    """
    The event's DTSTART.

    A date for an all-day event, a datetime otherwise -- and a naive
    datetime when the time is floating.
    """
    value: date | datetime = field(event, "DTSTART").dt
    return value


####################################################################
#
def is_all_day(event: Component) -> bool:
    """True when DTSTART is a plain date rather than a datetime."""
    return not isinstance(start_of(event), datetime)


####################################################################
#
def is_floating(event: Component) -> bool:
    """True when DTSTART is a datetime with no timezone attached."""
    start = start_of(event)
    return isinstance(start, datetime) and start.tzinfo is None


####################################################################
#
def day_of(event: Component) -> date:
    """The calendar day an event starts on, whichever form it uses."""
    start = start_of(event)
    return start.date() if isinstance(start, datetime) else start
