# scripts/bench/test_regen_pipeline_trace.py
#
# End-to-end regen pipeline diagnostic: traces every stage from sensor
# inputs through PI controller to VESC response.
#
# PURPOSE
# -------
# The rider reports weak braking on the road.  This test reveals WHERE
# the signal is attenuated by logging every intermediate value at ~50 Hz:
#
#   wheel_rpm → motor_rpm → lock_frac → carrier_rpm → error_rpm
#   → p_term → i_term → pi_raw → clamped → slew_out → VESC iq
#
# TWO PHASES:
#
#   Phase 1 — Direct VESC verification
#       For each brake current level the script:
#         (a) spins the wheel with assist (hands off)
#         (b) stops assist, prints "GRAB BRAKE NOW"
#         (c) waits for carrier lock (motor RPM tracks wheel × ratio)
#         (d) sends fixed brake current and measures VESC iq response
#       Confirms the VESC hardware actually delivers requested amps
#       through the locked drivetrain.
#
#   Phase 2 — Live PI trace
#       Run the real PI controller with live sensor data.
#       Motor spins the wheel, then rider grabs the brake.
#       CSV output shows every pipeline stage each cycle.
#       Output saved to /data/phase2_trace.csv on the Pico.
#
# BENCH PROCEDURE
# ---------------
# 1. Bike on stand, wheel free to spin.
# 2. Run:  mpremote mount . run scripts/bench/test_regen_pipeline_trace.py
# 3. Phase 1: when prompted, grab the disc brake firmly.
#    Release between levels (script will re-spin the wheel).
# 4. Phase 2: grab the disc brake when prompted.
#    Watch the terminal for live status.
# 5. After test, copy data:
#      mpremote cp :data/phase2_trace.csv ./phase2_trace.csv
#
# READING THE OUTPUT
# ------------------
# Phase 1:  Look at "cmd_a" vs "iq_avg" — if iq is much less than cmd,
#           the VESC has internal limits clamping the current.
#
# Phase 2:  Follow the CSV columns left-to-right.  Wherever a value is
#           unexpectedly low or zero, that stage is the bottleneck.
#           Common suspects:
#             lock_frac ≈ 0  → carrier not detected as locked
#             carrier_rpm high → error_rpm ≈ 0 → PI has nothing to correct
#             pi_raw reasonable but slew_out low → slew limiter too slow
#             slew_out reasonable but iq low → VESC limit or fault
#
# Safety:
# - Wheel must be off the ground.
# - Keep hands/clothing clear of drivetrain.

from time import sleep_ms, ticks_ms, ticks_diff

from core import SharedState
from drivers.wheel_speed_hall import WheelSpeedHall
from services.vesc_comm import UARTPort, VESCComm
from config.settings import (
    COMMAND_SLEW_A_PER_S,
    CONTROL_LOOP_PERIOD_MS,
    REGEN_COMMAND_MAX_A,
    REGEN_LOCKED_RATIO,
    REGEN_MIN_WHEEL_RPM,
    REGEN_PI_INTEGRAL_LIMIT_A,
    REGEN_PI_KI_A_PER_RPM_S,
    REGEN_PI_KP_A_PER_RPM,
    REGEN_TARGET_SLIP_FRAC,
    VCAP_SOFT_REGEN_CUTOFF,
    WHEEL_SPEED_MAX_ACCEL_KPH_PER_S,
    WHEEL_SPEED_MAX_DECEL_KPH_PER_S,
    WHEEL_SPEED_MAX_RPM,
    WHEEL_CIRCUMFERENCE_M,
)
from utils import SlewLimiter, clamp

try:
    from machine import WDT
except Exception:
    WDT = None

try:
    import os as _os
except Exception:
    _os = None

# ── Configuration ────────────────────────────────────────────────────────

SAMPLE_PERIOD_MS = 20          # 50 Hz trace rate
TELEMETRY_PERIOD_MS = 50       # VESC telemetry request rate

# Phase 1: direct brake verification
PHASE1_SPIN_A = 4.0            # Assist current to spin wheel
PHASE1_SPIN_MS = 2500          # Spin-up time
PHASE1_LEVELS_A = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 20.0)
PHASE1_SETTLE_MS = 200
PHASE1_MEASURE_MS = 800
PHASE1_LOCK_TIMEOUT_MS = 8000  # Max wait for carrier lock (brake squeeze)
PHASE1_LOCK_THRESHOLD = 0.50   # lock_frac above this = carrier engaged

