# config/settings.py — consolidated project constants
#
# Hardware: Raspberry Pi Pico (RP2040) + Flipsky Mini FSESC4.20 (hw 410, FW 6.6)
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
# Cap bank: 48 V rated.  Motor: 48 V system.  VESC FSESC4.20: 50 V rated.
# 42 V is the chosen operational ceiling — leaves 6 V headroom to cap rating.
VCAP_MIN_OPERATING = 10.0        # Below this: precharge active, motor inhibited
VCAP_REGEN_TAPER_START_V = 40.0  # Regen current starts linearly tapering down
VCAP_REGEN_TAPER_END_V = 42.0    # Regen current reaches zero (software)
VCAP_ABSOLUTE_MAX = 43.0         # Operational max — supervisor faults here (5 V below 48 V cap rating)

# --- Motor current limit (hard limit — must also be set in VESC Tool) ---
# FSESC4.20 is rated 50 A continuous.
MOTOR_CURRENT_MAX_A = 50.0     # VESC-side motor current limit (amps)
VESC_WATT_MAX = 1500.0         # VESC watt limit — both drive and regen (watts)

# Absolute maximum instantaneous phase current before ABS_OVER_CURRENT fault.
# FSESC4.20 FETs handle 130 A+ transiently — this protects against dead-shorts,
# not normal FOC commutation ripple.  Applied to VESC via Lisp conf-set.
VESC_ABS_CURRENT_MAX_A = 130.0

# Firmware command ceiling — the maximum current the Pico will ever command.
# Set 5 A below MOTOR_CURRENT_MAX_A so that steady-state FOC tracking error
# stays within the VESC's motor current limit.  Transient phase-current
# spikes are handled by l_abs_current_max (set to 130 A in VESC Tool).
MOTOR_COMMAND_LIMIT_A = 45.0

# --- Throttle ---
# Calibrated from measured WUXING 300X sweep at 3.3 V supply:
#   idle ~1073 counts, full ~3238 counts.
# Rounded to practical setpoints for stable 0-100% mapping.
THROTTLE_RAW_MIN = 1070
THROTTLE_RAW_MAX = 3240
THROTTLE_DEADBAND = 0.05        # Fraction of range — widens grace zone to cover RP2040 ADC noise
THROTTLE_OVERSAMPLE = 4         # Average N reads per sample — halves noise amplitude
THROTTLE_FAULT_LOW = 100        # Below this raw count → open-circuit / fault
THROTTLE_FAULT_HIGH = 4000      # Above this raw count → short-circuit / fault

# =============================================================================
# VESC SETTINGS
# =============================================================================
# Hardware: Flipsky Mini FSESC4.20, based on VESC® 4.12
# UART signals are 3.3 V logic (STM32F4 core) — direct connection to Pico GPIO.

VESC_BAUD_RATE = 115200                # Verify in VESC Tool → App Settings → UART baud

# Puyan H01-Front Drive Geared Hub Motor — 11 pole pairs (22 magnets).
# VERIFY with VESC Tool → Motor Settings → FOC → Detect and Calculate.
VESC_MOTOR_POLE_PAIRS = 11
VESC_ERPM_TO_MECH_RPM = 1.0 / VESC_MOTOR_POLE_PAIRS

# --- UART / telemetry ---
VESC_TELEMETRY_TIMEOUT_MS = 500  # Stale if no good packet in this window

# --- VESC keepalive & safety ---
VESC_ALIVE_PERIOD_MS = 200        # Send COMM_ALIVE every N ms (VESC default timeout 1000 ms)
VESC_ESTOP_TIMEOUT_MS = 1000      # ESTOP duration on fault (ms)

# =============================================================================
# ERROR HANDLING / EXCEPTION POLICY
# =============================================================================

CONTINUE_ON_MAIN_LOOP_EXCEPTION = True
EXCEPTION_LOG_MAX = 10                 # Max exception snapshots kept in RAM ring buffer

