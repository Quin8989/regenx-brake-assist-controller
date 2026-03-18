# config/thresholds.py — All numeric limits and threshold values
#
# Central location so behavior can be tuned without searching many files.

# --- Supercapacitor voltage thresholds (volts) ---
VCAP_MIN_OPERATING = 15.0      # Below this: precharge active, motor inhibited
VCAP_LOW_WARNING = 30.0        # Rider-facing low-energy indication
VCAP_SOFT_REGEN_CUTOFF = 40.0  # Software regen disable threshold
VCAP_ABSOLUTE_MAX = 42.0       # Hard bus voltage limit — NEVER exceed

# --- Motor command limits ---
ASSIST_CURRENT_LIMIT_A = 10.0  # TODO: set to match VESC configuration
REGEN_CURRENT_LIMIT_A = 5.0    # TODO: set to match VESC configuration

# --- Throttle ---
THROTTLE_RAW_MIN = 300         # TODO: measure actual idle ADC count
THROTTLE_RAW_MAX = 3800        # TODO: measure actual full-scale ADC count
THROTTLE_DEADBAND = 0.03       # Fraction of range — suppress creep near zero
THROTTLE_FAULT_LOW = 100       # Below this raw count → open-circuit / fault
THROTTLE_FAULT_HIGH = 4000     # Above this raw count → short-circuit / fault

# --- Precharge ---
PRECHARGE_TIMEOUT_MS = 10000   # Max time allowed for precharge to complete
PRECHARGE_COMPLETE_MARGIN_V = 1.0  # TODO: define completion threshold vs target

# --- UART / telemetry ---
VESC_TELEMETRY_TIMEOUT_MS = 500    # Stale if no good packet in this window
VESC_COMMAND_TIMEOUT_MS = 200      # Heartbeat / re-send interval

# --- Safe-state timing ---
FAULT_TO_DISABLE_MAX_MS = 100  # Target: < 100 ms from detection to command disable

# --- Energy estimation ---
CAPACITANCE_F = 20.0           # TODO: confirm effective pack capacitance

# TODO: Reconcile 40 V software cutoff vs 42 V absolute bus requirement
# TODO: Decide exact assist and regen command limits matching VESC config
# TODO: Decide debounce values for switches and state transitions
# TODO: Define fault clear hysteresis for overvoltage / undervoltage
# TODO: Define stale telemetry timeout
