# services/control_loop.py — Compute requested motor commands
#
# Uses current state, rider input, telemetry, and limits to compute
# assist, regen, or zero command requests. Does NOT transmit directly.

from core.enums import SystemState
from config.thresholds import (
    ASSIST_CURRENT_LIMIT_A,
    REGEN_CURRENT_LIMIT_A,
    VCAP_MIN_OPERATING,
    VCAP_SOFT_REGEN_CUTOFF,
)
from utils.math_helpers import clamp


class ControlLoop:
    def __init__(self, shared_state):
        self._state = shared_state

    def update(self):
        """Compute command requests based on current state and inputs."""
        s = self._state

        # Default: no command
        s.assist_command_request = 0.0
        s.regen_command_request = 0.0

        if s.inhibit_motor_commands:
            return

        if s.system_state == SystemState.ASSIST:
            self._compute_assist()
        elif s.system_state == SystemState.REGEN:
            self._compute_regen()
        # OFF, PRECHARGE, READY, FAULT → zero command (already set)

    def _compute_assist(self):
        s = self._state

        # Inhibit assist if voltage is below operating threshold
        if s.cap_voltage_v < VCAP_MIN_OPERATING:
            return

        # Map throttle fraction to assist current request
        request = s.throttle_percent * ASSIST_CURRENT_LIMIT_A
        request = clamp(request, 0.0, ASSIST_CURRENT_LIMIT_A)

        # TODO: Apply rate limiting so torque commands do not jump abruptly
        # TODO: Taper assist when capacitor voltage is low

        s.assist_command_request = request

    def _compute_regen(self):
        s = self._state

        # Inhibit regen if voltage is at or above soft cutoff
        if s.cap_voltage_v >= VCAP_SOFT_REGEN_CUTOFF:
            return

        # TODO: Define regen request source and scaling
        request = REGEN_CURRENT_LIMIT_A  # Placeholder — full regen
        request = clamp(request, 0.0, REGEN_CURRENT_LIMIT_A)

        # TODO: Apply rate limiting
        # TODO: Taper regen as cap voltage approaches upper threshold

        s.regen_command_request = request

    # TODO: Choose command variable type and units
    # TODO: Choose assist mapping from throttle to requested current
    # TODO: Choose regen request source and scaling
    # TODO: Choose rate limits and soft-start behavior
    # TODO: Decide whether rpm or vehicle speed affects command shaping
