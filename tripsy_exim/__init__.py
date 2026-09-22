#!/usr/bin/env python
#
"""
Export and import trip data for Tripsy.app.

The canonical models in `tripsy_exim.models` are the hinge between every
other layer: parsers in `sources` target them, `sync.importer` writes them
to Tripsy through `api`, and `store` is where they live on disk.

`sync.exporter` reads a whole account back out of Tripsy into a dated
export, a second kind of archive beside the staging ones.
"""

# system imports
from importlib.metadata import version

# The version is set in pyproject.toml and read back from the installed
# package's metadata.
#
__version__ = version("tripsy-exim")
