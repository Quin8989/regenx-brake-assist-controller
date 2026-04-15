# tests/test_vesc_protocol.py — VESC packet framing, CRC, parse, and extract

import struct

from services.vesc_protocol import (
    COMM_GET_VALUES,
    COMM_SET_CURRENT,
    FRAME_END,
    FRAME_START_SHORT,
    _TELEMETRY_FMT,
    _build_set_current,
    _build_telemetry_request,
    _crc16,
    _extract_payload,
    _parse_telemetry,
    _wrap_frame,
)


# ---- CRC-16/CCITT ----

class TestCRC16:
    def test_empty(self):
        assert _crc16(b"") == 0

    def test_known_value(self):
        # "123456789" → CRC-CCITT = 0x31C3 (well-known test vector)
        assert _crc16(b"123456789") == 0x31C3

    def test_single_byte(self):
        result = _crc16(b"\x04")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF

    def test_deterministic(self):
        data = bytes(range(256))
        assert _crc16(data) == _crc16(data)


# ---- Frame wrapping ----

class TestWrapFrame:
    def test_short_frame_structure(self):
        payload = bytes([COMM_GET_VALUES])
        frame = _wrap_frame(payload)
        assert frame[0] == FRAME_START_SHORT
        assert frame[1] == len(payload)
        assert frame[-1] == FRAME_END
        # CRC in bytes [-3:-1]
        crc = struct.unpack(">H", frame[-3:-1])[0]
        assert crc == _crc16(payload)

    def test_short_max_255_bytes(self):
        payload = bytes(255)
        frame = _wrap_frame(payload)
        assert frame[0] == FRAME_START_SHORT
        assert frame[1] == 255

    def test_long_frame_for_256_bytes(self):
        payload = bytes(256)
        frame = _wrap_frame(payload)
        assert frame[0] == 0x03  # FRAME_START_LONG
        assert frame[1] == 1  # length high byte
        assert frame[2] == 0  # length low byte

    def test_roundtrip_extract(self):
        payload = bytes([COMM_GET_VALUES])
        frame = _wrap_frame(payload)
        extracted, remaining = _extract_payload(bytearray(frame))
        assert extracted == payload
        assert len(remaining) == 0


# ---- Command builders ----

class TestCommandBuilders:
    def test_telemetry_request_opcode(self):
        frame = _build_telemetry_request()
        # Extract payload and verify opcode
        extracted, _ = _extract_payload(bytearray(frame))
        assert extracted == bytes([COMM_GET_VALUES])

    def test_set_current_encoding(self):
        frame = _build_set_current(10.5)
        extracted, _ = _extract_payload(bytearray(frame))
        assert extracted[0] == COMM_SET_CURRENT
        value = struct.unpack(">i", extracted[1:5])[0]
        assert value == 10500  # 10.5 * 1000

    def test_set_current_negative(self):
        frame = _build_set_current(-5.0)
        extracted, _ = _extract_payload(bytearray(frame))
        value = struct.unpack(">i", extracted[1:5])[0]
        assert value == -5000

    def test_set_current_zero(self):
        frame = _build_set_current(0.0)
        extracted, _ = _extract_payload(bytearray(frame))
        value = struct.unpack(">i", extracted[1:5])[0]
        assert value == 0


# ---- Telemetry parsing ----

def _make_telemetry_payload(
    temp_fet=250, temp_motor=300,
    motor_current=1500, input_current=800,
    avg_id=0, avg_iq=0,
    duty=500, rpm=4500, v_in=480,
    ah=10000, ah_charged=5000,
    wh=20000, wh_charged=8000,
    tach=1234, tach_abs=5678,
    fault=0,
):
    """Build a raw COMM_GET_VALUES payload with given raw integer values."""
    body = struct.pack(
        _TELEMETRY_FMT,
        temp_fet, temp_motor,
        motor_current, input_current,
        avg_id, avg_iq,
        duty, rpm, v_in,
        ah, ah_charged, wh, wh_charged,
        tach, tach_abs,
        fault,
    )
    return bytes([COMM_GET_VALUES]) + body


