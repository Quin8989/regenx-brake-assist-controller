# drivers/throttle.py — Low-level hall throttle input handling
#
# Reads the throttle ADC, applies calibration and deadband,
# and returns data only — does NOT decide whether assist is allowed.

from machine import ADC, Pin
from config.pins import THROTTLE_ADC_PIN
from config.thresholds import (
    THROTTLE_RAW_MIN,
    THROTTLE_RAW_MAX,
    THROTTLE_DEADBAND,
    THROTTLE_FAULT_LOW,
    THROTTLE_FAULT_HIGH,
)
from utils.math_helpers import linear_map, clamp


class Throttle:
    def __init__(self):
        self._adc = ADC(Pin(THROTTLE_ADC_PIN))
        self.raw = 0
        self.voltage = 0.0
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

        # Apply deadband near zero
        if self.fraction < THROTTLE_DEADBAND:
            self.fraction = 0.0

    # TODO: Measure actual throttle idle and full-scale voltage range
    # TODO: Decide acceptable range limits and fault thresholds
    # TODO: Decide the deadband value
    # TODO: Decide whether throttle fault should immediately force FAULT state
    #       or only inhibit assist
