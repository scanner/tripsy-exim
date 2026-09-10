#!/usr/bin/env python
#
"""
When a failed request is worth sending again, and how long to hold off.

Retrying is kept separate from pacing because they answer different
questions.  The pacer decides when the *next* call may go out at all; this
decides whether a particular failure deserves another attempt.  The two
meet at one point: a retry asks the pacer to hold off, so a retried send is
paced like every other send rather than going out immediately.

POST is retryable here, which it normally is not.  Every object this
package creates carries a minted `internal_identifier`, and Tripsy treats
a duplicate identifier as a no-op that answers an empty 200 -- so a create
that may or may not have landed can simply be sent again.  That safety is
a property of the payload rather than of the method, so it is checked
per-request: a POST without an identifier is not retried, because for that
one a lost response really could mean a duplicate object.

The identifier has to be long enough as well as present.  Trip-level
suppression only engages above five characters, verified against the live
API on 2026-09-09, so `key_must_exceed` carries the route's threshold in
rather than being assumed.  Getting this wrong is not a missed retry, it
is a duplicate trip: the first attempt writes, the reply is lost, and the
second attempt writes again because the identifier was too short to
suppress it.
"""

# system imports
import random
from dataclasses import dataclass, field

# Retryable by HTTP semantics: sending them twice has the same effect as
# sending them once.  POST is judged per-request instead.
#
IDEMPOTENT_METHODS = frozenset(
    {"GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"}
)

# Statuses worth another attempt.  500 is deliberately absent: from a DRF
# service it means an unhandled exception, which a second identical
# request reproduces rather than survives, and burying it under retries
# only delays a report of a real bug.
#
RETRY_STATUSES = frozenset({429, 502, 503, 504})


########################################################################
########################################################################
#
@dataclass(frozen=True)
class RetryPolicy:
    """How persistently a failed request is repeated."""

    # Total attempts, the first one included.  1 disables retrying.
    #
    max_attempts: int = 4

    # First hold-off, doubled per attempt.  This is on top of the pace,
    # which after a throttle has a raised floor of its own.
    #
    backoff_base: float = 0.5

    # The doubling stops here.
    #
    backoff_cap: float = 30.0

    # Fraction of the hold-off added at random, so a resumed batch import
    # does not line every retry up on the same instant.
    #
    jitter: float = 0.25

    statuses: frozenset[int] = field(default_factory=lambda: RETRY_STATUSES)

    ####################################################################
    #
    def may_retry(
        self,
        method: str,
        *,
        idempotency_key: str | None,
        key_must_exceed: int = 0,
    ) -> bool:
        """
        Report whether this request can safely be sent again.

        Args:
            method: The HTTP method.
            idempotency_key: The `internal_identifier` in the outgoing
                payload, when it carries one.
            key_must_exceed: The length the identifier has to be longer
                than for the route to suppress a duplicate.  Trips need
                more than five characters; child objects suppress at any
                length, which is the default.

        Returns:
            True when a repeat is harmless.
        """
        if method.upper() in IDEMPOTENT_METHODS:
            return True
        if not idempotency_key:
            return False
        return len(idempotency_key) > key_must_exceed

    ####################################################################
    #
    def backoff(self, attempt: int) -> float:
        """
        The hold-off before a given attempt.

        Args:
            attempt: Which attempt is about to be made, counting the
                first as 1.  The hold-off before attempt 2 is the base,
                and attempt 1 holds off not at all -- there has been no
                failure yet.

        Returns:
            Seconds to hold off.  Randomised unless `jitter` is zero,
            which is how a test pins it to one number.
        """
        if attempt <= 1:
            return 0.0
        doubled = self.backoff_base * (2.0 ** (attempt - 2))
        delay = min(doubled, self.backoff_cap)
        return delay * (1.0 + self.jitter * random.random())


DEFAULT = RetryPolicy()