# Phase 2: live PI trace
PHASE2_SPIN_A = 4.0            # Assist current to spin wheel before each cycle
PHASE2_SPIN_MS = 2500          # Spin-up time
PHASE2_CYCLES = 5              # Number of spin→brake cycles
PHASE2_TRACE_MS = 8000         # Trace duration per cycle after spin-up

SKIP_PHASE1 = True             # Phase 1 already validated; skip to save time
DATA_DIR = "data"
DATA_FILE = DATA_DIR + "/phase2_trace.csv"

# ── Wheel speed filter (mirrors InputManager) ──────────────────────────

_KPH_TO_RPM = (1000.0 / 60.0) / max(WHEEL_CIRCUMFERENCE_M, 1e-6)
_MAX_ACCEL_RPM_PER_S = WHEEL_SPEED_MAX_ACCEL_KPH_PER_S * _KPH_TO_RPM
_MAX_DECEL_RPM_PER_S = WHEEL_SPEED_MAX_DECEL_KPH_PER_S * _KPH_TO_RPM


class WheelFilter:
    """Rate-of-change + outlier filter matching production InputManager."""

    def __init__(self):
        self._filtered = 0.0
        self._last_ms = None

    def reset(self):
        self._filtered = 0.0
        self._last_ms = None

    def update(self, raw_rpm, valid, fresh=False):
        now = ticks_ms()
        if not valid:
            return self._filtered, self._filtered > 0.0, False
        raw_rpm = max(0.0, raw_rpm)
        if raw_rpm > WHEEL_SPEED_MAX_RPM:
            return self._filtered, self._filtered > 0.0, False
        if self._last_ms is None:
            self._filtered = raw_rpm
            self._last_ms = now
            return self._filtered, True, fresh
        dt_ms = max(ticks_diff(now, self._last_ms), 10)
        dt_s = dt_ms / 1000.0
        lo = max(0.0, self._filtered - _MAX_DECEL_RPM_PER_S * dt_s)
        hi = self._filtered + _MAX_ACCEL_RPM_PER_S * dt_s
        self._filtered = clamp(raw_rpm, lo, hi)
        self._last_ms = now
        return self._filtered, True, fresh

# ── Shared setup ─────────────────────────────────────────────────────────

_DT_S = CONTROL_LOOP_PERIOD_MS / 1000.0
_SLEW_DELTA = COMMAND_SLEW_A_PER_S * _DT_S

state = SharedState()
uart = UARTPort()
vesc = VESCComm(uart, state)
wheel = WheelSpeedHall()
wheel_filter = WheelFilter()
wdt = WDT(timeout=8000) if WDT is not None else None


def _feed_wdt():
    if wdt is not None:
        wdt.feed()


def _service_vesc(last_req_ms):
    """Request telemetry if due, always service RX. Returns updated timestamp."""
    now = ticks_ms()
    if ticks_diff(now, last_req_ms) >= TELEMETRY_PERIOD_MS:
        vesc.request_telemetry()
        return now
    vesc.service_rx()
    return last_req_ms


def _read_wheel():
    """Read wheel speed from hall sensor, filtered like production code."""
    raw_rpm, valid, fresh = wheel.update()
    return wheel_filter.update(raw_rpm, valid, fresh)


# ═══════════════════════════════════════════════════════════════════════════
#  PHASE 1 — Direct VESC brake current verification
# ═══════════════════════════════════════════════════════════════════════════

def _phase1_service_loop(duration_ms, cmd_fn):
    """Run a timed loop calling cmd_fn() each cycle. Returns (iq_sum, count)."""
    start = ticks_ms()
    last_req = start
    iq_sum = 0.0
    rpm_sum = 0.0
    count = 0

    while ticks_diff(ticks_ms(), start) < duration_ms:
        _feed_wdt()
        last_req = _service_vesc(last_req)
        cmd_fn()

        iq_sum += state.vesc_iq_current_a
        rpm_sum += abs(state.vesc_mech_rpm)
        count += 1
        sleep_ms(SAMPLE_PERIOD_MS)

    return iq_sum, rpm_sum, count


