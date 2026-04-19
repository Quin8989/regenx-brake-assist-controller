# core.py — Enums, fault handling, and shared system state
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
        self.system_state = SystemState.PRECHARGE

        # --- Fault / inhibit ---
        self.fault_flags = set()
        self.inhibit_motor_commands = True

        # --- Local measurements ---
        self.cap_voltage_v = 0.0
        self.cap_energy_percent = 0.0

        # --- Throttle ---
        self.throttle_raw = 0
        self.throttle_valid = False  # Set False until first valid ADC sample
        self.requested_mode = CommandMode.REGEN
        self.requested_level = 0.0

        # --- VESC telemetry ---
        # Fields populated by vesc_comm._handle_payload() each packet.
        self.vesc_motor_current_a = 0.0
        self.vesc_input_current_a = 0.0
        self.vesc_id_current_a = 0.0
        self.vesc_iq_current_a = 0.0
        self.vesc_mech_rpm = 0.0
        self.vesc_duty_cycle = 0.0
        self.vesc_fault_code = 0
        self.vesc_temp_fet_c = 0.0
        self.vesc_temp_motor_c = 0.0
        self.vesc_tach = 0
        self.vesc_tach_abs = 0
        self.vesc_pid_pos = 0.0
        self.vesc_controller_id = 0
        self.vesc_temp_mos1_c = 0.0
        self.vesc_temp_mos2_c = 0.0
        self.vesc_temp_mos3_c = 0.0
        self.vesc_vd = 0.0
        self.vesc_vq = 0.0
        self.vesc_status = 0

        # --- VESC firmware identity (populated by COMM_FW_VERSION) ---
        self.vesc_fw_major = 0
        self.vesc_fw_minor = 0
        self.vesc_hw_name = ""

        # --- VESC LispBM push telemetry (COMM_CUSTOM_APP_DATA) ---
        self.vesc_iq_instantaneous_a = 0.0   # lower-latency filtered iq from get-iq()
        self.vesc_erpm_fast = 0.0            # less-filtered electrical RPM from get-rpm-fast()
        self.vesc_mech_rpm_fast = 0.0        # vesc_erpm_fast / pole_pairs
        self.last_push_iq_rx_ms = 0

        # --- Motor command requests (written by ControlLoop) ---
        self.assist_command_request = 0.0
        self.regen_command_request = 0.0
        self.motor_command_a = 0.0

        # --- Wheel speed (optional Hall-sensor derived) ---
        self.wheel_speed_valid = False
        self.wheel_speed_rpm = 0.0

        # --- Timing ---
        self.last_vesc_rx_ms = 0

        # --- Diagnostics ---
        self.last_exception_str = ""



