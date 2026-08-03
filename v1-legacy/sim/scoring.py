"""sim.scoring - RideTrace-based strategy scoring.

Two dimensions (0-100 each), one composite.

  capture  Capture ratio: harvested electrical energy divided by
              the mechanical brake work an idealized band brake
              would dissipate RIGHT NOW at the strategy's own wheel
              speed, over brake windows:
                  int P_elec dt / int (brake_demand * w_ours) dt
              Numerator is net-into-cap (post-copper, post-ESR), so
              copper / ESR losses reduce capture.  Denominator is
              the "instantaneous budget" the rider's brake demand
              sets at the current wheel speed -- the mechanical
              power the ideal friction brake would remove from the
              wheel if applied to this wheel right now.  Doing
              nothing -> capture = 0.  Perfect capture (motor
              soaks up exactly what the ideal brake would have
              dissipated at ω_ours) -> capture = 100.  Clamped
              at 100 if the strategy over-engages (excess KE removal
              is caught by fidelity).

  fidelity        Power-tracking error vs the idealized band brake
              evaluated at the strategy's own wheel speed.  The
              total mechanical power the regen system pulls out of
              the wheel is ``P_regen = p_elec + p_copper + p_brake``
              (electrical harvest + copper loss + band slip heat --
              every path by which the regen system decelerates the
              wheel).  The target is
              ``P_base = brake_demand * w_ours`` -- the mechanical
              power the ideal friction brake would remove at the
              current wheel speed.  fidelity is the relative L1 tracking
              error:
                  100 * max(0, 1 - int|P_regen - P_base| dt
                                     / int P_base dt)
              inside ride.brake_windows.  Both over- and under-
              engagement are penalised; doing nothing gives
              int|0 - P_base| / int P_base = 1 -> fidelity = 0.

Composite per ride:
    0.40 * capture + 0.60 * fidelity   (fidelity-first)

Aggregation across the 20-ride set (5 seeds x 4 profiles):
  1. Average dimensions across seeds within each profile.
  2. Weight profiles by PROFILES[...].weight:
        casual 0.35, hilly 0.25, commuter 0.30, fast_commuter 0.10.
  3. Composite recomputed from the profile-weighted dimensions.

Jerk / command smoothness is not scored; hardware-level slew-rate
limiting is enforced at the strategy output instead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from .physics import (
    CAP_ESR,
    ETA_GEAR,
    FLUX_LINKAGE,
    FOC_TAU,
    IQ_BIAS_DEFAULT,
    IQ_NOISE_SIGMA_DEFAULT,
    J_CARRIER,
    MU_K,
    MU_S,
    R_PHASE,
    R_WHEEL,
    RPM_NOISE_SIGMA_DEFAULT,
    T_DRAG_COEFF,
    TELEM_DELAY,
    VCAP_NOISE_SIGMA_DEFAULT,
    simulate_ride,
)
from .ride_generator import PROFILES, RideTrace, generate_ride_set

SPEED_CUTOFF_KMH = 2.0   # decel deviation only counted above this speed

# ---------------------------------------------------------------------
#  Composite score -- the single rider-facing fitness function
# ---------------------------------------------------------------------
#
# Two dimensions, both first-class.  Composite is the only number any
# tuner ever sees.  Doing nothing aces fidelity but flunks capture, so
# the composite can't be gamed.
#
#   capture S_E : 100 * clamp01(int P_elec dt / int (brake_demand *
#                    w_ours) dt) over brake windows.  Numerator is
#                    net-into-cap (post-copper, post-ESR).  Denominator
#                    is the instantaneous band-brake budget at the
#                    strategy's own wheel speed.  Doing nothing ->
#                    S_E = 0.  Clamped at 100.
#
#   fidelity       S_F : 100 * max(0, 1 - int|P_regen - P_base| / int P_base).
#                    P_regen = p_elec + p_copper + p_brake is the
#                    total mechanical power the regen system pulls
#                    out of the wheel (harvested + resistive heat +
#                    band slip heat).  P_base = brake_demand * w_ours
#                    is the ideal-friction-brake target evaluated at
#                    the strategy's own wheel speed.  Rewards matching
#                    both the magnitude AND timing of the rider's
#                    brake demand.  Doing nothing scores ~0 because
#                    int|0 - P_base| / int P_base = 1.  Over-engagement
#                    is symmetric: lifting above P_base costs fidelity
#                    just as much as dropping below it.
#
#   composite     = W_CAPTURE * S_E + W_FIDELITY * S_F   (fidelity-first)

W_CAPTURE  = 0.40
W_FIDELITY        = 0.60


# =====================================================================
#  Helpers
# =====================================================================

def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return float(x)


def _dt_from_log(log: dict) -> float:
    t = log['t']
    if len(t) > 1:
        return float(t[1] - t[0])
    return 0.01


def _brake_window_mask(t_log: np.ndarray,
                       windows: Sequence[tuple[float, float]]) -> np.ndarray:
    """Bool mask: True where t_log[i] falls inside any brake window."""
    mask = np.zeros(t_log.shape, dtype=bool)
    if not windows:
        return mask
    for t0, t1 in windows:
        mask |= (t_log >= t0) & (t_log < t1)
    return mask


# =====================================================================
#  Per-tick ingredients (step-level decomposition of the scalar scores)
# =====================================================================
#
# Both scalar dimensions are reductions over arrays of per-tick
# contributions.  Exposing those arrays lets downstream consumers
# (RL reward shaping, PySR fitness, debug plots, regression tests)
# see the same signal the scalar score is built from, and lets the
# per-ride scalar be reconstructed exactly from the arrays.

def _capture_increments(on_log: dict,
                           brake_mask: np.ndarray
                           ) -> tuple[np.ndarray, np.ndarray]:
    """Per-tick numerator and denominator of the capture-ratio metric.

    Numerator   : net harvested electrical energy  ``p_elec * dt``
                  (already post-copper and post-ESR in the sim).
    Denominator : idealized band-brake mechanical work at the
                  STRATEGY's current wheel speed ``brake_demand *
                  w_ours * dt`` -- the energy the rider's brake
                  demand would remove from the wheel via a perfect
                  friction brake applied right now, at this wheel
                  speed.  This is the "instantaneous budget" the
                  regen has a chance to capture.

    Rationale for ω_ours over ω_base: an idealized brake applied to
    the free-decay baseline trajectory is not the target the
    strategy can actually match -- a strategy that slows the wheel
    less aggressively has ω_ours > ω_base, so comparing against
    ``brake_demand * w_base`` punishes it for chasing a smaller
    target that no longer exists in its reality.  Evaluating P_base
    at the strategy's own ω restores the "match me" contract.

    Note: this re-opens a mild gaming mode (under-engage shrinks
    denominator with numerator), but brake_demand is still strategy-
    independent so it's bounded; fidelity catches the residual.

    Doing nothing -> num=0, den>0 during braking => capture 0.
    Perfect capture -> num~=den => 100.  Over-capture (num>den) is
    clamped at 100 by ``_capture_score``; excess is already caught
    by the Fidelity dimension (over-braking).
    """
    dt = _dt_from_log(on_log)
    p_elec = np.asarray(on_log['p_elec'], dtype=float)
    brake = np.asarray(on_log['brake_demand'], dtype=float)
    v_ours_ms = np.asarray(on_log['speed'], dtype=float) / 3.6
    w_ours = v_ours_ms / R_WHEEL
    num = p_elec * dt
    den = brake * w_ours * dt
    m = brake_mask.astype(bool)
    num[~m] = 0.0
    den[~m] = 0.0
    return num, den


def _slip_heat_fraction(on_log: dict, brake_mask: np.ndarray) -> float:
    """Diagnostic: fraction of brake-window touched-energy dissipated as
    band slip heat (p_brake / (p_elec + p_copper + p_brake)).

    1.0 = pure mechanical brake (motor inert).  0.0 = motor torque
    perfectly matched to band torque, carrier locked, no slip.
    """
    dt = _dt_from_log(on_log)
    p_elec   = np.asarray(on_log['p_elec'],   dtype=float)
    p_copper = np.asarray(on_log['p_copper'], dtype=float)
    p_brake  = np.asarray(on_log['p_brake'],  dtype=float)
    m = brake_mask.astype(bool)
    if not np.any(m):
        return 0.0
    num = float((p_brake[m] * dt).sum())
    den = float(((p_elec[m] + p_copper[m] + p_brake[m]) * dt).sum())
    if den <= 1e-9:
        return 0.0
    return _clamp01(num / den)


def _fidelity_increments(on_log: dict,
                     brake_mask: np.ndarray,
                     speed_mask: np.ndarray
                     ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-tick ingredients of the power-tracking fidelity metric.

    Returns three arrays of length ``n`` (log length), with values
    outside the brake window zeroed so the ratio-reduction reconstructs
    :func:`_fidelity_score` exactly:

    err_abs_J : ``|P_regen - P_base| * dt``   -- per-tick mismatch energy
    base_J    : ``P_base * dt``               -- per-tick baseline energy
    counted   : ``brake_mask`` (bool)         -- ticks contributing to the metric

    Per-tick ``P_regen = p_elec + p_copper + p_brake`` is the total
    mechanical power extracted from the wheel by the regen system.
    Per-tick ``P_base = brake_demand * w_ours`` is the ideal
    friction-brake target evaluated at the strategy's own wheel
    speed.  ``speed_mask`` is kept in the signature for backward
    compatibility but is unused by the new metric (the baseline
    power naturally goes to zero at low speed, so there's nothing
    to gate on).
    """
    dt = _dt_from_log(on_log)
    p_elec = np.asarray(on_log['p_elec'],   dtype=float)
    p_copp = np.asarray(on_log['p_copper'], dtype=float)
    p_brk  = np.asarray(on_log['p_brake'],  dtype=float)
    brake  = np.asarray(on_log['brake_demand'], dtype=float)
    v_ours = np.asarray(on_log['speed'], dtype=float) / 3.6
    n = min(len(p_elec), len(brake), len(v_ours))
    if n == 0 or dt <= 0.0:
        return (np.zeros(0, dtype=float),
                np.zeros(0, dtype=float),
                np.zeros(0, dtype=bool))
    w_ours = v_ours[:n] / R_WHEEL
    p_regen = p_elec[:n] + p_copp[:n] + p_brk[:n]
    p_base  = brake[:n] * w_ours
    err_abs = np.abs(p_regen - p_base) * dt
    base_J  = p_base * dt
    mask = brake_mask[:n].astype(bool)
    err_abs[~mask] = 0.0
    base_J[~mask]  = 0.0
    return err_abs, base_J, mask


