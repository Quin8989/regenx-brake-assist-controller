# main.py — Firmware entry point and cooperative scheduler
#
# Execution order per main loop iteration:
#  1. Poll local inputs
#  2. Service UART receive / parse inbound VESC packets
#  3. Refresh VESC telemetry requests if due
#  4. Run safety supervisor
#  5. Run state machine
#  6. Run control loop
#  7. Run command manager (transmit assist / regen / neutral)
#  8. Energy estimation + Update LCD
#  9. Bench data capture

import sys

from app.controller import AppController
from app.state_machine import StateMachine
from config.settings import CONTINUE_ON_MAIN_LOOP_EXCEPTION, EXCEPTION_LOG_MAX
from core import FaultCode, FaultManager, SharedState, SystemState
from drivers.lcd_driver import LCDDriver
from drivers.gpio_io import ResetButton
from drivers.throttle import Throttle
from services.control_loop import ControlLoop
from services.display_manager import DisplayManager
from services.input_manager import InputManager
from services.safety_supervisor import SafetySupervisor
from services.bench_logger import BenchLogger
from services.vesc_comm import CommandManager, UARTPort, VESCComm
from utils import Logger

# --- Exception log — ring buffer of state snapshots at crash time ---
exception_log = []


def _capture_exception(exc, state, logger):
    """Record exception details and a system state snapshot."""
    try:
        import io
        buf = io.StringIO()
        sys.print_exception(exc, buf)
        tb_str = buf.getvalue()
    except Exception:
        tb_str = f"{type(exc).__name__}: {exc}"
    snapshot = {
        "state": state.system_state,
        "mode": state.requested_mode,
        "vcap": round(state.cap_voltage_v, 2),
        "vesc_fault": state.vesc_fault_code,
        "mech_rpm": round(state.vesc_mech_rpm, 1),
        "throttle_raw": state.throttle_raw,
        "inhibit": state.inhibit_motor_commands,
        "faults": list(state.fault_flags),
        "assist_cmd": round(state.assist_command_request, 2),
        "regen_cmd": round(state.regen_command_request, 2),
    }
    entry = {"traceback": tb_str, "snapshot": snapshot}
    exception_log.append(entry)
    if len(exception_log) > EXCEPTION_LOG_MAX:
        exception_log.pop(0)
    # Also store last exception text on state for LCD visibility
    state.last_exception_str = tb_str.split("\n")[-2] if "\n" in tb_str else str(exc)
    logger.error("main", f"Exception: {tb_str.rstrip()}")
    logger.error("main", f"Snapshot: {snapshot}")


def main():
    # --- Hardware watchdog — resets Pico if main loop stalls ---
    # boot.py already re-armed the WDT to handle stale-after-deploy.
    # Creating it here resets the counter to a fresh 8 s.
    from machine import WDT
    wdt = WDT(timeout=8000)

    # --- Shared state ---
    state = SharedState()
    faults = FaultManager(state)
    logger = Logger()

    # --- Drivers (LCD init has ~60 ms of blocking sleeps) ---
    uart = UARTPort()
    throttle = Throttle()
    reset_button = ResetButton()
    lcd = LCDDriver()
    wdt.feed()  # buy another 8 s after driver init

    # --- Services ---
    vesc_comm = VESCComm(uart, state)
    input_mgr = InputManager(throttle, state)
    control_loop = ControlLoop(state)
    command_mgr = CommandManager(vesc_comm, state)
    safety = SafetySupervisor(state, faults)
    display_mgr = DisplayManager(lcd, state)
    bench_log = BenchLogger(state)
    wdt.feed()  # buy another 8 s before app layer init

    # --- App layer ---
    state_machine = StateMachine(state, faults)
    app = AppController(
        state=state,
        input_mgr=input_mgr,
        vesc_comm=vesc_comm,
        safety=safety,
        state_machine=state_machine,
        control_loop=control_loop,
        command_mgr=command_mgr,
        display_mgr=display_mgr,
        reset_button=reset_button,
        fault_manager=faults,
        bench_logger=bench_log,
    )

    # --- Ensure motor commands are inhibited at startup ---
    state.inhibit_motor_commands = True
    logger.info("startup", "Initialization complete — entering main loop")

    # --- Main cooperative scheduler loop ---
    while True:
        wdt.feed()
        try:
            app.update()
        except Exception as e:
            _capture_exception(e, state, logger)
            state.inhibit_motor_commands = True
            state.system_state = SystemState.FAULT
            faults.set_fault(FaultCode.INTERNAL)
            vesc_comm.send_current(0.0)
            if not CONTINUE_ON_MAIN_LOOP_EXCEPTION:
                raise


try:
    main()
except Exception as e:
    print(f"FATAL: {e}")
    try:
        import io
        sys.print_exception(e, sys.stdout)
    except Exception:
        pass
