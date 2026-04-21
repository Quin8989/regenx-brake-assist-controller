# tests/test_vesc_comm.py — VESCComm UART rx/tx

import struct
import pytest
from tests.conftest import set_clock_ms
from core import SharedState
from services.vesc_comm import VESCComm
from services.vesc_protocol import (
    COMM_CUSTOM_APP_DATA,
    COMM_FW_VERSION,
    COMM_GET_VALUES,
    COMM_GET_VALUES_SELECTIVE,
    COMM_SET_CURRENT,
    SELECTIVE_MASK,
    _SELECTIVE_FMT,
    _crc16,
    _extract_payload,
    _wrap_frame,
    _TELEMETRY_FMT,
    _TELEMETRY_FMT_EXT,
)
from machine import UART


def _build_telemetry_frame(
    temp_fet=250, temp_motor=300,
    motor_current=1500, input_current=800,
    avg_id=0, avg_iq=0,
    duty=500, rpm=4500, v_in=2500,
    ah=10000, ah_charged=5000, wh=20000, wh_charged=8000,
    tach=1234, tach_abs=5678,
    fault_code=0,
    # Extension fields (bits 16-21)
    pid_pos=0, controller_id=0,
    temp_mos1=0, temp_mos2=0, temp_mos3=0,
    avg_vd=0, avg_vq=0, status=0,
):
    body = struct.pack(
        _TELEMETRY_FMT,
        temp_fet, temp_motor,
        motor_current, input_current,
        avg_id, avg_iq,
        duty, rpm, v_in,
        ah, ah_charged, wh, wh_charged,
        tach, tach_abs,
        fault_code,
    )
    body += struct.pack(
        _TELEMETRY_FMT_EXT,
        pid_pos, controller_id,
        temp_mos1, temp_mos2, temp_mos3,
        avg_vd, avg_vq, status,
    )
    return _wrap_frame(bytes([COMM_GET_VALUES]) + body)


def _make():
    uart = UART()
    state = SharedState()
    vc = VESCComm(state, uart)
    return uart, state, vc


# ---- Telemetry field population ----

@pytest.mark.parametrize("kw,attr,expected", [
    pytest.param({"v_in": 250}, "cap_voltage_v", 25.0, id="voltage"),
    pytest.param({"rpm": 4500}, "vesc_mech_rpm", 4500.0 / 11.0, id="mech_rpm"),
    pytest.param({"fault_code": 3}, "vesc_fault_code", 3, id="fault_code"),
    pytest.param({"temp_fet": 450}, "vesc_temp_fet_c", 45.0, id="temp_fet"),
    pytest.param({"temp_motor": 620}, "vesc_temp_motor_c", 62.0, id="temp_motor"),
    pytest.param({"motor_current": 2500}, "vesc_motor_current_a", 25.0, id="motor_current"),
    pytest.param({"input_current": 1200}, "vesc_input_current_a", 12.0, id="input_current"),
    pytest.param({"avg_id": -150}, "vesc_id_current_a", -1.5, id="id_current"),
    pytest.param({"avg_iq": -330}, "vesc_iq_current_a", -3.3, id="iq_current"),
    pytest.param({"duty": 750}, "vesc_duty_cycle", 0.75, id="duty_cycle"),
    pytest.param({"tach": 999}, "vesc_tach", 999, id="tach"),
    pytest.param({"tach_abs": 4321}, "vesc_tach_abs", 4321, id="tach_abs"),
    pytest.param({"pid_pos": 2000000}, "vesc_pid_pos", 2.0, id="pid_pos"),
    pytest.param({"controller_id": 7}, "vesc_controller_id", 7, id="controller_id"),
    pytest.param({"temp_mos1": 400}, "vesc_temp_mos1_c", 40.0, id="temp_mos1"),
    pytest.param({"temp_mos2": 410}, "vesc_temp_mos2_c", 41.0, id="temp_mos2"),
    pytest.param({"temp_mos3": 420}, "vesc_temp_mos3_c", 42.0, id="temp_mos3"),
    pytest.param({"avg_vd": -800}, "vesc_vd", -0.8, id="vd"),
    pytest.param({"avg_vq": 5000}, "vesc_vq", 5.0, id="vq"),
    pytest.param({"status": 3}, "vesc_status", 3, id="status"),
])
def test_service_rx_populates_field(kw, attr, expected):
    uart, state, vc = _make()
    uart._rx_buf.extend(_build_telemetry_frame(**kw))
    vc.service_rx()
    assert abs(getattr(state, attr) - expected) < 0.1


