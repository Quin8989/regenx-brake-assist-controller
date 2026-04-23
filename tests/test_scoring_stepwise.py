"""Regression tests for the step-level scoring decomposition.

:func:`score_ride_stepwise` returns per-tick ingredient arrays that
must aggregate to the same scalar ``RideScore`` that :func:`score_ride`
produces.  These tests pin that identity down so downstream consumers
(RL reward shaping, PySR fitness, debug plots) can trust the arrays.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from sim.ride_generator import generate_ride
from sim.scoring import (
    W_CAPTURE,
    W_FIDELITY,
    RideStepwise,
    score_ride,
    score_ride_stepwise,
)
from sim.strategies import STRATEGY_BY_NAME


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _factory(name: str):
    cls = STRATEGY_BY_NAME[name]
    return lambda: cls()


# ---------------------------------------------------------------------
# Scalar reconstruction identity
# ---------------------------------------------------------------------

@pytest.mark.parametrize("strategy", ["aimd_ff", "pi_controller", "fixed_ff"])
@pytest.mark.parametrize("profile,seed", [("casual", 7), ("commuter", 42)])
def test_stepwise_scalars_match_score_ride(strategy: str,
                                           profile: str,
                                           seed: int) -> None:
    ride = generate_ride(profile, seed=seed, duration=20.0)
    sw = score_ride_stepwise(_factory(strategy), ride)
    rs = score_ride(_factory(strategy), ride)

    # Scalars must match bit-for-bit (same sim, same reduction).
    assert sw.capture == pytest.approx(rs.capture, abs=1e-9)
    assert sw.fidelity == pytest.approx(rs.fidelity, abs=1e-9)
    assert sw.composite == pytest.approx(rs.composite, abs=1e-9)


# ---------------------------------------------------------------------
# Array aggregation identities
# ---------------------------------------------------------------------

def test_capture_aggregation_identity() -> None:
    """capture scalar == clamp01(sum(num)/sum(den)) * 100."""
    ride = generate_ride("commuter", seed=3, duration=20.0)
    sw = score_ride_stepwise(_factory("aimd_ff"), ride)

    num = float(sw.capture_num_per_tick.sum())
    den = float(sw.capture_den_per_tick.sum())
    if den <= 1e-6:
        expected = 0.0
    else:
        ratio = max(0.0, min(1.0, num / den))
        expected = ratio * 100.0
    assert sw.capture == pytest.approx(expected, abs=1e-9)


def test_fidelity_aggregation_identity() -> None:
    """fidelity scalar == clamp01(1 - sum(err_abs_J[counted])/sum(base_J[counted])) * 100."""
    ride = generate_ride("hilly", seed=11, duration=20.0)
    sw = score_ride_stepwise(_factory("aimd_ff"), ride)

    mask = sw.fidelity_counted_per_tick
    base = float(sw.fidelity_base_J_per_tick[mask].sum())
    if not np.any(mask) or base <= 1e-6:
        expected = 100.0
    else:
        err = float(sw.fidelity_err_abs_J_per_tick[mask].sum())
        expected = max(0.0, min(1.0, 1.0 - err / base)) * 100.0
    assert sw.fidelity == pytest.approx(expected, abs=1e-9)


def test_composite_is_weighted_sum() -> None:
    ride = generate_ride("casual", seed=5, duration=20.0)
    sw = score_ride_stepwise(_factory("aimd_ff"), ride)
    expected = W_CAPTURE * sw.capture + W_FIDELITY * sw.fidelity
    assert sw.composite == pytest.approx(expected, abs=1e-12)


# ---------------------------------------------------------------------
# Mask / shape invariants
# ---------------------------------------------------------------------

def test_stepwise_array_shapes() -> None:
    ride = generate_ride("commuter", seed=1, duration=15.0)
    sw = score_ride_stepwise(_factory("fixed_ff"), ride)
    assert isinstance(sw, RideStepwise)

    n = sw.t.size
    assert sw.brake_mask.shape == (n,)
    assert sw.fidelity_speed_mask.shape == (n,)
    assert sw.capture_num_per_tick.shape == (n,)
    assert sw.capture_den_per_tick.shape == (n,)
    # fidelity is now per-sample (power) not per-diff, so n
    assert sw.fidelity_err_abs_J_per_tick.shape == (n,)
    assert sw.fidelity_base_J_per_tick.shape == (n,)
    assert sw.fidelity_counted_per_tick.shape == (n,)


def test_capture_increments_zero_outside_brake_mask() -> None:
    ride = generate_ride("casual", seed=2, duration=15.0)
    sw = score_ride_stepwise(_factory("aimd_ff"), ride)
    outside = ~sw.brake_mask
    assert np.all(sw.capture_num_per_tick[outside] == 0.0)
    assert np.all(sw.capture_den_per_tick[outside] == 0.0)


def test_fidelity_increments_zero_outside_counted_mask() -> None:
    ride = generate_ride("hilly", seed=4, duration=15.0)
    sw = score_ride_stepwise(_factory("aimd_ff"), ride)
    outside = ~sw.fidelity_counted_per_tick
    assert np.all(sw.fidelity_err_abs_J_per_tick[outside] == 0.0)
    assert np.all(sw.fidelity_base_J_per_tick[outside] == 0.0)


def test_fidelity_counted_equals_brake_mask() -> None:
    """New fidelity metric gates on brake_mask only (no speed cutoff)."""
    ride = generate_ride("commuter", seed=9, duration=15.0)
    sw = score_ride_stepwise(_factory("aimd_ff"), ride)
    assert np.array_equal(sw.fidelity_counted_per_tick, sw.brake_mask)
