# config.py — every constant in one place, with provenance.
#
# [BENCH] = placeholder until the spec §11 measurement schedule replaces it.

try:
    from micropython import const
except ImportError:  # CPython (host tests)
    def const(x):
        return x

# --- Pins (drawing RGX-2-100 Rev D sheet 3, spec §8) -------------------------
PIN_UART_TX = const(0)        # -> A1 UART RX via R3
PIN_UART_RX = const(1)        # <- A1 UART TX via R4
PIN_SDA = const(4)            # DS1
PIN_SCL = const(5)
PIN_SPD = const(13)           # shell speed sensor via R6, pull-up on
PIN_THR = const(26)           # ADC0, throttle

# --- VESC link (RGX-2-003 D3-D7) --------------------------------------------
UART_ID = const(0)
UART_BAUD = const(115200)
UART_RXBUF = const(1024)
TICK_MS = const(10)           # 100 Hz (D8). The loop is fixed-rate, so control
DT = TICK_MS / 1000           # never does time arithmetic: timeouts count ticks.
                              # Each tick sends a current command and a
                              # telemetry request (20 bytes) and gets a 27-byte
                              # reply: at most a quarter of either direction.
LINK_TIMEOUT_TICKS = const(25)    # 250 ms of silence -> LIMP (D7)
LINK_RECOVER_FRAMES = const(10)   # consecutive clean frames before RUN

# --- Mechanics ---------------------------------------------------------------
# Sign convention, fixed by provisioning (tools/A1-SETUP.md): A1's motor
# direction is set so +current drives the wheel forward. With the carrier held
# (clutch in assist, brake in regen) the rotor then turns at +k x wheel, so
# ERPM >= 0 whenever torque flows, +amps motor and -amps generate. There is no
# direction parameter to get wrong.
K_RATIO = 5.0                 # [BENCH] ring/sun, teardown item 9
POLE_PAIRS = 10               # [BENCH] spec §11 item 2
WHEEL_CIRC_M = 2.10           # [BENCH] measure the actual tyre (display only)
SPD_K = 60_000_000 / 6        # rpm x us: wheel rpm = SPD_K / period_us (6 PPR)
SPD_MIN_PHASE_US = 3000       # shorter half-period = glitch (60 km/h is ~10 ms)
SPD_MIN_RPM = 10.0            # below ~1.3 km/h the wheel reads 0
W_MIN_RPM = 24.0              # ~3 km/h: slip undefined and no regen below this
                              # (regen current near standstill backs the wheel up)

# --- Regen: slip regulation ------------------------------------------------
# The rider's carrier brake sets how much torque the carrier can hold; regen
# current is driven until the carrier just slips at SLIP_SET, so braking
# follows the lever and (1 - SLIP_SET) of it is harvested.
SLIP_SET = 0.12               # [BENCH] allowed slip = pad-loss fraction. 6 PPR
                              # staleness needs >= 0.10-0.15 (motor-selection §4)
SLIP_KP = 100.0               # [BENCH] A per unit slip error. The carrier is an
SLIP_KI = 300.0               # [BENCH] A/s per unit   integrating plant, so I-only
                              # control limit-cycles; these settle with <= 60 ms of
                              # slip staleness in a toy plant. Tune in the sim.

# --- Safety envelope (spec §3, §10; RGX-2-003 §3 as amended) -----------------
I_ASSIST_MAX = 40.0           # A1 battery limit mirror (spec §10.4)
I_REGEN_MAX = 40.0
SLEW_STEP_A = 2.0             # per tick = 200 A/s on the requested current
R_BANK = 0.367                # [BENCH] bank ESR + wiring, worst case (spec §4.2)
V_TERM_MAX = 39.0             # regen holds A1's terminal 1 V under its 40 V OV trip
V_TERM_MIN = 9.0              # assist holds it above A1 start-up 8 V + 1 V (spec §3)

# --- Throttle (spec §10.3; idle/full from the v1 sweep of this unit) ---------
THR_LO = 0.20                 # outside [LO, HI] = open or shorted -> no assist
THR_HI = 0.85
THR_IDLE = 0.262              # [BENCH] re-measure on the v2 harness
THR_FULL = 0.79               # [BENCH]
THR_DEADBAND = 0.05           # fraction of span; also where assist arms

# --- Housekeeping ------------------------------------------------------------
WDT_MS = const(2000)
GC_DIV = const(10)            # scheduled gc.collect() every N ticks

# --- Display (RGX-2-003 D14 as amended) ----------------------------------------
OLED_ADDR = const(0x3C)
I2C_FREQ = const(400_000)
DISPLAY_MS = const(200)       # 5 Hz
TEMP_HOT = 80.0               # [BENCH] A1 transistor temperature, degrees C. A1
                              # starts cutting current at its own "MOSFET temp
                              # cutoff start" (85 by default): warn just before.
TEMP_COLD = -10.0             # [BENCH] at rest this is roughly the air
                              # temperature. Colder, the bank's resistance rises
                              # past R_BANK and the voltage limits lose margin.