# =====================================================================
#  Per-ride dimensions (scalars built from the per-tick ingredients)
# =====================================================================

def _capture_score(on_log: dict, brake_mask: np.ndarray) -> float:
    """Capture ratio: harvested / baseline-brake-work over brake windows,
    clamped to [0, 100].  Doing nothing -> 0.  Perfect capture -> 100."""
    if not np.any(brake_mask):
        return 0.0
    num, den = _capture_increments(on_log, brake_mask)
    n_sum = float(num.sum())
    d_sum = float(den.sum())
    if d_sum <= 1e-6:
        return 0.0
    return _clamp01(n_sum / d_sum) * 100.0


def _fidelity_score(on_log: dict,
                brake_mask: np.ndarray,
                speed_mask: np.ndarray) -> float:
    """100 = regen power tracks the idealized-band-brake power
    tick-for-tick over the brake windows; 0 = tracking error equals
    or exceeds the baseline energy (do-nothing).  Two-sided."""
    err_abs, base_J, mask = _fidelity_increments(on_log, brake_mask, speed_mask)
    if not np.any(mask):
        return 100.0
    base_sum = float(base_J.sum())
    if base_sum <= 1e-6:
        return 100.0
    return _clamp01(1.0 - float(err_abs.sum()) / base_sum) * 100.0


