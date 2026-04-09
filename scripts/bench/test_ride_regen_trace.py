# scripts/bench/test_ride_regen_trace.py
#
# Ride-ready regen diagnostic: runs the REAL production pipeline
# (InputManager + StateMachine + ControlLoop + CommandManager)
# WITHOUT the LCD, with CSV logging.
#
# PURPOSE: The drill test works, but main.py (with LCD) doesn't.
# This deploys the exact same pipeline as the drill test but for road use.
# If regen works → LCD/AppController is the culprit.
# If regen doesn't work → we have ride data to analyze.
#
# PROCEDURE
# ---------
# 1. Deploy:  mpremote mount . run scripts/bench/test_ride_regen_trace.py
# 2. Ride normally. Brake normally.
# 3. Return, Ctrl-C to stop.
# 4. Check CSV: data/ride_trace.csv
#
# NOTE: No LCD output during this test. Just serial terminal status.

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

CONTROL_PERIOD_MS = 10         # 100 Hz main loop (matches production)
TELEMETRY_PERIOD_MS = 50       # VESC telemetry request rate
LOG_PERIOD_MS = 20             # 50 Hz CSV logging
DATA_DIR = "data"
DATA_FILE = DATA_DIR + "/ride_trace.csv"

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


_SYS_STATE_MAP = {
    SystemState.OFF: 0, SystemState.PRECHARGE: 1,
    SystemState.ASSIST: 3, SystemState.REGEN: 4, SystemState.FAULT: 5,
}
_MODE_MAP = {
    CommandMode.NEUTRAL: 0, CommandMode.ASSIST: 1, CommandMode.REGEN: 2,
}

_CSV_HEADER = ",".join([
    "t_ms",
    "sys_state", "req_mode",
    "whl_rpm", "whl_ok", "whl_fresh",
    "mot_rpm",
    "mot_i_actual", "regen_tgt",
    "regen_cmd",
    "assist_cmd",
    "iq", "mot_i",
    "cap_v", "bus_v", "fault",
    "inhibit",
])


# ── Main loop ────────────────────────────────────────────────────────────

print()
print("*" * 72)
print("  RIDE REGEN TRACE — production pipeline, no LCD, CSV logging")
print("*" * 72)
print()
print("Ride normally. Brake normally. Ctrl-C to stop.")
print("Data: %s" % DATA_FILE)
print()

state.inhibit_motor_commands = True

_ensure_data_dir()
f = open(DATA_FILE, "w")
f.write(_CSV_HEADER + "\n")

start = ticks_ms()
last_telem = start
last_log = start
last_status = start
rows = 0

try:
    while True:
        _feed_wdt()
        now = ticks_ms()

        # 1. Poll inputs (throttle + wheel + mode detection)
        input_mgr.update()

        # 2. Service VESC RX + telemetry requests
        if ticks_diff(now, last_telem) >= TELEMETRY_PERIOD_MS:
            vesc.request_telemetry()
            last_telem = now
        else:
            vesc.service_rx()

        # 3. Energy estimation
        energy.update()

        # 4. Safety supervisor
        safety.update()

        # 5. State machine
        state_machine.update()

        # 6. Control loop (max-then-backoff)
        control_loop.update()

        # 7. Command manager (sends to VESC)
        command_mgr.update()

        # 8. CSV log at 50 Hz
        if ticks_diff(now, last_log) >= LOG_PERIOD_MS:
            last_log = now
            elapsed = ticks_diff(now, start)

            line = "%d,%d,%d,%.1f,%d,%d,%.1f,%+.2f,%.2f,%.2f,%.2f,%+.2f,%+.2f,%.1f,%.1f,%s,%d" % (
                elapsed,
                _SYS_STATE_MAP.get(state.system_state, -1),
                _MODE_MAP.get(state.requested_mode, -1),
                state.wheel_speed_rpm, int(state.wheel_speed_valid),
                int(state.wheel_speed_fresh),
                state.vesc_mech_rpm,
                state.vesc_motor_current_a, control_loop._regen_target_a,
                state.regen_command_request,
                state.assist_command_request,
                state.vesc_iq_current_a, state.vesc_motor_current_a,
                state.cap_voltage_v, state.vesc_bus_voltage_v,
                state.vesc_fault_code, int(state.inhibit_motor_commands),
            )
            f.write(line + "\n")
            rows += 1

        # 9. Terminal status at 2 Hz
        if ticks_diff(now, last_status) >= 500:
            last_status = now
            elapsed_s = ticks_diff(now, start) / 1000.0
            print("  %5.1fs  st=%-8s mode=%-7s whl=%3.0f mot=%4.0f act=%+5.1fA tgt=%4.1f cmd=%4.1fA iq=%+5.1fA inh=%d" % (
                elapsed_s, state.system_state, state.requested_mode,
                state.wheel_speed_rpm, state.vesc_mech_rpm,
                state.vesc_motor_current_a,
                control_loop._regen_target_a,
                state.regen_command_request,
                state.vesc_iq_current_a,
                int(state.inhibit_motor_commands),
            ))

        # 10. Fixed-rate loop
        sleep_ms(max(1, CONTROL_PERIOD_MS - ticks_diff(ticks_ms(), now)))

except KeyboardInterrupt:
    print("\n  [Stopped by user]")

# Neutral on exit
for _ in range(10):
    vesc.send_neutral()
    sleep_ms(20)

f.close()
print()
print("Saved %d rows to %s" % (rows, DATA_FILE))
