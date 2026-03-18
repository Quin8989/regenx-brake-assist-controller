# drivers/status_leds.py — Optional LED outputs for quick status indication
#
# Simple hardware-facing LED functions only.

from machine import Pin
from config.pins import (
    LED_READY_PIN,
    LED_ASSIST_PIN,
    LED_REGEN_PIN,
    LED_LOW_ENERGY_PIN,
    LED_FAULT_PIN,
)


class StatusLEDs:
    def __init__(self):
        self._leds = {}
        pin_map = {
            "ready": LED_READY_PIN,
            "assist": LED_ASSIST_PIN,
            "regen": LED_REGEN_PIN,
            "low_energy": LED_LOW_ENERGY_PIN,
            "fault": LED_FAULT_PIN,
        }
        for name, pin_num in pin_map.items():
            if pin_num is not None:
                self._leds[name] = Pin(pin_num, Pin.OUT, value=0)

    def set(self, name, on):
        """Turn an LED on (True) or off (False)."""
        if name in self._leds:
            self._leds[name].value(1 if on else 0)

    def all_off(self):
        for led in self._leds.values():
            led.value(0)

    # TODO: Decide whether LEDs remain in addition to the LCD
    # TODO: Decide blink patterns and priorities when multiple conditions are active
