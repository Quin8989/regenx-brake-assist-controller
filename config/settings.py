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
#   Pin 20 / GP15 / PRECHARGE ENABLE (active-high)
#   Pin 21 / GP16 / BOOST ENABLE (active-high)
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

# --- Wheel speed hall sensor (fork-mounted, 6 spoke magnets) ---
WHEEL_HALL_PIN = 9            # GP9, Pico pin 12 — digital input
WHEEL_HALL_ACTIVE_HIGH = True
WHEEL_HALL_USE_PULLUP = True
WHEEL_MAGNET_COUNT = 6
WHEEL_SPEED_TIMEOUT_MS = 1200
WHEEL_HALL_MIN_EDGE_US = 1500

# --- Precharge control ---
PRECHARGE_ENABLE_PIN = 15     # GP15, Pico pin 20 — active-high
BOOST_ENABLE_PIN = 16         # GP16, Pico pin 21 — DC/DC boost enable (active-high)

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

# --- Motor current limits (hard limits — must also be set in VESC Tool) ---
# FSESC4.20 is rated 50 A; project hard limit is 40 A.
ASSIST_CURRENT_LIMIT_A = 40.0  # Max assist current (amps)
REGEN_CURRENT_LIMIT_A = 40.0   # Max regen braking current (amps)

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

# --- Precharge watchdog ---
# RC circuit: R=62 Ohm, C=20 F → tau = 1240 s (~20.7 min).
# Worst case 0 V → 15 V (VCAP_MIN_OPERATING) ≈ 29 min.
#
# VESC boots at ~6 V.  Pico runs the whole time, but there is NO telemetry
# (and therefore no cap voltage reading) until the VESC powers on.
#   • 0 V → 6 V: ~7.4 min worst case.  Watchdog is blind; only the
#     telemetry-grace timer provides protection in this phase.
#   • Cap may have residual charge — if already ≥6 V the VESC boots
#     immediately and telemetry grace is irrelevant.
VESC_MIN_BOOT_VOLTAGE = 6.0              # VESC powers on at this cap voltage
PRECHARGE_WATCHDOG_ENABLE = True
PRECHARGE_SOURCE_V = 20.0
PRECHARGE_RESISTANCE_OHM = 62.0
PRECHARGE_PROGRESS_WINDOW_MS = 60_000     # 60 s — evaluate voltage rise per window
PRECHARGE_MIN_PROGRESS_RATIO = 0.10       # Must see ≥10% of expected dV per window
PRECHARGE_MAX_BAD_WINDOWS = 3             # 3 consecutive stalled windows → fault
PRECHARGE_HARD_TIMEOUT_MS = 35 * 60_000   # 35 min — 20% margin over 29 min worst case
PRECHARGE_TELEMETRY_GRACE_MS = 12 * 60_000  # 12 min — VESC needs ~7.4 min to boot from 0 V

# --- Regen slip-controller tuning ---
# The geared hub motor has a planetary carrier with a one-way freewheel clutch.
#
# Three riding states:
#   ASSIST  — Throttle applied → motor drives wheel (forward power).
#   COAST   — Throttle off, no brake → freewheel disengages, motor RPM ≈ 0,
#             true zero-drag coast (NEUTRAL mode).
#   REGEN   — Throttle off + mechanical brake → carrier locks, wheel drives
#             motor at ≈ wheel_rpm × REGEN_LOCKED_RATIO.  The PI controller
#             commands brake current to hold a fixed carrier-slip target.
#
# Carrier lock detection (no brake switch needed):
#   motor_rpm ≈ wheel_rpm × ratio → carrier locked → rider is braking
#   motor_rpm ≈ 0                  → carrier free   → rider is coasting
#   Hysteresis: enter REGEN at slip < ENGAGE, exit at slip > DISENGAGE.
#
# Regen is disabled entirely above VCAP_SOFT_REGEN_CUTOFF.

REGEN_MIN_WHEEL_RPM = 20.0             # Below this (~walking pace), no regen
REGEN_LOCKED_RATIO = 5.0               # motor_rpm / wheel_rpm when carrier fully locked
REGEN_ENGAGE_SLIP_FRAC = 0.30          # Enter REGEN when carrier slip below this
REGEN_DISENGAGE_SLIP_FRAC = 0.50       # Exit REGEN when carrier slip above this
REGEN_TARGET_SLIP_FRAC = 0.02          # Carrier slip target (2% of wheel speed)
REGEN_PI_KP_A_PER_RPM = 0.35           # PI proportional gain
REGEN_PI_KI_A_PER_RPM_S = 0.12         # PI integral gain
REGEN_PI_INTEGRAL_LIMIT_A = 25.0       # Anti-windup clamp (amps)

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
CONTROL_LOOP_PERIOD_MS = 10        # ~100 Hz
COMMAND_TRANSMIT_PERIOD_MS = 20    # ~50 Hz
THROTTLE_SAMPLE_PERIOD_MS = 10     # ~100 Hz
TELEMETRY_REQUEST_PERIOD_MS = 50   # ~20 Hz
LCD_REFRESH_PERIOD_MS = 200        # ~5 Hz
DEBUG_LOG_PERIOD_MS = 500          # ~2 Hz
