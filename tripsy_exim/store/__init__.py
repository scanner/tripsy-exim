#!/usr/bin/env python
#
"""
The local archive: canonical trips on disk, plus the sync manifest.

Writes merge on set fields rather than replacing whole objects, because any
Tripsy response can be partial -- `price` and `currency` are withheld
without expense permission, and `fields=` responses are partial by
construction.  A blind overwrite would let one restricted export erase data
an earlier one captured.
"""