def _wait_for_lock(timeout_ms):
    """Wait for carrier lock (rider grabs brake). Returns (locked, lock_frac)."""
    start = ticks_ms()
    last_req = start
    while ticks_diff(ticks_ms(), start) < timeout_ms:
        _feed_wdt()
        last_req = _service_vesc(last_req)
        vesc.send_neutral()

        wheel_rpm, _, _ = _read_wheel()
        motor_rpm = abs(state.vesc_mech_rpm)
        locked_motor_rpm = max(1e-6, wheel_rpm * REGEN_LOCKED_RATIO)
        lock_frac = min(motor_rpm / locked_motor_rpm, 1.0)

        if lock_frac >= PHASE1_LOCK_THRESHOLD:
            return True, lock_frac
        sleep_ms(SAMPLE_PERIOD_MS)
    return False, 0.0


def run_phase1():
    print()
    print("=" * 72)
    print("  PHASE 1: Direct VESC Brake Verification")
    print("=" * 72)
    print("For each level the script will:")
    print("  1. Spin up the wheel (hands off)")
    print("  2. Stop assist and say GRAB BRAKE")
    print("  3. Wait for carrier lock, then measure brake current")
    print()
    print("%-8s  %-10s  %-10s  %-8s  %-8s  %-8s" % (
        "cmd_a", "iq_avg", "rpm_avg", "lock_fr", "fault", "bus_v"))
    print("-" * 65)

    for level_a in PHASE1_LEVELS_A:
        # Spin up wheel
        print("  [Spinning up for %.0fA test...]" % level_a)
        _phase1_service_loop(PHASE1_SPIN_MS, lambda: vesc.send_assist(PHASE1_SPIN_A))
        pre_rpm = abs(state.vesc_mech_rpm)

        # Stop assist, wait for rider to grab brake
        print("  >>> GRAB BRAKE NOW (%.0f RPM spinning) <<<" % pre_rpm)
        locked, lock_frac = _wait_for_lock(PHASE1_LOCK_TIMEOUT_MS)

        if not locked:
            print("%6.1f A  -- SKIPPED (no carrier lock detected, squeeze harder)" % level_a)
            # Send neutral before next level
            for _ in range(5):
                vesc.send_neutral()
                sleep_ms(20)
            continue

        # Carrier is locked — apply brake current and measure
        _phase1_service_loop(PHASE1_SETTLE_MS, lambda: vesc.send_regen(level_a))
        iq_sum, rpm_sum, count = _phase1_service_loop(
            PHASE1_MEASURE_MS, lambda: vesc.send_regen(level_a))

        if count > 0:
            iq_avg = iq_sum / count
            rpm_avg = rpm_sum / count
        else:
            iq_avg = 0.0
            rpm_avg = 0.0

        print("%6.1f A  %+8.2f A  %8.1f    %.3f    %5d  %6.1f V" % (
            level_a, iq_avg, rpm_avg, lock_frac,
            int(state.vesc_fault_code), state.vesc_bus_voltage_v))
        print("  [Release brake]")
        # Neutral gap before next spin-up
        for _ in range(5):
            vesc.send_neutral()
            sleep_ms(20)
        sleep_ms(1500)  # Give rider time to release brake

    # Neutral
    for _ in range(10):
        vesc.send_neutral()
        sleep_ms(20)


# ═══════════════════════════════════════════════════════════════════════════
#  PHASE 2 — Live PI pipeline trace
# ═══════════════════════════════════════════════════════════════════════════

