# services/vesc_packets.py — VESC packet encoding and decoding
#
# Knows how a VESC UART frame is assembled and checked.
# Exact packet IDs, payload layouts, and scaling MUST be verified against the
# actual installed VESC firmware before implementation.

import struct


# --- VESC UART frame constants ---
FRAME_START_SHORT = 0x02      # Payload <= 256 bytes
FRAME_START_LONG = 0x03       # Payload > 256 bytes
FRAME_END = 0x03

# --- Packet IDs (placeholders — verify against actual VESC FW) ---
COMM_GET_VALUES = 4           # Request full telemetry
COMM_SET_CURRENT = 6          # Set motor current (amps * 1000)
COMM_SET_BRAKE_CURRENT = 8    # Set brake current (amps * 1000)
COMM_SET_DUTY = 5             # Set duty cycle (fraction * 100000)


class VESCPackets:
    """Build and parse VESC UART frames."""

    # --- Outbound frame builders ---

    def build_telemetry_request(self):
        """Build a request-values frame."""
        payload = bytes([COMM_GET_VALUES])
        return self._wrap_frame(payload)

    def build_set_current(self, amps):
        """Build a set-current command frame."""
        payload = struct.pack(">Bi", COMM_SET_CURRENT, int(amps * 1000))
        return self._wrap_frame(payload)

    def build_set_brake_current(self, amps):
        """Build a set-brake-current command frame."""
        payload = struct.pack(">Bi", COMM_SET_BRAKE_CURRENT, int(amps * 1000))
        return self._wrap_frame(payload)

    def build_set_duty(self, fraction):
        """Build a set-duty command frame."""
        payload = struct.pack(">Bi", COMM_SET_DUTY, int(fraction * 100000))
        return self._wrap_frame(payload)

    # --- Inbound frame parser ---

    def parse_telemetry(self, payload):
        """Parse a telemetry response payload into a dict.

        Returns dict with decoded fields, or None on failure.
        """
        if not payload or payload[0] != COMM_GET_VALUES:
            return None

        # TODO: Implement full field extraction once VESC FW packet layout is confirmed
        # Placeholder structure — offsets and scaling MUST be verified
        try:
            result = {
                "bus_voltage": 0.0,
                "motor_current": 0.0,
                "input_current": 0.0,
                "rpm": 0,
                "duty_cycle": 0.0,
                "fault_code": 0,
            }
            # TODO: Unpack actual fields from payload bytes here
            return result
        except Exception:
            return None

    # --- Frame helpers ---

    def _wrap_frame(self, payload):
        """Wrap a payload in VESC UART framing with CRC."""
        length = len(payload)
        if length <= 256:
            frame = bytes([FRAME_START_SHORT, length]) + payload
        else:
            frame = bytes([FRAME_START_LONG, length >> 8, length & 0xFF]) + payload
        crc = self._crc16(payload)
        frame += struct.pack(">H", crc)
        frame += bytes([FRAME_END])
        return frame

    @staticmethod
    def _crc16(data):
        """CRC-16/CCITT used by VESC framing."""
        crc = 0
        for b in data:
            crc ^= b << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ 0x1021
                else:
                    crc <<= 1
                crc &= 0xFFFF
        return crc

    def extract_payload(self, buf):
        """Attempt to extract a complete VESC frame from a byte buffer.

        Returns (payload, remaining_bytes) or (None, buf) if incomplete.
        """
        if len(buf) < 5:
            return None, buf

        if buf[0] == FRAME_START_SHORT:
            plen = buf[1]
            frame_len = 2 + plen + 3  # start + len + payload + crc(2) + end
            if len(buf) < frame_len:
                return None, buf
            payload = buf[2:2 + plen]
            crc_recv = struct.unpack(">H", buf[2 + plen:2 + plen + 2])[0]
            if self._crc16(payload) == crc_recv and buf[frame_len - 1] == FRAME_END:
                return payload, buf[frame_len:]
            # Bad CRC or end byte — discard first byte and retry
            return None, buf[1:]

        # Unknown start byte — discard
        return None, buf[1:]

    # TODO: Identify exact VESC commands to support first
    # TODO: Verify framing format and checksum method for target firmware
    # TODO: Verify scaling for all decoded telemetry fields
    # TODO: Decide whether one telemetry request is enough or multiple packet types are needed
