# config.py — every constant in one place, with provenance.
#
# Values marked [BENCH] are placeholders until the spec §11 measurement
# schedule replaces them. Nothing else in the firmware hardcodes a number.

try:
    from micropython import const
except ImportError:  # CPython (host tests)
    def const(x):
        return x

# --- Pins (drawing RGX-2-100 Rev D, sheet 3) --------------------------------
PIN_UART_TX = const(0)        # GP0  -> A1 UART RX (via R3)
PIN_UART_RX = const(1)        # GP1  <- A1 UART TX (via R4)
PIN_I2C_SDA = const(4)        # GP4  -> DS1
PIN_I2C_SCL = const(5)        # GP5  -> DS1
PIN_SPD = const(13)           # GP13 <- shell speed (white wire via R6)
PIN_BRAKE = const(14)         # GP14 <- optional lever sensor (D9); active low
BRAKE_FITTED = False          # flip when the sensor is installed
ADC_THROTTLE = const(0)       # ADC0 = GP26
ADC_VSYS = const(3)           # ADC3 = VSYS/3 (Pico internal)

# --- VESC link (RGX-2-003 D3-D7) --------------------------------------------
UART_ID = const(0)
UART_BAUD = const(115200)
UART_RXBUF = const(1024)      # postmortem #1: never default this again
UART_TXBUF = const(256)
TELEM_MASK = const(0x818C)    # i_motor, i_in, erpm, v_in, fault  (25 B resp)
TELEM_MASK_TEMP = const(0x818D)  # + temp_fet, polled sparsely
TICK_MS = const(10)           # 100 Hz control loop (D8)
TELEM_DIV = const(2)          # telemetry request every 2nd tick (50 Hz)
TEMP_DIV = const(50)          # every 50th request carries the temp mask (1 Hz)
LINK_TIMEOUT_MS = const(250)  # silence -> LIMP (D7)
LINK_RECOVER_FRAMES = const(10)
VESC_TIMEOUT_MS = const(200)  # provisioned on A1; documented here for the pair

# --- Kinematics (spec §1, BOM §5.1) -----------------------------------------
K_RATIO = 5.0                 # [BENCH] ring/sun, teardown item 9
POLE_PAIRS = const(10)        # 20 magnets (BOM §5.1)
DIR_SIGN = 1                  # [BENCH] B-2: ERPM sign for forward assist
WHEEL_CIRC_M = 2.10           # [BENCH] measure actual tyre
SPD_PPR = const(6)            # shell sensor pulses/rev (BOM §5.1, confirmed)
SPD_ZERO_MS = const(400)      # no edge -> speed decays to zero (§7.1)
SPD_MIN_PERIOD_US = const(5000)  # <5 ms period = >60 km/h = noise, reject

# --- Safety envelope (RGX-2-001 §9/§10, RGX-2-002 §5.3) ---------------------
V_BANK_MAX = 40.0             # regen zero here (spec §10.8)
V_TAPER_START = 38.0          # linear taper start
V_CROSSOVER_GUARD = 39.0      # with speed guard below -> regen zero
CROSSOVER_KMH = 28.0          # [BENCH] recompute from measured kV (gate 2)
I_ASSIST_MAX_A = 40.0         # A1 battery limit mirror (spec §10.4)
I_REGEN_MAX_A = 40.0
SLEW_A_PER_S = 200.0          # no strategy bug may step the torque (D-env)
THROTTLE_LO = 0.20            # spec §10.3 window, fraction of full scale
THROTTLE_HI = 0.85
THROTTLE_FAIL_MS = const(50)

# --- Loop housekeeping (RGX-2-003 §3) ---------------------------------------
WDT_MS = const(2000)
WDT_ENABLE = True             # set False only on a bench with the REPL
GC_DIV = const(10)            # scheduled gc.collect() every N ticks

# --- Logging (RGX-2-003 D13) ------------------------------------------------
LOG_RING_RECORDS = const(4096)   # x 24 B = 96 KB
LOG_RIDE_HZ = const(10)
LOG_IDLE_HZ = const(1)
STANDSTILL_MS = const(3000)   # wheel=0 and throttle idle this long -> flushable
LOG_DIR = "/logs"

# --- Display ----------------------------------------------------------------
OLED_ADDR = const(0x3C)
OLED_W = const(128)
OLED_H = const(64)
I2C_FREQ = const(400_000)
DISPLAY_HZ = const(5)