# --- Regen braking (motor-RPM detection + current backoff) ---
# The geared hub motor has a planetary carrier with a one-way freewheel clutch.
#
# Two riding modes (throttle-gated):
#   ASSIST  — Throttle applied → motor drives wheel (forward power).
#   REGEN   — Throttle off + motor RPM detected (carrier locked by brake).
#             The rider squeezes the mechanical brake, locking the carrier
#             and back-driving the motor through the planetary gear.  The
#             controller detects this via rising motor RPM while throttle
#             is off, and issues COMM_SET_CURRENT (negative) with current
#             derived from an efficiency-optimal model:
#               I = (1−η) · λ·ωe / R_phase
#             This targets a fixed regen efficiency (η) across all speeds,
#             naturally taping to zero at low speed while routing energy
#             to the DC bus instead of dissipating as heat.
#
# Regen tapers linearly to zero between VCAP_REGEN_TAPER_START_V and
# VCAP_REGEN_TAPER_END_V.

# Motor RPM thresholds for regen detection (mechanical RPM, post-gear).
# ENTRY: motor must exceed this RPM (with rising trend) while throttle is off.
# EXIT:  motor must drop below this RPM to exit regen (hysteresis gap).
# Set above the sensorless FOC instability floor (~200 ERPM / pp) so that
# regen only runs where the VESC can track rotor angle cleanly.
# Calibrated to ~3 km/h entry (115.7 motor RPM) and ~2 km/h exit (77.2 motor RPM).
# Wheel radius 0.33 m, gear ratio 4.8:1: rpm_motor = (v_kmh/3.6) / (0.33·2π/60) × 4.8
REGEN_ENTRY_RPM = 116.0
REGEN_EXIT_RPM = 77.0

# Motor physics constants for regen current computation.
# Flux linkage measured via VESC Tool → FOC → Detect and Calculate.
FLUX_LINKAGE_WB = 0.0111        # Puyan H01 flux linkage (weber)

# Phase resistance from VESC FOC detection (foc_motor_r in MCCONF).
# Read from vesc_snapshot_mcconf.bin offset 165 = 0.082261 Ω.
# VERIFY in VESC Tool → Motor Settings → FOC → General → R.
MOTOR_PHASE_RESISTANCE_OHM = 0.082

# Rotor drag coefficient — iron losses + bearing friction, applied to
# |ω_motor|.  Identified from a bench spin-down (2026-04-20, band brake
# disengaged): rotor coasts from 30 km/h equivalent to rest in ~0.48 s
# → first-order fit gives b = J_rotor / τ ≈ 3.7e-3 Nm·s/rad.
# See sim/physics.py (T_DRAG_COEFF) for derivation details.
MOTOR_ROTOR_DRAG_NM_S = 0.0037

# Efficiency-optimal regen current model (3-phase BLDC, FOC Id=0)
# ===============================================================
# Net power to caps:  P_net = E·I − 1.5·R·I² − D·ω_m
#   where  E = λ·ωe  (back-EMF),  D = MOTOR_ROTOR_DRAG_NM_S
#          copper loss includes the 3-phase factor of 1.5
# Target efficiency η: solve 1.5·R·I² − (1−η)·E·I + D·ω_m = 0
#   → I = [(1−η)·E + √(((1−η)·E)² − 6·R·D·ω_m)] / (3·R)
# At η=70%, D=0: I ≈ (0.30/1.5) · λ·ωe / R = 0.20 · λ·ωe / R
#
# At 200 mech RPM with R=82 mΩ, η=70%:
#    I ≈ 5.2 A  →  P_net ≈ 8.2 W,  P_copper ≈ 3.3 W
REGEN_COPPER_LOSS_FRACTION = 0.25  # legacy — used by old comments only
REGEN_CURRENT_MAX_A = 40.0      # Ceiling: thermal / component limit

