"""Stage B6a â€” JAX port of sim.scoring per-ride dimensions.

Inputs are already-simulated log dicts from
``simulate_ride_strategy_jax`` (one batched dict with leading batch
axis ``[B, n_ticks]`` per channel).  Outputs per-ride capture /
fidelity / composite plus ride-set aggregates.

Mirrors ``sim/scoring.py`` (composite = 0.40 * capture + 0.60 * fidelity).

Scope:
* ``score_rides_jax``   â€” per-ride triple for a set of batched logs.
* ``profile_weighted_aggregate`` â€” numpy helper since profile weights
  and rides-per-profile are static.

Not ported (done in numpy):
* Monte-Carlo perturbation sampling (``_sample_perturbations``) â€” this
  is one-shot per PySR candidate set, cheap in numpy.
* CVaR aggregation â€” trivial in numpy once we have the 21 composites.

Motor-off baseline: provided separately as a second batched log dict.
In practice the caller runs the simulator twice (once with the
strategy, once with a zero-strategy) and passes both in.
"""
from __future__ import annotations

from typing import Sequence

import jax
import jax.numpy as jnp
import numpy as np

from sim.jax.env import DEFAULT_FLOAT  # noqa: F401  (configures jax)
from sim.physics import R_WHEEL


W_CAPTURE = 0.40
W_FIDELITY       = 0.60
SPEED_CUTOFF_KMH = 2.0


def _clamp01_jax(x):
    return jnp.clip(x, 0.0, 1.0)


def score_ride_jax(
    *,
    # on-log channels, shape [n_ticks]
    t, speed_on, speed_base, p_elec, p_copper, p_brake, brake_demand,
    # per-tick brake mask (bool, [n_ticks]).  True where brake-window
    # covers the control tick.  In practice passed as
    # ``brake_ticks > 0.0`` from the ride resampler.
    brake_mask,
    # valid-sample count (scalar int, ticks with data).  Padding
    # past this point is zeroed.
    n_valid,
):
    """Return (capture, fidelity, composite) for one ride.

    Mirrors sim.scoring._capture_score + _fidelity_score:
      * capture = 100 * clamp01(
            sum(P_elec dt) / sum(brake_demand * w_ours * dt))
        over brake ticks.  Denominator is idealized-band-brake
        mechanical work at the STRATEGY's own wheel speed.
        Doing nothing -> 0.  Perfect capture -> 100.
      * fidelity       = 100 * max(0, 1 - sum|P_regen - P_base|*dt
                                     / sum P_base*dt)
        over brake ticks, where
            P_regen = p_elec + p_copper + p_brake  (total mechanical
                      power the regen system pulls out of the wheel)
            P_base  = brake_demand * w_ours        (ideal friction
                      brake target at strategy's own w)
        Symmetric over/under-engagement penalty; doing nothing -> 0.
      * composite  = 0.40 * capture + 0.60 * fidelity.

    brake_mask is derived from control-tick-level brake_ticks, not
    the numpy version's log-timestamp ``_brake_window_mask``; the two
    agree to within one control tick (10 ms).
    """
    dt = t[1] - t[0]
    v_ours_ms = speed_on / 3.6  # strategy's own wheel speed

    valid_mask = jnp.arange(t.shape[0]) < n_valid
    bm = brake_mask & valid_mask

    # capture / fidelity reference: idealized band brake evaluated at
    # the STRATEGY's current wheel speed (Ï‰_ours), not the free-decay
    # baseline's.  "How much wheel-decelerating power would an ideal
    # friction brake pull right now, at this wheel speed, for this
    # rider brake demand?"  Using Ï‰_base punishes a strategy that
    # slows the wheel less aggressively against a target that no
    # longer exists in its reality.
    w_ours = v_ours_ms / R_WHEEL
    p_base_brake = brake_demand * w_ours
    num = jnp.where(bm, p_elec       * dt, 0.0)
    den = jnp.where(bm, p_base_brake * dt, 0.0)
    e_num = jnp.sum(num)
    e_den = jnp.sum(den)
    ratio = jnp.where(e_den > 1e-6, e_num / e_den, 0.0)
    capture = _clamp01_jax(ratio) * 100.0

    # fidelity: relative L1 power-tracking error.  P_regen = total
    # mechanical power extracted from the wheel by the regen system
    # (electrical harvest + copper loss + band slip heat -- every
    # channel that contributes to wheel deceleration).
    p_regen = p_elec + p_copper + p_brake
    err_abs_J = jnp.where(bm, jnp.abs(p_regen - p_base_brake) * dt, 0.0)
    base_J    = den  # = p_base_brake * dt on brake ticks, else 0
    err_sum  = jnp.sum(err_abs_J)
    base_sum = jnp.sum(base_J)
    fidelity = jnp.where(base_sum > 1e-6,
                     _clamp01_jax(1.0 - err_sum / base_sum) * 100.0,
                     100.0)

    composite = W_CAPTURE * capture + W_FIDELITY * fidelity
    return capture, fidelity, composite


# vmap over a batched (ride, perturbation) axis.  kwargs â†’ positional
# wrapper because jax.vmap in_axes doesn't accept dict mapping.
def _score_pos(t, speed_on, speed_base, p_elec, p_copper, p_brake,
               brake_demand, brake_mask, n_valid):
    return score_ride_jax(
        t=t, speed_on=speed_on, speed_base=speed_base,
        p_elec=p_elec, p_copper=p_copper, p_brake=p_brake,
        brake_demand=brake_demand,
        brake_mask=brake_mask, n_valid=n_valid,
    )


_score_pos_vmap = jax.jit(
    jax.vmap(_score_pos, in_axes=(0, 0, 0, 0, 0, 0, 0, 0, 0))
)


def score_rides_jax(*, t, speed_on, speed_base, p_elec, p_copper, p_brake,
                    brake_demand, brake_mask, n_valid):
    return _score_pos_vmap(t, speed_on, speed_base,
                           p_elec, p_copper, p_brake, brake_demand,
                           brake_mask, n_valid)


def profile_weighted_composite(
    per_ride_capture: np.ndarray,  # [n_rides]
    per_ride_fidelity: np.ndarray,        # [n_rides]
    profile_names: Sequence[str],     # len n_rides
    profile_weights: dict,
) -> tuple[float, float, float]:
    """Aggregate per-ride dimensions up to a single composite.

    Same logic as ``sim.scoring.score_rides`` (mean-per-profile then
    profile-weight mean).  Pure numpy; cheap.
    """
    profiles: dict[str, list[int]] = {}
    for i, name in enumerate(profile_names):
        profiles.setdefault(name, []).append(i)

    total_w = sum(profile_weights[p] for p in profiles.keys())
    if total_w <= 0.0:
        return 0.0, 0.0, 0.0
    inv_w = 1.0 / total_w

    e_w = 0.0
    f_w = 0.0
    for name, idxs in profiles.items():
        e = float(np.mean([per_ride_capture[i] for i in idxs]))
        f = float(np.mean([per_ride_fidelity[i]    for i in idxs]))
        w = profile_weights[name]
        e_w += e * w * inv_w
        f_w += f * w * inv_w
    c_w = W_CAPTURE * e_w + W_FIDELITY * f_w
    return e_w, f_w, c_w


def cvar20(composites: np.ndarray) -> float:
    """Mean of the worst 20% of a 1-D array."""
    n = max(1, int(np.ceil(0.20 * len(composites))))
    return float(np.mean(np.sort(composites)[:n]))
