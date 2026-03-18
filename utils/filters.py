# utils/filters.py — Reusable signal-conditioning helpers
#
# Independent of application state. Coefficients configured externally.


def clamp(value, lo, hi):
    """Clamp a value to [lo, hi]."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def deadband(value, band):
    """Return 0 if abs(value) < band, otherwise return value."""
    if abs(value) < band:
        return 0.0
    return value


class LowPassFilter:
    """First-order exponential low-pass filter.

    alpha: smoothing factor in (0, 1]. Smaller = smoother / slower.
    """

    def __init__(self, alpha, initial=0.0):
        self.alpha = alpha
        self.value = initial

    def update(self, sample):
        self.value += self.alpha * (sample - self.value)
        return self.value

    def reset(self, value=0.0):
        self.value = value


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


class MovingAverage:
    """Simple moving average over a fixed window."""

    def __init__(self, size):
        self._size = size
        self._buf = [0.0] * size
        self._idx = 0
        self._sum = 0.0
        self._count = 0

    def update(self, sample):
        old = self._buf[self._idx]
        self._buf[self._idx] = sample
        self._sum += sample - old
        self._idx = (self._idx + 1) % self._size
        if self._count < self._size:
            self._count += 1
        return self._sum / self._count

    def reset(self):
        self._buf = [0.0] * self._size
        self._idx = 0
        self._sum = 0.0
        self._count = 0


# TODO: Choose the minimal set of filters needed
# TODO: Decide whether filtering coefficients are stored here or in config
