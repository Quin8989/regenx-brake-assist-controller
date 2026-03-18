# utils/timebase.py — Cooperative task timing support
#
# Helpers built around ticks_ms() for non-blocking periodic task scheduling.

from time import ticks_ms, ticks_diff


class PeriodicTimer:
    """Non-blocking periodic timer for cooperative scheduling."""

    def __init__(self, period_ms):
        self._period = period_ms
        self._last = ticks_ms()

    def ready(self):
        """Return True (once) if the period has elapsed since the last ready()."""
        now = ticks_ms()
        if ticks_diff(now, self._last) >= self._period:
            self._last = now
            return True
        return False

    def reset(self):
        self._last = ticks_ms()


class Timebase:
    """Central timebase helper for the cooperative scheduler."""

    def __init__(self):
        self._now = ticks_ms()

    def tick(self):
        """Call once per main loop iteration to snapshot the current time."""
        self._now = ticks_ms()

    def now(self):
        """Return the last-snapshotted time in ms."""
        return self._now

    def elapsed_since(self, timestamp_ms):
        """Return elapsed ms since a given timestamp (wrap-safe)."""
        return ticks_diff(self._now, timestamp_ms)

    def make_timer(self, period_ms):
        """Create a new PeriodicTimer with the given period."""
        return PeriodicTimer(period_ms)

    # TODO: Decide whether tasks are represented by small objects, tuples, or helper functions
    # TODO: Decide whether microsecond timing is needed anywhere beyond measurement
