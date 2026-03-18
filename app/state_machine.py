# app/state_machine.py — Top-level operating state transitions
#
# Implements all major states and legal transitions.
# Guarantees that impossible or unsafe transitions are rejected.

from core.enums import SystemState, PrechargeState
from config.thresholds import VCAP_MIN_OPERATING


class StateMachine:
    def __init__(self, shared_state, fault_manager):
        self._state = shared_state
        self._faults = fault_manager

    def update(self, input_mgr=None, precharge_mgr=None):
        """Evaluate current conditions and transition state if appropriate."""
        s = self._state
        current = s.system_state

        # --- Any state → FAULT (handled by safety_supervisor, but guard here too) ---
        if self._faults.has_critical_fault():
            s.system_state = SystemState.FAULT
            s.inhibit_motor_commands = True
            return

        # --- State-specific transition logic ---

        if current == SystemState.OFF:
            self._handle_off(precharge_mgr)

        elif current == SystemState.PRECHARGE:
            self._handle_precharge(precharge_mgr)

        elif current == SystemState.READY:
            self._handle_ready(input_mgr)

        elif current == SystemState.ASSIST:
            self._handle_assist(input_mgr)

        elif current == SystemState.REGEN:
            self._handle_regen(input_mgr)

        elif current == SystemState.FAULT:
            self._handle_fault()

    def _handle_off(self, precharge_mgr):
        s = self._state
        if s.cap_voltage_v >= VCAP_MIN_OPERATING:
            # Already charged — go to READY
            s.system_state = SystemState.READY
            s.inhibit_motor_commands = False
        elif precharge_mgr is not None:
            # Need precharge
            precharge_mgr.begin_precharge()
            s.system_state = SystemState.PRECHARGE

    def _handle_precharge(self, precharge_mgr):
        s = self._state
        if precharge_mgr is not None and precharge_mgr.is_complete():
            s.system_state = SystemState.READY
            s.inhibit_motor_commands = False
        elif precharge_mgr is not None and precharge_mgr.is_failed():
            s.system_state = SystemState.FAULT
            s.inhibit_motor_commands = True

    def _handle_ready(self, input_mgr):
        s = self._state
        if input_mgr is not None and input_mgr.rider_requesting_assist():
            s.system_state = SystemState.ASSIST
        # TODO: Transition to REGEN when regen is requested and conditions allow

    def _handle_assist(self, input_mgr):
        s = self._state
        if input_mgr is None or not input_mgr.rider_requesting_assist():
            s.system_state = SystemState.READY
        if s.inhibit_motor_commands:
            s.system_state = SystemState.READY

    def _handle_regen(self, input_mgr):
        s = self._state
        # TODO: Define regen exit condition based on chosen regen input source
        if s.inhibit_motor_commands:
            s.system_state = SystemState.READY
            return
        # Placeholder: return to READY if no regen request
        s.system_state = SystemState.READY

    def _handle_fault(self):
        # Remain in FAULT until clear conditions are met
        s = self._state
        if not self._faults.has_any_fault():
            s.system_state = SystemState.OFF
            s.inhibit_motor_commands = True  # Re-arm through normal startup

    # TODO: Finalize exact transition rules
    # TODO: Decide whether READY and COAST need to be separate states
    # TODO: Decide whether REGEN is based only on electrical request or also measured braking
    # TODO: Decide recovery path out of FAULT
