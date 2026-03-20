# drivers/gpio_io.py — GPIO-based output/input drivers
#
# Combines simple GPIO peripherals:
#   - PrechargeIO: precharge relay and optional DC/DC boost enable
#   - ResetButton: soft reset via momentary push-button

from machine import Pin

from config.settings import (
    BOOST_ENABLE_PIN,
    PRECHARGE_ENABLE_PIN,
    RESET_BUTTON_PIN,
)


class PrechargeIO:
    def __init__(self):
        # Precharge path — safe default: OFF (disabled)
        self._precharge_pin = Pin(PRECHARGE_ENABLE_PIN, Pin.OUT, value=0)

        # DC/DC boost enable (optional)
        self._boost_pin = None
        if BOOST_ENABLE_PIN is not None:
            self._boost_pin = Pin(BOOST_ENABLE_PIN, Pin.OUT, value=0)

    def enable_precharge(self):
        self._precharge_pin.value(1)

    def disable_precharge(self):
        self._precharge_pin.value(0)

    def precharge_active(self):
        return self._precharge_pin.value() == 1

    def enable_boost(self):
        if self._boost_pin is not None:
            self._boost_pin.value(1)

    def disable_boost(self):
        if self._boost_pin is not None:
            self._boost_pin.value(0)

    def boost_active(self):
        return self._boost_pin is not None and self._boost_pin.value() == 1

    def disable_all(self):
        """Safe shutdown — disable both paths."""
        self._precharge_pin.value(0)
        if self._boost_pin is not None:
            self._boost_pin.value(0)


class ResetButton:
    """Normally-open momentary button wired between pin and GND.
    Uses internal pull-up: reads HIGH when released, LOW when pressed.
    Returns True on the press edge (transition from released to pressed)."""

    def __init__(self):
        self._pin = Pin(RESET_BUTTON_PIN, Pin.IN, Pin.PULL_UP)
        self._was_pressed = False

    def poll(self):
        """Return True once per press (falling edge)."""
        pressed = self._pin.value() == 0
        edge = pressed and not self._was_pressed
        self._was_pressed = pressed
        return edge
