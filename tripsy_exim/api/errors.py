#!/usr/bin/env python
#
"""
Exceptions the API layer raises, and the mapping from status codes to them.

Callers above this layer act on the distinctions, not on numbers: an
importer retries nothing on a `BadRequest`, re-authenticates on an
`AuthenticationError`, and treats a `NotFound` on a child route as a trip
that has gone away.  Keeping the mapping here is what lets the sync layer
avoid knowing any status codes at all.

The response body is kept on the exception because Tripsy reports field
errors in it, and the field that was rejected is the only useful thing to
put in front of a person after 1300 writes.
"""

# system imports
from typing import Any

# 3rd party imports
import httpx


########################################################################
########################################################################
#
class TripsyError(Exception):
    """Anything this package raises while talking to Tripsy."""


########################################################################
########################################################################
#
class TransportError(TripsyError):
    """A request never produced a response, after any retries."""


########################################################################
########################################################################
#
class APIError(TripsyError):
    """Tripsy answered, with a status that is not success."""

    ####################################################################
    #
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        method: str = "",
        url: str = "",
        payload: Any = None,
    ) -> None:
        """
        Args:
            message: What went wrong, already readable.
            status_code: The HTTP status Tripsy answered with.
            method: The request method, for the message.
            url: The request URL, for the message.
            payload: The decoded response body, when there was one.
        """
        super().__init__(message)
        self.status_code = status_code
        self.method = method
        self.url = url
        self.payload = payload


########################################################################
########################################################################
#
class BadRequest(APIError):
    """400 -- the payload was rejected."""


########################################################################
########################################################################
#
class AuthenticationError(APIError):
    """401 -- the token is missing, wrong, or no longer valid."""


########################################################################
########################################################################
#
class PermissionDenied(APIError):
    """403 -- authenticated, but not allowed to do this."""


########################################################################
########################################################################
#
class NotFound(APIError):
    """404 -- no such object, or no such route."""


########################################################################
########################################################################
#
class MethodNotAllowed(APIError):
    """405 -- the route exists and does not accept this method."""


########################################################################
########################################################################
#
class RateLimited(APIError):
    """
    429, or a 503 that behaves like one.

    Raised only when the client has stopped retrying: either the attempts
    are used up, or the server asked for a longer wait than the profile
    permits.  `retry_after` carries what it asked for, in seconds, when it
    said at all.
    """

    ####################################################################
    #
    def __init__(
        self, message: str, *, retry_after: float | None = None, **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


########################################################################
########################################################################
#
class ServerError(APIError):
    """5xx -- Tripsy failed on its own side."""


# Statuses with a class of their own.  Anything else falls back to the
# 4xx/5xx split in `error_for`.
#
_BY_STATUS: dict[int, type[APIError]] = {
    400: BadRequest,
    401: AuthenticationError,
    403: PermissionDenied,
    404: NotFound,
    405: MethodNotAllowed,
    429: RateLimited,
}


####################################################################
#
def _detail(payload: Any) -> str:
    """
    Pull the readable part out of an error body.

    Args:
        payload: A decoded response body, which is whatever the server
            sent: DRF's `{"detail": "..."}`, its field-error form
            `{"field": ["message", ...]}`, a bare string from a proxy or
            a 502 page, or something unrecognised.  Each shape is handled
            and none of them raises -- this runs while an exception is
            already being built, so it must not be able to fail.

    Returns:
        A one-line summary, or an empty string when the body held
        nothing worth saying.
    """
    if isinstance(payload, dict):
        # DRF's own single-message form, which covers most errors.
        #
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail

        # Field errors arrive as {"field": ["message", ...]}.  Naming the
        # fields is the whole value of the message.
        #
        parts = [
            f"{name}: {'; '.join(str(v) for v in value)}"
            if isinstance(value, list)
            else f"{name}: {value}"
            for name, value in payload.items()
        ]
        if parts:
            return ", ".join(parts)
    elif isinstance(payload, str) and payload:
        # Not JSON at all -- a proxy's HTML error page, say, already
        # reduced to text by the caller.
        #
        return payload
    return ""


####################################################################
#
def error_for(
    response: httpx.Response, *, retry_after: float | None = None
) -> APIError:
    """
    Build the exception that stands for an unsuccessful response.

    Args:
        response: The response Tripsy sent.
        retry_after: The parsed pacing directive, when there was one.

    Returns:
        The most specific APIError subclass for the status.
    """
    try:
        payload: Any = response.json()
    except ValueError:
        payload = response.text or None

    status = response.status_code
    if status == 503:
        kind: type[APIError] = RateLimited
    else:
        kind = _BY_STATUS.get(
            status, ServerError if status >= 500 else APIError
        )

    detail = _detail(payload)
    message = f"{response.request.method} {response.request.url} -> {status}"
    if detail:
        message = f"{message}: {detail}"

    kwargs: dict[str, Any] = {
        "status_code": status,
        "method": response.request.method,
        "url": str(response.request.url),
        "payload": payload,
    }
    if kind is RateLimited:
        return RateLimited(message, retry_after=retry_after, **kwargs)
    return kind(message, **kwargs)
