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
        vals = _parse_telemetry(payload)
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