def _composite(capture: float, fidelity: float) -> float:
    return float(W_CAPTURE * capture + W_FIDELITY * fidelity)


# =====================================================================
#  Public single-log scorer (shared with the gallery + any ad-hoc caller)
# =====================================================================

def score_log(log: dict,
              brake_mask: Optional[np.ndarray] = None
              ) -> dict[str, float]:
    """Score a single simulator log against its idealized-band-brake
    baseline.  Mirrors :func:`score_ride_stepwise` exactly but takes a
    raw log dict (the output of :func:`sim.physics.simulate` or
    :func:`sim.physics.simulate_ride`) so it can be used outside the
    ride-basket pipeline -- e.g. the gallery's free-decel chips.

    Parameters
    ----------
    log
        Simulator log with ``t``, ``speed``, ``speed_baseline``,
        ``p_elec``, ``p_copper``, ``p_brake`` channels.
    brake_mask
        Optional bool mask over control ticks.  If ``None``, the whole
        log is treated as a brake window (appropriate for free-decel
        scenarios).

    Returns a dict with ``capture``, ``fidelity``, ``composite``,
    ``energy_J``, ``peak_decel`` and ``peak_jerk`` (the last three are
    presentation-only and not fed back into scoring).
    """
    t = np.asarray(log['t'], dtype=float)
    n = t.shape[0]
    if brake_mask is None:
        bmask = np.ones(n, dtype=bool)
    else:
        bmask = np.asarray(brake_mask, dtype=bool)[:n]
    speed = np.asarray(log['speed'], dtype=float)[:n]
    speed_mask = speed >= SPEED_CUTOFF_KMH

    capture = _capture_score(log, bmask)
    fidelity = _fidelity_score(log, bmask, speed_mask)
    composite = _composite(capture, fidelity)

    dt = _dt_from_log(log)
    v_ms = speed / 3.6
    energy_J = float((np.asarray(log['p_elec'], dtype=float)[:n] * dt).sum())
    if n >= 2 and dt > 0.0:
        a_on = -np.diff(v_ms) / dt
        peak_decel = float(np.max(a_on)) if a_on.size else 0.0
    else:
        peak_decel = 0.0
    if n >= 3 and dt > 0.0:
        jerk = np.diff(np.diff(v_ms)) / (dt * dt)
        peak_jerk = float(np.max(np.abs(jerk))) if jerk.size else 0.0
    else:
        peak_jerk = 0.0

    return {
        'capture': capture,
        'fidelity': fidelity,
        'composite': composite,
        'energy_J': energy_J,
        'peak_decel': peak_decel,
        'peak_jerk': peak_jerk,
    }


