"""Stage B6a — JAX port of sim.scoring per-ride dimensions.

Inputs are already-simulated log dicts from
``simulate_ride_strategy_jax`` (one batched dict with leading batch
axis ``[B, n_ticks]`` per channel).  Outputs per-ride energy /
linearity / composite plus ride-set aggregates.

Scope:
* ``score_rides_jax``   — per-ride triple for a set of batched logs.
* ``profile_weighted_aggregate`` — numpy helper since profile weights
  and rides-per-profile are static.

Not ported (done in numpy):
* Monte-Carlo perturbation sampling (``_sample_perturbations``) — this
  is one-shot per PySR candidate set, cheap in numpy.
* CVaR aggregation — trivial in numpy once we have the 21 composites.

Motor-off baseline: provided separately as a second batched log dict.
In practice the caller runs the simulator twice (once with the
strategy, once with a zero-strategy) and passes both in.
"""
from __future__ import annotations

from typing import Sequence

import jax
import jax.numpy as jnp
import numpy as np

from sim.jax_env import DEFAULT_FLOAT  # noqa: F401  (configures jax)


W_ENERGY     = 0.40
W_LINEARITY  = 0.60
A_NORM_MS2 = 2.0
SPEED_CUTOFF_LINEARITY_KMH = 2.0


def _clamp01_jax(x):
    return jnp.clip(x, 0.0, 1.0)


def score_ride_jax(
    *,
    # on-log channels, shape [n_ticks]
    t, speed_on, p_elec, p_copper, p_brake, eta,
    # off-log channel
    speed_off,
    # per-tick brake mask (bool, [n_ticks]).  True where brake-window
    # covers the control tick.  In practice passed as
    # ``brake_ticks > 0.0`` from the ride resampler.
    brake_mask,
    # valid-sample count (scalar int, ticks with data).  Padding
    # past this point is zeroed.
    n_valid,
):
    """Return (energy, linearity, composite) for one ride.

    Matches sim.scoring._energy_score + _linearity_score exactly apart
    from two minor differences:
      * brake_mask is derived from control-tick-level brake_ticks, not
        the numpy version's log-timestamp-level ``_brake_window_mask``.
        These agree to within one control tick (10 ms).
      * speed samples outside [0, n_valid) are zero (padding), so the
        v^2 weight collapses them naturally — no explicit slicing
        needed.
    """
    dt = t[1] - t[0]
    v_on_ms  = speed_on  / 3.6
    v_off_ms = speed_off / 3.6
    w = v_on_ms * v_on_ms

    # Device power: if eta>0, p_elec / eta; else p_copper + p_brake.
    safe_eta = jnp.where(eta > 0.0, eta, 1.0)
    device_power = jnp.where(eta > 0.0,
                             p_elec / safe_eta,
                             p_copper + p_brake)

    valid_mask = jnp.arange(t.shape[0]) < n_valid
    bm = brake_mask & valid_mask

    num = jnp.where(bm, p_elec       * w * dt, 0.0)
    den = jnp.where(bm, device_power * w * dt, 0.0)
    e_num = jnp.sum(num)
    e_den = jnp.sum(den)
    # clamp01(e_num / e_den) * 100, with e_den <= 1e-6 → 0
    ratio = jnp.where(e_den > 1e-6, e_num / e_den, 0.0)
    energy = _clamp01_jax(ratio) * 100.0

    # Linearity: decel error in m/s^2 over (brake ∧ speed>=cutoff),
    # reduced as RMS.  a = -diff(v)/dt.
    a_on  = -jnp.diff(v_on_ms) / dt
    a_off = -jnp.diff(v_off_ms) / dt
    err = a_on - a_off
    # brake_mask has n ticks; diff gives n-1 elements.  Valid counted
    # samples are those where brake_mask[i] ∧ speed[i]>=cutoff ∧ i<n_valid-1.
    speed_ok = speed_on >= SPEED_CUTOFF_LINEARITY_KMH
    ok = bm & speed_ok
    lin_mask = ok[:-1]  # align with diff length

    sq_err = err * err
    sq_err = jnp.where(lin_mask, sq_err, 0.0)
    n_counted = jnp.sum(lin_mask)
    mean_sq = jnp.where(n_counted > 0,
                        jnp.sum(sq_err) / jnp.maximum(n_counted, 1),
                        0.0)
    rms = jnp.sqrt(mean_sq)
    linearity = jnp.where(n_counted > 0,
                          _clamp01_jax(1.0 - rms / A_NORM_MS2) * 100.0,
                          100.0)

    composite = W_ENERGY * energy + W_LINEARITY * linearity
    return energy, linearity, composite


# vmap over a batched (ride, perturbation) axis.  kwargs → positional
# wrapper because jax.vmap in_axes doesn't accept dict mapping.
def _score_pos(t, speed_on, p_elec, p_copper, p_brake, eta,
               speed_off, brake_mask, n_valid):
    return score_ride_jax(
        t=t, speed_on=speed_on, p_elec=p_elec, p_copper=p_copper,
        p_brake=p_brake, eta=eta, speed_off=speed_off,
        brake_mask=brake_mask, n_valid=n_valid,
    )


_score_pos_vmap = jax.jit(
    jax.vmap(_score_pos, in_axes=(0, 0, 0, 0, 0, 0, 0, 0, 0))
)


def score_rides_jax(*, t, speed_on, p_elec, p_copper, p_brake, eta,
                    speed_off, brake_mask, n_valid):
    return _score_pos_vmap(t, speed_on, p_elec, p_copper, p_brake, eta,
                           speed_off, brake_mask, n_valid)


def profile_weighted_composite(
    per_ride_energy: np.ndarray,      # [n_rides]
    per_ride_linearity: np.ndarray,   # [n_rides]
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
    l_w = 0.0
    for name, idxs in profiles.items():
        e = float(np.mean([per_ride_energy[i]    for i in idxs]))
        l = float(np.mean([per_ride_linearity[i] for i in idxs]))
        w = profile_weights[name]
        e_w += e * w * inv_w
        l_w += l * w * inv_w
    c_w = W_ENERGY * e_w + W_LINEARITY * l_w
    return e_w, l_w, c_w


def cvar20(composites: np.ndarray) -> float:
    """Mean of the worst 20% of a 1-D array."""
    n = max(1, int(np.ceil(0.20 * len(composites))))
    return float(np.mean(np.sort(composites)[:n]))
