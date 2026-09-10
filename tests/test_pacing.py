#!/usr/bin/env python
#
"""
Test the pace this package keeps against the Tripsy API.

Tripsy publishes no limit and sends no rate-limit headers, so none of this
can be checked against the service.  What is checked is that the rules the
client follows are the ones intended: the gap tracks latency, never falls
below the profile's floor, never rises above its ceiling on latency alone,
and yields to a server directive when one arrives.

Time is injected throughout, so a test asserting a two-minute back-off
costs nothing to run.
"""

# system imports
import time
from datetime import UTC, datetime

# 3rd party imports
import pytest
import pytest_check as check
import time_machine

# Project imports
from tests.clock import FakeClock
from tripsy_exim.api.pacing import (
    BACKUP,
    IMPORT,
    INTERACTIVE,
    Pacer,
    PacingProfile,
    directive_seconds,
)

# A fixed instant for the date form of Retry-After, so the expected delta
# is a constant rather than something computed against the wall clock.
#
NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


####################################################################
#
def paced(profile: PacingProfile = IMPORT) -> Pacer:
    """
    A pacer on whatever clock is currently patched in.

    The caller must have asked for the `clock` fixture; a pacer built
    without it runs on the real clock and really sleeps.  Every test
    below takes `clock` for that reason, whether or not it reads it.
    """
    return Pacer(profile)


########################################################################
########################################################################
#
class TestDelayFromLatency:
    """Tests the gap the pacer derives from how slow the API is."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "profile,latency,expected",
        [
            # A fast API is still paced -- the floor is the point.
            (IMPORT, 0.0, 0.1),
            (IMPORT, 0.05, 0.1),
            # In the band between floor and ceiling the gap tracks
            # latency through the multiplier.
            (IMPORT, 0.3, 0.3),
            (IMPORT, 2.0, 2.0),
            # A very slow API is backed off from, but only so far.
            (IMPORT, 10.0, 5.0),
            # Backup is deliberately half the speed for the same latency,
            # and floors higher.
            (BACKUP, 0.3, 0.6),
            (BACKUP, 0.1, 0.5),
            (BACKUP, 30.0, 10.0),
            # Interactive has a person waiting, so it paces at half the
            # observed latency.
            (INTERACTIVE, 1.0, 0.5),
            (INTERACTIVE, 0.0, 0.05),
            (INTERACTIVE, 60.0, 2.0),
        ],
    )
    def test_the_gap_tracks_latency_between_floor_and_ceiling(
        self, profile: PacingProfile, latency: float, expected: float
    ) -> None:
        """
        GIVEN: a profile and an observed response time
        WHEN:  the response is folded into the pace
        THEN:  the gap is the latency scaled by the multiplier, held
               inside the profile's floor and ceiling
        """
        pacer = paced(profile)

        pacer.observe(latency)

        assert pacer.delay == pytest.approx(expected)

    ####################################################################
    #
    def test_a_slowdown_is_taken_at_once_and_given_up_slowly(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a pacer that has seen a slow response
        WHEN:  one fast response follows it
        THEN:  the estimate rises in full immediately but falls only
               partway, so a single quick reply cannot cancel a real
               slowdown
        """
        pacer = paced()

        pacer.observe(1.0)
        check.equal(pacer.latency, pytest.approx(1.0), "rise is immediate")

        pacer.observe(0.0)
        check.equal(pacer.latency, pytest.approx(0.8), "fall is damped")

        pacer.observe(0.0)
        check.equal(pacer.latency, pytest.approx(0.64), "and stays damped")