class PipelineTrace:
    """Standalone copy of the PI controller that exposes every intermediate."""

    def __init__(self):
        self.integral_a = 0.0
        self.slew = SlewLimiter(max_delta=_SLEW_DELTA)
        self.in_regen = False
        self._held_carrier_rpm = 0.0

        # Outputs (refreshed each cycle)
        self.lock_frac = 0.0
        self.carrier_rpm = 0.0
        self.carrier_slip = 0.0
        self.locked = False
        self.error_rpm = 0.0
        self.p_term = 0.0
        self.i_term = 0.0
        self.pi_raw = 0.0
        self.clamped = 0.0
        self.slew_out = 0.0
        self.regen_active = False

    def update(self, wheel_rpm, wheel_valid, wheel_fresh, motor_rpm, cap_v):
        """Run one cycle. Returns the command to send to VESC (A)."""

        # ── Gate checks (same as ControlLoop) ──
        if cap_v >= VCAP_SOFT_REGEN_CUTOFF:
            return self._reset("cap_cutoff")
        if not wheel_valid:
            return self._reset("wheel_invalid")
        if wheel_rpm < REGEN_MIN_WHEEL_RPM:
            return self._reset("too_slow")

        # ── Carrier slip estimate (for diagnostics) ──
        locked_motor_rpm = max(1e-6, wheel_rpm * REGEN_LOCKED_RATIO)
        self.lock_frac = clamp(abs(motor_rpm) / locked_motor_rpm, 0.0, 1.0)
        self.carrier_slip = 1.0 - self.lock_frac
        self.locked = self.carrier_slip < 0.30  # diagnostic only

        # Only recompute carrier_rpm on fresh wheel readings (sync fix)
        if wheel_fresh:
            self._held_carrier_rpm = wheel_rpm * (1.0 - self.lock_frac)
        self.carrier_rpm = self._held_carrier_rpm

        # Always run PI — no carrier-lock gating (production behavior)
        self.in_regen = True

        # ── PI controller (same as ControlLoop._pi_update) ──
        target_rpm = wheel_rpm * REGEN_TARGET_SLIP_FRAC
        self.error_rpm = target_rpm - self.carrier_rpm
        self.p_term = REGEN_PI_KP_A_PER_RPM * self.error_rpm

        self.integral_a += REGEN_PI_KI_A_PER_RPM_S * self.error_rpm * _DT_S
        self.integral_a = clamp(
            self.integral_a,
            0.0,
            REGEN_PI_INTEGRAL_LIMIT_A,
        )
        self.i_term = self.integral_a

        self.pi_raw = self.p_term + self.i_term
        self.clamped = clamp(self.pi_raw, 0.0, REGEN_COMMAND_MAX_A)
        self.slew_out = self.slew.update(self.clamped)
        self.regen_active = True

        return self.slew_out

    def _reset(self, _reason=""):
        self.integral_a = 0.0
        self.slew.reset(0.0)
        self._held_carrier_rpm = 0.0
        self.lock_frac = 0.0
        self.carrier_rpm = 0.0
        self.carrier_slip = 1.0
        self.locked = False
        self.error_rpm = 0.0
        self.p_term = 0.0
        self.i_term = 0.0
        self.pi_raw = 0.0
        self.clamped = 0.0
        self.slew_out = 0.0
        self.regen_active = False
        self.in_regen = False
        return 0.0


def _ensure_data_dir():
    """Create data/ directory on Pico if it doesn't exist."""
    if _os is None:
        return
    try:
        _os.stat(DATA_DIR)
    except OSError:
        _os.mkdir(DATA_DIR)


_CSV_HEADER = ",".join([
    "cycle", "t_ms",
    "whl_rpm", "whl_ok", "whl_fresh",
    "mot_rpm",
    "lock_fr", "c_slip", "locked",
    "err_rpm",
    "p_term", "i_term", "pi_raw",
    "clamp", "slew",
    "iq", "mot_i", "fault", "bus_v",
])


