# drivers/gpio_io.py — GPIO-based input drivers
#
# Simple GPIO peripherals:
#   - ResetButton: soft reset via momentary push-button

from machine import Pin

from config.settings import RESET_BUTTON_PIN


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
