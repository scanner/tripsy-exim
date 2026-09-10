#!/usr/bin/env python
#
"""
The httpx transport that paces every call and measures every response.

Pacing lives in a transport rather than in the client's request methods
because a transport cannot be gone around.  Every call an `httpx.Client`
makes passes through it, including retried sends and any route added
later, so no route can quietly escape the pace by forgetting to ask.

The transport is also the one place that sees both signals the pacer needs
-- how long the response took and any directive it carried -- so it is the
only layer that has to know they exist.  Deciding what a status *means*,
and whether to send again, stays above it in the client.
"""

# 3rd party imports
import httpx

# Project imports
from tripsy_exim.api.pacing import (
    THROTTLE_STATUSES,
    Pacer,
    directive_seconds,
)


########################################################################
########################################################################
#
class PacedTransport(httpx.BaseTransport):
    """Wraps another transport, pacing what goes through it."""

    ####################################################################
    #
    def __init__(
        self,
        pacer: Pacer,
        inner: httpx.BaseTransport | None = None,
    ) -> None:
        """
        Args:
            pacer: The session's pacer.  Sharing one across clients is
                what makes the pace session-wide rather than per-client.
                Requests are also timed by its clock, so a measurement
                cannot be taken against a different one than the pace is
                kept on.
            inner: The transport that actually sends.  Defaults to a real
                one; tests pass the fake's.
        """
        self._pacer = pacer
        self._inner = inner if inner is not None else httpx.HTTPTransport()

    ####################################################################
    #
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """
        Wait out the pace, send, and report the result to the pacer.

        The elapsed time is measured to the response headers rather than
        to the end of the body.  For this API the two are near enough the
        same -- no route streams -- and time-to-headers is the better
        reading of how loaded the service is.

        A request that raises is reported too, as a failure rather than
        a throttle -- the pace backs off either way, but a run reviewed
        afterwards can tell "the service asked us to slow down" from "the
        service stopped answering".  Both failure shapes need reporting
        and for opposite reasons: a timeout reports its full duration and
        so drives the estimate up on its own, while a connection refused
        comes back instantly and would otherwise look like the fastest
        response of the run.

        Args:
            request: The request to send.

        Returns:
            Whatever the inner transport answered, unexamined beyond its
            status and headers.
        """
        self._pacer.wait()

        # Measured in a try/finally shape on purpose: a request that
        # raised still has to reach the pacer, or a run of timeouts would
        # be the one case that produced no slowdown at all.
        #
        start = self._pacer.now()
        try:
            response = self._inner.handle_request(request)
        except Exception:
            self._pacer.observe(self._pacer.now() - start, failed=True)
            raise

        self._pacer.observe(
            self._pacer.now() - start,
            directive=directive_seconds(response.headers),
            throttled=response.status_code in THROTTLE_STATUSES,
        )
        return response

    ####################################################################
    #
    def close(self) -> None:
        """Close the transport underneath."""
        self._inner.close()
