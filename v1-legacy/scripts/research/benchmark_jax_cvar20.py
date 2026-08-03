"""Stage B5b — 420-trajectory cvar20 workload benchmark.

Canonical cvar20 shape is 20 rides × 21 perturbations = 420 trajectories.
Each perturbation draws physics constants and telemetry-noise sigmas
from :func:`sim.scoring._sample_perturbations`; this script measures
wall-clock simulation time on both paths.

Parity is *not* checked here — B4 (noise-free) and B5 (noise moments)
already covered correctness.  The goal is a speedup number for the
workload that drives PySR candidate evaluation.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import jax
import jax.numpy as jnp
import numpy as np

jax.config.update("jax_enable_x64", True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "firmware"))

from sim.physics import simulate_ride, DT, CTRL_PERIOD
from sim.ride_generator import PROFILES, generate_ride_set
from sim.scoring import (
    _sample_perturbations, _sim_kwargs_from_perturbation, UNCERTAIN_PARAMS,
)
from sim.jax.physics_strategy import (
    simulate_ride_strategy_jax, lambdify_expression_jax, K_FLOOR, K_CEIL,
)
from sim.jax.env import DEFAULT_FLOAT
from scripts.research.validate_jax_ride_strategy import build_jax_kwargs


PYSR_EQ = ("relu(0.1 + 0.0005 * drpm_peak_neg + 0.02 * k_prev) "
           "+ step(rpm - 100) * 0.05")
PYSR_JAX = lambdify_expression_jax(PYSR_EQ)


# ── Numpy strategy wrapper ──────────────────────────────────────────
from regen.regen_control import ff_current_from_rpm, voltage_taper
from config.settings import (
    MOTOR_PHASE_RESISTANCE_OHM as R_PHASE_NOM,
    FLUX_LINKAGE_WB, VESC_MOTOR_POLE_PAIRS, REGEN_CURRENT_MAX_A,
)
from sim.physics import VCAP_TAPER_START, VCAP_TAPER_END


class _NumpyPysr:
    key = "pysr_like"; name = "PySRLike"
    def __init__(self):
        sys.path.insert(0, str(_REPO_ROOT / "scripts" / "pysr"))
        from validate_candidates import lambdify_expression
        self._p = lambdify_expression(PYSR_EQ)
        self._k = 0.1
        self._drpm_mean_prev = 0.0
        self._drpm_peak_neg_prev = 0.0
        self._iq_prev = 0.0
    def update(self, ctx):
        rpm = ctx.preferred_rpm; iq = ctx.preferred_iq
        taper = voltage_taper(ctx.vcap, VCAP_TAPER_START, VCAP_TAPER_END)
        if taper == 0.0:
            self._k = 0.1; self._drpm_mean_prev = 0.0
            self._drpm_peak_neg_prev = 0.0; self._iq_prev = 0.0
            return 0.0
        jerk_mean = ctx.drpm_mean - self._drpm_mean_prev
        jerk_peak = ctx.drpm_peak_neg - self._drpm_peak_neg_prev
        slip_delta = ctx.drpm_peak_neg - ctx.drpm_mean
        decel_frac = ctx.drpm_mean / (rpm + 1.0)
        d_iq = iq - self._iq_prev
        power_mech = rpm * iq
        self._drpm_mean_prev = ctx.drpm_mean
        self._drpm_peak_neg_prev = ctx.drpm_peak_neg
        self._iq_prev = iq
        try:
            k_next = float(self._p(
                rpm, ctx.drpm_mean, ctx.drpm_peak_neg, iq,
                ctx.duty_cycle, ctx.vcap, self._k,
                jerk_mean, jerk_peak, slip_delta,
                decel_frac, d_iq, power_mech))
        except (ValueError, ZeroDivisionError, FloatingPointError):
            k_next = self._k
        if not np.isfinite(k_next):
            k_next = self._k
        k_next = max(K_FLOOR, min(K_CEIL, k_next))
        self._k = k_next
        i_cmd = ff_current_from_rpm(
            rpm, k_next,
            flux_linkage=FLUX_LINKAGE_WB,
            phase_resistance=R_PHASE_NOM,
            pole_pairs=VESC_MOTOR_POLE_PAIRS,
            current_limit=REGEN_CURRENT_MAX_A,
        )
        return max(0.0, min(REGEN_CURRENT_MAX_A, i_cmd * taper))


# ── Direct-pass params that both numpy simulate_ride and the JAX
#    simulator accept (so perturbations land in both identically).
#    j_carrier/mu_s/mu_k/foc_tau/telem_delay are nominal in JAX (they
#    feed derived kwargs like inv_j_carrier that we'd have to recompute
#    per-trajectory; acceptable for a wall-clock benchmark).
_JAX_DIRECT = {
    "eta_gear": "eta_gear",
    "t_drag_coeff": "t_drag_coeff",
    "r_phase": "r_phase",           # also need r_phase_15 derived
    "flux_linkage": "flux_linkage",
    "vesc_current_gain": "vesc_current_gain",
    "vesc_voltage_gain": "vcap_gain",
    "cap_esr": "cap_esr",
    "rpm_noise_sigma": "rpm_noise_sigma",
    "iq_noise_sigma": "iq_noise_sigma",
    "iq_bias": "iq_bias",
    "vcap_noise_sigma": "vcap_noise_sigma",
}


def _pert_to_jax_overrides(p):
    out = {}
    for npk, jaxk in _JAX_DIRECT.items():
        out[jaxk] = float(p[npk])
    # Derived
    out["r_phase_15"] = 1.5 * float(p["r_phase"])
    # mu_ratio = mu_k/mu_s
    out["mu_ratio"] = float(p["mu_k"] / p["mu_s"])
    return out


def main():
    # ── 20 rides (5 seeds × 4 profiles) ──
    rides = generate_ride_set(seeds_per_profile=5, base_seed=0,
                              duration=60.0)
    print(f"Built {len(rides)} rides")
    ctrl_steps = max(1, int(CTRL_PERIOD / DT))
    n_ticks_padded = max(r.n // ctrl_steps for r in rides)
    n_rides = len(rides)

    # ── 21 perturbations: nominal + 20 sampled ──
    rng = np.random.default_rng(42)
    nominal_p = {name: nom for name, nom, _, _ in UNCERTAIN_PARAMS}
    perts = [nominal_p] + _sample_perturbations(rng, 20)
    n_pert = len(perts)
    B = n_rides * n_pert
    print(f"Perturbations: {n_pert}; total trajectories: {B}")

    # ── Build per-(ride,pert) kwargs ──
    # Base kwargs (ride-dependent).
    per_ride_kwargs = [
        build_jax_kwargs(r, n_ticks_padded=n_ticks_padded,
                         strategy_fn=PYSR_JAX)[0]
        for r in rides
    ]
    # Per-trajectory: 420 dicts.  Override direct-pass params from pert.
    all_kwargs = []
    for ri in range(n_rides):
        base = per_ride_kwargs[ri]
        for pi, p in enumerate(perts):
            kw = dict(base)
            kw.update(_pert_to_jax_overrides(p))
            all_kwargs.append(kw)

    # Static args pulled out (same across batch).
    static_names = ("strategy_fn", "delay_slots", "n_ticks",
                    "ctrl_steps", "dt")
    static = {k: all_kwargs[0][k] for k in static_names}
    # Remove static from each kwargs dict for tree-stacking.
    traced = [{k: v for k, v in kw.items() if k not in static_names}
              for kw in all_kwargs]

    # Stack into batched dict: each leaf becomes [B, ...].
    def _to_array(x):
        if isinstance(x, (jnp.ndarray, np.ndarray)):
            return jnp.asarray(x)
        return jnp.asarray(x, dtype=DEFAULT_FLOAT)
    batched = {}
    for k in traced[0].keys():
        leaves = [_to_array(t[k]) for t in traced]
        batched[k] = jnp.stack(leaves, axis=0)

    # Per-trajectory noise keys.
    noise_keys = jax.random.split(jax.random.PRNGKey(0xB5B), B)
    batched["noise_key"] = noise_keys

    # vmap over all batched leaves.
    def one(kw):
        return simulate_ride_strategy_jax(**static, **kw)
    vone = jax.vmap(one)

    # Warm up (compile).
    print("Compiling + first vmap call...")
    t0 = time.perf_counter()
    r = vone(batched)
    r["t"].block_until_ready()
    t_compile = time.perf_counter() - t0
    print(f"  compile+first: {t_compile*1000:.1f} ms")

    # Hot run.
    t0 = time.perf_counter()
    r = vone(batched)
    r["t"].block_until_ready()
    t_jax = time.perf_counter() - t0
    print(f"  JAX hot ({B} trajectories): {t_jax*1000:.1f} ms  "
          f"({t_jax*1000/B:.2f} ms/traj)")

    # ── Numpy: time a representative subset, extrapolate ──
    N_NP_SAMPLE = 40
    idx = np.linspace(0, B - 1, N_NP_SAMPLE, dtype=int)
    t0 = time.perf_counter()
    for flat in idx:
        ri = flat // n_pert
        pi = flat % n_pert
        sim_kw = _sim_kwargs_from_perturbation(perts[pi])
        _ = simulate_ride(_NumpyPysr(), rides[ri], **sim_kw)
    t_np_sample = time.perf_counter() - t0
    per_np = t_np_sample / N_NP_SAMPLE
    t_np_est = per_np * B
    print(f"\nNumpy sample ({N_NP_SAMPLE} trajectories): "
          f"{t_np_sample*1000:.0f} ms  ({per_np*1000:.1f} ms/traj)")
    print(f"Numpy extrapolated to {B}: {t_np_est:.2f} s")

    speedup = t_np_est / t_jax
    print(f"\nSpeedup (JAX vmap vs numpy sequential): {speedup:.1f}x")

    # Target: ≥10× at 420 trajectories.
    ok = speedup >= 10.0
    print("PASS" if ok else "FAIL (target ≥10x)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
