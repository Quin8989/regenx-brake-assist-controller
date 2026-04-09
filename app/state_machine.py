# app/state_machine.py — Top-level operating state transitions
#
# InputManager decides what the rider wants (ASSIST / REGEN / NEUTRAL).
# This module gates those requests with safety checks before allowing
# the system state to change:
#
#   OFF → PRECHARGE → REGEN
#   Direct ASSIST ↔ REGEN transitions are allowed.
#   Any state → FAULT (when faults are present)
#   FAULT → REGEN (when all faults clear)
#
# REGEN is the default running state.  The ControlLoop naturally
# produces zero current when motor RPM is below threshold or when
# safety gates (cap voltage, etc.) prevent braking.

from config.settings import VCAP_MIN_OPERATING
from core import CommandMode, SystemState


class StateMachine:
    def __init__(self, shared_state, fault_manager):
        self._state = shared_state
        self._faults = fault_manager

    def update(self):
        """Evaluate current conditions and transition state if appropriate."""
        s = self._state
        current = s.system_state

        # --- Any state → FAULT ---
        if self._faults.has_fault():
            s.system_state = SystemState.FAULT
            s.inhibit_motor_commands = True
            return

        # --- State-specific transition logic ---

        if current == SystemState.OFF:
            self._handle_off()

        elif current == SystemState.PRECHARGE:
            self._handle_precharge()

        elif current == SystemState.ASSIST:
            self._handle_assist()

        elif current == SystemState.REGEN:
            self._handle_regen()

        elif current == SystemState.FAULT:
            self._handle_fault()

    def _handle_off(self):
        s = self._state
        if s.cap_voltage_v >= VCAP_MIN_OPERATING:
            s.system_state = SystemState.REGEN
        else:
            s.system_state = SystemState.PRECHARGE

    def _handle_precharge(self):
        s = self._state
        if s.cap_voltage_v >= VCAP_MIN_OPERATING:
            s.system_state = SystemState.REGEN

    def _handle_assist(self):
        s = self._state
        if s.requested_mode != CommandMode.ASSIST:
            s.system_state = SystemState.REGEN

    def _handle_regen(self):
        s = self._state
        if s.requested_mode == CommandMode.ASSIST:
            s.system_state = SystemState.ASSIST

    def _handle_fault(self):
        if not self._faults.has_fault():
            self._state.system_state = SystemState.REGEN
            self._state.inhibit_motor_commands = True
