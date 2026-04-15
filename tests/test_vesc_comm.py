# tests/test_vesc_comm.py — VESCComm UART rx/tx

import struct
import pytest
from tests.conftest import set_clock_ms
from core import SharedState
from services.vesc_comm import VESCComm
from services.vesc_protocol import (
    COMM_GET_VALUES,
    COMM_SET_CURRENT,
    _crc16,
    _extract_payload,
    _wrap_frame,
    _TELEMETRY_FMT,
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
    pytest.param({"avg_iq": -330}, "vesc_iq_current_a", -3.3, id="iq_current"),
    pytest.param({"duty": 750}, "vesc_duty_cycle", 0.75, id="duty_cycle"),
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
