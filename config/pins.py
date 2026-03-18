# config/pins.py — Single source of truth for all Pico pin assignments
#
# Contains names only, not runtime logic.
# All Pico GPIO are 3.3 V — external 5 V signals MUST be level-shifted.

# --- UART to VESC ---
VESC_UART_ID = 0              # Hardware UART instance (0 or 1)
VESC_UART_TX = 0              # GP0  — TODO: finalize pin
VESC_UART_RX = 1              # GP1  — TODO: finalize pin

# --- Hall throttle (analog) ---
THROTTLE_ADC_PIN = 26          # GP26 / ADC0 — TODO: finalize pin

# --- Capacitor voltage sense (analog, via resistor divider) ---
VCAP_ADC_PIN = 27              # GP27 / ADC1 — TODO: finalize pin

# --- Spare ADC channel ---
SPARE_ADC_PIN = 28             # GP28 / ADC2 — TODO: reserved for expansion

# --- Precharge control outputs ---
PRECHARGE_ENABLE_PIN = 15      # TODO: finalize pin, confirm active-high/low
MAIN_CONTACTOR_PIN = None      # TODO: set if hardware has a separate main path switch

# --- LCD bus ---
# TODO: Choose I2C or SPI, then set pins accordingly
LCD_I2C_ID = 1
LCD_SDA_PIN = 2                # GP2  — TODO: finalize pin
LCD_SCL_PIN = 3                # GP3  — TODO: finalize pin
LCD_I2C_ADDR = 0x27            # TODO: confirm address for chosen LCD module

# --- Status LEDs ---
LED_READY_PIN = 10             # TODO: finalize pin
LED_ASSIST_PIN = 11            # TODO: finalize pin
LED_REGEN_PIN = 12             # TODO: finalize pin
LED_LOW_ENERGY_PIN = 13        # TODO: finalize pin
LED_FAULT_PIN = 14             # TODO: finalize pin

# --- Optional switches ---
RESET_BUTTON_PIN = None        # TODO: set if a manual reset switch is added
MODE_SWITCH_PIN = None         # TODO: set if a mode / test switch is added

# TODO: Finalize actual Pico pin map
# TODO: Check for UART pin mux conflicts with LCD choice
# TODO: Check whether spare ADC channels are needed for expansion
# TODO: Confirm 5 V external signals are level-shifted before entering Pico GPIO
