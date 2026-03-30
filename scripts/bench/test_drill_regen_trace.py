# scripts/bench/test_drill_regen_trace.py
#
# Drill-spin regen diagnostic: runs the REAL production pipeline
# (InputManager + StateMachine + ControlLoop + CommandManager)
# while logging every intermediate value at ~50 Hz.
#
# PROCEDURE
# ---------
# 1. Bike on stand, drill attached to wheel axle or tire.
# 2. Run:  mpremote mount . run scripts/bench/test_drill_regen_trace.py
# 3. Spin wheel with drill (any reasonable speed).
# 4. While drill is spinning, squeeze the disc brake.
# 5. Hold for 10+ seconds — this is the interesting part.
# 6. Release brake, stop drill.
# 7. Press Ctrl-C to stop logging.
# 8. Copy data:  mpremote cp :data/drill_trace.csv ./drill_trace.csv
#
# The CSV captures the full pipeline every 20ms so we can see exactly
# where braking current is being limited or reset.

from time import sleep_ms, ticks_ms, ticks_diff

from core import SharedState, SystemState, CommandMode, FaultManager, FaultCode, EnergyEstimator
from drivers.wheel_speed_hall import WheelSpeedHall
from drivers.throttle import Throttle
from services.vesc_comm import UARTPort, VESCComm, CommandManager
from services.input_manager import InputManager
from services.control_loop import ControlLoop
from services.safety_supervisor import SafetySupervisor
from app.state_machine import StateMachine

try:
    from machine import WDT
except Exception:
    WDT = None

try:
    import os as _os
except Exception:
    _os = None

# ── Configuration ────────────────────────────────────────────────────────

SAMPLE_PERIOD_MS = 20          # 50 Hz logging rate
TELEMETRY_PERIOD_MS = 50       # VESC telemetry request rate
DATA_DIR = "data"
DATA_FILE = DATA_DIR + "/drill_trace.csv"
LOG_DURATION_MS = 60_000       # 60s max (Ctrl-C to stop early)

# ── Setup — real production objects ──────────────────────────────────────

state = SharedState()
faults = FaultManager(state)
uart = UARTPort()
throttle = Throttle()
wheel = WheelSpeedHall()
vesc = VESCComm(uart, state)
input_mgr = InputManager(throttle, state, wheel)
control_loop = ControlLoop(state)
command_mgr = CommandManager(vesc, state)
safety = SafetySupervisor(state, faults)
energy = EnergyEstimator(state)
state_machine = StateMachine(state, faults)

wdt = WDT(timeout=8000) if WDT is not None else None


def _feed_wdt():
    if wdt is not None:
        wdt.feed()


def _ensure_data_dir():
    if _os is None:
        return
    try:
        _os.stat(DATA_DIR)
    except OSError:
        _os.mkdir(DATA_DIR)


_CSV_HEADER = ",".join([
    "t_ms",
    "sys_state", "req_mode",
    "whl_rpm", "whl_ok", "whl_fresh",
    "mot_rpm",
    "carrier_rpm", "err_rpm",
    "integral", "regen_cmd",
    "assist_cmd",
    "iq", "mot_i",
    "cap_v", "bus_v", "fault",
    "inhibit",
])

_SYS_STATE_MAP = {
    SystemState.OFF: 0, SystemState.PRECHARGE: 1,
    SystemState.ASSIST: 3, SystemState.REGEN: 4, SystemState.FAULT: 5,
}
_MODE_MAP = {
    CommandMode.NEUTRAL: 0, CommandMode.ASSIST: 1, CommandMode.REGEN: 2,
}


# ── Main diagnostic loop ────────────────────────────────────────────────

print()
print("*" * 72)
print("  DRILL REGEN TRACE — real production pipeline + CSV logging")
print("*" * 72)
print()
print("1. Spin wheel with drill")
print("2. Squeeze brake while spinning")
print("3. Hold brake for 10+ seconds")
print("4. Ctrl-C to stop, then copy drill_trace.csv")
print()
print("Logging to %s for up to %ds" % (DATA_FILE, LOG_DURATION_MS // 1000))
print()

# Let safety supervisor clear inhibit after initial checks
state.inhibit_motor_commands = True

_ensure_data_dir()
f = open(DATA_FILE, "w")
f.write(_CSV_HEADER + "\n")

start = ticks_ms()
last_req = start
last_log = start
rows = 0

try:
    while ticks_diff(ticks_ms(), start) < LOG_DURATION_MS:
        _feed_wdt()
        now = ticks_ms()

        # 1. Poll inputs (throttle + wheel + mode detection)
        input_mgr.update()

        # 2. Service VESC RX + telemetry requests
        if ticks_diff(now, last_req) >= TELEMETRY_PERIOD_MS:
            vesc.request_telemetry()
            last_req = now
        else:
            vesc.service_rx()

        # 3. Energy estimation
        energy.update()

        # 4. Safety supervisor
        safety.update()

        # 5. State machine
        state_machine.update()

        # 6. Control loop (PI slip controller)
        control_loop.update()

        # 7. Command manager (sends to VESC)
        command_mgr.update()

        # 8. Log at sample rate
        if ticks_diff(now, last_log) >= SAMPLE_PERIOD_MS:
            last_log = now
            elapsed = ticks_diff(now, start)

            line = "%d,%d,%d,%.1f,%d,%d,%.1f,%.1f,%.2f,%.2f,%.2f,%.2f,%+.2f,%+.2f,%.1f,%.1f,%s,%d" % (
                elapsed,
                _SYS_STATE_MAP.get(state.system_state, -1),
                _MODE_MAP.get(state.requested_mode, -1),
                state.wheel_speed_rpm, int(state.wheel_speed_valid),
                int(state.wheel_speed_fresh),
                state.vesc_mech_rpm,
                state.gear_carrier_speed_rpm, state.regen_speed_error_rpm,
                control_loop._regen_integral_a, state.regen_command_request,
                state.assist_command_request,
                state.vesc_iq_current_a, state.vesc_motor_current_a,
                state.cap_voltage_v, state.vesc_bus_voltage_v,
                state.vesc_fault_code, int(state.inhibit_motor_commands),
            )
            f.write(line + "\n")
            rows += 1

            # Live status every 500ms
            if rows % 25 == 0:
                print("  %5.1fs  st=%-8s mode=%-7s whl=%3.0f mot=%4.0f car=%4.1f int=%4.1f cmd=%4.1fA iq=%+5.1fA inh=%d" % (
                    elapsed / 1000.0, state.system_state, state.requested_mode,
                    state.wheel_speed_rpm, state.vesc_mech_rpm,
                    state.gear_carrier_speed_rpm,
                    control_loop._regen_integral_a,
                    state.regen_command_request,
                    state.vesc_iq_current_a,
                    int(state.inhibit_motor_commands),
                ))

        sleep_ms(max(1, 10 - ticks_diff(ticks_ms(), now)))

except KeyboardInterrupt:
    print("\n  [Stopped by user]")

# Neutral on exit
for _ in range(10):
    vesc.send_neutral()
    sleep_ms(20)

f.close()
print()
print("Saved %d rows to %s" % (rows, DATA_FILE))
print("Copy with:  mpremote cp :data/drill_trace.csv ./drill_trace.csv")
