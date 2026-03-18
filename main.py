# main.py — Firmware entry point and cooperative scheduler
#
# Execution order per main loop iteration:
#  1. Update timebase
#  2. Poll local inputs and ADC channels
#  3. Service UART receive / parse inbound VESC packets
#  4. Refresh VESC telemetry requests if due
#  5. Run safety supervisor
#  6. Run precharge manager
#  7. Run state machine
#  8. Run control loop
#  9. Run command manager (transmit assist / regen / neutral)
# 10. Update LCD and LEDs
# 11. Emit low-rate debug logs

from config import pins, thresholds, timing, vesc_config
from core.enums import SystemState
from core.shared_state import SharedState
from core.faults import FaultManager
from drivers.uart_port import UARTPort
from drivers.throttle import Throttle
from drivers.adc_inputs import ADCInputs
from drivers.precharge_io import PrechargeIO
from drivers.lcd_driver import LCDDriver
from drivers.status_leds import StatusLEDs
from services.vesc_packets import VESCPackets
from services.vesc_comm import VESCComm
from services.telemetry_manager import TelemetryManager
from services.input_manager import InputManager
from services.control_loop import ControlLoop
from services.command_manager import CommandManager
from services.precharge_manager import PrechargeManager
from services.safety_supervisor import SafetySupervisor
from services.energy_estimator import EnergyEstimator
from services.display_manager import DisplayManager
from app.state_machine import StateMachine
from app.app_controller import AppController
from utils.logger import Logger
from utils.timebase import Timebase


def main():
    # --- Shared state ---
    state = SharedState()
    faults = FaultManager(state)
    logger = Logger()
    timebase = Timebase()

    # --- Drivers ---
    uart = UARTPort()
    throttle = Throttle()
    adc = ADCInputs()
    precharge_io = PrechargeIO()
    lcd = LCDDriver()
    leds = StatusLEDs()

    # --- Services ---
    vesc_packets = VESCPackets()
    vesc_comm = VESCComm(uart, vesc_packets, state)
    telemetry_mgr = TelemetryManager(state)
    input_mgr = InputManager(throttle, state)
    control_loop = ControlLoop(state)
    command_mgr = CommandManager(vesc_comm, state)
    precharge_mgr = PrechargeManager(precharge_io, adc, state, faults)
    safety = SafetySupervisor(state, faults)
    energy = EnergyEstimator(state)
    display_mgr = DisplayManager(lcd, leds, state)

    # --- App layer ---
    state_machine = StateMachine(state, faults)
    app = AppController(
        state=state,
        timebase=timebase,
        input_mgr=input_mgr,
        vesc_comm=vesc_comm,
        telemetry_mgr=telemetry_mgr,
        safety=safety,
        precharge_mgr=precharge_mgr,
        state_machine=state_machine,
        control_loop=control_loop,
        command_mgr=command_mgr,
        energy=energy,
        display_mgr=display_mgr,
        logger=logger,
        adc=adc,
    )

    # --- Ensure motor commands are inhibited at startup ---
    state.inhibit_motor_commands = True
    logger.info("startup", "Initialization complete — entering main loop")

    # --- Main cooperative scheduler loop ---
    while True:
        try:
            app.update()
        except Exception as e:
            logger.error("main", "Uncaught exception: {}".format(e))
            # Force safe state on unhandled error
            state.inhibit_motor_commands = True
            state.system_state = SystemState.FAULT
            command_mgr.send_neutral()
            # TODO: Decide whether to halt, retry, or reset after uncaught exception


# TODO: Define exact task rates
# TODO: Define startup initialization order beyond what is shown
# TODO: Define fault latching / clearing initiation

try:
    main()
except Exception as e:
    print("FATAL: {}".format(e))
    # Last-resort safe state — ensure no motor command output
