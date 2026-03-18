# drivers/precharge_io.py — Direct hardware control for precharge outputs
#
# Controls precharge enable path (relay / MOSFET / contactor) and
# optional main bus enable element. Purely hardware-facing.

from machine import Pin
from config.pins import PRECHARGE_ENABLE_PIN, MAIN_CONTACTOR_PIN


class PrechargeIO:
    def __init__(self):
        # Precharge path — safe default: OFF (disabled)
        self._precharge_pin = Pin(PRECHARGE_ENABLE_PIN, Pin.OUT, value=0)

        # Main contactor (optional — only if hardware has a separate main path)
        self._main_pin = None
        if MAIN_CONTACTOR_PIN is not None:
            self._main_pin = Pin(MAIN_CONTACTOR_PIN, Pin.OUT, value=0)

    def enable_precharge(self):
        self._precharge_pin.value(1)

    def disable_precharge(self):
        self._precharge_pin.value(0)

    def precharge_active(self):
        return self._precharge_pin.value() == 1

    def enable_main(self):
        if self._main_pin is not None:
            self._main_pin.value(1)

    def disable_main(self):
        if self._main_pin is not None:
            self._main_pin.value(0)

    def disable_all(self):
        """Safe shutdown — disable both paths."""
        self._precharge_pin.value(0)
        if self._main_pin is not None:
            self._main_pin.value(0)

    # TODO: Confirm actual precharge hardware topology
    # TODO: Confirm whether there is only one switched element or both precharge + main
    # TODO: Confirm active-high vs active-low logic
    # TODO: Confirm safe default state on boot
    # TODO: Add optional feedback pins if hardware provides relay/contactor state confirmation
