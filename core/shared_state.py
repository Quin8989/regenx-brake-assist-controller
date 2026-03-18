# core/shared_state.py — Shared system data container
#
# Holds current system state in one place.
# Readable by many modules; writable in controlled ways.

from core.enums import SystemState, PrechargeState, DisplayPage


class SharedState:
    def __init__(self):
        # --- System state ---
        self.system_state = SystemState.OFF
        self.precharge_state = PrechargeState.IDLE

        # --- Fault / inhibit ---
        self.fault_flags = set()
        self.inhibit_motor_commands = True

        # --- Local measurements ---
        self.cap_voltage_v = 0.0
        self.cap_energy_j = 0.0
        self.cap_energy_percent = 0.0

        # --- Throttle ---
        self.throttle_raw = 0
        self.throttle_percent = 0.0

        # --- VESC telemetry ---
        self.vesc_bus_voltage_v = 0.0
        self.vesc_motor_current_a = 0.0
        self.vesc_input_current_a = 0.0
        self.vesc_rpm = 0
        self.vesc_duty_cycle = 0.0
        self.vesc_fault_code = 0
        self.vehicle_speed_estimate = 0.0

        # --- Command requests ---
        self.assist_command_request = 0.0
        self.regen_command_request = 0.0

        # --- Display ---
        self.display_page = DisplayPage.STATUS

        # --- Timestamps (ms) ---
        self.last_vesc_rx_ms = 0
        self.last_command_tx_ms = 0
        self.last_hall_update_ms = 0

    # TODO: Decide whether this should be a plain class, dict-like, or lightweight data model
    # TODO: Decide authoritative source when local measurement and VESC measurement overlap
    # TODO: Decide speed source: VESC rpm, wheel sensor, hall-based estimate, or fusion
