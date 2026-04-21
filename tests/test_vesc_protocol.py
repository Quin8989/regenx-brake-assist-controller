# tests/test_vesc_protocol.py — VESC packet framing, CRC, parse, and extract

import struct
import pytest

from services.vesc_protocol import (
    COMM_CUSTOM_APP_DATA,
    COMM_FW_VERSION,
    COMM_GET_VALUES,
    COMM_GET_VALUES_SELECTIVE,
    COMM_SET_CURRENT,
    COMM_SET_MCCONF_TEMP,
    COMM_ALIVE,
    COMM_MOTOR_ESTOP,
    COMM_APP_DISABLE_OUTPUT,
    COMM_SET_BATTERY_CUT,
    FRAME_END,
    FRAME_START_SHORT,
    SELECTIVE_MASK,
    _SELECTIVE_FMT,
    _SELECTIVE_SIZE,
    _TELEMETRY_FMT,
    _TELEMETRY_FMT_EXT,
    _TELEMETRY_SIZE,
    _TELEMETRY_SIZE_FULL,
    _build_alive,
    _build_app_disable_output,
    _build_fw_version_request,
    _build_motor_estop,
    _build_selective_telemetry_request,
    _build_set_battery_cut,
    _build_set_current,
    _build_set_mcconf_temp,
    _build_telemetry_request,
    _crc16,
    _encode_float32_auto,
    _extract_payload,
    _parse_fw_version,
    _parse_push_iq,
    _parse_selective_telemetry,
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
    # Extension fields (bits 16-21)
    pid_pos=0, controller_id=0,
    temp_mos1=0, temp_mos2=0, temp_mos3=0,
    avg_vd=0, avg_vq=0, status=0,
    include_ext=True,
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
    if include_ext:
        body += struct.pack(
            _TELEMETRY_FMT_EXT,
            pid_pos, controller_id,
            temp_mos1, temp_mos2, temp_mos3,
            avg_vd, avg_vq, status,
        )
    return bytes([COMM_GET_VALUES]) + body


@pytest.mark.parametrize("field_idx,kw,expected", [
    pytest.param(0, {"temp_fet": 250}, 25.0, id="temp_fet"),
    pytest.param(1, {"temp_motor": 450}, 45.0, id="temp_motor"),
    pytest.param(2, {"motor_current": 1500}, 15.0, id="motor_current"),
    pytest.param(3, {"input_current": -800}, -8.0, id="input_current"),
    pytest.param(4, {"avg_id": -200}, -2.0, id="id_current"),
    pytest.param(5, {"avg_iq": -456}, -4.56, id="iq_current"),
    pytest.param(6, {"duty": 500}, 0.5, id="duty"),
    pytest.param(7, {"rpm": 4500}, 4500, id="rpm"),
    pytest.param(8, {"v_in": 480}, 48.0, id="v_in"),
    pytest.param(9, {"tach": 1234}, 1234, id="tach"),
    pytest.param(10, {"tach_abs": 5678}, 5678, id="tach_abs"),
    pytest.param(11, {"fault": 3}, 3, id="fault"),
    pytest.param(12, {"pid_pos": 1500000}, 1.5, id="pid_pos"),
    pytest.param(13, {"controller_id": 42}, 42, id="controller_id"),
    pytest.param(14, {"temp_mos1": 350}, 35.0, id="temp_mos1"),
    pytest.param(15, {"temp_mos2": 360}, 36.0, id="temp_mos2"),
    pytest.param(16, {"temp_mos3": 370}, 37.0, id="temp_mos3"),
    pytest.param(17, {"avg_vd": -500}, -0.5, id="avg_vd"),
    pytest.param(18, {"avg_vq": 12000}, 12.0, id="avg_vq"),
    pytest.param(19, {"status": 1}, 1, id="status"),
])
def test_telemetry_field_scaling(field_idx, kw, expected):
    payload = _make_telemetry_payload(**kw)
    result = _parse_telemetry(payload)
    assert result is not None
    assert len(result) == 20
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


def test_telemetry_base_only_defaults_extension():
    """53-byte payload (no bits 16-21) should still return 20 values with defaults."""
    payload = _make_telemetry_payload(avg_iq=-300, tach=99, include_ext=False)
    result = _parse_telemetry(payload)
    assert result is not None
    assert len(result) == 20
    assert abs(result[5] - (-3.0)) < 0.01   # avg_iq still parsed
    assert result[9] == 99                    # tach still parsed
    # Extension fields default to zero
    assert result[12] == 0.0   # pid_pos
    assert result[13] == 0     # controller_id
    assert result[17] == 0.0   # avg_vd
    assert result[18] == 0.0   # avg_vq
    assert result[19] == 0     # status


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


# ---- New command builders (Phase 3) ----

def test_fw_version_request_opcode():
    frame = _build_fw_version_request()
    extracted, _ = _extract_payload(bytearray(frame))
    assert extracted == bytes([COMM_FW_VERSION])


def test_alive_opcode():
    frame = _build_alive()
    extracted, _ = _extract_payload(bytearray(frame))
    assert extracted == bytes([COMM_ALIVE])


def test_estop_encoding():
    frame = _build_motor_estop(1000)
    extracted, _ = _extract_payload(bytearray(frame))
    assert extracted[0] == COMM_MOTOR_ESTOP
    timeout = struct.unpack(">H", extracted[1:3])[0]
    assert timeout == 1000


@pytest.mark.parametrize("timeout_ms", [0, 250, 65535])
def test_estop_timeout_range(timeout_ms):
    frame = _build_motor_estop(timeout_ms)
    extracted, _ = _extract_payload(bytearray(frame))
    assert struct.unpack(">H", extracted[1:3])[0] == timeout_ms


def test_disable_output_encoding():
    frame = _build_app_disable_output(500, fwd_can=False)
    extracted, _ = _extract_payload(bytearray(frame))
    assert extracted[0] == COMM_APP_DISABLE_OUTPUT
    assert extracted[1] == 0  # fwd_can = False
    timeout = struct.unpack(">i", extracted[2:6])[0]
    assert timeout == 500


def test_disable_output_fwd_can():
    frame = _build_app_disable_output(300, fwd_can=True)
    extracted, _ = _extract_payload(bytearray(frame))
    assert extracted[1] == 1  # fwd_can = True


def test_battery_cut_encoding():
    frame = _build_set_battery_cut(10.0, 8.0, store=False, fwd_can=False)
    extracted, _ = _extract_payload(bytearray(frame))
    assert extracted[0] == COMM_SET_BATTERY_CUT
    start_raw, end_raw = struct.unpack(">ii", extracted[1:9])
    assert start_raw == 10000
    assert end_raw == 8000
    assert extracted[9] == 0   # store = False
    assert extracted[10] == 0  # fwd_can = False


def test_battery_cut_store_and_fwd():
    frame = _build_set_battery_cut(40.5, 39.0, store=True, fwd_can=True)
    extracted, _ = _extract_payload(bytearray(frame))
    start_raw, end_raw = struct.unpack(">ii", extracted[1:9])
    assert start_raw == 40500
    assert end_raw == 39000
    assert extracted[9] == 1   # store
    assert extracted[10] == 1  # fwd_can


# ---- FW version parsing ----

def _make_fw_payload(major=6, minor=6, hw_name="410"):
    return bytes([COMM_FW_VERSION, major, minor]) + hw_name.encode("ascii") + b"\x00"


def test_parse_fw_version_basic():
    result = _parse_fw_version(_make_fw_payload(6, 6, "410"))
    assert result == (6, 6, "410")


def test_parse_fw_version_flipsky():
    result = _parse_fw_version(_make_fw_payload(6, 5, "Flipsky_412"))
    assert result == (6, 5, "Flipsky_412")


def test_parse_fw_version_no_null_terminator():
    """Missing null terminator — should still parse up to end."""
    payload = bytes([COMM_FW_VERSION, 6, 6]) + b"410"
    result = _parse_fw_version(payload)
    assert result == (6, 6, "410")


@pytest.mark.parametrize("payload", [
    pytest.param(b"", id="empty"),
    pytest.param(None, id="none"),
    pytest.param(bytes([0xFF, 6, 6]) + b"410\x00", id="wrong_opcode"),
    pytest.param(bytes([COMM_FW_VERSION, 6]), id="too_short"),
])
def test_parse_fw_version_invalid_returns_none(payload):
    assert _parse_fw_version(payload) is None


def test_all_new_commands_roundtrip():
    """All new command frames survive wrap → extract roundtrip."""
    for builder in [
        _build_fw_version_request,
        _build_alive,
        lambda: _build_motor_estop(500),
        lambda: _build_app_disable_output(200),
        lambda: _build_set_battery_cut(10.0, 8.0),
        _build_selective_telemetry_request,
        lambda: _build_set_mcconf_temp(
            -50, 50, 1.0, 1.0, -50, 50,
            -200000, 200000, 0.0, 0.95, -500, 500),
    ]:
        frame = builder()
        extracted, remaining = _extract_payload(bytearray(frame))
        assert extracted is not None
        assert len(remaining) == 0


# ---- Selective telemetry (COMM_GET_VALUES_SELECTIVE) ----

def _make_selective_payload(
    temp_fet=250, temp_motor=300,
    motor_current=1500, input_current=800,
    avg_iq=-330, duty=500, rpm=4500, v_in=250,
    fault_code=0,
):
    """Build a fake COMM_GET_VALUES_SELECTIVE response payload."""
    header = struct.pack(">BI", COMM_GET_VALUES_SELECTIVE, SELECTIVE_MASK)
    body = struct.pack(
        _SELECTIVE_FMT,
        temp_fet, temp_motor,
        motor_current, input_current,
        avg_iq, duty, rpm, v_in,
        fault_code,
    )
    return header + body


def test_selective_request_opcode_and_mask():
    frame = _build_selective_telemetry_request()
    extracted, _ = _extract_payload(bytearray(frame))
    assert extracted[0] == COMM_GET_VALUES_SELECTIVE
    mask = struct.unpack(">I", extracted[1:5])[0]
    assert mask == SELECTIVE_MASK


def test_selective_request_custom_mask():
    frame = _build_selective_telemetry_request(mask=0xFF)
    extracted, _ = _extract_payload(bytearray(frame))
    mask = struct.unpack(">I", extracted[1:5])[0]
    assert mask == 0xFF


@pytest.mark.parametrize("field_idx,kw,expected", [
    pytest.param(0, {"temp_fet": 250}, 25.0, id="temp_fet"),
    pytest.param(1, {"temp_motor": 450}, 45.0, id="temp_motor"),
    pytest.param(2, {"motor_current": 1500}, 15.0, id="motor_current"),
    pytest.param(3, {"input_current": -800}, -8.0, id="input_current"),
    pytest.param(4, {"avg_iq": -456}, -4.56, id="iq_current"),
    pytest.param(5, {"duty": 750}, 0.75, id="duty"),
    pytest.param(6, {"rpm": 4500}, 4500, id="rpm"),
    pytest.param(7, {"v_in": 480}, 48.0, id="v_in"),
    pytest.param(8, {"fault_code": 3}, 3, id="fault_code"),
])
def test_selective_field_scaling(field_idx, kw, expected):
    payload = _make_selective_payload(**kw)
    result = _parse_selective_telemetry(payload)
    assert result is not None
    assert len(result) == 9
    assert abs(result[field_idx] - expected) < 0.01


@pytest.mark.parametrize("payload", [
    pytest.param(b"", id="empty"),
    pytest.param(None, id="none"),
    pytest.param(bytes([0xFF]) + bytes(30), id="wrong_opcode"),
])
def test_selective_invalid_returns_none(payload):
    assert _parse_selective_telemetry(payload) is None


def test_selective_truncated_returns_none():
    payload = _make_selective_payload()
    assert _parse_selective_telemetry(payload[:10]) is None


def test_selective_wrong_mask_returns_none():
    """Response with a different mask than expected is rejected."""
    header = struct.pack(">BI", COMM_GET_VALUES_SELECTIVE, 0x000000FF)
    body = bytes(_SELECTIVE_SIZE)
    assert _parse_selective_telemetry(header + body) is None


def test_selective_smaller_than_full():
    """Selective response is significantly smaller than full telemetry."""
    sel_frame = _build_selective_telemetry_request()
    sel_extracted, _ = _extract_payload(bytearray(sel_frame))
    full_frame = _build_telemetry_request()
    full_extracted, _ = _extract_payload(bytearray(full_frame))
    # Selective request carries mask; full is just opcode
    assert len(sel_extracted) == 5  # opcode + 4-byte mask
    # Response payload: 5 header + 25 data = 30 vs full 1 + 73 = 74
    sel_resp = _make_selective_payload()
    full_resp = _make_telemetry_payload()
    assert len(sel_resp) < len(full_resp)


# ---- float32_auto encoding ----

@pytest.mark.parametrize("value,expected_type", [
    pytest.param(0.0, 0, id="zero"),
    pytest.param(0.1, 0, id="small_positive"),
    pytest.param(-0.05, 0, id="small_negative"),
    pytest.param(5.0, 1, id="mid_int16_1k"),
    pytest.param(-30.0, 1, id="neg_int16_1k"),
    pytest.param(50.0, 2, id="int16_100"),
    pytest.param(-200.0, 2, id="neg_int16_100"),
    pytest.param(500.0, 3, id="int32_1k"),
    pytest.param(-200000.0, 3, id="large_neg_int32"),
])
def test_float32_auto_type_selection(value, expected_type):
    encoded = _encode_float32_auto(value)
    assert encoded[0] == expected_type


@pytest.mark.parametrize("value", [0.0, 0.1, -0.05, 5.0, -30.0, 50.0, -200.0, 500.0, -200000.0, 0.95])
def test_float32_auto_roundtrip(value):
    """Encode → decode roundtrip preserves value within encoding precision."""
    encoded = _encode_float32_auto(value)
    t = encoded[0]
    if t == 0:
        decoded = struct.unpack(">b", encoded[1:2])[0] / 1000.0
    elif t == 1:
        decoded = struct.unpack(">h", encoded[1:3])[0] / 1000.0
    elif t == 2:
        decoded = struct.unpack(">h", encoded[1:3])[0] / 100.0
    elif t == 3:
        decoded = struct.unpack(">i", encoded[1:5])[0] / 1000.0
    else:
        decoded = struct.unpack(">f", encoded[1:5])[0]
    assert abs(decoded - value) < 0.02  # within 0.02 precision


# ---- MCCONF_TEMP builder ----

def test_mcconf_temp_opcode():
    frame = _build_set_mcconf_temp(
        -50, 50, 1.0, 1.0, -50, 50,
        -200000, 200000, 0.0, 0.95, -500, 500)
    extracted, _ = _extract_payload(bytearray(frame))
    assert extracted[0] == COMM_SET_MCCONF_TEMP


def test_mcconf_temp_flags():
    frame = _build_set_mcconf_temp(
        -50, 50, 1.0, 1.0, -50, 50,
        -200000, 200000, 0.0, 0.95, -500, 500,
        store=True, fwd_can=True, ack=True, divide=True)
    extracted, _ = _extract_payload(bytearray(frame))
    assert extracted[1] == 1  # store
    assert extracted[2] == 1  # fwd_can
    assert extracted[3] == 1  # ack
    assert extracted[4] == 1  # divide


def test_mcconf_temp_default_flags_zero():
    frame = _build_set_mcconf_temp(
        -50, 50, 1.0, 1.0, -50, 50,
        -200000, 200000, 0.0, 0.95, -500, 500)
    extracted, _ = _extract_payload(bytearray(frame))
    assert extracted[1:5] == bytes(4)  # all flags zero


def test_mcconf_temp_frame_valid():
    """MCCONF_TEMP frame survives wrap → extract roundtrip."""
    frame = _build_set_mcconf_temp(
        -50, 50, 1.0, 1.0, -50, 50,
        -200000, 200000, 0.0, 0.95, -500, 500)
    extracted, remaining = _extract_payload(bytearray(frame))
    assert extracted is not None
    assert len(remaining) == 0
    # Payload: opcode(1) + flags(4) + 12 auto-floats (~36-48 bytes)
    assert len(extracted) > 40


# ---- COMM_CUSTOM_APP_DATA (push-iq from LispBM) ----

def _make_push_iq_payload(iq=5.0, erpm=3300.0, drpm_mean=0.0, drpm_peak_neg=0.0):
    """Build a COMM_CUSTOM_APP_DATA 16-byte aggregate payload."""
    return bytes([COMM_CUSTOM_APP_DATA]) + struct.pack(
        ">ffff", erpm, drpm_mean, drpm_peak_neg, iq)


def test_parse_push_iq_happy():
    payload = _make_push_iq_payload(
        iq=-3.5, erpm=2200.0, drpm_mean=-400.0, drpm_peak_neg=-2500.0)
    result = _parse_push_iq(payload)
    assert result is not None
    erpm, drpm_mean, drpm_peak_neg, iq = result
    assert abs(iq - (-3.5)) < 1e-5
    assert abs(erpm - 2200.0) < 1e-2
    assert abs(drpm_mean - (-400.0)) < 1e-2
    assert abs(drpm_peak_neg - (-2500.0)) < 1e-2


def test_parse_push_iq_zero():
    result = _parse_push_iq(_make_push_iq_payload(iq=0.0, erpm=0.0))
    assert result is not None
    assert result == (0.0, 0.0, 0.0, 0.0)


def test_parse_push_iq_wrong_opcode():
    payload = bytes([0x99]) + struct.pack(">ffff", 1.0, 2.0, 3.0, 4.0)
    assert _parse_push_iq(payload) is None


def test_parse_push_iq_too_short():
    payload = bytes([COMM_CUSTOM_APP_DATA]) + struct.pack(">ff", 1.0, 2.0)  # only 8 bytes
    assert _parse_push_iq(payload) is None


def test_parse_push_iq_empty():
    assert _parse_push_iq(b"") is None
    assert _parse_push_iq(None) is None


def test_push_iq_frame_roundtrip():
    """Wrapping and extracting push-iq payload preserves values."""
    inner = _make_push_iq_payload(
        iq=-12.5, erpm=5500.0, drpm_mean=-80.0, drpm_peak_neg=-4000.0)
    frame = _wrap_frame(inner)
    extracted, remaining = _extract_payload(bytearray(frame))
    assert extracted is not None
    assert len(remaining) == 0
    result = _parse_push_iq(extracted)
    assert result is not None
    erpm, drpm_mean, drpm_peak_neg, iq = result
    assert abs(iq - (-12.5)) < 1e-4
    assert abs(erpm - 5500.0) < 1e-2
    assert abs(drpm_mean - (-80.0)) < 1e-2
    assert abs(drpm_peak_neg - (-4000.0)) < 1e-2
