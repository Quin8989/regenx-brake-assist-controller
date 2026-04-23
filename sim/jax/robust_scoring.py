"""JAX-backed robust scorer for the Optuna tuner.

Mirrors :func:`sim.scoring.score_strategy_robust` return dict but runs
the ride simulations on JAX (CPU) with a single vmapped call per trial.

Architecture
------------
One scorer instance per ride basket (screen or full).  Construction:
  * build per-ride kwargs via ``build_jax_kwargs``
  * stack per-ride arrays into a leading batch axis R
  * compile one ``jit(vmap(simulate_ride_strategy_jax))``

Per trial:
  * sample M perturbations from the same numpy sampler the numpy
    backend uses (so the MC realisations stay comparable when
    comparing backends)
  * build a flat R*(M+1) batched kwargs dict (nominal + M perts)
  * run motor-off baseline (cached per (seed, M))
  * run on-strategy sim
  * score each (ride, pert) pair via ``score_rides_jax``
  * reshape [R, M+1] and aggregate: composite = mean-over-rides, then
    cvar20 over perturbations

Caveats (documented mismatches vs numpy backend, accepted per
ρ=0.995 rank-correlation validation in
``scripts/research/validate_jax_rank_correlation.py``):
  * ``j_carrier`` / ``foc_tau`` / ``telem_delay`` perturbations are
    *not* threaded through because they'd force JAX retraces
    (they're static in the graph).
  * Noise-perturbation RNG differs (JAX Threefry vs numpy Philox).
  * Post-iq_kp clip and FOC step ordering differ slightly, per the
    existing PySR parity harness.
"""
from __future__ import annotations

from typing import Sequence

import jax
import jax.numpy as jnp
import numpy as np

from sim.jax.env import DEFAULT_FLOAT  # noqa: F401  (configures jax)
from sim.jax.physics_strategy import simulate_ride_strategy_jax
from sim.jax.tuner_strategies import build_step_fn
from sim.jax.scoring import score_rides_jax
from sim.physics import CTRL_PERIOD, DT
from sim.scoring import _sample_perturbations, UNCERTAIN_PARAMS


# Perturbation-field → batched kwarg key that the JAX simulator
# accepts as a traced array on the B axis.  Only these retrace-safe
# fields are threaded.  The rest are discarded.
_PERT_TO_JAX = {
    "eta_gear":          "eta_gear",
    "t_drag_coeff":      "t_drag_coeff",
    "r_phase":           "r_phase",
    "flux_linkage":      "flux_linkage",
    "vesc_current_gain": "vesc_current_gain",
    "vesc_voltage_gain": "vcap_gain",
    "cap_esr":           "cap_esr",
    "rpm_noise_sigma":   "rpm_noise_sigma",
    "iq_noise_sigma":    "iq_noise_sigma",
    "iq_bias":           "iq_bias",
    "vcap_noise_sigma":  "vcap_noise_sigma",
    "mu_k":              None,   # mu_ratio = mu_k / mu_s (special)
    "mu_s":              None,
}

# Keys that are per-ride arrays (stacked on B axis) vs constants
# (same value for every B entry).
_PER_RIDE_KEYS = (
    "w_ring0", "w_carrier0", "i_actual0", "e_cap0", "w_ring_base0",
    "brake_ticks", "grade_ticks", "pedal_active_ticks",
    "mass_kg", "cruise_mps", "inv_j_wheel",
)


def _noop_strategy(*_):
    return jnp.asarray(0.0)


def _pert_to_batched_overrides(perts: Sequence[dict]) -> dict:
    """Convert a list of perturbation dicts → dict of [M] arrays
    (one entry per JAX-threadable field).
    """
    M = len(perts)
    out = {}
    for src_key, jax_key in _PERT_TO_JAX.items():
        if jax_key is None:
            continue
        out[jax_key] = np.array([p[src_key] for p in perts],
                                dtype=np.float64)
    # r_phase_15 / mu_ratio derivations.
    out["r_phase_15"] = np.array([1.5 * p["r_phase"] for p in perts],
                                 dtype=np.float64)
    out["mu_ratio"]   = np.array([p["mu_k"] / p["mu_s"] for p in perts],
                                 dtype=np.float64)
    return out


