# config/settings.py â€” consolidated project constants
#
# Hardware: Raspberry Pi Pico (RP2040) + Flipsky Mini FSESC4.20 (hw 410, FW 6.6)
# All Pico GPIO are 3.3 V.
# FSESC4.20 UART is also 3.3 V â€” direct connection, no level-shifter needed.

# =============================================================================
# PIN ASSIGNMENTS
# =============================================================================
#
# Pico physical pin â†’ GPIO map (physical / GPIO / function)
#   Pin 6  / GP4  / UART1 TX â†’ FSESC UART RX
#   Pin 7  / GP5  / UART1 RX â† FSESC UART TX
#   Pin 11 / GP8  / Soft reset button (active-low, internal pull-up)
#   Pin 22 / GP17 / LCD RS (Register Select)
#   Pin 24 / GP18 / LCD E  (Enable)
#   Pin 25 / GP19 / LCD D4
#   Pin 26 / GP20 / LCD D5
#   Pin 27 / GP21 / LCD D6
#   Pin 29 / GP22 / LCD D7
#   Pin 34 / GP28 / LCD backlight enable (active-high)
#   Pin 31 / GP26 / ADC0 â€” Hall throttle

# --- UART1 to FSESC ---
VESC_UART_ID = 1              # Pico hardware UART1
VESC_UART_TX = 4              # GP4, Pico pin 6 â†’ FSESC UART RX
VESC_UART_RX = 5              # GP5, Pico pin 7 â† FSESC UART TX

# --- Hall throttle (3-wire, 5 V supply, analog out 0.8â€“4.2 V typical) ---
THROTTLE_ADC_PIN = 26         # GP26 / ADC0

# --- Soft reset button (active-low, normally-open to GND, internal pull-up) ---
RESET_BUTTON_PIN = 8          # GP8, Pico pin 11

# --- LCD selection --------------------------------------------------------
# LCD_TYPE = "parallel" â†’ drivers/lcd_driver.py  (HD44780 4-bit over 6 GPIO)
# LCD_TYPE = "i2c"      â†’ drivers/lcd_driver_i2c.py (PCF8574 backpack)
# main.py picks the driver based on this string.  Everything downstream
# (DisplayManager) is driver-agnostic.
LCD_TYPE = "parallel"
LCD_COLS = 16
LCD_ROWS = 2

# --- LCD (parallel: RG1602A, ST7066U/HD44780, 4-bit parallel GPIO) -------
LCD_RS_PIN = 17               # GP17, Pico pin 22
LCD_E_PIN = 18                # GP18, Pico pin 24
LCD_D4_PIN = 19               # GP19, Pico pin 25
LCD_D5_PIN = 20               # GP20, Pico pin 26
LCD_D6_PIN = 21               # GP21, Pico pin 27
LCD_D7_PIN = 22               # GP22, Pico pin 29
LCD_BL_PIN = 28               # GP28, Pico pin 34 â€” backlight enable (active-high)

# --- LCD (I2C: PCF8574 backpack, HD44780-compatible) ---------------------
# Typical backpack addresses are 0x27 (most common) or 0x3F.
# Run i2c.scan() from a REPL once to confirm for your module.
LCD_I2C_ADDR = 0x27
LCD_I2C_BUS = 0               # I2C0 on the Pico
LCD_I2C_SDA_PIN = 16          # GP16, Pico pin 21 â€” I2C0 SDA
LCD_I2C_SCL_PIN = 17          # GP17, Pico pin 22 â€” I2C0 SCL
LCD_I2C_FREQ = 100_000        # 100 kHz standard-mode; 400 kHz also works

# =============================================================================
# VOLTAGE & CURRENT THRESHOLDS
# =============================================================================

# --- Supercapacitor voltage thresholds (volts) ---
# Cap bank: 48 V rated.  Motor: 48 V system.  VESC FSESC4.20: 50 V rated.
# 42 V is the chosen operational ceiling â€” leaves 6 V headroom to cap rating.
VCAP_MIN_OPERATING = 10.0        # Below this: precharge active, motor inhibited
VCAP_REGEN_TAPER_START_V = 40.0  # Regen current starts linearly tapering down
VCAP_REGEN_TAPER_END_V = 42.0    # Regen current reaches zero (software)
VCAP_ABSOLUTE_MAX = 43.0         # Operational max â€” supervisor faults here (5 V below 48 V cap rating)

