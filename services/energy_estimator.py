# services/energy_estimator.py — Compute rider-facing stored-energy values
#
# Uses measured capacitor voltage and known capacitance to estimate
# stored energy in joules and usable percentage.

from config.thresholds import (
    CAPACITANCE_F,
    VCAP_MIN_OPERATING,
    VCAP_SOFT_REGEN_CUTOFF,
)


class EnergyEstimator:
    def __init__(self, shared_state):
        self._state = shared_state

    def update(self):
        """Recompute energy estimates from current cap voltage."""
        v = self._state.cap_voltage_v

        # E = 0.5 * C * V^2
        self._state.cap_energy_j = 0.5 * CAPACITANCE_F * v * v

        # Usable percentage relative to operating window
        e_min = 0.5 * CAPACITANCE_F * VCAP_MIN_OPERATING * VCAP_MIN_OPERATING
        e_max = 0.5 * CAPACITANCE_F * VCAP_SOFT_REGEN_CUTOFF * VCAP_SOFT_REGEN_CUTOFF
        if e_max > e_min:
            pct = (self._state.cap_energy_j - e_min) / (e_max - e_min) * 100.0
            self._state.cap_energy_percent = max(0.0, min(100.0, pct))
        else:
            self._state.cap_energy_percent = 0.0

    # TODO: Confirm whether total effective capacitance is exactly 20 F
    # TODO: Define energy percentage scaling range
    # TODO: Decide whether to use local cap voltage or VESC bus voltage as energy source
    # TODO: Decide display rounding