# =====================================================================
#  Ride scoring
# =====================================================================
# =====================================================================

@dataclass
class RideScore:
    ride: RideTrace
    capture: float
    fidelity: float
    composite: float
    slip_heat_frac: float = 0.0


@dataclass
class RideStepwise:
    """Per-tick decomposition of a single-ride score.

    The scalar :class:`RideScore` is reconstructed exactly from these
    arrays; any downstream consumer (RL reward shaping, symbolic
    regression fitness, debug plots) can use the ingredients directly
    without re-running the simulator.

    Conventions (length matches the motor-on log, ``n_samples``):
      - ``t``:                shape (n,), seconds
      - ``brake_mask``:       shape (n,), bool
      - ``fidelity_speed_mask``:  shape (n,), bool (v >= SPEED_CUTOFF_KMH)
      - ``capture_num_per_tick``: shape (n,), J (0 outside brake_mask)
      - ``capture_den_per_tick``: shape (n,), J (0 outside brake_mask)
      - ``fidelity_err_abs_J_per_tick``: shape (n,), J (0 outside brake_mask)
      - ``fidelity_base_J_per_tick``:   shape (n,), J (0 outside brake_mask)
      - ``fidelity_counted_per_tick``:  shape (n,), bool

    Aggregation identities:
      capture = clamp01(sum(num) / sum(den)) * 100
      fidelity       = clamp01(1 - sum(err_abs_J[counted]) / sum(base_J[counted])) * 100
      composite  = W_CAPTURE * capture + W_FIDELITY * fidelity
    """
    ride: RideTrace
    t: np.ndarray
    brake_mask: np.ndarray
    fidelity_speed_mask: np.ndarray
    capture_num_per_tick: np.ndarray
    capture_den_per_tick: np.ndarray
    fidelity_err_abs_J_per_tick: np.ndarray
    fidelity_base_J_per_tick: np.ndarray
    fidelity_counted_per_tick: np.ndarray
    capture: float
    fidelity: float
    composite: float
    slip_heat_frac: float = 0.0

    @property
    def score(self) -> "RideScore":
        return RideScore(ride=self.ride,
                         capture=self.capture,
                         fidelity=self.fidelity,
                         composite=self.composite,
                         slip_heat_frac=self.slip_heat_frac)


def score_ride(strategy_factory, ride: RideTrace, *,
               motor_off_log: Optional[dict] = None,  # deprecated, ignored
               sim_kwargs: Optional[dict] = None) -> RideScore:
    return score_ride_stepwise(strategy_factory, ride,
                               sim_kwargs=sim_kwargs).score


