# services/vesc_comm.py — VESC telemetry service and command output
#
# VESCComm: reads UART, parses telemetry into SharedState, sends commands.

from machine import UART, Pin
from time import ticks_ms

from config.settings import (
    VESC_BAUD_RATE,
    VESC_ERPM_TO_MECH_RPM,
    VESC_UART_ID,
    VESC_UART_RX,
    VESC_UART_TX,
)
from services.vesc_protocol import (
    COMM_CUSTOM_APP_DATA,
    COMM_FW_VERSION,
    COMM_GET_VALUES_SELECTIVE,
    _build_alive,
    _build_app_disable_output,
    _build_fw_version_request,
    _build_motor_estop,
    _build_selective_telemetry_request,
    _build_set_battery_cut,
    _build_set_current,
    _build_telemetry_request,
    _extract_payload,
    _parse_fw_version,
    _parse_push_iq,
    _parse_selective_telemetry,
    _parse_telemetry,
)


# =============================================================================
# VESCComm — high-level communication service
# =============================================================================


class VESCComm:
    """UART telemetry service and motor command transmitter."""

    def __init__(self, shared_state, uart_port=None):
        if uart_port is None:
            uart_port = UART(
                VESC_UART_ID,
                baudrate=VESC_BAUD_RATE,
                tx=Pin(VESC_UART_TX),
                rx=Pin(VESC_UART_RX),
            )
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
            # Find last short-frame-start byte to avoid splitting a partial frame.
            # Only anchor on 0x02 (FRAME_START_SHORT) — 0x03 is ambiguous
            # because it serves as both FRAME_START_LONG and FRAME_END.
            cut = len(self._rx_buf) - 256
            for i in range(cut, len(self._rx_buf)):
                if self._rx_buf[i] == 0x02:
                    cut = i
                    break
            self._rx_buf = self._rx_buf[cut:]

    def _handle_payload(self, payload):
        # Route by opcode
        if not payload:
            return
        opcode = payload[0]

        if opcode == COMM_FW_VERSION:
            result = _parse_fw_version(payload)
            if result is not None:
                s = self._state
                s.vesc_fw_major, s.vesc_fw_minor, s.vesc_hw_name = result
            return

        if opcode == COMM_GET_VALUES_SELECTIVE:
            self._handle_selective(payload)
            return

        if opcode == COMM_CUSTOM_APP_DATA:
            self._handle_push_iq(payload)
            return

        # Full telemetry (COMM_GET_VALUES)
        vals = _parse_telemetry(payload)
        if vals is None:
            return
        s = self._state
        (
            s.vesc_temp_fet_c, s.vesc_temp_motor_c,
            s.vesc_motor_current_a, s.vesc_input_current_a,
            s.vesc_id_current_a, s.vesc_iq_current_a,
            s.vesc_duty_cycle, erpm, s.cap_voltage_v,
            s.vesc_tach, s.vesc_tach_abs,
            s.vesc_fault_code,
            s.vesc_pid_pos, s.vesc_controller_id,
            s.vesc_temp_mos1_c, s.vesc_temp_mos2_c, s.vesc_temp_mos3_c,
            s.vesc_vd, s.vesc_vq,
            s.vesc_status,
        ) = vals
        s.vesc_mech_rpm = erpm * VESC_ERPM_TO_MECH_RPM
        s.last_vesc_rx_ms = ticks_ms()

    def _handle_selective(self, payload):
        """Handle COMM_GET_VALUES_SELECTIVE response — populate used fields."""
        vals = _parse_selective_telemetry(payload)
        if vals is None:
            return
        s = self._state
        (
            s.vesc_temp_fet_c, s.vesc_temp_motor_c,
            s.vesc_motor_current_a, s.vesc_input_current_a,
            s.vesc_iq_current_a,
            s.vesc_duty_cycle, erpm, s.cap_voltage_v,
            s.vesc_fault_code,
        ) = vals
        s.vesc_mech_rpm = erpm * VESC_ERPM_TO_MECH_RPM
        s.last_vesc_rx_ms = ticks_ms()

    def _handle_push_iq(self, payload):
        """Handle COMM_CUSTOM_APP_DATA from VESC LispBM push-iq script.

        Updates lower-latency iq and fast RPM fields from the LispBM push.
        The iq value comes from get-iq() on the VESC, which is filtered but
        not the long-window averaged telemetry value.
        """
        vals = _parse_push_iq(payload)
        if vals is None:
            return
        s = self._state
        iq, erpm = vals
        s.vesc_iq_instantaneous_a = iq
        s.vesc_erpm_fast = erpm
        s.vesc_mech_rpm_fast = erpm * VESC_ERPM_TO_MECH_RPM
        s.last_push_iq_rx_ms = ticks_ms()

    # --- Commands ---

    def send_current(self, current_a):
        """Send a motor current command via COMM_SET_CURRENT.

        Positive → forward drive.
        Negative → FOC regen braking (energy flows motor → bus → caps).
        Zero     → idle.

        Always uses COMM_SET_CURRENT (cmd 6).  Negative values put the
        FOC controller into standard current mode with negative iq,
        which actively switches the inverter to return energy to the bus.
        COMM_SET_CURRENT_BRAKE (cmd 7) is intentionally NOT used — its
        phase-shorting behaviour dissipates energy as heat.
        """
        self._uart.write(_build_set_current(current_a))

    def send_alive(self):
        """Send COMM_ALIVE to reset the VESC communication timeout watchdog."""
        self._uart.write(_build_alive())

    def send_estop(self, timeout_ms=1000):
        """Send COMM_MOTOR_ESTOP — VESC ignores input and releases motor."""
        self._uart.write(_build_motor_estop(timeout_ms))

    def send_disable_output(self, timeout_ms=500, fwd_can=False):
        """Send COMM_APP_DISABLE_OUTPUT — suppress VESC app output."""
        self._uart.write(_build_app_disable_output(timeout_ms, fwd_can))

    def request_fw_version(self):
        """Send COMM_FW_VERSION request.  Response handled in service_rx."""
        self._uart.write(_build_fw_version_request())

    def set_battery_cut(self, start_v, end_v, store=False, fwd_can=False):
        """Send COMM_SET_BATTERY_CUT to protect supercap overvoltage at VESC.

        start_v: soft cutoff start voltage (current limiting begins).
        end_v:   hard cutoff (current drops to zero).
        """
        self._uart.write(_build_set_battery_cut(start_v, end_v, store, fwd_can))

    def request_telemetry_selective(self):
        """Send COMM_GET_VALUES_SELECTIVE with our control-loop bitmask.

        Returns only the 9 fields the system actively uses (25 data bytes
        vs 73 for full telemetry).  Halves UART RX time from ~6.9ms to
        ~3.0ms at 115200 baud, reducing telemetry latency by ~45%.
        """
        self._uart.write(_build_selective_telemetry_request())
