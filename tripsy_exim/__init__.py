#!/usr/bin/env python
#
"""
Export and import trip data for Tripsy.app.

The canonical models in `tripsy_exim.models` are the hinge between every
other layer: parsers in `sources` target them, `sync.importer` writes them
to Tripsy through `api`, and `store` is where they live on disk.

Reading a whole account back out of Tripsy is not written yet: `api` takes
an `updated_since` and `store` keeps the watermark, but nothing drives
them.
"""

__version__ = "0.1.0"
