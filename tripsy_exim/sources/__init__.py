#!/usr/bin/env python
#
"""
Parsers that turn an external format into canonical trips.

A source is a pure function from bytes to models: no network access, no API
knowledge, no writes.  That keeps every format we ingest testable against
fixtures alone.
"""

# Project imports
from tripsy_exim.sources.ics import (
    ACTIVITY,
    HOSTING,
    TRANSPORTATION,
    TRIPIT_UID_NAMESPACE,
    EventNote,
    ParsedCalendar,
    classify,
    is_trip_event,
    parse,
    uuid_from_uid,
)
from tripsy_exim.sources.timezones import zone_for

__all__ = [
    "ACTIVITY",
    "HOSTING",
    "TRANSPORTATION",
    "TRIPIT_UID_NAMESPACE",
    "EventNote",
    "ParsedCalendar",
    "classify",
    "is_trip_event",
    "parse",
    "uuid_from_uid",
    "zone_for",
]
