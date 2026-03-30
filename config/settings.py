# config/settings.py — consolidated project constants
#
# Hardware: Raspberry Pi Pico (RP2040) + Flipsky Mini FSESC4.20 (VESC 4.12)
# All Pico GPIO are 3.3 V.
# FSESC4.20 UART is also 3.3 V — direct connection, no level-shifter needed.

# =============================================================================
# PIN ASSIGNMENTS
# =============================================================================
#
# Pico physical pin → GPIO map (physical / GPIO / function)
#   Pin 6  / GP4  / UART1 TX → FSESC UART RX
#   Pin 7  / GP5  / UART1 RX ← FSESC UART TX
#   Pin 11 / GP8  / Soft reset button (active-low, internal pull-up)
#   Pin 12 / GP9  / Wheel speed hall sensor input (digital)
#   Pin 22 / GP17 / LCD RS (Register Select)
#   Pin 24 / GP18 / LCD E  (Enable)
#   Pin 25 / GP19 / LCD D4
#   Pin 26 / GP20 / LCD D5
#   Pin 27 / GP21 / LCD D6
#   Pin 29 / GP22 / LCD D7
#   Pin 34 / GP28 / LCD backlight enable (active-high)
#   Pin 31 / GP26 / ADC0 — Hall throttle

# --- UART1 to FSESC ---
VESC_UART_ID = 1              # Pico hardware UART1
VESC_UART_TX = 4              # GP4, Pico pin 6 → FSESC UART RX
VESC_UART_RX = 5              # GP5, Pico pin 7 ← FSESC UART TX

# --- Hall throttle (3-wire, 5 V supply, analog out 0.8–4.2 V typical) ---
THROTTLE_ADC_PIN = 26         # GP26 / ADC0

# --- Soft reset button (active-low, normally-open to GND, internal pull-up) ---
RESET_BUTTON_PIN = 8          # GP8, Pico pin 11

# --- Wheel speed hall sensor (fork-mounted, 3 spoke magnets) ---
WHEEL_HALL_PIN = 9            # GP9, Pico pin 12 — digital input
WHEEL_HALL_ACTIVE_HIGH = True
WHEEL_HALL_USE_PULLUP = True
WHEEL_MAGNET_COUNT = 3
WHEEL_SPEED_TIMEOUT_MS = 1200
WHEEL_HALL_MIN_EDGE_US = 1500
# Approximate loaded wheel circumference used for LCD speed conversion and
# km/h-based regen thresholding.
WHEEL_CIRCUMFERENCE_M = 2.10
# Wheel-speed plausibility filter.
# - Reject impossible raw speed spikes above MAX_KPH.
# - Limit how quickly reported speed can rise/fall to ride-plausible values,
#   which masks single missed magnets and bogus ultra-fast samples.
WHEEL_SPEED_MAX_KPH = 80.0
WHEEL_SPEED_MAX_RPM = (WHEEL_SPEED_MAX_KPH * 1000.0 / 60.0) / max(WHEEL_CIRCUMFERENCE_M, 1e-6)
WHEEL_SPEED_MAX_ACCEL_KPH_PER_S = 40.0
WHEEL_SPEED_MAX_DECEL_KPH_PER_S = 60.0
WHEEL_SPEED_INVALID_HOLD_MS = 1500



# --- LCD (RG1602A, ST7066U/HD44780, 4-bit parallel GPIO, no I2C backpack) ---
LCD_RS_PIN = 17               # GP17, Pico pin 22
LCD_E_PIN = 18                # GP18, Pico pin 24
LCD_D4_PIN = 19               # GP19, Pico pin 25
LCD_D5_PIN = 20               # GP20, Pico pin 26
LCD_D6_PIN = 21               # GP21, Pico pin 27
LCD_D7_PIN = 22               # GP22, Pico pin 29
LCD_BL_PIN = 28               # GP28, Pico pin 34 — backlight enable (active-high)
LCD_COLS = 16
LCD_ROWS = 2

# =============================================================================
# VOLTAGE & CURRENT THRESHOLDS
# =============================================================================

# --- Supercapacitor voltage thresholds (volts) ---
VCAP_MIN_OPERATING = 15.0      # Below this: precharge active, motor inhibited
VCAP_SOFT_REGEN_CUTOFF = 40.0  # Software regen disable threshold
VCAP_ABSOLUTE_MAX = 42.0       # Hard bus voltage limit — NEVER exceed

# --- Motor current limit (hard limit — must also be set in VESC Tool) ---
# FSESC4.20 is rated 50 A; project hard limit is 40 A.
MOTOR_CURRENT_MAX_A = 40.0     # Shared max motor current for assist and regen (amps)

