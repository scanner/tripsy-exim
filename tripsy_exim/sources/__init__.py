#!/usr/bin/env python
#
"""
Parsers that turn an external format into canonical trips.

A source is a pure function from bytes to models: no network access, no API
knowledge, no writes.  That keeps every format we ingest testable against
fixtures alone.
"""
