# services/vesc_protocol.py — VESC UART packet framing, CRC, and parsing
#
# Pure protocol layer — no hardware I/O, no application state.
# Handles frame encoding/decoding, command building, and telemetry parsing
# per the VESC UART packet specification.

import struct

# =============================================================================
# VESC UART frame constants and opcodes
# =============================================================================

FRAME_START_SHORT = 0x02      # Payload <= 256 bytes
FRAME_START_LONG = 0x03       # Payload > 256 bytes
FRAME_END = 0x03

COMM_GET_VALUES = 4           # Request full telemetry
COMM_SET_CURRENT = 6          # Set motor current (amps * 1000)
COMM_SET_CURRENT_BRAKE = 7    # Set brake current ceiling (amps * 1000, positive)

# Telemetry struct: everything after the opcode byte
# Includes dq current terms reported by VESC (avg_id, avg_iq).
_TELEMETRY_FMT = ">hhiiiihihiiiiiiB"
_TELEMETRY_SIZE = 53  # bytes after opcode


# =============================================================================
# CRC and frame wrapping
# =============================================================================


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


def _wrap_frame(payload):
    """Wrap a payload in VESC UART framing with CRC."""
    length = len(payload)
    if length <= 255:
        frame = bytes([FRAME_START_SHORT, length]) + payload
    else:
        frame = bytes([FRAME_START_LONG, length >> 8, length & 0xFF]) + payload
    crc = _crc16(payload)
    frame += struct.pack(">H", crc)
    frame += bytes([FRAME_END])
    return frame


# =============================================================================
# Command builders
# =============================================================================


def _build_telemetry_request():
    return _wrap_frame(bytes([COMM_GET_VALUES]))


def _build_set_current(amps):
    return _wrap_frame(struct.pack(">Bi", COMM_SET_CURRENT, int(amps * 1000)))


def _build_set_brake_current(amps):
    """Build COMM_SET_CURRENT_BRAKE packet.  amps must be >= 0."""
    return _wrap_frame(struct.pack(">Bi", COMM_SET_CURRENT_BRAKE, int(amps * 1000)))


# =============================================================================
# Telemetry parsing
# =============================================================================


def _parse_telemetry(payload):
    """Parse a COMM_GET_VALUES response into a tuple of scaled values, or None."""
    if not payload or payload[0] != COMM_GET_VALUES:
        return None
    if len(payload) < 1 + _TELEMETRY_SIZE:
        return None
    try:
        (
            temp_fet, temp_motor,
            motor_current_raw, input_current_raw,
            _avg_id, _avg_iq,
            duty_raw, rpm, v_in_raw,
            _ah, _ah_charged, _wh, _wh_charged,
            _tach, _tach_abs,
            fault_code,
        ) = struct.unpack_from(_TELEMETRY_FMT, payload, 1)
    except Exception:
        return None
    return (
        temp_fet / 10.0,
        temp_motor / 10.0,
        motor_current_raw / 100.0,
        input_current_raw / 100.0,
        _avg_iq / 100.0,
        duty_raw / 1000.0,
        rpm,
        v_in_raw / 10.0,
        fault_code,
    )


# =============================================================================
# Frame extraction
# =============================================================================


def _extract_payload(buf):
    """Extract a complete VESC frame (short or long) from a byte buffer.

    Returns (payload_bytes, remaining_buf) on success,
    or (None, buf) when the buffer does not yet contain a complete frame.
    """
    while len(buf) >= 6:
        if buf[0] == FRAME_START_SHORT:
            length = buf[1]
            header_size = 2  # start(1) + len(1)
        elif buf[0] == FRAME_START_LONG:
            if len(buf) < 7:
                break  # Need more data for long-frame header
            length = (buf[1] << 8) | buf[2]
            header_size = 3  # start(1) + len_hi(1) + len_lo(1)
        else:
            buf = buf[1:]
            continue

        frame_size = header_size + length + 3  # payload + crc(2) + end(1)

        if len(buf) < frame_size:
            break

        payload = bytes(buf[header_size:header_size + length])
        crc_offset = header_size + length
        crc_recv = (buf[crc_offset] << 8) | buf[crc_offset + 1]
        end_byte = buf[crc_offset + 2]

        if end_byte != FRAME_END or crc_recv != _crc16(payload):
            buf = buf[1:]
            continue

        return payload, buf[frame_size:]

    return None, buf
