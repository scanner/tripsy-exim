#!/usr/bin/env python
#
"""
Session-wide pacing for every call to the Tripsy API.

Tripsy publishes no rate limits and sends no rate-limit headers, so there
is no budget to read and no way to find a limit except by hitting it.  The
pace is therefore derived from the one signal the API does give -- how long
it takes to answer.  After each response the next request waits

    delay = clamp(multiplier * latency, floor, ceiling)

so a fast API is still paced, because the floor is never zero, and a
slowing one is backed away from up to the ceiling.  Deriving the gap from
latency is what keeps a degraded service from being hammered by a run that
would otherwise be unaffected by its own load.

A server directive raises the floor rather than replacing the estimate.
Tripsy has never been seen to send one, and may never; this is built
because a directive is the only authoritative statement about pace a
service can make, and arriving at a 1300-write import without the ability
to read one would be the expensive way to find out it sends them.  When
one does arrive it becomes the minimum bar for the rest of the session and
then decays back toward the profile's floor as calls succeed.  The decay
is deliberate: a permanent floor would turn one 429 in the middle of an
import into an hour of crawling.

Nothing depends on a directive ever appearing.  Latency alone is enough to
pace a run, and the directive path only tightens it.

`monotonic` and `sleep` are imported by name rather than reached through
the `time` module, which is what lets a test replace them here without
replacing them for every other library in the process.  Pacing measures
intervals, so the clock has to be monotonic: a wall-clock pacer would
mis-measure every gap the moment NTP stepped the clock mid-run.

Two limits belong to callers above this layer.  Pacer state lives in the
process, so a batched import starts each batch at the profile floor with
no memory of a throttle that ended the previous one.  And two runs at once
-- a scheduled backup and a manual one -- pace independently rather than
cooperatively.  A single Pacer paces one serial caller; it is guarded
against concurrent use but does not coordinate it.
"""

# system imports
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic, sleep

# Headers that carry a "wait this long" directive, most authoritative
# first.  Only Retry-After is standard; the others are widespread
# conventions.  Tripsy sends none of them as of 2026-09-09, so all three
# are read opportunistically and none is ever required.
#
DELAY_HEADERS = ("retry-after", "x-ratelimit-reset-after", "ratelimit-reset")

# Statuses that mean the request was refused for pace rather than for
# content.  503 is included because a service shedding load wants the same
# treatment as one enforcing a quota.
#
THROTTLE_STATUSES = frozenset({429, 503})


########################################################################
########################################################################
#
@dataclass(frozen=True)
class PacingProfile:
    """How hard one kind of run is allowed to hit the API."""

    # Names the profile in logs and in `--pace` on the command line.
    #
    name: str

    # Seconds of gap per second of observed latency.  1.0 leaves the API
    # idle for as long as it was busy.
    #
    multiplier: float

    # The gap never falls below this, however fast the API answers.
    #
    min_delay: float

    # The gap never rises above this on anything we work out for
    # ourselves, latency and back-off alike.  A server directive may
    # exceed it -- their word beats our guess.
    #
    max_delay: float

    # Weight given to a faster sample when the latency estimate falls.  A
    # rise is taken immediately and in full; only the recovery is damped,
    # so one quick response cannot cancel a real slowdown.
    #
    latency_decay: float

    # What remains of an elevated floor after each successful call.
    #
    floor_decay: float

    # Applied to the current delay when throttled without a directive,
    # which is the only signal available in that case.
    #
    throttle_backoff: float

    # A directive longer than this is not waited out.  The client raises
    # rather than parking a run for an unbounded time on a header it
    # cannot sanity-check.
    #
    max_retry_after: float

    # How long a single request may hang before it is abandoned.  It
    # belongs to the profile because it is the same judgement as the rest
    # of one: how patient this kind of run can afford to be.  It also
    # bounds the worst latency sample the pacer can ever see, which is
    # what stops one hung connection from defining the pace.
    #
    timeout: float


# Thousands of writes in one run.  The most likely run to find a limit, so
# it recovers from a throttle briskly but never runs unpaced.
#
IMPORT = PacingProfile(
    name="import",
    multiplier=1.0,
    min_delay=0.1,
    max_delay=5.0,
    latency_decay=0.2,
    floor_decay=0.5,
    throttle_backoff=2.0,
    max_retry_after=120.0,
    timeout=30.0,
)

