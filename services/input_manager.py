# services/input_manager.py — Gather and interpret rider inputs
#
# Reads throttle, normalizes and filters rider assist request,
# and exposes clean rider-intent to the control loop and state machine.


class InputManager:
    def __init__(self, throttle_driver, shared_state):
        self._throttle = throttle_driver
        self._state = shared_state

    def update(self):
        """Sample rider inputs and update shared_state."""
        self._throttle.update()

        self._state.throttle_raw = self._throttle.raw
        self._state.throttle_percent = self._throttle.fraction

        # TODO: Incorporate other inputs if added later:
        #   - regen lever switch or analog level
        #   - mode switch
        #   - arming switch
        #   - fault reset button

    def throttle_valid(self):
        return self._throttle.is_valid

    def rider_requesting_assist(self):
        return self._throttle.is_valid and self._throttle.fraction > 0.0

    # TODO: Decide whether regen is commanded electronically by a separate rider input,
    #       mechanically by brake band only, or by both
    # TODO: Decide whether assist requires an arming condition in addition to throttle
    # TODO: Decide how zero-throttle is distinguished from invalid throttle
