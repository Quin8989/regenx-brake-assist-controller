# services/safety_supervisor.py — Highest-priority protection layer
#
# Runs frequently and overrides all non-safety logic.
# Ensures safe shutdown within the target of < 100 ms on detected fault.

from time import ticks_diff, ticks_ms

from config.settings import (
    VCAP_ABSOLUTE_MAX,
    VCAP_MIN_OPERATING,
    VESC_TELEMETRY_TIMEOUT_MS,
)
from core import FaultCode, SystemState

# VESC fault codes (from datatypes.h mc_fault_code enum):
#   0 = NONE, 1 = OVER_VOLTAGE, 2 = UNDER_VOLTAGE, 3 = DRV, 4 = ABS_OVER_CURRENT,
#   5 = OVER_TEMP_FET, 6 = OVER_TEMP_MOTOR

# Prebuilt sets for state membership checks (avoid recreating each call)
_TELEMETRY_EXEMPT_STATES = {SystemState.OFF, SystemState.PRECHARGE, SystemState.FAULT}
_UNDERVOLTAGE_STATES = {SystemState.ASSIST, SystemState.REGEN}
_INHIBIT_STATES = {SystemState.OFF, SystemState.PRECHARGE, SystemState.FAULT}


class SafetySupervisor:
    def __init__(self, shared_state, fault_manager):
        self._state = shared_state
        self._faults = fault_manager

    def update(self):
        """Run all safety checks. Call this at the highest priority rate."""
        self._check_overvoltage()
        self._check_undervoltage()
        self._check_telemetry_health()
        self._check_vesc_fault()
        self._check_throttle_validity()
        self._apply_inhibits()

    def _check_overvoltage(self):
        if self._state.cap_voltage_v >= VCAP_ABSOLUTE_MAX:
            self._faults.set_fault(FaultCode.OVERVOLTAGE)

    def _check_undervoltage(self):
        if (
            self._state.cap_voltage_v < VCAP_MIN_OPERATING
            and self._state.system_state in _UNDERVOLTAGE_STATES
        ):
            self._faults.set_fault(FaultCode.UNDERVOLTAGE)
        else:
            self._faults.clear_fault(FaultCode.UNDERVOLTAGE)

    def _check_telemetry_health(self):
        if self._state.system_state in _TELEMETRY_EXEMPT_STATES:
            self._faults.clear_fault(FaultCode.VESC_TIMEOUT)
            return
        if self._state.last_vesc_rx_ms == 0:
            return  # Haven't received any packet yet — not a fault
        age = ticks_diff(ticks_ms(), self._state.last_vesc_rx_ms)
        if age > VESC_TELEMETRY_TIMEOUT_MS:
            self._faults.set_fault(FaultCode.VESC_TIMEOUT)
        else:
            self._faults.clear_fault(FaultCode.VESC_TIMEOUT)

    def _check_vesc_fault(self):
        """Inhibit motor commands when the VESC reports an internal fault.

        VESC fault_code 0 = no fault.  Any non-zero value (over-temp, DRV error,
        over-current, etc.) means the VESC has shut down its motor output.
        Auto-clears once the VESC reports fault_code 0 again.
        """
        if self._state.vesc_fault_code != 0:
            self._faults.set_fault(FaultCode.VESC_FAULT)
        else:
            self._faults.clear_fault(FaultCode.VESC_FAULT)

    def _check_throttle_validity(self):
        """Invalid throttle (wire open/shorted) inhibits motor and sets a
        non-latching fault that clears automatically once the sensor recovers."""
        if not self._state.throttle_valid:
            self._faults.set_fault(FaultCode.THROTTLE_RANGE)
        else:
            self._faults.clear_fault(FaultCode.THROTTLE_RANGE)

    def _apply_inhibits(self):
        """Single source of truth for motion inhibit policy."""
        if self._faults.has_fault():
            self._state.inhibit_motor_commands = True
            return

        if self._state.cap_voltage_v < VCAP_MIN_OPERATING:
            self._state.inhibit_motor_commands = True
            return

        if self._state.system_state in _INHIBIT_STATES:
            self._state.inhibit_motor_commands = True
            return

        self._state.inhibit_motor_commands = False
