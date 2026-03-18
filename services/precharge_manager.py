# services/precharge_manager.py — Precharge sequence and motor enable interlocks
#
# Controls precharge path activation, monitors capacitor voltage,
# and inhibits motor activity until the system is electrically ready.

from time import ticks_ms, ticks_diff
from core.enums import PrechargeState, FaultCode
from config.thresholds import (
    VCAP_MIN_OPERATING,
    PRECHARGE_TIMEOUT_MS,
)


class PrechargeManager:
    def __init__(self, precharge_io, adc_inputs, shared_state, fault_manager):
        self._io = precharge_io
        self._adc = adc_inputs
        self._state = shared_state
        self._faults = fault_manager
        self._start_ms = 0

    def update(self):
        """Run precharge state machine each cycle."""
        ps = self._state.precharge_state

        if ps == PrechargeState.IDLE:
            pass

        elif ps == PrechargeState.START:
            self._io.enable_precharge()
            self._start_ms = ticks_ms()
            self._state.precharge_state = PrechargeState.WAIT_FOR_VOLTAGE

        elif ps == PrechargeState.WAIT_FOR_VOLTAGE:
            if self._state.cap_voltage_v >= VCAP_MIN_OPERATING:
                self._io.disable_precharge()
                self._io.enable_main()
                self._state.precharge_state = PrechargeState.COMPLETE
            elif ticks_diff(ticks_ms(), self._start_ms) > PRECHARGE_TIMEOUT_MS:
                self._io.disable_all()
                self._faults.set_fault(FaultCode.PRECHARGE_TIMEOUT)
                self._state.precharge_state = PrechargeState.FAILED

        elif ps == PrechargeState.COMPLETE:
            pass  # Steady state — main path enabled

        elif ps == PrechargeState.FAILED:
            self._io.disable_all()

    def begin_precharge(self):
        """Initiate the precharge sequence."""
        self._state.precharge_state = PrechargeState.START

    def is_complete(self):
        return self._state.precharge_state == PrechargeState.COMPLETE

    def is_failed(self):
        return self._state.precharge_state == PrechargeState.FAILED

    def reset(self):
        """Return to idle — for retry after failure or manual reset."""
        self._io.disable_all()
        self._state.precharge_state = PrechargeState.IDLE

    # TODO: Confirm the actual switched hardware
    # TODO: Decide what "precharge complete" means numerically
    # TODO: Decide how long the firmware waits before timing out
    # TODO: Decide whether precharge can be manually forced for bench testing
    # TODO: Decide how restart / retry behaves after a failed precharge
