# services/command_manager.py — Final gate between control requests and VESC transmissions
#
# Ensures only one motor-control mode is active at a time.
# Sends zero / neutral command whenever motion is not allowed.

from core.enums import SystemState


class CommandManager:
    def __init__(self, vesc_comm, shared_state):
        self._vesc = vesc_comm
        self._state = shared_state

    def update(self):
        """Evaluate current state and transmit the appropriate VESC command."""
        s = self._state

        # If any critical fault or inhibit is active → zero command
        if s.inhibit_motor_commands or s.system_state == SystemState.FAULT:
            self.send_neutral()
            return

        # Precharge active or incomplete → zero command
        if s.system_state == SystemState.PRECHARGE:
            self.send_neutral()
            return

        # ASSIST
        if s.system_state == SystemState.ASSIST and s.assist_command_request > 0.0:
            self._vesc.send_assist(s.assist_command_request)
            return

        # REGEN
        if s.system_state == SystemState.REGEN and s.regen_command_request > 0.0:
            self._vesc.send_regen(s.regen_command_request)
            return

        # Default (READY, OFF, or no active request) → neutral
        self.send_neutral()

    def send_neutral(self):
        """Send a zero / neutral command to the VESC."""
        self._vesc.send_neutral()

    # TODO: Decide neutral command behavior for the target VESC mode
    # TODO: Decide heartbeat rate
    # TODO: Decide whether repeated zero commands are needed in FAULT
    # TODO: Decide how to arbitrate if both assist and regen are somehow requested at once
