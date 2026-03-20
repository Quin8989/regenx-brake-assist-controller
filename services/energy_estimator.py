# services/energy_estimator.py — Compute rider-facing stored-energy values
#
# Uses measured capacitor voltage and known capacitance to estimate
# stored energy in joules and usable percentage.

from config.settings import (
    CAPACITANCE_F,
    VCAP_MIN_OPERATING,
    VCAP_SOFT_REGEN_CUTOFF,
)
from utils import clamp


# Precomputed energy bounds (constant for given capacitance and voltage window)
_HALF_C = 0.5 * CAPACITANCE_F
_E_MIN = _HALF_C * VCAP_MIN_OPERATING * VCAP_MIN_OPERATING
_E_MAX = _HALF_C * VCAP_SOFT_REGEN_CUTOFF * VCAP_SOFT_REGEN_CUTOFF
_E_RANGE = _E_MAX - _E_MIN if _E_MAX > _E_MIN else 0.0


class EnergyEstimator:
    def __init__(self, shared_state):
        self._state = shared_state

    def update(self):
        """Recompute energy estimates from current cap voltage."""
        v = self._state.cap_voltage_v

        energy_j = _HALF_C * v * v
        self._state.cap_energy_j = energy_j

        if _E_RANGE > 0.0:
            pct = (energy_j - _E_MIN) / _E_RANGE * 100.0
            self._state.cap_energy_percent = clamp(pct, 0.0, 100.0)
        else:
            self._state.cap_energy_percent = 0.0