def run_phase2():
    print()
    print("=" * 72)
    print("  PHASE 2: Live PI Pipeline Trace (%d cycles)" % PHASE2_CYCLES)
    print("=" * 72)
    print("Settings: KP=%.2f  KI=%.2f  max=%.1fA  slew=%.1f A/s  slip_target=%.2f" % (
        REGEN_PI_KP_A_PER_RPM, REGEN_PI_KI_A_PER_RPM_S, REGEN_COMMAND_MAX_A,
        COMMAND_SLEW_A_PER_S, REGEN_TARGET_SLIP_FRAC))
    print("Always-REGEN mode (no carrier-lock gating), locked ratio=%.1f" % (
        REGEN_LOCKED_RATIO,))
    print("Integral windup floor=0  Wheel filter: accel=%.0f decel=%.0f RPM/s  max=%.0f RPM" % (
        _MAX_ACCEL_RPM_PER_S, _MAX_DECEL_RPM_PER_S, WHEEL_SPEED_MAX_RPM))
    print()
    print("Each cycle: motor spins wheel, then GRAB BRAKE when prompted.")
    print("Data saved to %s" % DATA_FILE)
    print()

    _ensure_data_dir()
    f = open(DATA_FILE, "w")
    f.write(_CSV_HEADER + "\n")

    for cycle in range(1, PHASE2_CYCLES + 1):
        # Reset wheel filter for each cycle (fresh spin-up)
        wheel_filter.reset()

        # Spin up wheel with assist
        print("# Cycle %d/%d — spinning up..." % (cycle, PHASE2_CYCLES))
        _phase1_service_loop(PHASE2_SPIN_MS, lambda: vesc.send_assist(PHASE2_SPIN_A))
        pre_rpm = abs(state.vesc_mech_rpm)
        print("# >>> GRAB BRAKE NOW (%.0f RPM) <<<" % pre_rpm)

        # Trace the PI pipeline while wheel decelerates
        trace = PipelineTrace()
        start = ticks_ms()
        last_req = start
        last_print = start
        last_status = start
        rows = 0
        peak_slew = 0.0

        while ticks_diff(ticks_ms(), start) < PHASE2_TRACE_MS:
            _feed_wdt()
            now = ticks_ms()

            last_req = _service_vesc(last_req)
            wheel_rpm, wheel_valid, wheel_fresh = _read_wheel()

            cmd_a = trace.update(
                wheel_rpm, wheel_valid, wheel_fresh,
                state.vesc_mech_rpm,
                state.vesc_bus_voltage_v,
            )

            if cmd_a > 0.0:
                vesc.send_regen(cmd_a)
            else:
                vesc.send_neutral()

            if ticks_diff(now, last_print) >= SAMPLE_PERIOD_MS:
                last_print = now
                elapsed = ticks_diff(now, start)
                line = "%d,%d,%.1f,%d,%d,%.1f,%.3f,%.3f,%d,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%+.2f,%+.2f,%d,%.1f" % (
                    cycle, elapsed,
                    wheel_rpm, int(wheel_valid), int(wheel_fresh),
                    state.vesc_mech_rpm,
                    trace.lock_frac, trace.carrier_slip, int(trace.locked),
                    trace.error_rpm,
                    trace.p_term, trace.i_term, trace.pi_raw,
                    trace.clamped, trace.slew_out,
                    state.vesc_iq_current_a, state.vesc_motor_current_a,
                    int(state.vesc_fault_code), state.vesc_bus_voltage_v,
                )
                f.write(line + "\n")
                rows += 1
                if trace.slew_out > peak_slew:
                    peak_slew = trace.slew_out

            # Live status to terminal every 500ms
            if ticks_diff(now, last_status) >= 500:
                last_status = now
                elapsed_s = ticks_diff(now, start) / 1000.0
                print("  %.1fs  whl=%.0f mot=%.0f slip=%.3f cmd=%.1fA iq=%+.1fA int=%.1f" % (
                    elapsed_s, wheel_rpm, state.vesc_mech_rpm,
                    trace.carrier_slip, trace.slew_out,
                    state.vesc_iq_current_a, trace.i_term))

            sleep_ms(max(1, SAMPLE_PERIOD_MS - ticks_diff(ticks_ms(), now)))

        # Neutral between cycles
        for _ in range(10):
            vesc.send_neutral()
            sleep_ms(20)
        print("# Cycle %d done: %d rows, peak_cmd=%.1fA. Release brake." % (
            cycle, rows, peak_slew))
        sleep_ms(1500)

    f.close()
    print()
    print("Phase 2 complete. Data saved to %s" % DATA_FILE)


# ═══════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════

print()
print("*" * 72)
print("  REGEN PIPELINE DIAGNOSTIC")
print("*" * 72)

if not SKIP_PHASE1:
    run_phase1()
else:
    print("(Phase 1 skipped — VESC hardware already validated)")

run_phase2()

# Final neutral
for _ in range(10):
    vesc.send_neutral()
    sleep_ms(20)

print()
print("All done.")
