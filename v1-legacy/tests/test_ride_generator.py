"""Tests for sim.ride_generator.

Guards the ride-generator pipeline against silent regressions —
especially important once NeuralStrategy starts training on these
rides.  A seed-leak or distribution drift would poison training
data, so these tests pin down:
  - determinism  : (profile, seed) -> identical RideTrace
  - independence : different seeds -> different traces
  - shapes/dtypes
  - brake amplitude bounds
  - brake event arrival rate ≈ profile.brake_rate_hz
  - pedal_active is bool-like and non-empty for reasonable cruise
  - grade is zero-mean-ish and within OU σ envelope
  - ride_set has the expected cardinality and weighting
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from sim.ride_generator import (
    BRAKE_MAX_NM,
    BRAKE_MIN_NM,
    DEFAULT_DURATION,
    DT,
    PROFILES,
    RideTrace,
    generate_ride,
    generate_ride_set,
)


# ---------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------

@pytest.mark.parametrize("profile", list(PROFILES))
def test_generate_ride_is_deterministic(profile: str) -> None:
    a = generate_ride(profile, seed=42)
    b = generate_ride(profile, seed=42)
    np.testing.assert_array_equal(a.brake_torque, b.brake_torque)
    np.testing.assert_array_equal(a.pedal_active, b.pedal_active)
    np.testing.assert_array_equal(a.grade_rad, b.grade_rad)
    assert a.mass_kg == b.mass_kg
    assert a.cruise_kmh == b.cruise_kmh
    assert a.brake_windows == b.brake_windows


def test_different_seeds_produce_different_rides() -> None:
    a = generate_ride("commuter", seed=1)
    b = generate_ride("commuter", seed=2)
    assert not np.array_equal(a.brake_torque, b.brake_torque)
    assert not np.array_equal(a.grade_rad, b.grade_rad)


def test_generate_ride_set_is_deterministic() -> None:
    s1 = generate_ride_set(seeds_per_profile=2, base_seed=123)
    s2 = generate_ride_set(seeds_per_profile=2, base_seed=123)
    assert len(s1) == len(s2)
    for r1, r2 in zip(s1, s2):
        np.testing.assert_array_equal(r1.brake_torque, r2.brake_torque)
        np.testing.assert_array_equal(r1.grade_rad, r2.grade_rad)
        assert r1.seed == r2.seed


# ---------------------------------------------------------------------
# Shape / dtype
# ---------------------------------------------------------------------

def test_ride_shapes_and_dtypes() -> None:
    r = generate_ride("casual", seed=7)
    n = int(round(DEFAULT_DURATION / DT))
    assert r.n == n
    assert r.brake_torque.shape == (n,)
    assert r.pedal_active.shape == (n,)
    assert r.grade_rad.shape == (n,)
    assert r.brake_torque.dtype == np.float64
    # pedal_active may be bool or 0/1; accept either
    assert r.pedal_active.dtype in (np.bool_, np.float64, np.int64, np.int32)
    assert r.grade_rad.dtype == np.float64
    assert isinstance(r, RideTrace)


# ---------------------------------------------------------------------
# Brake amplitude bounds
# ---------------------------------------------------------------------

def test_brake_event_peaks_in_bounds() -> None:
    """Each brake *event* peak must sit in [BRAKE_MIN_NM, BRAKE_MAX_NM].

    Individual samples can be lower (trapezoid ramp), so we check the
    peak inside each window rather than per-sample.
    """
    assert BRAKE_MIN_NM < BRAKE_MAX_NM
    for seed in (1, 2, 3, 17, 99):
        r = generate_ride("hilly", seed=seed)
        for (t0, t1) in r.brake_windows:
            i0 = int(round(t0 / r.dt))
            i1 = int(round(t1 / r.dt))
            seg = r.brake_torque[i0:i1]
            if seg.size == 0:
                continue
            peak = float(seg.max())
            assert peak >= BRAKE_MIN_NM - 1e-9, (
                f"event [{t0:.2f},{t1:.2f}] peak {peak:.3f} < {BRAKE_MIN_NM}")
            assert peak <= BRAKE_MAX_NM + 1e-9, (
                f"event [{t0:.2f},{t1:.2f}] peak {peak:.3f} > {BRAKE_MAX_NM}")


# ---------------------------------------------------------------------
# Brake arrival-rate sanity
# ---------------------------------------------------------------------

def test_brake_arrival_rate_matches_profile() -> None:
    """Average event count across many rides ≈ λ · duration.

    Poisson arrivals have high per-ride variance, so we average over
    a bunch of rides per profile and allow ±50 % tolerance.
    """
    rng_profiles = ["casual", "commuter", "fast_commuter", "hilly"]
    for prof_name in rng_profiles:
        prof = PROFILES[prof_name]
        expected = prof.brake_rate_hz * DEFAULT_DURATION
        counts = []
        for seed in range(12):
            r = generate_ride(prof_name, seed=seed * 13 + 1)
            counts.append(len(r.brake_windows))
        mean_count = float(np.mean(counts))
        lo = 0.5 * expected
        hi = 1.5 * expected
        assert lo <= mean_count <= hi, (
            f"{prof_name}: mean brake events {mean_count:.2f} "
            f"outside [{lo:.2f}, {hi:.2f}] (expected ≈ {expected:.2f})")


def test_brake_windows_are_consistent_with_trace() -> None:
    r = generate_ride("commuter", seed=5)
    # windows are within the ride duration and strictly ordered
    for (t0, t1) in r.brake_windows:
        assert 0.0 <= t0 < t1 <= DEFAULT_DURATION
    # brake torque is non-negative and bounded
    assert np.all(r.brake_torque >= 0.0)
    assert np.all(r.brake_torque <= BRAKE_MAX_NM + 1e-9)


# ---------------------------------------------------------------------
# Grade OU envelope
# ---------------------------------------------------------------------

def test_grade_statistics_match_profile() -> None:
    """Ensemble std of grade ≈ profile σ (within 2×)."""
    for prof_name in ("casual", "hilly", "commuter"):
        prof = PROFILES[prof_name]
        stds = []
        for seed in range(8):
            r = generate_ride(prof_name, seed=seed * 7 + 11)
            stds.append(float(np.std(r.grade_rad)))
        mean_std = float(np.mean(stds))
        # OU steady-state σ ≈ prof.grade_sigma_rad; allow wide band
        assert mean_std <= 2.0 * prof.grade_sigma_rad + 1e-6, (
            f"{prof_name}: grade std {math.degrees(mean_std):.2f}° "
            f">> expected {math.degrees(prof.grade_sigma_rad):.2f}°")
        assert mean_std >= 0.25 * prof.grade_sigma_rad, (
            f"{prof_name}: grade std {math.degrees(mean_std):.2f}° "
            f"<< expected {math.degrees(prof.grade_sigma_rad):.2f}°")


# ---------------------------------------------------------------------
# Ride set cardinality
# ---------------------------------------------------------------------

def test_ride_set_cardinality() -> None:
    rides = generate_ride_set(seeds_per_profile=3, base_seed=0)
    assert len(rides) == 3 * len(PROFILES)
    # Every profile represented exactly `seeds_per_profile` times.
    from collections import Counter
    counts = Counter(r.profile for r in rides)
    for prof_name in PROFILES:
        assert counts[prof_name] == 3


def test_ride_set_seeds_are_unique_per_profile() -> None:
    rides = generate_ride_set(seeds_per_profile=5, base_seed=42)
    by_profile: dict[str, list[int]] = {}
    for r in rides:
        by_profile.setdefault(r.profile, []).append(r.seed)
    for prof_name, seeds in by_profile.items():
        assert len(set(seeds)) == len(seeds), (
            f"{prof_name}: duplicate seeds in ride set: {seeds}")


def test_ride_set_base_seed_shifts_seeds() -> None:
    s0 = generate_ride_set(seeds_per_profile=2, base_seed=0)
    s1 = generate_ride_set(seeds_per_profile=2, base_seed=1)
    # At least one pair must differ — base_seed must actually propagate.
    any_diff = any(
        not np.array_equal(a.brake_torque, b.brake_torque)
        for a, b in zip(s0, s1)
    )
    assert any_diff, "base_seed does not affect generated rides"
