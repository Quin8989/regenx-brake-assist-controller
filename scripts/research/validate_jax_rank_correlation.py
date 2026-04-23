"""Rank-correlation parity: if JAX and numpy preserve *ordering* of
candidate params, the JAX tuner can explore validly and numpy
validates the final pick.

Enumerate the aimd_ff param_grid (81 seeds), score each with both
backends, and report Spearman rank correlation.  > 0.9 means JAX is
usable as the exploration objective.
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

from sim.physics import CTRL_PERIOD, DT
from sim.ride_generator import generate_ride, PROFILES
from sim.scoring import score_rides
from sim.jax.physics_strategy import simulate_ride_strategy_jax
from sim.jax.tuner_strategies import build_step_fn
from sim.jax.scoring import score_rides_jax
from scripts.research.validate_jax_ride_strategy import build_jax_kwargs

import sim.physics as _sp
_orig = _sp._init_state


def _no_noise(*a, **kw):
    s = _orig(*a, **kw)
    s["noise_rng"] = None
    return s


_sp._init_state = _no_noise

from regen.strategies import AimdFfRegenStrategy


def main():
    # Use a small but diverse grid to keep runtime manageable.
    grid = [
        dict(k=k, beta_md=bmd, unlock_thresh=ut, k_ai=ka)
        for k in [0.08, 0.15, 0.28]
        for bmd in [0.04, 0.08, 0.14]
        for ut in [800.0, 1500.0, 3000.0]
        for ka in [0.005, 0.05]
    ]  # 54 candidates

    # 4 rides (one per profile).
    prof_list = list(PROFILES.items())
    rides = [generate_ride(prof, seed=100 * i + 1, duration=60.0)
             for i, (_n, prof) in enumerate(prof_list)]

    # Numpy scores.
    print(f"[numpy] scoring {len(grid)} candidates on {len(rides)} rides…")
    t0 = time.perf_counter()
    np_comps = []
    for p in grid:
        r = score_rides(
            lambda: AimdFfRegenStrategy(**p), rides,
            sim_kwargs=dict(rpm_noise_sigma=0.0, iq_noise_sigma=0.0,
                            iq_bias=0.0, vcap_noise_sigma=0.0),
        )
        np_comps.append(r.composite)
    t_np = time.perf_counter() - t0
    print(f"  done in {t_np:.1f}s ({t_np/len(grid)*1000:.0f} ms/candidate)")

    # JAX scores: build jit once, reuse.
    ctrl_steps = max(1, int(CTRL_PERIOD / DT))
    n_ticks_padded = max(r.n // ctrl_steps for r in rides)

    def _noop(*_):
        return jnp.asarray(0.0)

    per_ride_kwargs = []
    n_valid = []
    brake_masks = []
    for r in rides:
        kw, n_ride = build_jax_kwargs(r, n_ticks_padded=n_ticks_padded,
                                       strategy_fn=_noop)
        per_ride_kwargs.append(kw)
        n_valid.append(n_ride)
        brake_masks.append(np.asarray(kw["brake_ticks"]) > 0.0)

    jit_fn = jax.jit(
        simulate_ride_strategy_jax,
        static_argnames=("strategy_fn", "strategy_step_fn",
                         "delay_slots", "n_ticks", "ctrl_steps", "dt"),
    )

    # Motor-off baseline (shared across candidates).
    off_step, off_s0, _ = build_step_fn("fixed_ff", dict(k=0.0),
                                         dt_ctrl=CTRL_PERIOD)
    off_logs = []
    for kw in per_ride_kwargs:
        okw = dict(kw)
        okw["strategy_fn"] = None
        okw["strategy_step_fn"] = off_step
        okw["strategy_state0"] = off_s0
        okw["iq_kp"] = 0.0
        off_logs.append(jit_fn(**okw))
    speed_off = jnp.stack([l["speed"] for l in off_logs])

    n_valid_j = jnp.asarray(n_valid, dtype=jnp.int32)
    brake_mask_j = jnp.asarray(np.stack(brake_masks, axis=0), dtype=jnp.bool_)

    print(f"\n[jax] scoring {len(grid)} candidates…")
    t0 = time.perf_counter()
    jx_comps = []
    for p in grid:
        step_fn, state0, _ = build_step_fn("aimd_ff", p, dt_ctrl=CTRL_PERIOD)
        on_logs = []
        for kw in per_ride_kwargs:
            ckw = dict(kw)
            ckw["strategy_fn"] = None
            ckw["strategy_step_fn"] = step_fn
            ckw["strategy_state0"] = state0
            ckw["iq_kp"] = 0.0
            on_logs.append(jit_fn(**ckw))

        def _stack(key):
            return jnp.stack([l[key] for l in on_logs])

        eff_B, feel_B, comp_B = score_rides_jax(
            t=_stack("t"), speed_on=_stack("speed"),
            speed_base=_stack("speed_baseline"),
            p_elec=_stack("p_elec"),
            p_copper=_stack("p_copper"),
            p_brake=_stack("p_brake"),
            brake_demand=_stack("brake_demand"),
            brake_mask=brake_mask_j, n_valid=n_valid_j,
        )
        jx_comps.append(float(np.mean(np.asarray(comp_B))))
    t_jx = time.perf_counter() - t0
    print(f"  done in {t_jx:.1f}s ({t_jx/len(grid)*1000:.0f} ms/candidate)")

    np_comps = np.asarray(np_comps)
    jx_comps = np.asarray(jx_comps)

    # Spearman via numpy (ranks).
    np_ranks = np.argsort(np.argsort(np_comps))
    jx_ranks = np.argsort(np.argsort(jx_comps))
    d = np_ranks - jx_ranks
    n = len(grid)
    spearman = 1.0 - 6.0 * float(np.sum(d * d)) / (n * (n * n - 1))

    abs_diff = np.abs(np_comps - jx_comps)
    print("\n── composite parity ──")
    print(f"  n candidates: {n}")
    print(f"  numpy range:  [{np_comps.min():.1f}, {np_comps.max():.1f}]  "
          f"(best idx {int(np.argmax(np_comps))})")
    print(f"  jax   range:  [{jx_comps.min():.1f}, {jx_comps.max():.1f}]  "
          f"(best idx {int(np.argmax(jx_comps))})")
    print(f"  |Δ| mean: {abs_diff.mean():.2f}  "
          f"p50: {np.median(abs_diff):.2f}  "
          f"p95: {np.percentile(abs_diff, 95):.2f}  "
          f"max: {abs_diff.max():.2f}")
    print(f"  Spearman ρ:  {spearman:.3f}  (target > 0.9)")

    # Best candidates agreement.
    top_k = 5
    np_top = set(np.argsort(-np_comps)[:top_k])
    jx_top = set(np.argsort(-jx_comps)[:top_k])
    overlap = len(np_top & jx_top)
    print(f"  top-{top_k} overlap: {overlap}/{top_k}")

    # numpy's best under JAX score, JAX's best under numpy score.
    np_best_idx = int(np.argmax(np_comps))
    jx_best_idx = int(np.argmax(jx_comps))
    np_best_np = np_comps[np_best_idx]
    np_best_jx = jx_comps[np_best_idx]
    jx_best_np = np_comps[jx_best_idx]
    jx_best_jx = jx_comps[jx_best_idx]
    print(f"  numpy-best: np={np_best_np:.2f}  jx={np_best_jx:.2f}  "
          f"(diff {np_best_np - np_best_jx:+.2f})")
    print(f"  jax-best:   np={jx_best_np:.2f}  jx={jx_best_jx:.2f}  "
          f"(regret at numpy: {np_best_np - jx_best_np:+.2f})")

    # PASS: JAX tuner is usable if
    #   (i) Spearman > 0.9, and
    #   (ii) numpy-regret of jax-best is < 2 pts (i.e., picking the
    #        JAX optimum costs less than 2 composite points vs picking
    #        the true numpy optimum).
    ok = spearman > 0.9 and (np_best_np - jx_best_np) < 2.0
    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
