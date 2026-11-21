"""Byte-counter delta and rate calculation.

All counters are integer byte counts. Rates are derived from deltas over
*monotonic* elapsed time. Counter decreases are classified as a 32-bit wrap
(rare on 64-bit kernels) or a reset (interface re-created, provider
restarted, router rebooted). After a reset the new counter value is only
trusted if it is physically plausible for the elapsed time; otherwise the
counter is re-baselined and the interval is reported as unmeasured rather
than inventing traffic.
"""

U32 = 1 << 32

# Upper bound used for plausibility checks: 10 Gbit/s expressed in bytes/s.
DEFAULT_MAX_RATE = 10_000_000_000 // 8

FIRST = "first"
OK = "ok"
WRAP = "wrap"
RESET = "reset"
REBASELINE = "rebaseline"
GLITCH = "glitch"


class Delta:
    __slots__ = ("bytes", "dt", "event", "valid")

    def __init__(self, nbytes, dt, event, valid):
        self.bytes = nbytes
        self.dt = dt
        self.event = event
        self.valid = valid  # True when ``bytes`` is a real measurement

    @property
    def rate(self):
        if not self.valid or self.dt <= 0:
            return None
        return self.bytes / self.dt

    def __repr__(self):
        return "Delta(%r, %.3f, %s, %s)" % (self.bytes, self.dt, self.event, self.valid)


def plausible_max(dt, max_rate=DEFAULT_MAX_RATE):
    # 25 % headroom plus 1 MiB of slack for timer jitter / bursts.
    return int(max_rate * max(dt, 0.0) * 1.25) + (1 << 20)


class CounterTracker:
    """Tracks one cumulative counter.

    ``identity`` captures what makes two readings comparable (for example
    the interface ifindex plus boot id, or the aria2 session id). When it
    changes the counter is treated as reset.
    """

    __slots__ = ("allow_wrap", "identity", "max_rate", "t", "value")

    def __init__(self, max_rate=DEFAULT_MAX_RATE, allow_wrap=False):
        self.value = None
        self.t = None
        self.identity = None
        self.max_rate = max_rate
        # Linux exposes 64-bit interface counters on current kernels, so a
        # decrease is a reset. 32-bit wrap handling is opt-in for providers
        # known to use 32-bit counters.
        self.allow_wrap = allow_wrap

    def reset(self):
        self.value = self.t = self.identity = None

    def restore(self, value, t, identity):
        self.value, self.t, self.identity = int(value), t, identity

    def update(self, value, t, identity=None):
        """Feed a new reading at monotonic time ``t``; return a :class:`Delta`."""
        value = int(value)
        if value < 0:
            return Delta(0, 0.0, GLITCH, False)
        if self.value is None:
            self.value, self.t, self.identity = value, t, identity
            return Delta(0, 0.0, FIRST, False)
        dt = t - self.t
        if dt <= 0:
            # Duplicate or out-of-order reading; ignore without moving state.
            return Delta(0, 0.0, GLITCH, False)
        limit = plausible_max(dt, self.max_rate)
        prev = self.value
        self.value, self.t = value, t
        if identity != self.identity:
            self.identity = identity
            if value <= limit:
                return Delta(value, dt, RESET, True)
            return Delta(0, dt, REBASELINE, False)
        if value >= prev:
            delta = value - prev
            if delta > limit:
                return Delta(0, dt, GLITCH, False)
            return Delta(delta, dt, OK, True)
        # Counter went backwards.
        if self.allow_wrap and prev < U32:
            wrapped = U32 - prev + value
            if wrapped <= limit:
                return Delta(wrapped, dt, WRAP, True)
        if value <= limit:
            return Delta(value, dt, RESET, True)
        return Delta(0, dt, REBASELINE, False)