def score_ride_stepwise(strategy_factory, ride: RideTrace, *,
                        motor_off_log: Optional[dict] = None,  # deprecated, ignored
                        sim_kwargs: Optional[dict] = None) -> RideStepwise:
    """Same as :func:`score_ride` but also returns per-tick ingredients.

    The aggregation of the returned per-tick arrays matches the scalar
    ``capture`` / ``fidelity`` fields exactly (see
    :class:`RideStepwise` docstring).

    ``motor_off_log`` is accepted for backward compatibility but
    ignored -- the Fidelity baseline is now the ``speed_baseline`` channel
    emitted alongside the motor-on sim (idealized band brake applied
    directly at the wheel).
    """
    sk = sim_kwargs or {}
    on_log = simulate_ride(strategy_factory(), ride, **sk)

    n = len(on_log['t'])
    t_log = np.asarray(on_log['t'][:n], dtype=float)
    speed = np.asarray(on_log['speed'][:n], dtype=float)

    brake_mask = _brake_window_mask(t_log, ride.brake_windows)
    fidelity_speed_mask = speed >= SPEED_CUTOFF_KMH

    on_trunc  = {k: (v[:n] if isinstance(v, np.ndarray) else v) for k, v in on_log.items()}

    e_num, e_den = _capture_increments(on_trunc, brake_mask)
    f_err, f_base, f_mask = _fidelity_increments(on_trunc, brake_mask, fidelity_speed_mask)

    num_sum = float(e_num.sum())
    den_sum = float(e_den.sum())
    if den_sum <= 1e-6 or not np.any(brake_mask):
        capture = 0.0
    else:
        capture = _clamp01(num_sum / den_sum) * 100.0

    f_base_sum = float(f_base.sum())
    if not np.any(f_mask) or f_base_sum <= 1e-6:
        fidelity = 100.0
    else:
        fidelity = _clamp01(1.0 - float(f_err.sum()) / f_base_sum) * 100.0

    composite = _composite(capture, fidelity)
    slip_heat_frac = _slip_heat_fraction(on_trunc, brake_mask)

    return RideStepwise(
        ride=ride,
        t=t_log,
        brake_mask=brake_mask,
        fidelity_speed_mask=fidelity_speed_mask,
        capture_num_per_tick=e_num,
        capture_den_per_tick=e_den,
        fidelity_err_abs_J_per_tick=f_err,
        fidelity_base_J_per_tick=f_base,
        fidelity_counted_per_tick=f_mask,
        capture=capture,
        fidelity=fidelity,
        composite=composite,
        slip_heat_frac=slip_heat_frac,
    )


# =====================================================================
#  Motor-off caching
# =====================================================================

def precompute_motor_off_logs(rides: Sequence[RideTrace], *,
                              sim_kwargs: Optional[dict] = None) -> list[dict]:
    """Deprecated: fidelity baseline is now the ``speed_baseline`` channel
    of the motor-on log, so no separate precomputation is needed.
    Retained as a no-op stub so existing tuner wiring keeps working.
    """
    return [None] * len(rides)  # type: ignore[list-item]


# =====================================================================
#  Ride-set scoring
# =====================================================================

@dataclass
class RideSetScore:
    per_ride: list[RideScore]
    per_profile: dict[str, dict]
    capture: float
    fidelity: float
    composite: float


def score_rides(strategy_factory,
                rides: Sequence[RideTrace],
                *,
                motor_off_logs: Optional[Sequence[dict]] = None,
                sim_kwargs: Optional[dict] = None) -> RideSetScore:
    """Score a controller across a ride set."""
    if motor_off_logs is not None and len(motor_off_logs) != len(rides):
        raise ValueError("motor_off_logs length must match rides")

    per_ride: list[RideScore] = []
    for i, ride in enumerate(rides):
        off_log = motor_off_logs[i] if motor_off_logs is not None else None
        per_ride.append(score_ride(strategy_factory, ride,
                                   motor_off_log=off_log,
                                   sim_kwargs=sim_kwargs))

    profiles: dict[str, list[RideScore]] = {}
    for rs in per_ride:
        profiles.setdefault(rs.ride.profile, []).append(rs)

    per_profile: dict[str, dict] = {}
    for name, scores in profiles.items():
        e = float(np.mean([r.capture for r in scores]))
        f = float(np.mean([r.fidelity       for r in scores]))
        per_profile[name] = dict(
            capture=e, fidelity=f, composite=_composite(e, f),
            weight=PROFILES[name].weight, n=len(scores),
        )

    total_w = sum(p['weight'] for p in per_profile.values())
    if total_w <= 0.0:
        return RideSetScore(per_ride=per_ride, per_profile=per_profile,
                            capture=0.0, fidelity=0.0, composite=0.0)
    inv_w = 1.0 / total_w
    e_w = sum(p['capture'] * p['weight'] for p in per_profile.values()) * inv_w
    f_w = sum(p['fidelity']       * p['weight'] for p in per_profile.values()) * inv_w

    return RideSetScore(per_ride=per_ride, per_profile=per_profile,
                        capture=e_w, fidelity=f_w,
                        composite=_composite(e_w, f_w))


def score_strategy(strategy_factory, *,
                   rides: Optional[Sequence[RideTrace]] = None,
                   seeds_per_profile: int = 5,
                   base_seed: int = 0,
                   motor_off_logs: Optional[Sequence[dict]] = None,
                   sim_kwargs: Optional[dict] = None) -> RideSetScore:
    """High-level entry point used by run_tune / run_gallery."""
    if rides is None:
        rides = generate_ride_set(seeds_per_profile=seeds_per_profile,
                                  base_seed=base_seed)
    return score_rides(strategy_factory, rides,
                       motor_off_logs=motor_off_logs,
                       sim_kwargs=sim_kwargs)


