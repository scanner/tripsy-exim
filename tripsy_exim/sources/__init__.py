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
from tripsy_exim.sources.join import (
    key_from_calendar,
    trip_key,
    trip_name,
)
from tripsy_exim.sources.text import (
    drop_orphan_leads,
    recover,
    repair_mojibake,
    repair_strings,
)
from tripsy_exim.sources.timezones import zone_for
from tripsy_exim.sources.tripit import (
    TRIPIT_JSON_NAMESPACE,
    load,
    parse_export,
)

__all__ = [
    "ACTIVITY",
    "HOSTING",
    "TRANSPORTATION",
    "TRIPIT_JSON_NAMESPACE",
    "TRIPIT_UID_NAMESPACE",
    "EventNote",
    "ParsedCalendar",
    "classify",
    "drop_orphan_leads",
    "is_trip_event",
    "key_from_calendar",
    "load",
    "parse",
    "parse_export",
    "recover",
    "repair_mojibake",
    "repair_strings",
    "trip_key",
    "trip_name",
    "uuid_from_uid",
    "zone_for",
]
