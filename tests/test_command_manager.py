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


class TestCommandManager:
    def test_inhibit_sends_neutral(self):
        s, uart, cm = _make()
        s.inhibit_motor_commands = True
        s.assist_command_request = 30.0
        s.regen_command_request = 20.0
        cm.update()
        assert len(uart.sent) == 1
        import struct
        from services.vesc_protocol import COMM_SET_CURRENT, _extract_payload
        payload, _ = _extract_payload(bytearray(uart.sent[0]))
        assert payload[0] == COMM_SET_CURRENT
        value = struct.unpack(">i", payload[1:5])[0]
        assert value == 0

    def test_sends_assist_current(self):
        s, uart, cm = _make()
        s.inhibit_motor_commands = False
        s.assist_command_request = 15.5
        s.regen_command_request = 0.0
        cm.update()
        import struct
        from services.vesc_protocol import COMM_SET_CURRENT, _extract_payload
        payload, _ = _extract_payload(bytearray(uart.sent[0]))
        assert payload[0] == COMM_SET_CURRENT
        value = struct.unpack(">i", payload[1:5])[0]
        assert value == 15500

    def test_sends_regen_current(self):
        s, uart, cm = _make()
        s.inhibit_motor_commands = False
        s.assist_command_request = 0.0
        s.regen_command_request = 20.0
        cm.update()
        import struct
        from services.vesc_protocol import COMM_SET_BRAKE_CURRENT, _extract_payload
        payload, _ = _extract_payload(bytearray(uart.sent[0]))
        assert payload[0] == COMM_SET_BRAKE_CURRENT
        value = struct.unpack(">i", payload[1:5])[0]
        assert value == 20000

    def test_neutral_when_no_requests(self):
        s, uart, cm = _make()
        s.inhibit_motor_commands = False
        s.assist_command_request = 0.0
        s.regen_command_request = 0.0
        cm.update()
        import struct
        from services.vesc_protocol import COMM_SET_CURRENT, _extract_payload
        payload, _ = _extract_payload(bytearray(uart.sent[0]))
        assert payload[0] == COMM_SET_CURRENT
        value = struct.unpack(">i", payload[1:5])[0]
        assert value == 0

    def test_assist_wins_if_both_nonzero(self):
        """If control loop accidentally sets both, assist takes priority."""
        s, uart, cm = _make()
        s.inhibit_motor_commands = False
        s.assist_command_request = 10.0
        s.regen_command_request = 5.0
        cm.update()
        from services.vesc_protocol import COMM_SET_CURRENT, _extract_payload
        payload, _ = _extract_payload(bytearray(uart.sent[0]))
        assert payload[0] == COMM_SET_CURRENT  # assist, not brake
