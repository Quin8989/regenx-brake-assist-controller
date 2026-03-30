# app/controller.py — High-level application orchestrator
#
# Provides a single update() entry point called by main.py.
# Sequences service-level modules in the correct order.

from config.settings import (
    BENCH_LOG_PERIOD_MS,
    COMMAND_TRANSMIT_PERIOD_MS,
    CONTROL_LOOP_PERIOD_MS,
    DEBUG_LOG_PERIOD_MS,
    LCD_REFRESH_PERIOD_MS,
    SAFETY_SUPERVISOR_PERIOD_MS,
    STATE_MACHINE_PERIOD_MS,
    TELEMETRY_REQUEST_PERIOD_MS,
    THROTTLE_SAMPLE_PERIOD_MS,
)
from core import CommandMode, SystemState
from utils import PeriodicTimer


class AppController:
    def __init__(self, state, input_mgr, vesc_comm,
                 safety, state_machine, control_loop,
                 command_mgr, energy, display_mgr, logger,
                 reset_button=None, fault_manager=None,
                 bench_logger=None):
        self._state = state
        self._input_mgr = input_mgr
        self._vesc_comm = vesc_comm
        self._safety = safety
        self._state_machine = state_machine
        self._control_loop = control_loop
        self._command_mgr = command_mgr
        self._energy = energy
        self._display_mgr = display_mgr
        self._logger = logger
        self._reset_button = reset_button
        self._fault_manager = fault_manager
        self._bench_logger = bench_logger
        # --- Periodic task timers ---
        self._t_safety = PeriodicTimer(SAFETY_SUPERVISOR_PERIOD_MS)
        self._t_state = PeriodicTimer(STATE_MACHINE_PERIOD_MS)
        self._t_input = PeriodicTimer(THROTTLE_SAMPLE_PERIOD_MS)
        self._t_telem_req = PeriodicTimer(TELEMETRY_REQUEST_PERIOD_MS)
        self._t_control = PeriodicTimer(CONTROL_LOOP_PERIOD_MS)
        self._t_command = PeriodicTimer(COMMAND_TRANSMIT_PERIOD_MS)
        self._t_display = PeriodicTimer(LCD_REFRESH_PERIOD_MS)
        self._t_debug = PeriodicTimer(DEBUG_LOG_PERIOD_MS)
        self._t_bench = PeriodicTimer(BENCH_LOG_PERIOD_MS)

    def update(self):
        """Single update tick — called every iteration of the main loop."""

        # 0. Soft reset button
        if self._reset_button is not None and self._reset_button.poll():
            self._soft_reset()
            return

        # 1. Poll local inputs (throttle)
        if self._t_input.ready():
            self._input_mgr.update()

        # 2. Service UART receive
        self._vesc_comm.service_rx()

        # 3. Refresh VESC telemetry requests if due
        if self._t_telem_req.ready():
            self._vesc_comm.request_telemetry()

        # 4. Energy estimation
        self._energy.update()

        # 5. Safety supervisor
        if self._t_safety.ready():
            self._safety.update()

        # 6. State machine
        if self._t_state.ready():
            self._state_machine.update()

        # 7. Control loop
        if self._t_control.ready():
            self._control_loop.update()

        # 8. Command manager (transmit)
        if self._t_command.ready():
            self._command_mgr.update()

        # 9. Display
        if self._t_display.ready():
            self._display_mgr.update()

        # 10. Debug logging (low rate)
        if self._t_debug.ready():
            self._logger.debug(
                "loop",
                f"state={self._state.system_state} "
                f"vcap={self._state.cap_voltage_v:.1f}V "
                f"e={self._state.cap_energy_percent:.0f}%",
            )

        # 11. Bench data capture (RAM ring buffer)
        if self._bench_logger is not None and self._t_bench.ready():
            self._bench_logger.snapshot()

    def _soft_reset(self):
        """Clear all faults and return to OFF state.

        Also dumps and clears the bench log so the data captured up to
        the reset press is available on the serial console.
        """
        if self._bench_logger is not None:
            self._bench_logger.dump()
            self._bench_logger.clear()
        if self._fault_manager is not None:
            self._fault_manager.reset_all()
        self._state.system_state = SystemState.OFF
        self._state.inhibit_motor_commands = True
        self._state.requested_mode = CommandMode.NEUTRAL
        self._state.requested_level = 0.0
        self._state.assist_command_request = 0.0
        self._state.regen_command_request = 0.0
        self._control_loop.update()  # resets slew/integral via inhibit path
