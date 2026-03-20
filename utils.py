# utils.py — Utility helpers and debug logging

from time import ticks_diff, ticks_ms

# =============================================================================
# Math / signal helpers
# =============================================================================


def clamp(value, lo, hi):
    """Clamp value to [lo, hi]."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def linear_map(x, in_lo, in_hi, out_lo, out_hi):
    """Linearly map x from [in_lo, in_hi] to [out_lo, out_hi]."""
    if in_hi == in_lo:
        return out_lo
    return out_lo + (x - in_lo) * (out_hi - out_lo) / (in_hi - in_lo)


class SlewLimiter:
    """Rate limiter — clamps change per call to ±max_delta."""

    def __init__(self, max_delta, initial=0.0):
        self.max_delta = max_delta
        self.value = initial

    def update(self, target):
        delta = target - self.value
        if delta > self.max_delta:
            delta = self.max_delta
        elif delta < -self.max_delta:
            delta = -self.max_delta
        self.value += delta
        return self.value

    def reset(self, value=0.0):
        self.value = value


class PeriodicTimer:
    """Non-blocking periodic timer for cooperative scheduling."""

    def __init__(self, period_ms):
        self._period = period_ms
        self._last = ticks_ms()

    def ready(self):
        now = ticks_ms()
        if ticks_diff(now, self._last) >= self._period:
            self._last = now
            return True
        return False


# =============================================================================
# Logger
# =============================================================================

# Log categories — set to True to enable
_ENABLED = {
    "startup": True,
    "telemetry": False,
    "faults": True,
    "state": True,
    "precharge": True,
    "commands": False,
    "loop": False,
}


class Logger:
    def _emit(self, level, category, msg):
        if not _ENABLED.get(category, False):
            return
        print(f"[{ticks_ms():>8d}] {level} {category}: {msg}")

    def info(self, category, msg):
        self._emit("INFO", category, msg)

    def warn(self, category, msg):
        self._emit("WARN", category, msg)

    def error(self, category, msg):
        self._emit("ERR ", category, msg)

    def debug(self, category, msg):
        self._emit("DBG ", category, msg)

    @staticmethod
    def enable(category):
        _ENABLED[category] = True

    @staticmethod
    def disable(category):
        _ENABLED[category] = False