# --- Motor current limit (hard limit â€” must also be set in VESC Tool) ---
# FSESC4.20 is rated 50 A continuous.
MOTOR_CURRENT_MAX_A = 50.0     # VESC-side motor current limit (amps)
VESC_WATT_MAX = 1500.0         # VESC watt limit â€” both drive and regen (watts)

# Absolute maximum instantaneous phase current before ABS_OVER_CURRENT fault.
# FSESC4.20 FETs handle 130 A+ transiently â€” this protects against dead-shorts,
# not normal FOC commutation ripple.  Applied to VESC via Lisp conf-set.
VESC_ABS_CURRENT_MAX_A = 130.0

# Firmware command ceiling â€” the maximum current the Pico will ever command.
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
THROTTLE_DEADBAND = 0.05        # Fraction of range â€” widens grace zone to cover RP2040 ADC noise
THROTTLE_OVERSAMPLE = 4         # Average N reads per sample â€” halves noise amplitude
THROTTLE_FAULT_LOW = 100        # Below this raw count â†’ open-circuit / fault
THROTTLE_FAULT_HIGH = 4000      # Above this raw count â†’ short-circuit / fault

# =============================================================================
# VESC SETTINGS
# =============================================================================
# Hardware: Flipsky Mini FSESC4.20, based on VESCÂ® 4.12
# UART signals are 3.3 V logic (STM32F4 core) â€” direct connection to Pico GPIO.

VESC_BAUD_RATE = 115200                # Verify in VESC Tool â†’ App Settings â†’ UART baud

# Puyan H01-Front Drive Geared Hub Motor â€” 11 pole pairs (22 magnets).
# VERIFY with VESC Tool â†’ Motor Settings â†’ FOC â†’ Detect and Calculate.
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
#   ASSIST  â€” Throttle applied â†’ motor drives wheel (forward power).
#   REGEN   â€” Throttle off + motor RPM detected (carrier locked by brake).
#             The rider squeezes the mechanical brake, locking the carrier
#             and back-driving the motor through the planetary gear.  The
#             controller detects this via rising motor RPM while throttle
#             is off, and issues COMM_SET_CURRENT (negative) with current
#             derived from an efficiency-optimal model:
#               I = (1âˆ’Î·) Â· Î»Â·Ï‰e / R_phase
#             This targets a fixed regen efficiency (Î·) across all speeds,
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
# Wheel radius 0.33 m, gear ratio 4.8:1: rpm_motor = (v_kmh/3.6) / (0.33Â·2Ï€/60) Ã— 4.8
REGEN_ENTRY_RPM = 116.0
REGEN_EXIT_RPM = 77.0

# Motor physics constants for regen current computation.
# Flux linkage measured via VESC Tool â†’ FOC â†’ Detect and Calculate.
FLUX_LINKAGE_WB = 0.0111        # Puyan H01 flux linkage (weber)

# Phase resistance from VESC FOC detection (foc_motor_r in MCCONF).
# Read from vesc_snapshot_mcconf.bin offset 165 = 0.082261 Î©.
# VERIFY in VESC Tool â†’ Motor Settings â†’ FOC â†’ General â†’ R.
MOTOR_PHASE_RESISTANCE_OHM = 0.082

# Rotor drag coefficient â€” iron losses + bearing friction, applied to
# |Ï‰_motor|.  Identified from a bench spin-down (2026-04-22, band brake
# disengaged): rotor coasts from 35 km/h equivalent to rest in ~1.5 s
# â†’ first-order fit gives b = J_rotor / Ï„ â‰ˆ 1.2e-3 NmÂ·s/rad.
# Supersedes the earlier 2026-04-20 estimate (0.5 s â†’ 3.7e-3).
# See sim/physics.py (T_DRAG_COEFF) for derivation details.
MOTOR_ROTOR_DRAG_NM_S = 0.0012

