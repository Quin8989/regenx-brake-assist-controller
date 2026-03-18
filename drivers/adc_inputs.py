# drivers/adc_inputs.py — Local analog channels (excluding throttle)
#
# Reads capacitor voltage sense and any other local analog inputs.
# Converts raw ADC counts to engineering units.

from machine import ADC, Pin
from config.pins import VCAP_ADC_PIN


# --- Calibration ---
# TODO: Finalize resistor-divider ratios and calibrate counts-to-voltage
VCAP_DIVIDER_RATIO = 16.0     # Placeholder: Vactual = Vadc * ratio
ADC_VREF = 3.3
ADC_MAX_COUNTS = 4095          # 12-bit


class ADCInputs:
    def __init__(self):
        self._vcap_adc = ADC(Pin(VCAP_ADC_PIN))
        self.cap_voltage_raw = 0
        self.cap_voltage_v = 0.0

    def update(self):
        """Sample all local analog channels and convert to engineering units."""
        self.cap_voltage_raw = self._vcap_adc.read_u16() >> 4  # 12-bit
        adc_volts = (self.cap_voltage_raw / ADC_MAX_COUNTS) * ADC_VREF
        self.cap_voltage_v = adc_volts * VCAP_DIVIDER_RATIO

    # TODO: Finalize resistor-divider ratios
    # TODO: Calibrate counts-to-voltage conversion
    # TODO: Add sanity checks for disconnected or out-of-range analog values
    # TODO: Decide whether filtering lives here or in a service layer
