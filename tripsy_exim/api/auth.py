#!/usr/bin/env python
#
"""
The two authentication schemes the Tripsy API accepts.

A token from `POST /auth` is sent as `Authorization: Token <token>`, and an
OAuth2 token as `Authorization: Bearer <token>`.  Only the prefix differs,
which is exactly why this is a small class rather than a string built at
each call site: the two are trivial to confuse and produce the same 401
when confused.

The token is mutable.  A run that starts with a cached token and is told
401 replaces it in place, and the client holding this object keeps working
-- nothing has to be rebuilt or re-wired to re-authenticate.

Resolving credentials is not done here.  This layer is handed a token; the
command line owns where it came from.
"""

# system imports
from collections.abc import Generator

# 3rd party imports
import httpx


########################################################################
########################################################################
#
class TripsyAuth(httpx.Auth):
    """An Authorization header with a scheme and a token."""

    # The scheme name that precedes the token in the header.
    #
    scheme = "Token"

    ####################################################################
    #
    def __init__(self, token: str = "") -> None:
        """
        Args:
            token: The token to send.  Empty means unauthenticated, which
                is the state a client is in before it logs in.
        """
        self.token = token

    ####################################################################
    #
    @property
    def header(self) -> str:
        """The full header value."""
        return f"{self.scheme} {self.token}"

    ####################################################################
    #
    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response]:
        """
        Attach the header, unless there is no token to attach.

        `POST /auth` is the request that obtains the token, so it must be
        able to go out without one rather than being blocked by its own
        precondition.
        """
        if self.token:
            request.headers["Authorization"] = self.header
        yield request

    ####################################################################
    #
    def __repr__(self) -> str:
        state = "set" if self.token else "unset"
        return f"<{type(self).__name__} token={state}>"


########################################################################
########################################################################
#
class TokenAuth(TripsyAuth):
    """A token issued by `POST /auth`."""

    scheme = "Token"


########################################################################
########################################################################
#
class BearerAuth(TripsyAuth):
    """An OAuth2 access token."""

    scheme = "Bearer"
