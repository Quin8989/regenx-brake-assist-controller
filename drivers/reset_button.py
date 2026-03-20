# drivers/reset_button.py — Soft reset button driver
#
# Normally-open momentary button wired between GP4 and GND.
# Uses internal pull-up: pin reads HIGH when released, LOW when pressed.
# Returns True on the press edge (transition from released to pressed).

from machine import Pin

from config.settings import RESET_BUTTON_PIN


class ResetButton:
    def __init__(self):
        self._pin = Pin(RESET_BUTTON_PIN, Pin.IN, Pin.PULL_UP)
        self._was_pressed = False

    def poll(self):
        """Return True once per press (falling edge)."""
        pressed = self._pin.value() == 0
        edge = pressed and not self._was_pressed
        self._was_pressed = pressed
        return edge
