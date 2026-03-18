# services/safety_supervisor.py — Highest-priority protection layer
#
# Runs frequently and overrides all non-safety logic.
# Ensures safe shutdown within the target of < 100 ms on detected fault.

from time import ticks_ms, ticks_diff
from core.enums import SystemState, FaultCode
from config.thresholds import (
    VCAP_ABSOLUTE_MAX,
    VCAP_SOFT_REGEN_CUTOFF,
    VCAP_MIN_OPERATING,
    VESC_TELEMETRY_TIMEOUT_MS,
)


class SafetySupervisor:
    def __init__(self, shared_state, fault_manager):
        self._state = shared_state
        self._faults = fault_manager

    def update(self):
        """Run all safety checks. Call this at the highest priority rate."""
        self._check_overvoltage()
        self._check_undervoltage()
        self._check_telemetry_health()
        self._check_throttle_validity()
        self._apply_inhibits()

    def _check_overvoltage(self):
        if self._state.cap_voltage_v >= VCAP_ABSOLUTE_MAX:
            self._faults.set_fault(FaultCode.OVERVOLTAGE)
            self._state.inhibit_motor_commands = True

    def _check_undervoltage(self):
        if self._state.cap_voltage_v < VCAP_MIN_OPERATING:
            # Not necessarily a FAULT — but motor assist should be inhibited
            self._state.inhibit_motor_commands = True
            # Only raise a fault if we were previously in an operating state
            if self._state.system_state in (SystemState.ASSIST, SystemState.REGEN):
                self._faults.set_fault(FaultCode.UNDERVOLTAGE)

    def _check_telemetry_health(self):
        age = ticks_diff(ticks_ms(), self._state.last_vesc_rx_ms)
        if age > VESC_TELEMETRY_TIMEOUT_MS and self._state.last_vesc_rx_ms != 0:
            self._faults.set_fault(FaultCode.VESC_TIMEOUT)
            self._state.inhibit_motor_commands = True

    def _check_throttle_validity(self):
        # Throttle validity is set by the throttle driver
        # If invalid, inhibit assist but don't necessarily force full FAULT
        # TODO: Decide policy — immediate FAULT or just inhibit?
        pass

    def _apply_inhibits(self):
        """Force FAULT state if critical faults are present."""
        if self._faults.has_critical_fault():
            self._state.system_state = SystemState.FAULT
            self._state.inhibit_motor_commands = True

    # TODO: Finalize which events are immediate FAULTs vs warnings
    # TODO: Define recovery behavior after transient issues
    # TODO: Define maximum allowed time from fault detection to command disable
    # TODO: Decide whether supervisor uses local voltage, VESC telemetry, or both
