"""Data collectors (providers).

Each collector owns a :class:`Status` describing availability, so the UI can
explain *why* a measurement is missing instead of showing zeros.
"""

import time

OK = "ok"
UNAVAILABLE = "unavailable"  # provider not installed / not reachable
ERROR = "error"  # provider present but failing
DISABLED = "disabled"  # turned off in settings
STARTING = "starting"
DEGRADED = "degraded"  # working with reduced capability or accuracy


class Status:
    def __init__(self, name, label):
        self.name = name
        self.label = label
        self.state = STARTING
        self.message = "Starting"
        self.provider = None
        self.version = None
        self.last_ok = None
        self.last_error = None
        self.error_count = 0
        self.notes = []
        self.extra = {}

    def ok(self, message="Working", now=None):
        self.state = OK
        self.message = message
        self.last_ok = now or time.time()

    def set(self, state, message, now=None):
        if state in (ERROR, UNAVAILABLE) and self.state != state:
            self.error_count += 1
        if state in (ERROR, UNAVAILABLE):
            self.last_error = now or time.time()
        self.state = state
        self.message = message

    def as_dict(self):
        return {
            "name": self.name,
            "label": self.label,
            "state": self.state,
            "message": self.message,
            "provider": self.provider,
            "version": self.version,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "error_count": self.error_count,
            "notes": list(self.notes),
            **self.extra,
        }


class Backoff:
    """Retry scheduler for unavailable providers (bounded exponential backoff)."""

    def __init__(self, base=10.0, cap=300.0):
        self.base = base
        self.cap = cap
        self.delay = 0.0
        self.next_try = 0.0

    def ready(self, now):
        return now >= self.next_try

    def fail(self, now):
        self.delay = self.base if self.delay == 0 else min(self.cap, self.delay * 2)
        self.next_try = now + self.delay

    def succeed(self):
        self.delay = 0.0
        self.next_try = 0.0
