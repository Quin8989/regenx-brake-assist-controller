# drivers/wheel_speed_hall.py — Wheel speed input from fork-mounted hall sensor

# pyright: reportMissingImports=false

from time import ticks_diff, ticks_ms, ticks_us

try:
    from machine import Pin
except Exception:
    Pin = None

from config.settings import (
    WHEEL_HALL_ACTIVE_HIGH,
    WHEEL_HALL_MIN_EDGE_US,
    WHEEL_HALL_PIN,
    WHEEL_HALL_USE_PULLUP,
    WHEEL_MAGNET_COUNT,
    WHEEL_SPEED_TIMEOUT_MS,
)


class WheelSpeedHall:
    def __init__(self):
        self._enabled = False
        self._last_edge_us = None
        self._last_edge_ms = None
        self._period_us = None

        if WHEEL_HALL_PIN is None or Pin is None:
            return

        pull = Pin.PULL_UP if WHEEL_HALL_USE_PULLUP else None
        if pull is None:
            self._pin = Pin(WHEEL_HALL_PIN, Pin.IN)
        else:
            self._pin = Pin(WHEEL_HALL_PIN, Pin.IN, pull)

        trigger = Pin.IRQ_RISING if WHEEL_HALL_ACTIVE_HIGH else Pin.IRQ_FALLING
        self._pin.irq(handler=self._on_edge, trigger=trigger)
        self._enabled = True

    def _on_edge(self, _):
        now = ticks_us()
        now_ms = ticks_ms()
        if self._last_edge_us is None:
            self._last_edge_us = now
            self._last_edge_ms = now_ms
            return
        dt = ticks_diff(now, self._last_edge_us)
        self._last_edge_us = now
        self._last_edge_ms = now_ms
        if dt >= WHEEL_HALL_MIN_EDGE_US:
            self._period_us = dt

    def update(self):
        if not self._enabled or self._period_us is None or self._last_edge_ms is None:
            return 0.0, False

        # If no pulse arrives for too long, wheel is considered stopped/stale.
        age_ms = ticks_diff(ticks_ms(), self._last_edge_ms)
        if age_ms > WHEEL_SPEED_TIMEOUT_MS:
            return 0.0, False

        # rpm = 60 / (period_s * magnets)
        rpm = 60.0 * 1_000_000.0 / (self._period_us * WHEEL_MAGNET_COUNT)
        return rpm, True
