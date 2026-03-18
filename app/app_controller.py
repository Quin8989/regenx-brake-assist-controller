# app/app_controller.py — High-level application orchestrator
#
# Provides a single update() entry point called by main.py.
# Sequences service-level modules in the correct order.

from config.timing import (
    SAFETY_SUPERVISOR_PERIOD_MS,
    CONTROL_LOOP_PERIOD_MS,
    COMMAND_TRANSMIT_PERIOD_MS,
    THROTTLE_SAMPLE_PERIOD_MS,
    VCAP_SAMPLE_PERIOD_MS,
    TELEMETRY_REQUEST_PERIOD_MS,
    LCD_REFRESH_PERIOD_MS,
    LED_UPDATE_PERIOD_MS,
    DEBUG_LOG_PERIOD_MS,
)
from utils.timebase import Timebase


class AppController:
    def __init__(self, state, timebase, input_mgr, vesc_comm, telemetry_mgr,
                 safety, precharge_mgr, state_machine, control_loop,
                 command_mgr, energy, display_mgr, logger, adc):
        self._state = state
        self._timebase = timebase
        self._input_mgr = input_mgr
        self._vesc_comm = vesc_comm
        self._telemetry_mgr = telemetry_mgr
        self._safety = safety
        self._precharge_mgr = precharge_mgr
        self._state_machine = state_machine
        self._control_loop = control_loop
        self._command_mgr = command_mgr
        self._energy = energy
        self._display_mgr = display_mgr
        self._logger = logger
        self._adc = adc

        # --- Periodic task timers ---
        self._t_safety = timebase.make_timer(SAFETY_SUPERVISOR_PERIOD_MS)
        self._t_input = timebase.make_timer(THROTTLE_SAMPLE_PERIOD_MS)
        self._t_adc = timebase.make_timer(VCAP_SAMPLE_PERIOD_MS)
        self._t_telem_req = timebase.make_timer(TELEMETRY_REQUEST_PERIOD_MS)
        self._t_control = timebase.make_timer(CONTROL_LOOP_PERIOD_MS)
        self._t_command = timebase.make_timer(COMMAND_TRANSMIT_PERIOD_MS)
        self._t_display = timebase.make_timer(LCD_REFRESH_PERIOD_MS)
        self._t_debug = timebase.make_timer(DEBUG_LOG_PERIOD_MS)

    def update(self):
        """Single update tick — called every iteration of the main loop."""

        # 1. Update timebase
        self._timebase.tick()

        # 2. Poll local inputs (throttle + ADC)
        if self._t_input.ready():
            self._input_mgr.update()

        if self._t_adc.ready():
            self._adc.update()
            self._state.cap_voltage_v = self._adc.cap_voltage_v

        # 3. Service UART receive
        self._vesc_comm.service_rx()

        # 4. Refresh VESC telemetry requests if due
        if self._t_telem_req.ready():
            self._vesc_comm.request_telemetry()

        # 5. Telemetry interpretation
        self._telemetry_mgr.update()

        # 6. Energy estimation
        self._energy.update()

        # 7. Safety supervisor (high priority)
        if self._t_safety.ready():
            self._safety.update()

        # 8. Precharge manager
        self._precharge_mgr.update()

        # 9. State machine
        self._state_machine.update(
            input_mgr=self._input_mgr,
            precharge_mgr=self._precharge_mgr,
        )

        # 10. Control loop
        if self._t_control.ready():
            self._control_loop.update()

        # 11. Command manager (transmit)
        if self._t_command.ready():
            self._command_mgr.update()

        # 12. Display (LCD + LEDs)
        if self._t_display.ready():
            self._display_mgr.update()

        # 13. Debug logging (low rate)
        if self._t_debug.ready():
            self._logger.debug("loop", "state={} vcap={:.1f}V e={:.0f}%".format(
                self._state.system_state,
                self._state.cap_voltage_v,
                self._state.cap_energy_percent,
            ))

    # TODO: Finalize call order and data dependencies
    # TODO: Decide whether each service exposes update() or more specific methods
    # TODO: Decide whether app_controller performs runtime sanity checks on module init
