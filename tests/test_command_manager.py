# tests/test_command_manager.py — CommandManager final gate logic

from core import SharedState
from services.vesc_comm import CommandManager, VESCComm


class _FakeUART:
    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(bytes(data))

    def read(self, n=-1):
        return None


def _make():
    state = SharedState()
    uart = _FakeUART()
    vesc_comm = VESCComm(uart, state)
    cm = CommandManager(vesc_comm, state)
    return state, uart, cm


def _sent_current(uart, index):
    import struct
    from services.vesc_protocol import _extract_payload
    payload, _ = _extract_payload(bytearray(uart.sent[index]))
    return struct.unpack(">i", payload[1:5])[0] / 1000.0


def _sent_command_id(uart, index):
    from services.vesc_protocol import _extract_payload
    payload, _ = _extract_payload(bytearray(uart.sent[index]))
    return payload[0]


class TestCommandManager:
    def test_sends_positive_command(self):
        s, uart, cm = _make()
        s.inhibit_motor_commands = False
        s.motor_command_a = 15.5
        cm.update()
        from services.vesc_protocol import COMM_SET_CURRENT
        assert _sent_command_id(uart, 0) == COMM_SET_CURRENT
        assert _sent_current(uart, 0) == 15.5

    def test_sends_negative_command_as_regen(self):
        """Negative motor_command_a routes to COMM_SET_CURRENT (regen)."""
        s, uart, cm = _make()
        s.inhibit_motor_commands = False
        s.motor_command_a = -20.0
        cm.update()
        from services.vesc_protocol import COMM_SET_CURRENT
        assert _sent_command_id(uart, 0) == COMM_SET_CURRENT
        assert _sent_current(uart, 0) == -20.0  # negative = regen

    def test_sends_zero_when_no_command(self):
        s, uart, cm = _make()
        s.inhibit_motor_commands = False
        s.motor_command_a = 0.0
        cm.update()
        assert _sent_current(uart, 0) == 0.0