def test_service_rx_updates_timestamp():
    uart, state, vc = _make()
    set_clock_ms(42000)
    uart._rx_buf.extend(_build_telemetry_frame())
    vc.service_rx()
    assert state.last_vesc_rx_ms == 42000


def test_two_frames_uses_latest():
    uart, state, vc = _make()
    uart._rx_buf.extend(_build_telemetry_frame(v_in=200))
    uart._rx_buf.extend(_build_telemetry_frame(v_in=300))
    vc.service_rx()
    assert abs(state.cap_voltage_v - 30.0) < 0.01


def test_partial_frame_waits():
    uart, state, vc = _make()
    frame = _build_telemetry_frame(v_in=250)
    uart._rx_buf.extend(frame[:len(frame) // 2])
    vc.service_rx()
    assert state.cap_voltage_v == 0.0
    uart._rx_buf.extend(frame[len(frame) // 2:])
    vc.service_rx()
    assert abs(state.cap_voltage_v - 25.0) < 0.01


def test_buffer_trimmed_on_overflow():
    uart, state, vc = _make()
    uart._rx_buf.extend(bytes(600))
    vc.service_rx()
    assert len(vc._rx_buf) <= 256


def test_valid_frame_after_junk():
    uart, state, vc = _make()
    uart._rx_buf.extend(bytes(100))
    uart._rx_buf.extend(_build_telemetry_frame(v_in=180))
    vc.service_rx()
    assert abs(state.cap_voltage_v - 18.0) < 0.01


# ---- Send commands ----

def test_send_current_writes_uart():
    uart, state, vc = _make()
    set_clock_ms(500)
    vc.send_current(15.0)
    assert len(uart._tx_buf) > 0


def test_request_telemetry_writes_uart():
    uart, state, vc = _make()
    vc.request_telemetry()
    assert len(uart._tx_buf) > 0


# ---- New Phase 3 commands ----

def _build_fw_version_frame(major=6, minor=6, hw_name="410"):
    """Build a FW_VERSION response frame as if the VESC sent it."""
    payload = bytes([COMM_FW_VERSION, major, minor]) + hw_name.encode("ascii") + b"\x00"
    return _wrap_frame(payload)


def test_fw_version_populates_state():
    uart, state, vc = _make()
    uart._rx_buf.extend(_build_fw_version_frame(6, 6, "410"))
    vc.service_rx()
    assert state.vesc_fw_major == 6
    assert state.vesc_fw_minor == 6
    assert state.vesc_hw_name == "410"


def test_fw_version_flipsky_name():
    uart, state, vc = _make()
    uart._rx_buf.extend(_build_fw_version_frame(6, 5, "Flipsky_412"))
    vc.service_rx()
    assert state.vesc_hw_name == "Flipsky_412"


def test_fw_version_then_telemetry():
    """FW version followed by telemetry — both parsed correctly."""
    uart, state, vc = _make()
    uart._rx_buf.extend(_build_fw_version_frame(6, 6, "410"))
    uart._rx_buf.extend(_build_telemetry_frame(v_in=300))
    vc.service_rx()
    assert state.vesc_hw_name == "410"
    assert abs(state.cap_voltage_v - 30.0) < 0.01


def test_send_alive_writes_uart():
    uart, state, vc = _make()
    vc.send_alive()
    assert len(uart._tx_buf) > 0


def test_send_estop_writes_uart():
    uart, state, vc = _make()
    vc.send_estop(1000)
    assert len(uart._tx_buf) > 0


def test_send_disable_output_writes_uart():
    uart, state, vc = _make()
    vc.send_disable_output(500)
    assert len(uart._tx_buf) > 0


def test_request_fw_version_writes_uart():
    uart, state, vc = _make()
    vc.request_fw_version()
    assert len(uart._tx_buf) > 0


def test_set_battery_cut_writes_uart():
    uart, state, vc = _make()
    vc.set_battery_cut(10.0, 8.0)
    assert len(uart._tx_buf) > 0


def test_send_alive_frame_valid():
    """The alive command produces a valid extractable frame."""
    uart, state, vc = _make()
    vc.send_alive()
    extracted, _ = _extract_payload(bytearray(uart._tx_buf))
    assert extracted is not None


def test_send_estop_frame_valid():
    """ESTOP produces a valid frame with the correct timeout."""
    uart, state, vc = _make()
    vc.send_estop(750)
    extracted, _ = _extract_payload(bytearray(uart._tx_buf))
    assert extracted is not None
    import struct as _struct
    timeout = _struct.unpack(">H", extracted[1:3])[0]
    assert timeout == 750


# ---- Selective telemetry ----

def _build_selective_frame(
    temp_fet=250, temp_motor=300,
    motor_current=1500, input_current=800,
    avg_iq=-330, duty=500, rpm=4500, v_in=250,
    fault_code=0,
):
    """Build a fake COMM_GET_VALUES_SELECTIVE response frame."""
    header = struct.pack(">BI", COMM_GET_VALUES_SELECTIVE, SELECTIVE_MASK)
    body = struct.pack(
        _SELECTIVE_FMT,
        temp_fet, temp_motor,
        motor_current, input_current,
        avg_iq, duty, rpm, v_in,
        fault_code,
    )
    return _wrap_frame(header + body)


@pytest.mark.parametrize("kw,attr,expected", [
    pytest.param({"v_in": 250}, "cap_voltage_v", 25.0, id="voltage"),
    pytest.param({"rpm": 4400}, "vesc_mech_rpm", 4400.0 / 11.0, id="mech_rpm"),
    pytest.param({"fault_code": 5}, "vesc_fault_code", 5, id="fault_code"),
    pytest.param({"temp_fet": 450}, "vesc_temp_fet_c", 45.0, id="temp_fet"),
    pytest.param({"temp_motor": 620}, "vesc_temp_motor_c", 62.0, id="temp_motor"),
    pytest.param({"motor_current": 2500}, "vesc_motor_current_a", 25.0, id="motor_current"),
    pytest.param({"input_current": 1200}, "vesc_input_current_a", 12.0, id="input_current"),
    pytest.param({"avg_iq": -330}, "vesc_iq_current_a", -3.3, id="iq_current"),
    pytest.param({"duty": 750}, "vesc_duty_cycle", 0.75, id="duty_cycle"),
])
def test_selective_populates_field(kw, attr, expected):
    uart, state, vc = _make()
    uart._rx_buf.extend(_build_selective_frame(**kw))
    vc.service_rx()
    assert abs(getattr(state, attr) - expected) < 0.1


def test_selective_updates_timestamp():
    uart, state, vc = _make()
    set_clock_ms(55000)
    uart._rx_buf.extend(_build_selective_frame())
    vc.service_rx()
    assert state.last_vesc_rx_ms == 55000


def test_selective_then_full_telemetry():
    """Selective followed by full telemetry — both parsed."""
    uart, state, vc = _make()
    uart._rx_buf.extend(_build_selective_frame(v_in=300))
    uart._rx_buf.extend(_build_telemetry_frame(v_in=400))
    vc.service_rx()
    # Last frame wins
    assert abs(state.cap_voltage_v - 40.0) < 0.01


def test_full_then_selective():
    """Full telemetry followed by selective — selective wins."""
    uart, state, vc = _make()
    uart._rx_buf.extend(_build_telemetry_frame(v_in=300))
    uart._rx_buf.extend(_build_selective_frame(v_in=200))
    vc.service_rx()
    assert abs(state.cap_voltage_v - 20.0) < 0.01


# ---- Opcode whitelist ----

def test_unknown_opcode_does_not_corrupt_state():
    """A valid-framing frame with an unknown opcode must be dropped, not
    silently mis-parsed as full telemetry.  This guards the C2 fix.
    """
    uart, state, vc = _make()
    # Pre-seed state so we can assert it's untouched
    state.cap_voltage_v = 12.34

    # Build a COMM_GET_VALUES-shaped body but label it opcode 0x99
    # (not in the firmware's whitelist).  If the whitelist were missing,
    # _parse_telemetry would overwrite cap_voltage_v with 25.0.
    body = struct.pack(
        _TELEMETRY_FMT,
        250, 300, 1500, 800, 0, 0,
        500, 4500, 2500,  # v_in=25.0 V if mis-parsed
        10000, 5000, 20000, 8000,
        1234, 5678, 0,
    )
    body += struct.pack(
        _TELEMETRY_FMT_EXT,
        0, 0, 0, 0, 0, 0, 0, 0,
    )
    frame = _wrap_frame(bytes([0x99]) + body)
    uart._rx_buf.extend(frame)
    vc.service_rx()

    assert state.cap_voltage_v == pytest.approx(12.34)


def test_request_telemetry_selective_writes_uart():
    uart, state, vc = _make()
    vc.request_telemetry_selective()
    assert len(uart._tx_buf) > 0
    extracted, _ = _extract_payload(bytearray(uart._tx_buf))
    assert extracted[0] == COMM_GET_VALUES_SELECTIVE


# ---- COMM_CUSTOM_APP_DATA (push-iq from LispBM) ----

VESC_ERPM_TO_MECH_RPM = 1.0 / 11.0   # mirror the constant from vesc_comm


def _build_push_iq_frame(iq=5.0, erpm=3300.0, drpm_mean=0.0, drpm_peak_neg=0.0):
    """Build a COMM_CUSTOM_APP_DATA frame with the 16-byte aggregate packet.

    Matches scripts/vesc_lisp_push_iq.lisp layout:
      [0..3]   rpm_now (erpm)
      [4..7]   drpm_mean (erpm/s)
      [8..11]  drpm_peak_neg (erpm/s)
      [12..15] iq_mean (A)
    """
    payload = bytes([COMM_CUSTOM_APP_DATA]) + struct.pack(
        ">ffff", erpm, drpm_mean, drpm_peak_neg, iq)
    return _wrap_frame(payload)


def test_push_iq_populates_instantaneous_fields():
    uart, state, vc = _make()
    uart._rx_buf.extend(_build_push_iq_frame(
        iq=-4.2, erpm=2200.0, drpm_mean=-500.0, drpm_peak_neg=-3000.0))
    vc.service_rx()
    assert abs(state.vesc_iq_mean_a - (-4.2)) < 1e-4
    assert abs(state.vesc_erpm_fast - 2200.0) < 1e-2
    assert abs(state.vesc_mech_rpm_fast - 2200.0 * VESC_ERPM_TO_MECH_RPM) < 0.1
    assert abs(state.vesc_drpm_mean_mech - (-500.0) * VESC_ERPM_TO_MECH_RPM) < 0.1
    assert abs(state.vesc_drpm_peak_neg_mech - (-3000.0) * VESC_ERPM_TO_MECH_RPM) < 0.1


def test_push_iq_updates_timestamp():
    uart, state, vc = _make()
    set_clock_ms(77000)
    uart._rx_buf.extend(_build_push_iq_frame())
    vc.service_rx()
    assert state.last_push_iq_rx_ms == 77000


def test_push_iq_after_selective():
    """Push-iq after selective — both parsed, independent fields."""
    uart, state, vc = _make()
    uart._rx_buf.extend(_build_selective_frame(v_in=300))
    uart._rx_buf.extend(_build_push_iq_frame(iq=-7.5, erpm=4400.0))
    vc.service_rx()
    # Selective field intact
    assert abs(state.cap_voltage_v - 30.0) < 0.1
    # Push-iq fields populated
    assert abs(state.vesc_iq_mean_a - (-7.5)) < 1e-4
    assert abs(state.vesc_erpm_fast - 4400.0) < 1e-2


def test_push_iq_does_not_clobber_averaged_iq():
    """Push-iq must NOT overwrite the averaged vesc_iq_current_a field."""
    uart, state, vc = _make()
    uart._rx_buf.extend(_build_selective_frame(avg_iq=-330))
    uart._rx_buf.extend(_build_push_iq_frame(iq=-7.5))
    vc.service_rx()
    # Averaged iq unchanged (from selective)
    assert abs(state.vesc_iq_current_a - (-3.3)) < 0.1
    # Mean iq from push
    assert abs(state.vesc_iq_mean_a - (-7.5)) < 1e-4

