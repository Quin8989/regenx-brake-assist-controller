# app/controller.py — High-level application orchestrator
#
# Provides a single update() entry point called by main.py.
# Sequences service-level modules in the correct order.

from config.settings import (
    BENCH_LOG_PERIOD_MS,
    FAST_LOOP_PERIOD_MS,
    LCD_REFRESH_PERIOD_MS,
    TELEMETRY_REQUEST_PERIOD_MS,
)
from core import SystemState
from utils import PeriodicTimer


class AppController:
    def __init__(self, state, input_mgr, vesc_comm,
                 safety, control_loop,
                 display_mgr,
                 reset_button=None, fault_manager=None,
                 bench_logger=None):
        self._state = state
        self._input_mgr = input_mgr
        self._vesc_comm = vesc_comm
        self._safety = safety
        self._control_loop = control_loop
        self._display_mgr = display_mgr
        self._reset_button = reset_button
        self._fault_manager = fault_manager
        self._bench_logger = bench_logger
        # --- Periodic task timers ---
        self._t_fast = PeriodicTimer(FAST_LOOP_PERIOD_MS)
        self._t_telem_req = PeriodicTimer(TELEMETRY_REQUEST_PERIOD_MS)
        self._t_display = PeriodicTimer(LCD_REFRESH_PERIOD_MS)
        self._t_bench = PeriodicTimer(BENCH_LOG_PERIOD_MS)

    def update(self):
        """Single update tick — called every iteration of the main loop."""

        # 0. Soft reset button
        if self._reset_button is not None and self._reset_button.poll():
            self._soft_reset()
            return

        # 1. Service UART receive
        self._vesc_comm.service_rx()

        # 2. Refresh VESC telemetry requests if due
        if self._t_telem_req.ready():
            self._vesc_comm.request_telemetry()

        # 3. Fast loop (input → supervisor → control → command TX)
        if self._t_fast.ready():
            self._input_mgr.update()
            self._safety.update()
            self._control_loop.update()
            self._vesc_comm.send_current(self._state.motor_command_a)

        # 4. Display (energy estimation + LCD)
        if self._t_display.ready():
            self._display_mgr.update()

        # 5. Bench data capture (RAM ring buffer)
        if self._bench_logger is not None and self._t_bench.ready():
            self._bench_logger.snapshot()

    def _soft_reset(self):
        """Clear all faults and return to PRECHARGE state.

        Also dumps and clears the bench log so the data captured up to
        the reset press is available on the serial console.
        """
        if self._bench_logger is not None:
            self._bench_logger.dump()
            self._bench_logger.clear()
        if self._fault_manager is not None:
            self._fault_manager.reset_all()
        self._state.system_state = SystemState.PRECHARGE
        self._state.inhibit_motor_commands = True  # safe default until supervisor runs
        self._state.requested_level = 0.0
        self._control_loop.update()  # zeros commands via inhibit path
