#!/usr/bin/env python
#
"""
Canonical trip models, and the schema of the local archive.

These are provider-neutral: Tripsy is the first service they are mapped to,
not the shape they are derived from.  Fields a source supplies that Tripsy
has nowhere to put are retained rather than dropped, so an import through
this model is lossless even where Tripsy's schema is narrower.
"""
