# main.py — Firmware entry point and cooperative scheduler
#
# Execution order per main loop iteration:
#  1. Poll local inputs
#  2. Service UART receive / parse inbound VESC packets
#  3. Refresh VESC telemetry requests if due
#  4. Energy estimation
#  5. Run safety supervisor
#  6. Run precharge manager
#  7. Run state machine
#  8. Run control loop
#  9. Run command manager (transmit assist / regen / neutral)
# 10. Update LCD
# 11. Emit low-rate debug logs
# 12. Bench data capture

from app.controller import AppController
from app.state_machine import StateMachine
from config.settings import CONTINUE_ON_MAIN_LOOP_EXCEPTION
from core import FaultCode, FaultManager, SharedState, SystemState, EnergyEstimator
from drivers.lcd_driver import LCDDriver
from drivers.gpio_io import PrechargeIO, ResetButton
from drivers.throttle import Throttle
from drivers.wheel_speed_hall import WheelSpeedHall
from services.control_loop import ControlLoop
from services.display_manager import DisplayManager
from services.input_manager import InputManager
from services.precharge_manager import PrechargeManager
from services.safety_supervisor import SafetySupervisor
from services.bench_logger import BenchLogger
from services.vesc_comm import CommandManager, UARTPort, VESCComm
from utils import Logger


def main():
    # --- Hardware watchdog — resets Pico if main loop stalls ---
    from machine import WDT
    wdt = WDT(timeout=8000)  # 8 s — longest safe interval on RP2040

    # --- Shared state ---
    state = SharedState()
    faults = FaultManager(state)
    logger = Logger()

    # --- Drivers ---
    uart = UARTPort()
    throttle = Throttle()
    wheel_speed = WheelSpeedHall()
    precharge_io = PrechargeIO()
    reset_button = ResetButton()
    lcd = LCDDriver()

    # --- Services ---
    vesc_comm = VESCComm(uart, state)
    input_mgr = InputManager(throttle, state, wheel_speed)
    control_loop = ControlLoop(state)
    command_mgr = CommandManager(vesc_comm, state)
    precharge_mgr = PrechargeManager(precharge_io, state, faults)
    safety = SafetySupervisor(state, faults)
    energy = EnergyEstimator(state)
    display_mgr = DisplayManager(lcd, state)
    bench_log = BenchLogger(state)

    # --- App layer ---
    state_machine = StateMachine(state, faults)
    app = AppController(
        state=state,
        input_mgr=input_mgr,
        vesc_comm=vesc_comm,
        safety=safety,
        precharge_mgr=precharge_mgr,
        state_machine=state_machine,
        control_loop=control_loop,
        command_mgr=command_mgr,
        energy=energy,
        display_mgr=display_mgr,
        logger=logger,
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
            logger.error("main", f"Uncaught exception: {e}")
            # Force safe state on unhandled error
            state.inhibit_motor_commands = True
            state.system_state = SystemState.FAULT
            faults.set_fault(FaultCode.INTERNAL)
            vesc_comm.send_neutral()
            if not CONTINUE_ON_MAIN_LOOP_EXCEPTION:
                raise


try:
    main()
except Exception as e:
    print(f"FATAL: {e}")
    # Last-resort safe state — force precharge/boost pins low
    from machine import Pin
    for gp in (15, 16):
        Pin(gp, Pin.OUT, value=0)
