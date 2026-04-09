# tests/test_vesc_comm.py — VESCComm integration: UART bytes → SharedState

import struct
from tests.conftest import advance_ms, set_clock_ms
from core import SharedState
from services.vesc_comm import VESCComm
from services.vesc_protocol import (
    COMM_GET_VALUES,
    _crc16,
    _wrap_frame,
    _TELEMETRY_FMT,
    _TELEMETRY_SIZE,
)
from machine import UART


def _build_telemetry_payload(
    temp_fet=250, temp_motor=300,
    motor_current=1500, input_current=800,
    avg_id=0, avg_iq=0,
    duty=500, rpm=4500, v_in=2500,
    ah=10000, ah_charged=5000, wh=20000, wh_charged=8000,
    tach=1234, tach_abs=5678,
    fault_code=0,
):
    """Build a raw COMM_GET_VALUES response payload with controllable fields."""
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
    return bytes([COMM_GET_VALUES]) + body


def _build_telemetry_frame(**kw):
    """Build a fully framed VESC UART telemetry response."""
    return _wrap_frame(_build_telemetry_payload(**kw))


def _make():
    uart = UART()
    state = SharedState()
    vc = VESCComm(uart, state)
    return uart, state, vc


class TestServiceRxSingleFrame:
    def test_populates_cap_voltage(self):
        uart, state, vc = _make()
        uart._rx_buf.extend(_build_telemetry_frame(v_in=250))
        set_clock_ms(100)
        vc.service_rx()
        assert abs(state.cap_voltage_v - 25.0) < 0.01

    def test_populates_mech_rpm(self):
        uart, state, vc = _make()
        # rpm field is in ERPM; with 11 pole pairs → mech = erpm / 11
        uart._rx_buf.extend(_build_telemetry_frame(rpm=4500))
        vc.service_rx()
        assert abs(state.vesc_mech_rpm - 4500.0 / 11.0) < 0.1

    def test_populates_fault_code(self):
        uart, state, vc = _make()
        uart._rx_buf.extend(_build_telemetry_frame(fault_code=3))
        vc.service_rx()
        assert state.vesc_fault_code == 3

    def test_populates_temperatures(self):
        uart, state, vc = _make()
        uart._rx_buf.extend(_build_telemetry_frame(temp_fet=450, temp_motor=620))
        vc.service_rx()
        assert abs(state.vesc_temp_fet_c - 45.0) < 0.01
        assert abs(state.vesc_temp_motor_c - 62.0) < 0.01

    def test_populates_currents(self):
        uart, state, vc = _make()
        uart._rx_buf.extend(_build_telemetry_frame(motor_current=2500, input_current=1200))
        vc.service_rx()
        assert abs(state.vesc_motor_current_a - 25.0) < 0.01
        assert abs(state.vesc_input_current_a - 12.0) < 0.01

    def test_populates_iq_current(self):
        uart, state, vc = _make()
        uart._rx_buf.extend(_build_telemetry_frame(avg_iq=-330))
        vc.service_rx()
        assert abs(state.vesc_iq_current_a + 3.3) < 0.01

    def test_populates_duty_cycle(self):
        uart, state, vc = _make()
        uart._rx_buf.extend(_build_telemetry_frame(duty=750))
        vc.service_rx()
        assert abs(state.vesc_duty_cycle - 0.75) < 0.001

    def test_updates_last_rx_timestamp(self):
        uart, state, vc = _make()
        set_clock_ms(42000)
        uart._rx_buf.extend(_build_telemetry_frame())
        vc.service_rx()
        assert state.last_vesc_rx_ms == 42000


class TestServiceRxMultiFrame:
    def test_two_frames_back_to_back(self):
        uart, state, vc = _make()
        uart._rx_buf.extend(_build_telemetry_frame(v_in=200))
        uart._rx_buf.extend(_build_telemetry_frame(v_in=300))
        vc.service_rx()
        # Second frame should overwrite first
        assert abs(state.cap_voltage_v - 30.0) < 0.01

    def test_partial_frame_waits(self):
        uart, state, vc = _make()
        frame = _build_telemetry_frame(v_in=250)
        # Feed only half the frame
        uart._rx_buf.extend(frame[:len(frame) // 2])
        vc.service_rx()
        assert state.cap_voltage_v == 0.0  # Not yet parsed

        # Feed the rest
        uart._rx_buf.extend(frame[len(frame) // 2:])
        vc.service_rx()
        assert abs(state.cap_voltage_v - 25.0) < 0.01


class TestServiceRxBufferOverflow:
    def test_buffer_trimmed_at_512(self):
        uart, state, vc = _make()
        # Fill buffer with junk > 512 bytes
        uart._rx_buf.extend(bytes(600))
        vc.service_rx()
        assert len(vc._rx_buf) <= 256

    def test_valid_frame_after_junk(self):
        uart, state, vc = _make()
        # 100 bytes of junk followed by a valid frame
        uart._rx_buf.extend(bytes(100))
        uart._rx_buf.extend(_build_telemetry_frame(v_in=180))
        vc.service_rx()
        assert abs(state.cap_voltage_v - 18.0) < 0.01


class TestSendCommands:
    def test_send_assist_writes_uart(self):
        uart, state, vc = _make()
        set_clock_ms(500)
        vc.send_assist(15.0)
        assert len(uart._tx_buf) > 0

    def test_send_regen_writes_uart(self):
        uart, state, vc = _make()
        set_clock_ms(600)
        vc.send_regen(10.0)
        assert len(uart._tx_buf) > 0

    def test_send_neutral_writes_uart(self):
        uart, state, vc = _make()
        set_clock_ms(700)
        vc.send_neutral()
        assert len(uart._tx_buf) > 0

    def test_request_telemetry_writes_uart(self):
        uart, state, vc = _make()
        vc.request_telemetry()
        assert len(uart._tx_buf) > 0
