# drivers/throttle.py — Low-level hall throttle input handling
#
# Reads the throttle ADC, applies calibration and deadband,
# and returns data only — does NOT decide whether assist is allowed.

from machine import ADC, Pin

from config.settings import (
    THROTTLE_ADC_PIN,
    THROTTLE_DEADBAND,
    THROTTLE_FAULT_HIGH,
    THROTTLE_FAULT_LOW,
    THROTTLE_RAW_MAX,
    THROTTLE_RAW_MIN,
)
from utils import clamp, linear_map


class Throttle:
    def __init__(self):
        self._adc = ADC(Pin(THROTTLE_ADC_PIN))
        self.raw = 0
        self.fraction = 0.0      # 0.0 – 1.0 normalized command
        self.is_valid = True

    def update(self):
        """Sample throttle and compute normalized fraction."""
        self.raw = self._adc.read_u16() >> 4  # 12-bit range (0–4095)

        # Fault detection
        if self.raw < THROTTLE_FAULT_LOW or self.raw > THROTTLE_FAULT_HIGH:
            self.is_valid = False
            self.fraction = 0.0
            return

        self.is_valid = True

        # Normalize to 0.0–1.0 within calibrated range
        self.fraction = linear_map(
            self.raw,
            THROTTLE_RAW_MIN, THROTTLE_RAW_MAX,
            0.0, 1.0,
        )
        self.fraction = clamp(self.fraction, 0.0, 1.0)

        # Apply deadband near zero, then re-scale so output remains linear
        # from 0.0 to 1.0 above the deadband threshold.
        if self.fraction < THROTTLE_DEADBAND:
            self.fraction = 0.0
        elif THROTTLE_DEADBAND < 1.0:
            self.fraction = (self.fraction - THROTTLE_DEADBAND) / (1.0 - THROTTLE_DEADBAND)
            self.fraction = clamp(self.fraction, 0.0, 1.0)
