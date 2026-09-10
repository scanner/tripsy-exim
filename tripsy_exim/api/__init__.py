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

# Project imports
from tripsy_exim.api.auth import BearerAuth, TokenAuth, TripsyAuth
from tripsy_exim.api.client import (
    BASE,
    COLLECTIONS,
    AlreadyExists,
    Created,
    TripsyClient,
    WriteResult,
)
from tripsy_exim.api.errors import (
    APIError,
    AuthenticationError,
    BadRequest,
    MethodNotAllowed,
    NotFound,
    PermissionDenied,
    RateLimited,
    ServerError,
    TransportError,
    TripsyError,
)
from tripsy_exim.api.pacing import (
    BACKUP,
    IMPORT,
    INTERACTIVE,
    PROFILES,
    Pacer,
    PacingProfile,
)
from tripsy_exim.api.retry import RetryPolicy
from tripsy_exim.api.transport import PacedTransport

__all__ = [
    "BACKUP",
    "BASE",
    "COLLECTIONS",
    "IMPORT",
    "INTERACTIVE",
    "PROFILES",
    "APIError",
    "AlreadyExists",
    "AuthenticationError",
    "BadRequest",
    "BearerAuth",
    "Created",
    "MethodNotAllowed",
    "NotFound",
    "PacedTransport",
    "Pacer",
    "PacingProfile",
    "PermissionDenied",
    "RateLimited",
    "RetryPolicy",
    "ServerError",
    "TokenAuth",
    "TransportError",
    "TripsyAuth",
    "TripsyClient",
    "TripsyError",
    "WriteResult",
]