class JaxRobustScorer:
    """Study-level JAX scorer.

    Construction is one-shot per ride basket; each ``.score(...)`` call
    runs one jit(vmap) on-strategy sim + (cached) motor-off baseline.
    """

    def __init__(self, rides):
        # Lazy import (avoid circular).
        from scripts.research.validate_jax_ride_strategy import build_jax_kwargs

        self.rides = list(rides)
        R = len(self.rides)
        ctrl_steps = max(1, int(CTRL_PERIOD / DT))
        n_ticks_padded = max(r.n // ctrl_steps for r in self.rides)
        self._n_ticks = n_ticks_padded
        self._R = R

        per_ride_kwargs = []
        n_valid = []
        brake_masks = []
        for r in self.rides:
            kw, n_ride = build_jax_kwargs(
                r, n_ticks_padded=n_ticks_padded, strategy_fn=_noop_strategy)
            per_ride_kwargs.append(kw)
            n_valid.append(n_ride)
            brake_masks.append(np.asarray(kw["brake_ticks"]) > 0.0)

        self._n_valid = np.asarray(n_valid, dtype=np.int32)
        self._brake_mask = np.stack(brake_masks, axis=0)

        # Stack per-ride keys → [R, ...].
        self._per_ride_stacked = {
            k: np.stack([np.asarray(kw[k]) for kw in per_ride_kwargs])
            for k in _PER_RIDE_KEYS
        }
        # All other keys are shared (identical across rides).
        sample = per_ride_kwargs[0]
        skip = set(_PER_RIDE_KEYS) | {"strategy_fn", "iq_kp"}
        self._shared = {k: v for k, v in sample.items() if k not in skip}

        # Simulator, jitted with static strategy_step_fn.
        def _one(
            w_ring0, w_carrier0, i_actual0, e_cap0, w_ring_base0,
            brake_ticks, grade_ticks, pedal_active_ticks,
            mass_kg, cruise_mps, inv_j_wheel,
            # Perturbation-threaded overrides (per-B scalars):
            eta_gear, t_drag_coeff, r_phase, r_phase_15,
            flux_linkage, vesc_current_gain, vcap_gain, cap_esr,
            rpm_noise_sigma, iq_noise_sigma, iq_bias, vcap_noise_sigma,
            mu_ratio,
            noise_key,
            strategy_step_fn, strategy_state0, iq_kp,
        ):
            shared = dict(self._shared)
            shared.update(dict(
                eta_gear=eta_gear, t_drag_coeff=t_drag_coeff,
                r_phase=r_phase, r_phase_15=r_phase_15,
                flux_linkage=flux_linkage,
                vesc_current_gain=vesc_current_gain,
                vcap_gain=vcap_gain, cap_esr=cap_esr,
                rpm_noise_sigma=rpm_noise_sigma,
                iq_noise_sigma=iq_noise_sigma,
                iq_bias=iq_bias,
                vcap_noise_sigma=vcap_noise_sigma,
                mu_ratio=mu_ratio,
            ))
            return simulate_ride_strategy_jax(
                w_ring0=w_ring0, w_carrier0=w_carrier0,
                i_actual0=i_actual0, e_cap0=e_cap0,
                w_ring_base0=w_ring_base0,
                brake_ticks=brake_ticks, grade_ticks=grade_ticks,
                pedal_active_ticks=pedal_active_ticks,
                mass_kg=mass_kg, cruise_mps=cruise_mps,
                inv_j_wheel=inv_j_wheel,
                strategy_fn=None,
                strategy_step_fn=strategy_step_fn,
                strategy_state0=strategy_state0,
                iq_kp=iq_kp,
                noise_key=noise_key,
                **shared,
            )

        # vmap over B (leading axis on stacked / per-pert / noise_key).
        n_per_ride = len(_PER_RIDE_KEYS)        # 11
        n_pert     = 13                          # incl. mu_ratio, r_phase_15
        in_axes = (*(0,) * n_per_ride,
                   *(0,) * n_pert,
                   0,          # noise_key
                   None, None, None)  # static: step_fn / state0 / iq_kp
        self._batched = jax.jit(
            jax.vmap(_one, in_axes=in_axes),
            static_argnames=("strategy_step_fn",),
        )

        # Motor-off cache: keyed by (seed, n_samples) tuple.
        self._off_cache: dict = {}

    # ------------------------------------------------------------
    def _build_B(self, n_samples: int, seed: int):
        """Return (perts_list, B, batched_pert_arrays, stacked_pos_args,
        noise_keys).

        B = R*(n_samples+1); index 0 in the per-pert axis is the nominal.
        """
        rng = np.random.default_rng(seed)
        perts = _sample_perturbations(rng, n_samples)
        nominal_p = {name: nominal for name, nominal, _, _ in UNCERTAIN_PARAMS}
        all_p = [nominal_p] + perts    # length M+1

        overrides = _pert_to_batched_overrides(all_p)    # [M+1] arrays

        R = self._R
        Mp1 = len(all_p)
        B = R * Mp1

        # Broadcast per-ride arrays [R, ...] → [R, M+1, ...] then flatten.
        per_ride_B = {}
        for k, arr in self._per_ride_stacked.items():
            expanded = np.repeat(arr[:, None], Mp1, axis=1)
            per_ride_B[k] = expanded.reshape((B,) + arr.shape[1:])

        # Per-pert arrays [M+1] → [R, M+1] → [B].
        per_pert_B = {
            k: np.tile(arr, R) for k, arr in overrides.items()
        }

        # Noise keys per-B.  Derive from seed + position so they're
        # deterministic and unique.
        keys = jax.random.split(jax.random.PRNGKey(seed & 0xFFFFFFFF), B)

        pos_args = [per_ride_B[k] for k in _PER_RIDE_KEYS]
        pert_order = ("eta_gear", "t_drag_coeff", "r_phase", "r_phase_15",
                      "flux_linkage", "vesc_current_gain", "vcap_gain",
                      "cap_esr", "rpm_noise_sigma", "iq_noise_sigma",
                      "iq_bias", "vcap_noise_sigma", "mu_ratio")
        pert_args = [per_pert_B[k] for k in pert_order]

        return perts, all_p, B, pos_args, pert_args, keys

    # ------------------------------------------------------------
    def _motor_off(self, pos_args, pert_args, keys):
        # Zero-k fixed_ff → motor off.
        off_step, off_s0, _ = build_step_fn(
            "fixed_ff", dict(k=0.0), dt_ctrl=CTRL_PERIOD)
        logs = self._batched(*pos_args, *pert_args, keys,
                             off_step, off_s0, 0.0)
        logs["speed"].block_until_ready()
        return logs["speed"]

    # ------------------------------------------------------------
    def score(self, strategy_key: str, params: dict, *,
              n_samples: int, seed: int) -> dict:
        """Run one trial's robust sweep.

        Returns a dict with the same fields the numpy
        ``score_strategy_robust`` produces (nominal, mean, std, p5, p95,
        cvar10, cvar20, scores, capture_mean, fidelity_mean).
        """
        perts, all_p, B, pos_args, pert_args, keys = self._build_B(
            n_samples, seed)
        R, Mp1 = self._R, n_samples + 1

        step_fn, state0, iq_kp = build_step_fn(
            strategy_key, params, dt_ctrl=CTRL_PERIOD)

        on_logs = self._batched(*pos_args, *pert_args, keys,
                                step_fn, state0, float(iq_kp))
        on_logs["speed"].block_until_ready()

        # Per-(ride,pert) score: broadcast brake_mask and n_valid
        # across the M+1 axis → shape [B].
        brake_mask_B = jnp.asarray(
            np.repeat(self._brake_mask[:, None], Mp1, axis=1)
              .reshape(B, -1),
            dtype=jnp.bool_,
        )
        n_valid_B = jnp.asarray(
            np.tile(self._n_valid[:, None], (1, Mp1)).reshape(B),
            dtype=jnp.int32,
        )
        eff_B, feel_B, comp_B = score_rides_jax(
            t=on_logs["t"], speed_on=on_logs["speed"],
            speed_base=on_logs["speed_baseline"],
            p_elec=on_logs["p_elec"],
            p_copper=on_logs["p_copper"],
            p_brake=on_logs["p_brake"],
            brake_demand=on_logs["brake_demand"],
            brake_mask=brake_mask_B, n_valid=n_valid_B,
        )
        comp_np = np.asarray(comp_B).reshape(R, Mp1)
        eff_np = np.asarray(eff_B).reshape(R, Mp1)
        feel_np = np.asarray(feel_B).reshape(R, Mp1)

        # Composite per-perturbation = mean over rides.
        comp_per_p = comp_np.mean(axis=0)   # [M+1]
        eff_per_p = eff_np.mean(axis=0)
        feel_per_p = feel_np.mean(axis=0)

        nominal = float(comp_per_p[0])
        perturbed = comp_per_p[1:]

        def _cvar(arr, alpha):
            if len(arr) == 0:
                return nominal
            n = max(1, int(np.ceil(alpha * len(arr))))
            return float(np.mean(np.sort(arr)[:n]))

        if len(perturbed):
            mean_p = float(np.mean(perturbed))
            std_p  = float(np.std(perturbed))
            p5     = float(np.percentile(perturbed, 5))
            p95    = float(np.percentile(perturbed, 95))
            cv10   = _cvar(perturbed, 0.10)
            cv20   = _cvar(perturbed, 0.20)
            eff_m  = float(np.mean(eff_per_p[1:]))
            feel_m = float(np.mean(feel_per_p[1:]))
        else:
            mean_p = std_p = cv10 = cv20 = nominal
            p5 = p95 = nominal
            eff_m = float(eff_per_p[0])
            feel_m = float(feel_per_p[0])

        return dict(
            nominal=nominal,
            mean=mean_p, std=std_p,
            p5=p5, p95=p95,
            cvar10=cv10, cvar20=cv20,
            scores=perturbed,
            efficiency_mean=eff_m,
            feel_mean=feel_m,
            capture_mean=eff_m,
            fidelity_mean=feel_m,
        )
