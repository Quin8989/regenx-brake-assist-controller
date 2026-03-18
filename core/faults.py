# core/faults.py — Central fault handling policy
#
# Supports setting, clearing, querying, latching, and classifying faults.

from core.enums import FaultCode


# Faults that require manual reset / restart to clear
LATCHING_FAULTS = {
    FaultCode.OVERVOLTAGE,
    FaultCode.PRECHARGE_TIMEOUT,
    FaultCode.INTERNAL,
}

# Faults that are informational / warning only (do not force FAULT state)
WARNING_ONLY_FAULTS = {
    FaultCode.MOTOR_INHIBITED,
}

# Human-readable fault descriptions for LCD / logs
FAULT_LABELS = {
    FaultCode.VESC_TIMEOUT: "VESC Timeout",
    FaultCode.VESC_PACKET: "VESC Packet Err",
    FaultCode.OVERVOLTAGE: "Overvoltage",
    FaultCode.UNDERVOLTAGE: "Undervoltage",
    FaultCode.PRECHARGE_TIMEOUT: "Precharge Timeout",
    FaultCode.PRECHARGE_INVALID: "Precharge Invalid",
    FaultCode.THROTTLE_RANGE: "Throttle Range",
    FaultCode.ADC_INVALID: "ADC Invalid",
    FaultCode.INTERNAL: "Internal Error",
    FaultCode.MOTOR_INHIBITED: "Motor Inhibited",
    FaultCode.SENSOR_STALE: "Sensor Stale",
}


class FaultManager:
    def __init__(self, shared_state):
        self._state = shared_state

    def set_fault(self, code):
        self._state.fault_flags.add(code)

    def clear_fault(self, code):
        if code not in LATCHING_FAULTS:
            self._state.fault_flags.discard(code)

    def clear_all_non_latching(self):
        non_latching = {f for f in self._state.fault_flags if f not in LATCHING_FAULTS}
        self._state.fault_flags -= non_latching

    def force_clear(self, code):
        """Clear even a latching fault (for manual reset path)."""
        self._state.fault_flags.discard(code)

    def has_fault(self, code):
        return code in self._state.fault_flags

    def has_any_fault(self):
        return len(self._state.fault_flags) > 0

    def has_critical_fault(self):
        return any(f not in WARNING_ONLY_FAULTS for f in self._state.fault_flags)

    def active_faults(self):
        return list(self._state.fault_flags)

    def fault_text(self, code):
        return FAULT_LABELS.get(code, str(code))

    def highest_priority_fault_text(self):
        if not self._state.fault_flags:
            return ""
        # Return first critical fault found, or first warning
        for f in self._state.fault_flags:
            if f not in WARNING_ONLY_FAULTS:
                return self.fault_text(f)
        return self.fault_text(next(iter(self._state.fault_flags)))

    # TODO: Finalize critical vs non-critical faults
    # TODO: Define which faults auto-clear and which require manual reset
    # TODO: Define whether multiple simultaneous faults are shown in priority order
