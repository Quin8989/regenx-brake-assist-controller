"""Stage B6a — PySR candidate → CVaR-20 composite, JAX-batched.

Design:
* Precompute motor-off logs once (they don't depend on the strategy).
* Precompute batched kwargs once (ride × perturbation = 420 traces).
* Per candidate: lambdify expression → compile jit(vmap(sim)) →
  evaluate, score, aggregate.
* Compile-cache keyed on the expression string: evaluating the same
  expression twice skips recompile (trivial dict lookup).

``evaluate_candidate`` is the entry point a PySR search loop would
call once per candidate.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable

import jax
import jax.numpy as jnp
import numpy as np

from sim.jax.env import DEFAULT_FLOAT  # noqa: F401  (configures jax)

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "firmware"))

from sim.physics import DT, CTRL_PERIOD
from sim.jax.physics_strategy import (
    simulate_ride_strategy_jax, lambdify_expression_jax,
)
from sim.ride_generator import PROFILES
from sim.scoring import UNCERTAIN_PARAMS, _sample_perturbations
from sim.jax.scoring import (
    score_rides_jax, profile_weighted_composite, cvar20,
)


# =====================================================================
#  Perturbation → JAX kwargs
# =====================================================================

_JAX_DIRECT = {
    "eta_gear": "eta_gear",
    "t_drag_coeff": "t_drag_coeff",
    "r_phase": "r_phase",
    "flux_linkage": "flux_linkage",
    "vesc_current_gain": "vesc_current_gain",
    "vesc_voltage_gain": "vcap_gain",
    "cap_esr": "cap_esr",
    "rpm_noise_sigma": "rpm_noise_sigma",
    "iq_noise_sigma": "iq_noise_sigma",
    "iq_bias": "iq_bias",
    "vcap_noise_sigma": "vcap_noise_sigma",
}


def _pert_to_jax_overrides(p: dict) -> dict:
    out = {jaxk: float(p[npk]) for npk, jaxk in _JAX_DIRECT.items()}
    out["r_phase_15"] = 1.5 * float(p["r_phase"])
    out["mu_ratio"] = float(p["mu_k"] / p["mu_s"])
    return out


# =====================================================================
#  Batched evaluation harness
# =====================================================================

def _zero_strategy(*args):
    """Strategy that always returns k=0 → motor-off baseline."""
    return DEFAULT_FLOAT(0.0)


def build_batch(rides, perturbations, *, seed_base=0xB6B6):
    """Flatten (rides × perturbations) → B kwargs + static config.

    Returns ``(static, traced_batched, profile_names_B, n_valid_B,
               brake_mask_B, noise_keys_B)``.

    Lazy-imports to avoid a circular init from scripts.research.validate_jax_*.
    """
    from scripts.research.validate_jax_ride_strategy import build_jax_kwargs

    ctrl_steps = max(1, int(CTRL_PERIOD / DT))
    n_ticks_padded = max(r.n // ctrl_steps for r in rides)
    n_rides = len(rides)
    n_pert = len(perturbations)
    B = n_rides * n_pert

    # Shared per-ride kwargs (strategy_fn placeholder; overridden).
    per_ride = [
        build_jax_kwargs(r, n_ticks_padded=n_ticks_padded,
                         strategy_fn=_zero_strategy)[0]
        for r in rides
    ]
    # Also need the valid tick count and brake mask per-ride for scoring.
    per_ride_nvalid = [r.n // ctrl_steps for r in rides]
    per_ride_brake_mask = [
        (np.asarray(per_ride[i]["brake_ticks"]) > 0.0)
        for i in range(n_rides)
    ]
    per_ride_brake_mask = [
        np.concatenate([m, np.zeros(n_ticks_padded - len(m), dtype=bool)])
        if len(m) < n_ticks_padded else m[:n_ticks_padded]
        for m in per_ride_brake_mask
    ]
    profile_names_B = []
    for r in rides:
        profile_names_B.extend([r.profile] * n_pert)

    all_kwargs = []
    n_valid_B = []
    brake_B = []
    for ri in range(n_rides):
        base = per_ride[ri]
        for _pi, p in enumerate(perturbations):
            kw = dict(base)
            kw.update(_pert_to_jax_overrides(p))
            all_kwargs.append(kw)
            n_valid_B.append(per_ride_nvalid[ri])
            brake_B.append(per_ride_brake_mask[ri])

    static_names = ("strategy_fn", "delay_slots", "n_ticks",
                    "ctrl_steps", "dt")
    static = {k: all_kwargs[0][k] for k in static_names}
    traced = [{k: v for k, v in kw.items() if k not in static_names}
              for kw in all_kwargs]

    def _to_arr(x):
        if isinstance(x, (jnp.ndarray, np.ndarray)):
            return jnp.asarray(x)
        return jnp.asarray(x, dtype=DEFAULT_FLOAT)
    batched = {k: jnp.stack([_to_arr(t[k]) for t in traced], axis=0)
               for k in traced[0].keys()}

    noise_keys = jax.random.split(jax.random.PRNGKey(seed_base), B)
    batched["noise_key"] = noise_keys

    return (
        static,
        batched,
        profile_names_B,
        jnp.asarray(n_valid_B, dtype=jnp.int32),
        jnp.asarray(np.stack(brake_B, axis=0), dtype=jnp.bool_),
    )


# =====================================================================
#  Compile cache + candidate evaluator
# =====================================================================

class CandidateEvaluator:
    """Evaluates PySR-style expressions to a CVaR-20 composite.

    One instance per (rides, perturbations) fixture.  Holds:
      * pre-batched kwargs (420 trajectories of on-path inputs),
      * the motor-off baseline (computed once on construction),
      * a per-expression compile cache for the on-path jitted vmap.
    """

    def __init__(self, rides, perturbations, *, seed_base=0xB6B6):
        self.rides = rides
        self.perturbations = perturbations
        (self._static, self._batched, self._profile_names,
         self._n_valid, self._brake_mask) = build_batch(
            rides, perturbations, seed_base=seed_base)
        self._B = self._n_valid.shape[0]
        self._cache: dict[str, Callable] = {}

        # Motor-off baseline: vmap with zero strategy, compute once.
        def _vmap_one(kw):
            return simulate_ride_strategy_jax(**self._static, **kw)
        off_fn = jax.jit(jax.vmap(_vmap_one))
        t0 = time.perf_counter()
        off_logs = off_fn(self._batched)
        off_logs["speed"].block_until_ready()
        self._off_compile_s = time.perf_counter() - t0
        self._speed_off = off_logs["speed"]  # [B, n_ticks]

    # ---- cache-aware jitter ----
    def _get_jit(self, expression: str):
        if expression not in self._cache:
            strat_fn = lambdify_expression_jax(expression)
            static = dict(self._static)
            static["strategy_fn"] = strat_fn

            def _one(kw):
                return simulate_ride_strategy_jax(**static, **kw)
            self._cache[expression] = jax.jit(jax.vmap(_one))
        return self._cache[expression]

    # ---- end-to-end score ----
    def evaluate(self, expression: str) -> dict:
        jfn = self._get_jit(expression)
        t0 = time.perf_counter()
        on_logs = jfn(self._batched)
        on_logs["speed"].block_until_ready()
        t_sim = time.perf_counter() - t0

        t0 = time.perf_counter()
        efficiency_B, feel_B, composite_B = score_rides_jax(
            t=on_logs["t"], speed_on=on_logs["speed"],
            speed_base=on_logs["speed_baseline"],
            p_elec=on_logs["p_elec"],
            p_copper=on_logs["p_copper"],
            p_brake=on_logs["p_brake"],
            brake_demand=on_logs["brake_demand"],
            brake_mask=self._brake_mask,
            n_valid=self._n_valid,
        )
        composite_B.block_until_ready()
        t_score = time.perf_counter() - t0

        e_np = np.asarray(efficiency_B)
        f_np = np.asarray(feel_B)

        # Group by perturbation index (outer = ride, inner = pert).
        n_pert = len(self.perturbations)
        n_rides = len(self.rides)
        composites_per_pert = np.zeros(n_pert)
        for pi in range(n_pert):
            idxs = [ri * n_pert + pi for ri in range(n_rides)]
            e_ride = e_np[idxs]
            f_ride = f_np[idxs]
            prof_names = [self.rides[ri].profile for ri in range(n_rides)]
            weights = {n: PROFILES[n].weight for n in PROFILES}
            _, _, c_w = profile_weighted_composite(
                e_ride, f_ride, prof_names, weights)
            composites_per_pert[pi] = c_w

        nominal = float(composites_per_pert[0])
        perturbed = composites_per_pert[1:]
        return dict(
            expression=expression,
            nominal=nominal,
            mean=float(np.mean(perturbed)) if len(perturbed) else nominal,
            std=float(np.std(perturbed)) if len(perturbed) else 0.0,
            cvar20=cvar20(perturbed) if len(perturbed) else nominal,
            composites=composites_per_pert,
            t_sim_s=t_sim,
            t_score_s=t_score,
            cache_hit=(expression in self._cache
                       and len(self._cache) >= 1),
        )