########################################################################
########################################################################
#
class TestServerDirectives:
    """Tests what happens when the API says how long to wait."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "headers,expected",
        [
            # The delta-seconds form, which is what nearly everything
            # sends.
            ({"Retry-After": "5"}, 5.0),
            ({"retry-after": "0"}, 0.0),
            ({"Retry-After": "2.5"}, 2.5),
            # The date form.
            ({"Retry-After": "Thu, 10 Sep 2026 12:00:30 GMT"}, 30.0),
            # A date already past is a wait of nothing, never a negative
            # one -- the two clocks need not agree.
            ({"Retry-After": "Thu, 10 Sep 2026 11:59:00 GMT"}, 0.0),
            # Conventions Tripsy does not use, read in case it starts.
            ({"X-RateLimit-Reset-After": "3"}, 3.0),
            ({"RateLimit-Reset": "12"}, 12.0),
            # Retry-After is the standard one, so it wins.
            ({"Retry-After": "5", "RateLimit-Reset": "99"}, 5.0),
            # Nothing usable is no directive, not a wait of zero: the
            # caller must be able to tell "wait 0" from "said nothing".
            ({}, None),
            ({"Retry-After": "soon"}, None),
            ({"Retry-After": ""}, None),
        ],
    )
    def test_a_directive_is_read_in_either_form(
        self, headers: dict[str, str], expected: float | None
    ) -> None:
        """
        GIVEN: response headers that may carry a pacing directive
        WHEN:  they are parsed
        THEN:  both Retry-After forms are understood, an unparseable one
               is no directive at all, and a stale date is not negative
        """
        assert directive_seconds(headers, NOW) == pytest.approx(expected)

    ####################################################################
    #
    def test_a_directive_outranks_our_own_ceiling(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a directive longer than the profile's maximum gap
        WHEN:  it is folded into the pace
        THEN:  the gap becomes the directive, because a stated limit
               beats a guessed one
        """
        pacer = paced()

        pacer.observe(0.1, directive=45.0, throttled=True)

        check.greater(45.0, IMPORT.max_delay, "the test is meaningful")
        check.equal(pacer.delay, pytest.approx(45.0), "the server wins")

    ####################################################################
    #
    def test_an_absurd_directive_is_capped(self, clock: FakeClock) -> None:
        """
        GIVEN: a directive far longer than the profile will ever wait
        WHEN:  it is folded into the pace
        THEN:  the floor stops at max_retry_after, so a malformed header
               cannot park a run for an hour
        """
        pacer = paced()

        pacer.observe(0.1, directive=9999.0, throttled=True)

        assert pacer.delay == pytest.approx(IMPORT.max_retry_after)

    ####################################################################
    #
    def test_the_raised_floor_decays_back_as_calls_succeed(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a directive that raised the floor
        WHEN:  successful calls follow
        THEN:  the floor halves each time and finally returns to the
               profile's own, so one 429 does not slow the rest of a
               1300-write import to a crawl
        """
        pacer = paced()
        pacer.observe(0.0, directive=4.0, throttled=True)

        seen = []
        for _ in range(6):
            seen.append(pacer.delay)
            pacer.observe(0.0)

        check.equal(
            [round(d, 4) for d in seen],
            [4.0, 2.0, 1.0, 0.5, 0.25, 0.125],
            "halves after each success",
        )
        check.equal(
            pacer.delay,
            pytest.approx(IMPORT.min_delay),
            "and settles at the profile floor",
        )

    ####################################################################
    #
    def test_a_throttle_with_no_directive_still_backs_off(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a refusal that says nothing about how long to wait
        WHEN:  it is folded into the pace
        THEN:  the gap doubles anyway, since backing off from where we
               are is the only signal left
        """
        pacer = paced()

        pacer.observe(0.0, throttled=True)
        check.equal(pacer.delay, pytest.approx(0.2), "doubled once")

        pacer.observe(0.0, throttled=True)
        check.equal(pacer.delay, pytest.approx(0.4), "and again")

        check.equal(pacer.throttles, 2, "both were counted")


########################################################################
########################################################################
#
class TestWaiting:
    """Tests the blocking half: when a call is actually let through."""

    ####################################################################
    #
    def test_the_first_call_is_not_delayed(self, clock: FakeClock) -> None:
        """
        GIVEN: a pacer that has seen nothing yet
        WHEN:  a call asks to go
        THEN:  it goes at once, because there is nothing to pace it
               against
        """
        pacer = paced()

        check.equal(pacer.wait(), 0.0, "no wait")
        check.equal(clock.slept, [], "and nothing slept")

    ####################################################################
    #
    def test_a_call_waits_out_the_gap_from_the_last_response(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a response that set a gap
        WHEN:  the next call asks to go
        THEN:  it sleeps the remainder of that gap and no more
        """
        pacer = paced()
        pacer.observe(0.4)

        check.equal(pacer.wait(), pytest.approx(0.4), "slept the gap")
        check.equal(pacer.wait(), 0.0, "the gap is now spent")
        check.equal(pacer.total_wait, pytest.approx(0.4), "counted once")

    ####################################################################
    #
    def test_time_already_spent_elsewhere_counts_toward_the_gap(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a gap set by the last response
        WHEN:  the caller spent that long doing something else
        THEN:  nothing is slept, because the gap is a minimum spacing
               rather than a mandatory pause
        """
        pacer = paced()
        pacer.observe(0.4)

        clock.advance(0.4)

        assert pacer.wait() == 0.0

    ####################################################################
    #
    def test_defer_can_only_push_the_next_call_later(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a gap already set by the last response
        WHEN:  a shorter hold-off is deferred on top of it
        THEN:  the longer of the two stands, so a retry's backoff can
               never shorten the pace
        """
        pacer = paced()
        pacer.observe(2.0)

        pacer.defer(0.1)

        assert pacer.wait() == pytest.approx(2.0)

    ####################################################################
    #
    def test_our_own_backing_off_never_beats_our_own_ceiling(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a long run of refusals that say nothing about how long to
               wait
        WHEN:  the pacer backs off from each one
        THEN:  the gap stops at the profile's ceiling, because that is a
               limit on how slow a run may get and a guess of ours does
               not get to overrule it -- only a server directive does
        """
        pacer = paced()

        for _ in range(20):
            pacer.observe(0.0, throttled=True)

        check.equal(
            pacer.delay, pytest.approx(IMPORT.max_delay), "held at the cap"
        )

    ####################################################################
    #
    def test_a_directive_is_the_one_thing_that_may_beat_the_ceiling(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: the same profile
        WHEN:  the slowdown comes from the server rather than from us
        THEN:  it is allowed past the ceiling, which is the asymmetry
               the two cases are meant to have
        """
        ours = paced()
        theirs = paced()

        for _ in range(20):
            ours.observe(0.0, throttled=True)
        theirs.observe(0.0, directive=45.0, throttled=True)

        check.equal(ours.delay, pytest.approx(5.0), "ours is capped")
        check.equal(theirs.delay, pytest.approx(45.0), "theirs is not")


########################################################################
########################################################################
#
class TestDateFormDirectivesEndToEnd:
    """
    Tests the one part of pacing that reads wall-clock time.

    The delta-seconds form of `Retry-After` is an interval and needs no
    clock.  The date form is an instant, and turning it into a wait means
    subtracting the current time -- which is real wall-clock time, not the
    injected monotonic clock the rest of the pacer runs on.  `time_machine`
    pins it so the resulting wait is a constant.
    """

    ####################################################################
    #
    @time_machine.travel(NOW, tick=False)
    def test_a_date_directive_becomes_a_wait_against_the_real_clock(
        self,
    ) -> None:
        """
        GIVEN: a Retry-After naming an instant half a minute out
        WHEN:  it is parsed without being told what 'now' is
        THEN:  it resolves against the system clock, which is the path
               taken when the header comes off a live response
        """
        headers = {"Retry-After": "Thu, 10 Sep 2026 12:00:30 GMT"}

        assert directive_seconds(headers) == pytest.approx(30.0)

    ####################################################################
    #
    @time_machine.travel(NOW, tick=False)
    def test_a_stale_date_directive_does_not_go_backwards(self) -> None:
        """
        GIVEN: a Retry-After already in the past, as a server whose clock
               runs behind ours would send
        WHEN:  it is parsed against the system clock
        THEN:  it is a wait of nothing rather than a negative one, which
               would otherwise subtract from the pace
        """
        headers = {"Retry-After": "Thu, 10 Sep 2026 11:00:00 GMT"}

        assert directive_seconds(headers) == 0.0


########################################################################
########################################################################
#
class TestTheClockPatchIsContained:
    """
    Tests that putting a clock under the pacer does not move everyone's.

    This is the reason `pacing` imports `monotonic` and `sleep` by name
    instead of reaching through the `time` module.  Were they reached
    through it, the only way to replace them would be to replace them
    for pytest, httpx and everything else in the process at the same
    time.
    """

    ####################################################################
    #
    def test_the_real_time_module_is_left_alone(self, clock: FakeClock) -> None:
        """
        GIVEN: a test with the pacer's clock patched
        WHEN:  the `time` module is read directly
        THEN:  it is untouched, so no other library is running on the
               fake clock while this test does
        """
        pacer = paced()
        pacer.observe(0.5)

        check.equal(
            pacer.now(),
            clock.now,
            "the pacer is on the fake clock",
        )
        check.not_equal(
            time.monotonic(),
            clock.now,
            "and time.monotonic is not",
        )
        check.is_true(
            time.monotonic() > 0.0,
            "the real clock still runs",
        )
