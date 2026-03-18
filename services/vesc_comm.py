# services/vesc_comm.py — High-level VESC communication service
#
# Periodically requests telemetry, feeds inbound bytes to the parser,
# and sends motor commands. Does NOT decide when assist/regen is allowed,
# render display text, or own the state machine.

from time import ticks_ms, ticks_diff
from config.thresholds import VESC_TELEMETRY_TIMEOUT_MS


class VESCComm:
    def __init__(self, uart_port, vesc_packets, shared_state):
        self._uart = uart_port
        self._packets = vesc_packets
        self._state = shared_state
        self._rx_buf = bytearray()

    # --- Telemetry ---

    def request_telemetry(self):
        """Send a telemetry request frame to the VESC."""
        frame = self._packets.build_telemetry_request()
        self._uart.write(frame)

    def service_rx(self):
        """Read available UART bytes and attempt to parse complete frames."""
        data = self._uart.read()
        if data:
            self._rx_buf.extend(data)

        # Try to extract and process frames
        while True:
            payload, self._rx_buf = self._packets.extract_payload(self._rx_buf)
            if payload is None:
                break
            self._handle_payload(payload)

        # Limit buffer growth if no valid frames are found
        if len(self._rx_buf) > 512:
            self._rx_buf = self._rx_buf[-256:]

    def _handle_payload(self, payload):
        """Process a decoded payload and update shared state."""
        result = self._packets.parse_telemetry(payload)
        if result is not None:
            self._state.vesc_bus_voltage_v = result.get("bus_voltage", 0.0)
            self._state.vesc_motor_current_a = result.get("motor_current", 0.0)
            self._state.vesc_input_current_a = result.get("input_current", 0.0)
            self._state.vesc_rpm = result.get("rpm", 0)
            self._state.vesc_duty_cycle = result.get("duty_cycle", 0.0)
            self._state.vesc_fault_code = result.get("fault_code", 0)
            self._state.last_vesc_rx_ms = ticks_ms()

    # --- Commands ---

    def send_assist(self, current_a):
        """Send an assist (positive motor current) command."""
        frame = self._packets.build_set_current(current_a)
        self._uart.write(frame)
        self._state.last_command_tx_ms = ticks_ms()

    def send_regen(self, current_a):
        """Send a regen (brake current) command."""
        frame = self._packets.build_set_brake_current(current_a)
        self._uart.write(frame)
        self._state.last_command_tx_ms = ticks_ms()

    def send_neutral(self):
        """Send a zero / neutral command."""
        frame = self._packets.build_set_current(0.0)
        self._uart.write(frame)
        self._state.last_command_tx_ms = ticks_ms()

    # --- Health ---

    def is_telemetry_healthy(self):
        """Return True if telemetry has been received recently."""
        return ticks_diff(ticks_ms(), self._state.last_vesc_rx_ms) < VESC_TELEMETRY_TIMEOUT_MS

    # TODO: Define retry / timeout behavior
    # TODO: Define how packet parsing resynchronizes after corrupted data
    # TODO: Define whether commands are sent every cycle or only when changed + heartbeat
    # TODO: Decide what happens if the VESC reports its own fault
