# drivers/precharge_io.py — Direct hardware control for precharge outputs
#
# Controls precharge enable path (relay / MOSFET / contactor) and
# optional boost path. Purely hardware-facing.

from machine import Pin

from config.settings import BOOST_ENABLE_PIN, PRECHARGE_ENABLE_PIN


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
