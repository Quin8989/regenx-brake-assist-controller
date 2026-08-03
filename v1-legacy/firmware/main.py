# main.py — Firmware entry point and cooperative scheduler
#
# Execution order per main loop iteration:
#  1. Service UART receive / parse inbound VESC packets
#  2. Refresh VESC telemetry requests if due
#  3. Fast loop: poll inputs → supervisor → control → command TX
#  4. Energy estimation + Update LCD
#  5. Bench data capture

import sys

from app.controller import AppController
from config.settings import CONTINUE_ON_MAIN_LOOP_EXCEPTION, EXCEPTION_LOG_MAX, LCD_TYPE, VCAP_MIN_OPERATING
from core import FaultCode, FaultManager, SharedState, SystemState
from drivers.gpio_io import ResetButton
from drivers.throttle import Throttle
from services.control_loop import ControlLoop
from services.display_manager import DisplayManager
from services.input_manager import InputManager
from services.system_supervisor import SystemSupervisor
from services.bench_logger import BenchLogger
from services.vesc_comm import VESCComm
from utils import Logger

# --- Exception log — ring buffer of state snapshots at crash time ---
exception_log = []
runtime_refs = {
    "bench_logger": None,
    "state": None,
}


def _make_lcd():
    """Instantiate the LCD driver selected in settings.LCD_TYPE.

    Importing is lazy so we only pull in the `machine.I2C` bindings when
    the I2C backpack is actually configured, and vice-versa — keeps the
    parallel-LCD build size unchanged.
    """
    if LCD_TYPE == "i2c":
        from drivers.lcd_driver_i2c import LCDDriver as _LCD
    elif LCD_TYPE == "parallel":
        from drivers.lcd_driver import LCDDriver as _LCD
    else:
        raise ValueError("settings.LCD_TYPE must be 'parallel' or 'i2c'")
    return _LCD()


def dump_bench_log(clear=False):
    """Dump the rolling ride log from the running firmware over USB serial.

    Intended for use from a resumed REPL session after an unplugged ride:
        mpremote resume exec "import main; main.dump_bench_log()"
    """
    bench_logger = runtime_refs.get("bench_logger")
    if bench_logger is None:
        print("bench_log: unavailable")
        return False
    bench_logger.dump()
    if clear:
        bench_logger.clear()
    return True


def clear_bench_log():
    """Clear the rolling ride log without dumping it."""
    bench_logger = runtime_refs.get("bench_logger")
    if bench_logger is None:
        print("bench_log: unavailable")
        return False
    bench_logger.clear()
    print("bench_log: cleared")
    return True


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
    throttle = Throttle()
    reset_button = ResetButton()
    lcd = _make_lcd()
    wdt.feed()  # buy another 8 s after driver init

    # --- Services ---
    vesc_comm = VESCComm(state)
    input_mgr = InputManager(throttle, state)
    control_loop = ControlLoop(state)
    safety = SystemSupervisor(state, faults)
    display_mgr = DisplayManager(lcd, state)
    bench_log = BenchLogger(state)
    runtime_refs["bench_logger"] = bench_log
    runtime_refs["state"] = state
    wdt.feed()  # buy another 8 s before app layer init

    # --- App layer ---
    app = AppController(
        state=state,
        input_mgr=input_mgr,
        vesc_comm=vesc_comm,
        safety=safety,
        control_loop=control_loop,
        display_mgr=display_mgr,
        reset_button=reset_button,
        fault_manager=faults,
        bench_logger=bench_log,
    )

    logger.info("startup", "Initialization complete — entering main loop")

    # --- Startup VESC configuration ---
    # These one-shot commands configure the VESC before the main loop.
    # Responses (FW version) arrive asynchronously via service_rx.
    vesc_comm.send_disable_output(0)       # Suppress any VESC app control (permanent until reset)
    vesc_comm.request_fw_version()         # HW_NAME check — response populates SharedState
    vesc_comm.set_battery_cut(             # Confirm low-side cap under-voltage cutoffs
        start_v=VCAP_MIN_OPERATING,        # 10 V — VESC starts limiting current
        end_v=VCAP_MIN_OPERATING - 1.0,    # 9 V — VESC cuts current to zero
    )
    wdt.feed()

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