# A monthly backup, or a manual run after a new trip.  Unattended and in
# no hurry, so it is markedly politer and gives up an elevated floor
# slowly -- nothing is waiting on it finishing sooner.
#
BACKUP = PacingProfile(
    name="backup",
    multiplier=2.0,
    min_delay=0.5,
    max_delay=10.0,
    latency_decay=0.2,
    floor_decay=0.8,
    throttle_backoff=2.0,
    max_retry_after=300.0,
    timeout=60.0,
)

# A handful of calls with a person waiting: a login, a status check,
# resolving an identifier to an id.
#
INTERACTIVE = PacingProfile(
    name="interactive",
    multiplier=0.5,
    min_delay=0.05,
    max_delay=2.0,
    latency_decay=0.3,
    floor_decay=0.5,
    throttle_backoff=2.0,
    max_retry_after=30.0,
    timeout=15.0,
)

PROFILES: dict[str, PacingProfile] = {
    p.name: p for p in (IMPORT, BACKUP, INTERACTIVE)
}


####################################################################
#
def directive_seconds(
    headers: Mapping[str, str], now: datetime | None = None
) -> float | None:
    """
    Read a "wait this long" directive out of response headers.

    Both `Retry-After` forms are accepted.  A date is turned into a delta
    against `now`, and a date already in the past becomes 0 rather than a
    negative wait -- the server and this machine need not agree on the
    time, and Tripsy is already known to run a two-day cushion on
    `updatedSince` for exactly that reason.

    Args:
        headers: Response headers, matched case-insensitively.  Values
            are whatever the server sent and are not trusted: one may
            hold a number, an HTTP-date, an empty string, or prose.
            Anything unrecognised is treated as no directive rather than
            as an error -- a header this package cannot read is not a
            reason to fail a request that otherwise succeeded.
        now: The instant a date-form directive is measured against.
            Defaults to the current UTC time.  Only the date form reads
            it; the delta-seconds form is already an interval.

    Returns:
        Seconds to wait, or None when no directive was present or the
        one that was could not be parsed.  None and 0.0 are different
        answers -- "said nothing" against "said go now" -- and callers
        rely on telling them apart.  The value is not capped here; the
        caller decides what is too long to honour.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in DELAY_HEADERS:
        raw = lowered.get(name)
        if raw is None:
            continue

        # The delta-seconds form, which is what nearly everything sends.
        #
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass

        # Not a number, so try the HTTP-date form before giving up on
        # this header and falling through to the next one.
        #
        try:
            when = parsedate_to_datetime(raw)
        except TypeError, ValueError:
            continue

        # A date sent without a zone means UTC by convention.  Read as
        # naive it would be subtracted from an aware `now` and raise.
        #
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())

    return None


########################################################################
########################################################################
#
class Pacer:
    """The gap between calls, adjusted as the API answers."""

    ####################################################################
    #
    def __init__(self, profile: PacingProfile = IMPORT) -> None:
        """
        Args:
            profile: The pace this run is allowed to keep.
        """
        self.profile = profile
        self._lock = threading.Lock()

        # The first call is not delayed; there is nothing to pace it
        # against yet.
        #
        self._next_allowed = -math.inf
        self._latency = 0.0
        self._floor = 0.0

        self.requests = 0

        # Kept apart because they mean different things when a run is
        # reviewed afterwards: a throttle is the service asking to be
        # left alone, a failure is it not answering at all.  Both back
        # the pace off identically.
        #
        self.throttles = 0
        self.failures = 0

        self.total_wait = 0.0

    ####################################################################
    #
    def now(self) -> float:
        """
        Read the clock the pace is measured against.

        The transport times its requests with this rather than calling
        `monotonic` itself, so there is one clock in the layer and no
        way for a measurement to be taken against a different one.

        Returns:
            A monotonic reading in seconds, meaningful only as a
            difference against another reading.
        """
        return monotonic()

    ####################################################################
    #
    @property
    def latency(self) -> float:
        """The current latency estimate, in seconds."""
        return self._latency

    ####################################################################
    #
    @property
    def floor(self) -> float:
        """The effective minimum gap, directives included."""
        return max(self.profile.min_delay, self._floor)

    ####################################################################
    #
    @property
    def delay(self) -> float:
        """The gap currently required between a response and the next call."""
        with self._lock:
            return self._delay()

    ####################################################################
    #
    def _delay(self) -> float:
        """The gap, from the latency estimate and the elevated floor."""
        floor = max(self.profile.min_delay, self._floor)

        # A directive can push the floor past our own ceiling, and when it
        # does the directive wins: it is the server's stated limit rather
        # than our guess at one.  Nothing we derive ourselves gets here
        # above max_delay, so this only ever widens for a directive.
        #
        ceiling = max(self.profile.max_delay, floor)
        paced = self.profile.multiplier * self._latency
        return min(max(paced, floor), ceiling)

    ####################################################################
    #
    def _observe_latency(self, elapsed: float) -> None:
        """Fold one sample in: rises taken in full, falls damped."""
        sample = max(0.0, elapsed)
        if sample >= self._latency:
            self._latency = sample
        else:
            self._latency += self.profile.latency_decay * (
                sample - self._latency
            )

    ####################################################################
    #
    def observe(
        self,
        elapsed: float,
        *,
        directive: float | None = None,
        throttled: bool = False,
        failed: bool = False,
    ) -> None:
        """
        Fold a finished request into the pace.

        Called for failures as well as successes, and a failure is the
        case that most needs it.  A request that timed out reports the
        full timeout as its elapsed time, which drives the estimate to
        the ceiling on its own; one refused instantly reports almost
        nothing, and would look like the fastest response of the run if
        `failed` did not back the pace off regardless.

        Args:
            elapsed: Seconds the request took, measured to the response
                headers.  For a timeout this is the timeout itself; for
                a connection refused it is close to zero.
            directive: Seconds the server asked to be left alone for, if
                it said so in a header.  None means it said nothing,
                which is different from asking for zero.  Capped at the
                profile's `max_retry_after` before it may raise the
                floor.
            throttled: The server answered, refusing for pace -- a 429,
                or a 503 behaving like one.
            failed: No response arrived at all: a timeout, a refused
                connection, a dropped socket.  Backs the pace off the
                same way a throttle does, since a service that will not
                answer wants leaving alone just as much as one saying so.
        """
        refused = throttled or failed
        with self._lock:
            self.requests += 1
            self._observe_latency(elapsed)

            if directive is not None:
                capped = min(max(directive, 0.0), self.profile.max_retry_after)
                self._floor = max(self._floor, capped)

            if throttled:
                self.throttles += 1
            if failed:
                self.failures += 1

            if refused:
                # Refused with nothing said about how long to wait, so the
                # only move left is to back off from wherever we are.
                # This one stops at the profile's ceiling: it is our own
                # guess escalating, and a guess does not get to overrule
                # the limit the profile sets on how slow a run may go.
                # Only a directive does that.  Repeated failures still
                # escalate without bound through the retry policy's
                # hold-off, which is a one-off wait rather than a pace.
                #
                if directive is None:
                    backed_off = self._delay() * self.profile.throttle_backoff
                    self._floor = min(backed_off, self.profile.max_delay)
            else:
                # A call got through, so give up part of any raised floor.
                # Below the profile's own floor there is nothing left to
                # give up, and holding a residue would keep the arithmetic
                # alive forever for no effect.
                #
                self._floor *= self.profile.floor_decay
                if self._floor < self.profile.min_delay:
                    self._floor = 0.0

            self._next_allowed = monotonic() + self._delay()

    ####################################################################
    #
    def defer(self, seconds: float) -> None:
        """Hold the next call back by at least this long."""
        with self._lock:
            self._next_allowed = max(self._next_allowed, monotonic() + seconds)

    ####################################################################
    #
    def wait(self) -> float:
        """
        Block until the next call may be sent.

        Returns:
            Seconds actually waited, which is 0.0 when the gap had already
            elapsed on its own.
        """
        with self._lock:
            gap = self._next_allowed - monotonic()
            if gap <= 0.0:
                return 0.0
            self.total_wait += gap

        sleep(gap)
        return gap

    ####################################################################
    #
    def __repr__(self) -> str:
        return (
            f"<Pacer {self.profile.name} requests={self.requests} "
            f"throttles={self.throttles} failures={self.failures} "
            f"delay={self.delay:.3f}s "
            f"waited={self.total_wait:.1f}s>"
        )