# =====================================================================
#  Monte Carlo robustness (perturbed physics)
# =====================================================================

UNCERTAIN_PARAMS = [
    ("mu_s",              MU_S,         0.20,  "Static friction coeff"),
    ("mu_k",              MU_K,         0.20,  "Kinetic friction coeff"),
    ("eta_gear",          ETA_GEAR,     0.025, "Gear efficiency"),
    ("flux_linkage",      FLUX_LINKAGE, 0.05,  "Motor flux linkage"),
    ("r_phase",           R_PHASE,      0.08,  "Phase resistance"),
    ("j_carrier",         J_CARRIER,    0.12,  "Carrier inertia"),
    ("t_drag_coeff",      T_DRAG_COEFF, 0.25,  "Rotor drag coeff"),
    ("vesc_current_gain", 1.0,          0.06,  "VESC current sensor gain"),
    ("vesc_voltage_gain", 1.0,          0.015, "VESC voltage sensor gain"),
    ("cap_esr",           CAP_ESR,      0.30,  "Supercap bank ESR"),
    ("foc_tau",           FOC_TAU,      0.20,  "FOC current loop tau"),
    ("telem_delay",       TELEM_DELAY,  0.25,  "RPM telemetry delay"),
    # Sensor-noise knobs. sigma_rel is applied relative to the
    # nominal; iq_bias uses an absolute sigma (nominal 0) and is
    # special-cased in the sampler below.
    ("rpm_noise_sigma",   RPM_NOISE_SIGMA_DEFAULT,  0.30, "Sensorless RPM noise \u03c3"),
    ("iq_noise_sigma",    IQ_NOISE_SIGMA_DEFAULT,   0.30, "Post-FOC iq noise \u03c3"),
    ("vcap_noise_sigma",  VCAP_NOISE_SIGMA_DEFAULT, 0.30, "Vcap ADC noise \u03c3"),
    ("iq_bias",           IQ_BIAS_DEFAULT,          0.30, "iq sensor offset (\u03c3_abs A)"),
]

# iq_bias is a zero-mean offset, so sigma_rel is meaningless; the
# sampler treats it as an absolute 1-\u03c3 in amps.
_IQ_BIAS_SIGMA_ABS = 0.30

_THERMAL_COEFF = {
    "r_phase":      +0.60,
    "flux_linkage": -0.60,
    "t_drag_coeff": +0.60,
}


def _sample_perturbations(rng: np.random.Generator, n: int) -> list[dict]:
    samples: list[dict] = []
    for _ in range(n):
        u_th = rng.normal(0.0, 1.0)
        p: dict = {}
        for name, nominal, sigma_rel, _desc in UNCERTAIN_PARAMS:
            # iq_bias: absolute sigma, zero-mean, truncated at ±2σ.
            if name == "iq_bias":
                sigma_abs = _IQ_BIAS_SIGMA_ABS
                while True:
                    val = rng.normal(0.0, sigma_abs)
                    if abs(val) <= 2.0 * sigma_abs:
                        break
                p[name] = val
                continue
            sigma_abs = nominal * sigma_rel
            tc = _THERMAL_COEFF.get(name, 0.0)
            while True:
                z_indep = rng.normal(0.0, 1.0)
                z = tc * u_th + math.sqrt(max(0.0, 1.0 - tc * tc)) * z_indep
                val = nominal + z * sigma_abs
                if abs(val - nominal) <= 2.0 * sigma_abs:
                    break
            if name in ("mu_s", "mu_k", "eta_gear"):
                val = max(0.01, min(1.0, val))
            elif name in ("j_carrier", "t_drag_coeff", "r_phase", "flux_linkage"):
                val = max(nominal * 0.3, val)
            elif name in ("vesc_current_gain", "vesc_voltage_gain"):
                val = max(0.7, min(1.3, val))
            elif name == "cap_esr":
                val = max(0.01, val)
            elif name == "foc_tau":
                val = max(0.0002, val)
            elif name == "telem_delay":
                val = max(0.005, val)
            elif name in ("rpm_noise_sigma", "iq_noise_sigma", "vcap_noise_sigma"):
                # Sensor-noise magnitudes must stay non-negative.
                val = max(0.0, val)
            p[name] = val
        if p["mu_k"] >= p["mu_s"]:
            p["mu_k"] = p["mu_s"] * 0.67
        samples.append(p)
    return samples


