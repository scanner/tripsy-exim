#!/usr/bin/env python
#
"""
A controllable monotonic clock for pacing tests.

The `clock` fixture patches these two methods over `monotonic` and `sleep`
in `tripsy_exim.api.pacing`, so a test can drive a pace of minutes without
the suite taking minutes.  `sleep` advances the same counter `monotonic`
reads, which is what makes a sequence of paced calls move the clock
exactly as real time would -- a sleep that did not advance the clock would
leave every gap looking unspent.

`time-machine` does not replace this, and the reason is worth stating so
the question is not reopened.  It patches `time.time`, the `datetime`
constructors, and `clock_gettime` for CLOCK_REALTIME -- it patches neither
`time.monotonic` nor `time.sleep`, which are the only two functions the
pacer uses.  Pacing measures intervals, and an interval must come from a
monotonic clock: a wall-clock pacer would mis-measure every gap the moment
NTP stepped the clock mid-import.  Nor does `time-machine` help with
sleeping, which has to be replaced regardless or a back-off test really
waits.

`time-machine` is used, in `test_pacing.py`, for the one thing that really
does read wall-clock time: the date form of `Retry-After`, which is a
point in time rather than an interval.
"""


########################################################################
########################################################################
#
class FakeClock:
    """A monotonic clock a test moves by hand."""

    ####################################################################
    #
    def __init__(self, start: float = 1000.0) -> None:
        """
        Args:
            start: The initial reading.  Non-zero so that a bug treating
                an unset instant as 0.0 shows up as a huge wait rather
                than passing quietly.
        """
        self.now = start
        self.slept: list[float] = []

    ####################################################################
    #
    def monotonic(self) -> float:
        """The current reading, in seconds."""
        return self.now

    ####################################################################
    #
    def sleep(self, seconds: float) -> None:
        """Advance the clock, recording the interval for assertions."""
        self.slept.append(seconds)
        self.now += seconds

    ####################################################################
    #
    def advance(self, seconds: float) -> None:
        """Move the clock without recording a sleep."""
        self.now += seconds