# --- Throttle ---
# Calibrated from measured WUXING 300X sweep at 3.3 V supply:
#   idle ~1073 counts, full ~3238 counts.
# Rounded to practical setpoints for stable 0-100% mapping.
THROTTLE_RAW_MIN = 1070
THROTTLE_RAW_MAX = 3240
THROTTLE_DEADBAND = 0.03        # Fraction of range — intentional grace zone near zero
THROTTLE_FAULT_LOW = 100        # Below this raw count → open-circuit / fault
THROTTLE_FAULT_HIGH = 4000      # Above this raw count → short-circuit / fault

# =============================================================================
# VESC SETTINGS
# =============================================================================
# Hardware: Flipsky Mini FSESC4.20, based on VESC® 4.12
# UART signals are 3.3 V logic (STM32F4 core) — direct connection to Pico GPIO.

VESC_BAUD_RATE = 115200                # Verify in VESC Tool → App Settings → UART baud

# Puyan H01-Front Drive Geared Hub Motor — 15 pole pairs (30 magnets) is the
# standard configuration for 250W-350W Chinese geared hub motors in this class.
# VERIFY with VESC Tool → Motor Settings → FOC → Detect and Calculate.
VESC_MOTOR_POLE_PAIRS = 15
VESC_ERPM_TO_MECH_RPM = 1.0 / VESC_MOTOR_POLE_PAIRS

# --- UART / telemetry ---
VESC_TELEMETRY_TIMEOUT_MS = 500  # Stale if no good packet in this window

# --- Runtime behavior policy ---
CONTINUE_ON_MAIN_LOOP_EXCEPTION = True



# --- Regen slip-controller tuning ---
# The geared hub motor has a planetary carrier with a one-way freewheel clutch.
#
# Two riding modes (throttle-gated):
#   ASSIST  — Throttle applied → motor drives wheel (forward power).
#   REGEN   — Throttle off + wheel moving → PI slip controller active.
#             When coasting (carrier free, motor RPM ≈ 0), the PI naturally
#             produces zero brake current.  When the rider squeezes the
#             mechanical brake (carrier locks), positive slip error builds
#             the integral and brake current ramps up.
#
# Regen is disabled entirely above VCAP_SOFT_REGEN_CUTOFF or below
# REGEN_MIN_WHEEL_KPH.

# Minimum speed to allow regen request detection.
REGEN_MIN_WHEEL_KPH = 5.0
REGEN_MIN_WHEEL_RPM = (REGEN_MIN_WHEEL_KPH * 1000.0 / 60.0) / max(WHEEL_CIRCUMFERENCE_M, 1e-6)
# Calibrated from auto-spin bench data (scripts/bench/test_regen_ratio_motor_spin.py).
REGEN_LOCKED_RATIO = 3.0               # motor_rpm / wheel_rpm when carrier fully locked
REGEN_TARGET_SLIP_FRAC = 0.10          # Carrier slip target (10% of wheel speed)
REGEN_COMMAND_MAX_A = 30.0             # Regen command ceiling (A)
REGEN_PI_KP_A_PER_RPM = 0.0            # Proportional gain disabled — pure integral controller
REGEN_PI_KI_A_PER_RPM_S = 1.0          # Integral gain — ramps to whatever current the load needs
REGEN_PI_INTEGRAL_LIMIT_A = 30.0       # Anti-windup clamp — match regen command ceiling
COMMAND_SLEW_A_PER_S = 30.0             # Shared slew rate for assist and regen commands
EXCEPTION_LOG_MAX = 10                  # Max exception snapshots kept in RAM ring buffer

# --- Bench debug logger (RAM ring buffer) ---
BENCH_LOG_PERIOD_MS = 500             # Capture rate (~2 Hz), same as DEBUG_LOG_PERIOD_MS
BENCH_LOG_MAX_RECORDS = 2000           # ~160 KB at ~80 bytes/record, ~16 min at 2 Hz
BENCH_LOG_FIELDS = (
    "system_state",
    "cap_voltage_v",
    "wheel_speed_rpm",
    "vesc_mech_rpm",
    "requested_mode",
    "requested_level",
    "assist_command_request",
    "regen_command_request",
    "gear_carrier_speed_rpm",
    "regen_speed_error_rpm",
)

# --- Energy estimation ---
CAPACITANCE_F = 20.0

# --- Task periods ---
SAFETY_SUPERVISOR_PERIOD_MS = 10   # ~100 Hz
STATE_MACHINE_PERIOD_MS = 10       # ~100 Hz — bounded to prevent free-running
CONTROL_LOOP_PERIOD_MS = 10        # ~100 Hz
COMMAND_TRANSMIT_PERIOD_MS = 20    # ~50 Hz
THROTTLE_SAMPLE_PERIOD_MS = 10     # ~100 Hz
TELEMETRY_REQUEST_PERIOD_MS = 50   # ~20 Hz
LCD_REFRESH_PERIOD_MS = 200        # ~5 Hz
DEBUG_LOG_PERIOD_MS = 500          # ~2 Hz