def _sim_kwargs_from_perturbation(p: dict) -> dict:
    return dict(
        mu_s=p["mu_s"], mu_k=p["mu_k"], eta_gear=p["eta_gear"],
        flux_linkage_override=p["flux_linkage"],
        r_phase_override=p["r_phase"],
        j_carrier_override=p["j_carrier"],
        t_drag_coeff=p["t_drag_coeff"],
        vesc_current_gain=p["vesc_current_gain"],
        vesc_voltage_gain=p["vesc_voltage_gain"],
        cap_esr=p["cap_esr"], foc_tau=p["foc_tau"],
        telem_delay=p["telem_delay"],
        rpm_noise_sigma=p["rpm_noise_sigma"],
        iq_noise_sigma=p["iq_noise_sigma"],
        iq_bias=p["iq_bias"],
        vcap_noise_sigma=p["vcap_noise_sigma"],
    )


class _RobustWorker:
    """Per-task callable.  Pickles strategy+params+perturbation only;
    the ride basket is fetched from worker-process globals (stashed
    once by ``_robust_pool_initializer``) to avoid re-pickling 20 x
    ~5 k-float arrays on every trial.
    """
    def __init__(self, strat_cls, strat_params, rides_fp):
        self.strat_cls = strat_cls
        self.strat_params = strat_params or {}
        self.rides_fp = rides_fp

    def __call__(self, perturbation):
        rides = _WORKER_RIDES.get(self.rides_fp)
        if rides is None:
            raise RuntimeError(
                "Worker rides not staged for fingerprint "
                f"{self.rides_fp[:1]}...  -- call stage_rides_in_pool(...) first"
            )
        sim_kw = _sim_kwargs_from_perturbation(perturbation)
        key = (self.rides_fp, _perturbation_cache_key(perturbation))
        off_logs = _WORKER_OFFLOG_CACHE.get(key)
        if off_logs is None:
            off_logs = precompute_motor_off_logs(rides, sim_kwargs=sim_kw)
            _WORKER_OFFLOG_CACHE[key] = off_logs
        result = score_rides(
            lambda: self.strat_cls(**self.strat_params),
            rides,
            motor_off_logs=off_logs,
            sim_kwargs=sim_kw,
        )
        return (result.composite, result.capture, result.fidelity)


# ── Worker-process state for reusable Pool ─────────────────────────────
# When the tuner creates a persistent mp.Pool:
#   * ``_WORKER_RIDES``         -- rides staged once by stage_rides_in_pool;
#                                  keyed by fingerprint so screen and full
#                                  baskets can coexist.
#   * ``_WORKER_OFFLOG_CACHE``  -- motor-off logs (strategy-independent)
#                                  keyed by (rides_fp, perturbation).
_WORKER_RIDES: dict = {}
_WORKER_OFFLOG_CACHE: dict = {}


def _worker_stage_rides(rides_fp, rides):
    """Callable run inside worker: stash rides under their fingerprint."""
    global _WORKER_RIDES
    _WORKER_RIDES[rides_fp] = rides
    return rides_fp


def stage_rides_in_pool(pool, *ride_sets):
    """Push ride sets into every worker process's ``_WORKER_RIDES``.

    Call once after creating an mp.Pool and before any
    ``score_strategy_robust(pool=pool, ...)`` call.  Each ride set is
    pickled ``n_workers`` times (once per worker) rather than once per
    trial (currently ~21 tasks x 300 trials x 3 strategies).
    """
    # Determine pool size.
    # We submit a no-op per worker by using a unique marker and
    # trusting that a tight apply_async loop distributes across all
    # workers; to be correct we rely on ``pool.map`` which guarantees
    # *every element* is processed but not *by a distinct worker*.
    # Instead: submit max(1, n_workers) copies of the stage task with
    # a sentinel argument; Pool will round-robin them.
    n = getattr(pool, "_processes", None) or 1
    results = []
    for rs in ride_sets:
        fp = _rides_fingerprint(rs)
        # Fan out to *every* worker: we use pool.map with n copies of
        # the same args so each worker handles at least one.  This is
        # the standard trick; the chunksize=1 guarantees distribution.
        out = pool.map(_stage_rides_shim, [(fp, rs)] * n, chunksize=1)
        results.extend(out)
    return results


def _stage_rides_shim(args):
    fp, rides = args
    return _worker_stage_rides(fp, rides)


# Per-pool memo so we stage each ride basket at most once per Pool
# lifetime.  Keyed by id(pool); entries are sets of staged fingerprints.
_POOL_STAGED: dict = {}


def _ensure_rides_staged(pool, rides_fp, rides):
    staged = _POOL_STAGED.setdefault(id(pool), set())
    if rides_fp in staged:
        return
    stage_rides_in_pool(pool, rides)
    staged.add(rides_fp)


