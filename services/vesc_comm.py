# services/vesc_comm.py — VESC telemetry service and command output
#
# VESCComm: reads UART, parses telemetry into SharedState, sends commands.
# CommandManager: final gate between control requests and VESC transmissions.

from time import ticks_ms

from config.settings import VESC_ERPM_TO_MECH_RPM
from services.vesc_protocol import (
    _build_set_brake_current,
    _build_set_current,
    _build_telemetry_request,
    _extract_payload,
    _parse_telemetry,
)


# =============================================================================
# VESCComm — high-level communication service
# =============================================================================


class VESCComm:
    """UART telemetry service and motor command transmitter."""

    def __init__(self, uart_port, shared_state):
        self._uart = uart_port
        self._state = shared_state
        self._rx_buf = bytearray()

    # --- Telemetry ---

    def request_telemetry(self):
        self._uart.write(_build_telemetry_request())

    def service_rx(self):
        """Read available UART bytes and attempt to parse complete frames."""
        data = self._uart.read()
        if data:
            self._rx_buf.extend(data)

        while True:
            payload, self._rx_buf = _extract_payload(self._rx_buf)
            if payload is None:
                break
            self._handle_payload(payload)

        if len(self._rx_buf) > 512:
            self._rx_buf = self._rx_buf[-256:]

    def _handle_payload(self, payload):
        vals = _parse_telemetry(payload)
        if vals is None:
            return
        s = self._state
        (
            s.vesc_temp_fet_c, s.vesc_temp_motor_c,
            s.vesc_motor_current_a, s.vesc_input_current_a,
            s.vesc_duty_cycle, s.vesc_rpm, s.vesc_bus_voltage_v,
            s.vesc_ah, s.vesc_ah_charged,
            s.vesc_wh, s.vesc_wh_charged,
            s.vesc_tach, s.vesc_tach_abs,
            s.vesc_fault_code,
        ) = vals
        s.cap_voltage_v = s.vesc_bus_voltage_v
        s.vesc_mech_rpm = s.vesc_rpm * VESC_ERPM_TO_MECH_RPM
        s.last_vesc_rx_ms = ticks_ms()

    # --- Commands ---

    def send_assist(self, current_a):
        self._uart.write(_build_set_current(current_a))
        self._state.last_command_tx_ms = ticks_ms()

    def send_regen(self, current_a):
        self._uart.write(_build_set_brake_current(current_a))
        self._state.last_command_tx_ms = ticks_ms()

    def send_neutral(self):
        self._uart.write(_build_set_current(0.0))
        self._state.last_command_tx_ms = ticks_ms()


# =============================================================================
# CommandManager — final gate between control requests and VESC transmissions
# =============================================================================


class CommandManager:
    def __init__(self, vesc_comm, shared_state):
        self._vesc = vesc_comm
        self._state = shared_state

    def update(self):
        s = self._state

        if s.inhibit_motor_commands:
            self._vesc.send_neutral()
            return

        if s.assist_command_request > 0.0:
            self._vesc.send_assist(s.assist_command_request)
            return

        if s.regen_command_request > 0.0:
            self._vesc.send_regen(s.regen_command_request)
            return

        self._vesc.send_neutral()