class TestParseTelemetry:
    def test_valid_parse(self):
        payload = _make_telemetry_payload()
        result = _parse_telemetry(payload)
        assert result is not None
        # tuple: (temp_fet, temp_motor, motor_current, input_current,
        #         iq_current, duty, rpm, v_in, fault_code)
        assert len(result) == 9

    def test_scaling_temp_fet(self):
        payload = _make_telemetry_payload(temp_fet=250)
        result = _parse_telemetry(payload)
        assert result[0] == 25.0  # 250 / 10

    def test_scaling_temp_motor(self):
        payload = _make_telemetry_payload(temp_motor=450)
        result = _parse_telemetry(payload)
        assert result[1] == 45.0

    def test_scaling_motor_current(self):
        payload = _make_telemetry_payload(motor_current=1500)
        result = _parse_telemetry(payload)
        assert result[2] == 15.0  # 1500 / 100

    def test_scaling_input_current(self):
        payload = _make_telemetry_payload(input_current=-800)
        result = _parse_telemetry(payload)
        assert result[3] == -8.0

    def test_scaling_duty(self):
        payload = _make_telemetry_payload(duty=500)
        result = _parse_telemetry(payload)
        assert result[5] == 0.5  # 500 / 1000

    def test_scaling_iq_current(self):
        payload = _make_telemetry_payload(avg_iq=-456)
        result = _parse_telemetry(payload)
        assert result[4] == -4.56

    def test_rpm_passed_raw(self):
        payload = _make_telemetry_payload(rpm=4500)
        result = _parse_telemetry(payload)
        assert result[6] == 4500

    def test_v_in_scaling(self):
        payload = _make_telemetry_payload(v_in=480)
        result = _parse_telemetry(payload)
        assert abs(result[7] - 48.0) < 0.01  # 480 / 10

    def test_fault_code(self):
        payload = _make_telemetry_payload(fault=3)
        result = _parse_telemetry(payload)
        assert result[8] == 3

    def test_wrong_opcode_returns_none(self):
        payload = _make_telemetry_payload()
        payload = bytes([0xFF]) + payload[1:]
        assert _parse_telemetry(payload) is None

    def test_empty_returns_none(self):
        assert _parse_telemetry(b"") is None
        assert _parse_telemetry(None) is None

    def test_truncated_returns_none(self):
        payload = _make_telemetry_payload()
        assert _parse_telemetry(payload[:10]) is None


# ---- Frame extraction ----

class TestExtractPayload:
    def test_complete_frame(self):
        payload = bytes([COMM_GET_VALUES])
        frame = _wrap_frame(payload)
        extracted, remaining = _extract_payload(bytearray(frame))
        assert extracted == payload
        assert len(remaining) == 0

    def test_incomplete_frame(self):
        payload = bytes([COMM_GET_VALUES])
        frame = _wrap_frame(payload)
        extracted, buf = _extract_payload(bytearray(frame[:3]))
        assert extracted is None

    def test_garbage_prefix_skipped(self):
        payload = bytes([COMM_GET_VALUES])
        frame = _wrap_frame(payload)
        buf = bytearray(b"\xFF\xFE\xFD") + bytearray(frame)
        extracted, remaining = _extract_payload(buf)
        assert extracted == payload

    def test_corrupted_crc_skipped(self):
        payload = bytes([COMM_GET_VALUES])
        frame = bytearray(_wrap_frame(payload))
        frame[-2] ^= 0xFF  # corrupt CRC
        extracted, remaining = _extract_payload(frame)
        assert extracted is None

    def test_two_frames_in_buffer(self):
        p1 = bytes([COMM_GET_VALUES])
        p2 = bytes([COMM_SET_CURRENT, 0, 0, 0x27, 0x10])
        buf = bytearray(_wrap_frame(p1)) + bytearray(_wrap_frame(p2))
        e1, buf = _extract_payload(buf)
        assert e1 == p1
        e2, buf = _extract_payload(buf)
        assert e2 == p2

    def test_empty_buffer(self):
        extracted, remaining = _extract_payload(bytearray())
        assert extracted is None
        assert len(remaining) == 0

    def test_bad_end_byte_skipped(self):
        payload = bytes([COMM_GET_VALUES])
        frame = bytearray(_wrap_frame(payload))
        frame[-1] = 0xFF  # corrupt end byte
        extracted, remaining = _extract_payload(frame)
        assert extracted is None

    def test_long_frame_roundtrip(self):
        """Long frame (payload > 255 bytes) should be wrapped and extracted."""
        payload = bytes(range(256))  # 256 bytes triggers long frame
        frame = _wrap_frame(payload)
        assert frame[0] == 0x03  # FRAME_START_LONG
        extracted, remaining = _extract_payload(bytearray(frame))
        assert extracted == payload
        assert len(remaining) == 0

    def test_long_frame_incomplete(self):
        payload = bytes(range(256))
        frame = _wrap_frame(payload)
        extracted, buf = _extract_payload(bytearray(frame[:10]))
        assert extracted is None
