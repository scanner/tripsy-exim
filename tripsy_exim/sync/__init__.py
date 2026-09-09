#!/usr/bin/env python
#
"""
Trip-level orchestration: what to upload, what to download, and when.

The importer writes canonical trips to Tripsy idempotently; the exporter
pulls changes into the local archive incrementally.  All policy lives here,
so `api` stays a transport and `sources` stay parsers.
"""
