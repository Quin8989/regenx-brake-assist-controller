# config/timing.py — Task periods and scheduler timing
#
# All intervals in milliseconds unless noted.

# --- High-priority ---
SAFETY_SUPERVISOR_PERIOD_MS = 10       # ~100 Hz

# --- Control ---
CONTROL_LOOP_PERIOD_MS = 10           # ~100 Hz
COMMAND_TRANSMIT_PERIOD_MS = 20       # ~50 Hz

# --- Sensing ---
THROTTLE_SAMPLE_PERIOD_MS = 10        # ~100 Hz
VCAP_SAMPLE_PERIOD_MS = 20            # ~50 Hz
TELEMETRY_REQUEST_PERIOD_MS = 50      # ~20 Hz

# --- Display ---
LCD_REFRESH_PERIOD_MS = 200           # ~5 Hz
LED_UPDATE_PERIOD_MS = 100            # ~10 Hz

# --- Debug ---
DEBUG_LOG_PERIOD_MS = 500             # ~2 Hz

# TODO: Choose initial rates and validate on hardware
# TODO: Decide whether some tasks use event-driven updates instead of pure periodic timing
