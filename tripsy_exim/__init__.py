#!/usr/bin/env python
#
"""
Export and import trip data for Tripsy.app.

The canonical models in `tripsy_exim.models` are the hinge between every
other layer: parsers in `sources` target them, `sync.importer` writes them
to Tripsy through `api`, `sync.exporter` reconstructs them from Tripsy, and
`store` is where they live on disk.
"""

__version__ = "0.1.0"