def _rides_fingerprint(rides: Sequence) -> tuple:
    """Stable hashable identity for a ride basket that survives pickling.

    Rides are fully determined by (profile, seed); fingerprint is the
    ordered tuple of those pairs.
    """
    return tuple((r.profile, int(r.seed)) for r in rides)


def _perturbation_cache_key(perturbation: dict) -> tuple:
    """Stable hashable key for a perturbation dict."""
    return tuple(sorted((k, float(v)) for k, v in perturbation.items()))


def _robust_pool_initializer():
    """mp.Pool initializer: silence stdout/stderr + reset caches.

    Run_tune.main() passes this to mp.Pool(initializer=...).  The
    stdout redirect keeps VS Code's integrated terminal clean; the
    cache dict starts empty per worker process.
    """
    import os as _os
    import sys as _sys
    _devnull = open(_os.devnull, "w")  # noqa: SIM115
    _sys.stdout = _devnull
    _sys.stderr = _devnull
    global _WORKER_OFFLOG_CACHE
    _WORKER_OFFLOG_CACHE = {}


def score_strategy_robust(strategy_factory=None, *,
                          rides: Optional[Sequence[RideTrace]] = None,
                          seeds_per_profile: int = 5,
                          base_seed: int = 0,
                          n_samples: int = 20,
                          seed: int = 42,
                          workers: int = 1,
                          strat_cls=None,
                          strat_params: Optional[dict] = None,
                          pool=None) -> dict:
    """Score a strategy across Monte-Carlo perturbations of physics."""
    if rides is None:
        rides = generate_ride_set(seeds_per_profile=seeds_per_profile,
                                  base_seed=base_seed)

    rng = np.random.default_rng(seed)
    perturbations = _sample_perturbations(rng, n_samples)

    nominal_p = {name: nominal for name, nominal, _, _ in UNCERTAIN_PARAMS}
    all_runs = [nominal_p] + perturbations

    if (workers > 1 or pool is not None) and strat_cls is not None:
        rides_fp = _rides_fingerprint(rides)
        if pool is not None:
            _ensure_rides_staged(pool, rides_fp, rides)
            worker = _RobustWorker(strat_cls, strat_params or {}, rides_fp)
            results = pool.map(worker, all_runs)
        else:
            import multiprocessing as mp
            with mp.Pool(workers, initializer=_robust_pool_initializer) as _pool:
                _ensure_rides_staged(_pool, rides_fp, rides)
                worker = _RobustWorker(strat_cls, strat_params or {}, rides_fp)
                results = _pool.map(worker, all_runs)
    else:
        if strategy_factory is None:
            if strat_cls is None:
                raise ValueError(
                    "score_strategy_robust needs strategy_factory or strat_cls")
            sp = strat_params or {}
            strategy_factory = lambda: strat_cls(**sp)  # noqa: E731
        results = []
        rides_fp = _rides_fingerprint(rides)
        for p in all_runs:
            sim_kw = _sim_kwargs_from_perturbation(p)
            key = (rides_fp, _perturbation_cache_key(p))
            off_logs = _WORKER_OFFLOG_CACHE.get(key)
            if off_logs is None:
                off_logs = precompute_motor_off_logs(rides, sim_kwargs=sim_kw)
                _WORKER_OFFLOG_CACHE[key] = off_logs
            r = score_rides(strategy_factory, rides,
                            motor_off_logs=off_logs, sim_kwargs=sim_kw)
            results.append((r.composite, r.capture, r.fidelity))

    composites = np.array([r[0] for r in results])
    capture = np.array([r[1] for r in results])
    fidelity       = np.array([r[2] for r in results])

    nominal = float(composites[0])
    perturbed = composites[1:]

    def _cvar(arr, alpha):
        n = max(1, int(np.ceil(alpha * len(arr))))
        return float(np.mean(np.sort(arr)[:n]))

    return dict(
        nominal=nominal,
        mean=float(np.mean(perturbed)) if len(perturbed) else nominal,
        std=float(np.std(perturbed))   if len(perturbed) else 0.0,
        p5=float(np.percentile(perturbed, 5))  if len(perturbed) else nominal,
        p95=float(np.percentile(perturbed, 95)) if len(perturbed) else nominal,
        cvar10=_cvar(perturbed, 0.10) if len(perturbed) else nominal,
        cvar20=_cvar(perturbed, 0.20) if len(perturbed) else nominal,
        scores=perturbed,
        capture_mean=float(np.mean(capture[1:])) if len(perturbed) else float(capture[0]),
        fidelity_mean=float(np.mean(fidelity[1:]))             if len(perturbed) else float(fidelity[0]),
    )
