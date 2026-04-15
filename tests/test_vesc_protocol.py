# tests/test_vesc_protocol.py — VESC packet framing, CRC, parse, and extract

import struct
import pytest

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


# ---- CRC-16 ----

@pytest.mark.parametrize("data,check", [
    pytest.param(b"", lambda c: c == 0, id="empty"),
    pytest.param(b"123456789", lambda c: c == 0x31C3, id="known_vector"),
    pytest.param(b"\x04", lambda c: 0 <= c <= 0xFFFF, id="single_byte"),
])
def test_crc16(data, check):
    assert check(_crc16(data))


def test_crc16_deterministic():
    data = bytes(range(256))
    assert _crc16(data) == _crc16(data)


# ---- Frame wrapping ----

def test_short_frame_structure():
    payload = bytes([COMM_GET_VALUES])
    frame = _wrap_frame(payload)
    assert frame[0] == FRAME_START_SHORT
    assert frame[1] == len(payload)
    assert frame[-1] == FRAME_END
    crc = struct.unpack(">H", frame[-3:-1])[0]
    assert crc == _crc16(payload)


def test_long_frame_for_256_bytes():
    payload = bytes(256)
    frame = _wrap_frame(payload)
    assert frame[0] == 0x03  # FRAME_START_LONG


def test_wrap_then_extract_roundtrip():
    payload = bytes([COMM_GET_VALUES])
    frame = _wrap_frame(payload)
    extracted, remaining = _extract_payload(bytearray(frame))
    assert extracted == payload
    assert len(remaining) == 0


# ---- Command builders ----

@pytest.mark.parametrize("current_a,expected_raw", [
    pytest.param(10.5, 10500, id="positive"),
    pytest.param(-5.0, -5000, id="negative"),
    pytest.param(0.0, 0, id="zero"),
])
def test_set_current_encoding(current_a, expected_raw):
    frame = _build_set_current(current_a)
    extracted, _ = _extract_payload(bytearray(frame))
    assert extracted[0] == COMM_SET_CURRENT
    assert struct.unpack(">i", extracted[1:5])[0] == expected_raw


def test_telemetry_request_opcode():
    frame = _build_telemetry_request()
    extracted, _ = _extract_payload(bytearray(frame))
    assert extracted == bytes([COMM_GET_VALUES])


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


@pytest.mark.parametrize("field_idx,kw,expected", [
    pytest.param(0, {"temp_fet": 250}, 25.0, id="temp_fet"),
    pytest.param(1, {"temp_motor": 450}, 45.0, id="temp_motor"),
    pytest.param(2, {"motor_current": 1500}, 15.0, id="motor_current"),
    pytest.param(3, {"input_current": -800}, -8.0, id="input_current"),
    pytest.param(4, {"avg_iq": -456}, -4.56, id="iq_current"),
    pytest.param(5, {"duty": 500}, 0.5, id="duty"),
    pytest.param(6, {"rpm": 4500}, 4500, id="rpm"),
    pytest.param(7, {"v_in": 480}, 48.0, id="v_in"),
    pytest.param(8, {"fault": 3}, 3, id="fault"),
])
def test_telemetry_field_scaling(field_idx, kw, expected):
    payload = _make_telemetry_payload(**kw)
    result = _parse_telemetry(payload)
    assert result is not None
    assert len(result) == 9
    assert abs(result[field_idx] - expected) < 0.01


@pytest.mark.parametrize("payload", [
    pytest.param(b"", id="empty"),
    pytest.param(None, id="none"),
    pytest.param(bytes([0xFF]) + bytes(60), id="wrong_opcode"),
])
def test_telemetry_invalid_returns_none(payload):
    assert _parse_telemetry(payload) is None


def test_telemetry_truncated_returns_none():
    payload = _make_telemetry_payload()
    assert _parse_telemetry(payload[:10]) is None


# ---- Frame extraction ----

def test_extract_complete_frame():
    payload = bytes([COMM_GET_VALUES])
    frame = _wrap_frame(payload)
    extracted, remaining = _extract_payload(bytearray(frame))
    assert extracted == payload
    assert len(remaining) == 0


@pytest.mark.parametrize("buf_factory,expect_extracted", [
    pytest.param(lambda: bytearray(_wrap_frame(bytes([COMM_GET_VALUES])))[:3], False, id="incomplete"),
    pytest.param(lambda: bytearray(), False, id="empty"),
    pytest.param(lambda: _corrupt_crc(_wrap_frame(bytes([COMM_GET_VALUES]))), False, id="bad_crc"),
    pytest.param(lambda: _corrupt_end(_wrap_frame(bytes([COMM_GET_VALUES]))), False, id="bad_end"),
])
def test_extract_rejects_invalid(buf_factory, expect_extracted):
    extracted, _ = _extract_payload(buf_factory())
    assert (extracted is not None) == expect_extracted


def _corrupt_crc(frame):
    buf = bytearray(frame)
    buf[-2] ^= 0xFF
    return buf


def _corrupt_end(frame):
    buf = bytearray(frame)
    buf[-1] = 0xFF
    return buf


def test_garbage_prefix_skipped():
    payload = bytes([COMM_GET_VALUES])
    frame = _wrap_frame(payload)
    buf = bytearray(b"\xFF\xFE\xFD") + bytearray(frame)
    extracted, _ = _extract_payload(buf)
    assert extracted == payload


def test_two_frames_in_buffer():
    p1 = bytes([COMM_GET_VALUES])
    p2 = bytes([COMM_SET_CURRENT, 0, 0, 0x27, 0x10])
    buf = bytearray(_wrap_frame(p1)) + bytearray(_wrap_frame(p2))
    e1, buf = _extract_payload(buf)
    assert e1 == p1
    e2, buf = _extract_payload(buf)
    assert e2 == p2


def test_long_frame_roundtrip():
    payload = bytes(range(256))
    frame = _wrap_frame(payload)
    assert frame[0] == 0x03
    extracted, remaining = _extract_payload(bytearray(frame))
    assert extracted == payload
    assert len(remaining) == 0
