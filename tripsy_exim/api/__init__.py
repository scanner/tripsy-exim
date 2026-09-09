#!/usr/bin/env python
#
"""
HTTP access to the Tripsy public API.

This layer owns transport, authentication headers, pagination, retries, and
the mapping from status codes to exceptions.  It knows routes and payload
shapes; it knows nothing about import or export policy, and it never decides
what should be written.  Callers hand it a token -- resolving credentials
belongs to the command line layer.
"""