# Regen control strategy selection
# =================================
# Keep the runtime choice explicit but limited to the only two supported
# controllers: the PI reference controller and the AIMD-FF model.
REGEN_STRATEGY = "motor_eff_ff"
REGEN_STRATEGY_PARAMS = {
    "motor_eff_ff": dict(target_eta=0.70),
    # Tuned 2026-04-21 (run 20260421_054733, scipy DE, maxiter=200
    # popsize=36 polish=80 seeds=[7, 42, 123], robust_cvar20).
    # First tune under the static-friction scoring baseline (baseline
    # integrator removed the µ_k ratio — see sim/physics.py).
    #   aimd_ff         78.30  (seed=42)  ← runtime default
    #   pi_controller   68.79  (seed=42)  ← reference baseline
    "pi_controller": dict(
        # Composite 68.79 | E=65.9 T=61.5 S=91.9 | robust P5=64.1.
        k_ff=0.4914219881500656,
        ki=0.20315699098530135,
        decel_target=0.7820535082009775,
    ),
    "fixed_ff": dict(
        k=0.10413268626528605,
    ),
    # JERK_BOUNDARY_FF_AUTOGEN_START
    # composite=60.37 | robust mean=59.6 ±3.4 | DE seed=42 gen30 | run=20260422_002026
    # Note: unlock_thresh_jerk fixed at 80000 (not tuned — sim uses instantaneous unlock).
    "jerk_boundary_ff": dict(
        k=0.08000765817046386,
        beta_md=0.13249021187667062,
        unlock_thresh_jerk=80000.0,
        k_ai=0.008837379693296482,
    ),
    # JERK_BOUNDARY_FF_AUTOGEN_END
    # AIMD_MEAN_FF_AUTOGEN_START
    # Composite 69.20 | E=66.4 T=71.2 S=73.7 | robust mean=68.0 ±2.4 | seed=42 run=20260421_215024
    "aimd_mean_ff": dict(
        k=0.10348902475263133,
        beta_md=0.13600017774532458,
        unlock_thresh_mean=198.0,
        k_ai=0.02919838087036067,
    ),
    # AIMD_MEAN_FF_AUTOGEN_END
    # AIMD_FF_AUTOGEN_START
    # Composite 78.30 | E=67.8 T=83.7 S=86.3 | robust P5=72.7.
    "aimd_ff": dict(
        k=0.08326140868410246,
        beta_md=0.05467909462877643,
        unlock_thresh=842.0,
        k_ai=0.12576438848635152,
    ),
    # AIMD_FF_AUTOGEN_END
}

# Holdoff after throttle release before regen is allowed — prevents false
# triggers from motor inertia after assist.
REGEN_HOLDOFF_MS = 300

# --- Ride / bench debug logger (RAM ring buffer) ---
# Tuned for short dynamic tests (hallway sprints): 600 records at 10 Hz
# captures the most recent ~60 s with enough resolution to see brief regen
# engagement windows.
BENCH_LOG_PERIOD_MS = 100
BENCH_LOG_MAX_RECORDS = 600
BENCH_LOG_FIELDS = (
    "system_state",
    "cap_voltage_v",
    "vesc_mech_rpm",
    "vesc_motor_current_a",
    "vesc_input_current_a",
    "vesc_duty_cycle",
    "vesc_fault_code",
    "requested_mode",
    "requested_level",
    "throttle_raw",
    "throttle_valid",
    "inhibit_motor_commands",
    "assist_command_request",
    "regen_command_request",
    "motor_command_a",
)

# Persist ride logs to Pico flash so data survives runtime faults/resets.
# File is reset each firmware boot/session and rolls over when reaching
# BENCH_LOG_PERSIST_MAX_BYTES.
BENCH_LOG_PERSIST_ENABLE = True
BENCH_LOG_PERSIST_PATH = "/data/ride_log.csv"
BENCH_LOG_PERSIST_MAX_BYTES = 220000

# Selective capture: record only meaningful motion/torque windows.
# This avoids filling logs with long stationary periods between sprints.
BENCH_LOG_SELECTIVE_CAPTURE = True
BENCH_LOG_ACTIVE_RPM_MIN = 5.0
BENCH_LOG_ACTIVE_CMD_A_MIN = 0.5
BENCH_LOG_ACTIVE_INPUT_A_MIN = 0.3
BENCH_LOG_ACTIVE_LEVEL_MIN = 0.05

# --- Energy estimation ---
CAPACITANCE_F = 20.0

# --- Bike geometry ---
WHEEL_RADIUS_M = 0.33            # 26" wheel with tyre (~660 mm diameter)

# --- Task periods ---
FAST_LOOP_PERIOD_MS = 10           # ~100 Hz — input, supervisor, control, command TX
TELEMETRY_REQUEST_PERIOD_MS = 10   # ~100 Hz — match fast loop for tightest RPM tracking
LCD_REFRESH_PERIOD_MS = 200        # ~5 Hz
