# core.py — Enums, fault handling, shared system state, and energy estimation
#
# Single module for all shared definitions and runtime state.

# =============================================================================
# Enums
# =============================================================================


class SystemState:
    OFF = "OFF"
    PRECHARGE = "PRECHARGE"
    ASSIST = "ASSIST"
    REGEN = "REGEN"
    FAULT = "FAULT"


class FaultCode:
    VESC_TIMEOUT = "VESC_TIMEOUT"
    VESC_FAULT = "VESC_FAULT"
    OVERVOLTAGE = "OVERVOLTAGE"
    THROTTLE_RANGE = "THROTTLE_RANGE"
    INTERNAL = "INTERNAL"


class CommandMode:
    NEUTRAL = "NEUTRAL"
    ASSIST = "ASSIST"
    REGEN = "REGEN"


# =============================================================================
# Fault manager
# =============================================================================

# Faults that require manual reset / restart to clear
LATCHING_FAULTS = {
    FaultCode.OVERVOLTAGE,
    FaultCode.INTERNAL,
}

# Human-readable fault descriptions for LCD / logs
FAULT_LABELS = {
    FaultCode.VESC_TIMEOUT: "VESC Timeout",
    FaultCode.VESC_FAULT: "VESC Fault",
    FaultCode.OVERVOLTAGE: "Overvoltage",
    FaultCode.THROTTLE_RANGE: "Throttle Range",
    FaultCode.INTERNAL: "Internal Error",
}


class FaultManager:
    def __init__(self, shared_state):
        self._state = shared_state

    def set_fault(self, code):
        self._state.fault_flags.add(code)

    def clear_fault(self, code):
        if code not in LATCHING_FAULTS:
            self._state.fault_flags.discard(code)

    def reset_all(self):
        """Soft reset: clear all faults including latching ones."""
        self._state.fault_flags.clear()

    def has_fault(self):
        return len(self._state.fault_flags) > 0

    def fault_text(self, code):
        return FAULT_LABELS.get(code, str(code))


# =============================================================================
# Shared state
# =============================================================================


class SharedState:
    def __init__(self):
        # --- System state ---
        self.system_state = SystemState.OFF

        # --- Fault / inhibit ---
        self.fault_flags = set()
        self.inhibit_motor_commands = True

        # --- Local measurements ---
        self.cap_voltage_v = 0.0
        self.cap_energy_j = 0.0
        self.cap_energy_percent = 0.0

        # --- Throttle ---
        self.throttle_raw = 0
        self.throttle_valid = False  # Set False until first valid ADC sample
        self.requested_mode = CommandMode.NEUTRAL
        self.requested_level = 0.0

        # --- VESC telemetry ---
        self.vesc_bus_voltage_v = 0.0
        self.vesc_motor_current_a = 0.0
        self.vesc_input_current_a = 0.0
        self.vesc_id_current_a = 0.0
        self.vesc_iq_current_a = 0.0
        self.vesc_rpm = 0
        self.vesc_mech_rpm = 0.0
        self.vesc_duty_cycle = 0.0
        self.vesc_fault_code = 0
        self.vesc_temp_fet_c = 0.0
        self.vesc_temp_motor_c = 0.0
        self.vesc_ah = 0.0              # Cumulative amp-hours consumed
        self.vesc_ah_charged = 0.0      # Cumulative amp-hours regenerated
        self.vesc_wh = 0.0              # Cumulative watt-hours consumed
        self.vesc_wh_charged = 0.0      # Cumulative watt-hours regenerated
        self.vesc_tach = 0              # Tachometer (signed, half-ERPM counts)
        self.vesc_tach_abs = 0          # Tachometer absolute (unsigned)

        # --- Wheel speed input for regen slip control ---
        self.wheel_speed_rpm = 0.0
        self.wheel_speed_raw_rpm = 0.0
        self.wheel_speed_valid = False
        self.wheel_speed_fresh = False
        self.gear_carrier_speed_rpm = 0.0
        self.regen_speed_error_rpm = 0.0

        # --- Command requests ---
        self.assist_command_request = 0.0
        self.regen_command_request = 0.0

        # --- Timestamps (ms) ---
        self.last_vesc_rx_ms = 0
        self.last_command_tx_ms = 0

        # --- Exception diagnostics ---
        self.last_exception_str = ""


# =============================================================================
# Energy estimator — ½CV² stored energy and usable percentage
# =============================================================================

from config.settings import (
    CAPACITANCE_F,
    VCAP_MIN_OPERATING,
    VCAP_SOFT_REGEN_CUTOFF,
)
from utils import clamp

# Precomputed energy bounds (constant for given capacitance and voltage window)
_HALF_C = 0.5 * CAPACITANCE_F
_E_MIN = _HALF_C * VCAP_MIN_OPERATING * VCAP_MIN_OPERATING
_E_MAX = _HALF_C * VCAP_SOFT_REGEN_CUTOFF * VCAP_SOFT_REGEN_CUTOFF
_E_RANGE = _E_MAX - _E_MIN if _E_MAX > _E_MIN else 0.0


class EnergyEstimator:
    def __init__(self, shared_state):
        self._state = shared_state

    def update(self):
        """Recompute energy estimates from current cap voltage."""
        v = self._state.cap_voltage_v

        energy_j = _HALF_C * v * v
        self._state.cap_energy_j = energy_j

        if _E_RANGE > 0.0:
            pct = (energy_j - _E_MIN) / _E_RANGE * 100.0
            self._state.cap_energy_percent = clamp(pct, 0.0, 100.0)
        else:
            self._state.cap_energy_percent = 0.0