# Efficiency-optimal regen current model (3-phase BLDC, FOC Id=0)
# ===============================================================
# Net power to caps:  P_net = EÂ·I âˆ’ 1.5Â·RÂ·IÂ² âˆ’ DÂ·Ï‰_m
#   where  E = Î»Â·Ï‰e  (back-EMF),  D = MOTOR_ROTOR_DRAG_NM_S
#          copper loss includes the 3-phase factor of 1.5
# Target efficiency Î·: solve 1.5Â·RÂ·IÂ² âˆ’ (1âˆ’Î·)Â·EÂ·I + DÂ·Ï‰_m = 0
#   â†’ I = [(1âˆ’Î·)Â·E + âˆš(((1âˆ’Î·)Â·E)Â² âˆ’ 6Â·RÂ·DÂ·Ï‰_m)] / (3Â·R)
# At Î·=70%, D=0: I â‰ˆ (0.30/1.5) Â· Î»Â·Ï‰e / R = 0.20 Â· Î»Â·Ï‰e / R
#
# At 200 mech RPM with R=82 mÎ©, Î·=70%:
#    I â‰ˆ 5.2 A  â†’  P_net â‰ˆ 8.2 W,  P_copper â‰ˆ 3.3 W
REGEN_COPPER_LOSS_FRACTION = 0.25  # legacy â€” used by old comments only
REGEN_CURRENT_MAX_A = 40.0      # Ceiling: thermal / component limit

# Regen control strategy selection
# =================================
# Keep the runtime choice explicit but limited to the only two supported
# controllers: the PI reference controller and the AIMD-FF model.
REGEN_STRATEGY = "aimd_ff"
REGEN_STRATEGY_PARAMS = {
    # Retuned 2026-04-23 (run 20260423_091738, Optuna TPE + MedianPruner,
    # JAX backend, 200 trials, seeds=[7,11], 30-sample robustness sweep).
    # This tune uses the Ï‰_ours power-tracking fidelity metric:
    #   S_F = 100 * (1 - âˆ«|P_regen - P_base|dt / âˆ«P_base dt)
    # where P_regen = P_elec + P_copper + P_band is the total wheel-decelerating
    # power and P_base = brake_demand * Ï‰_ours is the ideal-friction-brake
    # target at the strategy's own wheel speed â€” replaces the earlier Ï‰_base
    # variant which punished strategies for slowing the wheel less aggressively.
    # Capture is the harvested-energy ratio at Ï‰_ours:
    #   S_C = 100 * clamp(âˆ«P_elec dt / âˆ«(brake_demandÂ·Ï‰_ours) dt)
    # over brake windows. Composite = 0.40*S_C + 0.60*S_F.
    # aimd_ff stays the shipped runtime REGEN_STRATEGY for its slip-backoff.
    # Composite 37.32 | C=15.1 F=54.3 | cvar20=26.7 | seed=7 | run=20260423_105037
    # (observation-only PI: alpha weights motor-attributable decel proxy).
    "pi_controller": dict(
        k_ff=0.21880074648534797,
        ki=0.20000248403765908,
        alpha=0.8023389374966289,
    ),
    "fixed_ff": dict(
        # Composite 41.40 | C=26.7 F=56.2 | cvar20=37.1 | seed=7
        # run=20260423_091738 (Optuna TPE, 200 trials, jax backend)
        k=0.16171799232844863,
    ),
    # AIMD_FF_AUTOGEN_START
    # Composite 43.35 | C=33.2 F=58.0 | cvar20=38.9 | seed=11 | run=20260423_105037
    "aimd_ff": dict(
        k=0.26043566804415147,
        beta_md=0.13999752213684555,
        unlock_thresh=869,
        k_ai=0.08038794982282485,
    ),
    # AIMD_FF_AUTOGEN_END
    # Sim-only â€” not loaded by firmware (firmware ships the PySR distill).
    # Retrain produces best theta under sim/output/neural_teacher/.
    "neural_teacher_gru": dict(
        theta_path="sim/output/neural_teacher/gru_theta_v2.npz",
    ),
}

# Holdoff after throttle release before regen is allowed â€” prevents false
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
FAST_LOOP_PERIOD_MS = 10           # ~100 Hz â€” input, supervisor, control, command TX
TELEMETRY_REQUEST_PERIOD_MS = 10   # ~100 Hz â€” match fast loop for tightest RPM tracking
LCD_REFRESH_PERIOD_MS = 200        # ~5 Hz
