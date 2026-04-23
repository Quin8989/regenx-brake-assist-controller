"""Benchmark: measure JAX trial throughput after the traced-params
refactor.  Target: >> 4 s/trial (current numba+cache rate).

Runs the JAX objective on aimd_ff over many random param draws on the
same rides, reusing the JIT cache.  Reports compile vs hot times.
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
from sim.jax.physics_strategy import simulate_ride_strategy_jax
from sim.jax.tuner_strategies import build_step_fn
from sim.jax.scoring import score_rides_jax
from scripts.research.validate_jax_ride_strategy import build_jax_kwargs


def _noop(*_):
    return jnp.asarray(0.0)


def main():
    # 8 rides (2 per profile) - matches typical screen basket size.
    prof_list = list(PROFILES.items())
    rides = []
    for i, (_n, prof) in enumerate(prof_list):
        for k in range(2):
            rides.append(generate_ride(prof, seed=100 * i + k + 1,
                                        duration=60.0))

    ctrl_steps = max(1, int(CTRL_PERIOD / DT))
    n_ticks_padded = max(r.n // ctrl_steps for r in rides)

    # Pre-build per-ride kwargs (traced inputs, same across trials).
    per_ride_kwargs = []
    n_valid = []
    brake_masks = []
    for r in rides:
        kw, n_ride = build_jax_kwargs(r, n_ticks_padded=n_ticks_padded,
                                       strategy_fn=_noop)
        per_ride_kwargs.append(kw)
        n_valid.append(n_ride)
        brake_masks.append(np.asarray(kw["brake_ticks"]) > 0.0)

    n_valid_j = jnp.asarray(n_valid, dtype=jnp.int32)
    brake_mask_j = jnp.asarray(np.stack(brake_masks, axis=0), dtype=jnp.bool_)

    # Stack per-ride kwargs into batched arrays for vmap.
    stacked_keys = ("w_ring0", "w_carrier0", "i_actual0", "e_cap0",
                    "w_ring_base0", "brake_ticks", "grade_ticks",
                    "pedal_active_ticks", "mass_kg", "cruise_mps",
                    "inv_j_wheel")
    stacked = {k: jnp.stack([jnp.asarray(kw[k]) for kw in per_ride_kwargs])
               for k in stacked_keys}
    shared = {k: v for k, v in per_ride_kwargs[0].items()
              if k not in stacked_keys
              and k not in ("strategy_fn", "iq_kp")}

    def _one(w_ring0, w_carrier0, i_actual0, e_cap0, w_ring_base0,
             brake_ticks, grade_ticks, pedal_active_ticks,
             mass_kg, cruise_mps, inv_j_wheel,
             strategy_step_fn, strategy_state0, iq_kp):
        return simulate_ride_strategy_jax(
            w_ring0=w_ring0, w_carrier0=w_carrier0, i_actual0=i_actual0,
            e_cap0=e_cap0, w_ring_base0=w_ring_base0,
            brake_ticks=brake_ticks, grade_ticks=grade_ticks,
            pedal_active_ticks=pedal_active_ticks,
            mass_kg=mass_kg, cruise_mps=cruise_mps,
            inv_j_wheel=inv_j_wheel,
            strategy_fn=None,
            strategy_step_fn=strategy_step_fn,
            strategy_state0=strategy_state0,
            iq_kp=iq_kp, **shared,
        )

    batched = jax.jit(
        jax.vmap(_one, in_axes=(
            0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            None, None, None,
        )),
        static_argnames=("strategy_step_fn",),
    )

    # Precompute motor-off baseline once (shared across all candidates).
    off_step, off_s0, _ = build_step_fn("fixed_ff", dict(k=0.0),
                                         dt_ctrl=CTRL_PERIOD)
    t0 = time.perf_counter()
    off_logs = batched(*[stacked[k] for k in stacked_keys],
                       off_step, off_s0, 0.0)
    off_logs["speed"].block_until_ready()
    t_off = time.perf_counter() - t0
    speed_off = off_logs["speed"]
    print(f"motor-off compile+run: {t_off*1000:.0f} ms")

    # Candidate draws for aimd_ff.
    rng = np.random.default_rng(0)
    n_trials = 30
    params_list = []
    for _ in range(n_trials):
        params_list.append(dict(
            k=float(rng.uniform(0.05, 0.5)),
            beta_md=float(rng.uniform(0.02, 0.20)),
            unlock_thresh=float(rng.uniform(500.0, 4000.0)),
            k_ai=float(rng.uniform(0.001, 0.30)),
        ))

    # First trial pays compile cost for aimd_ff's step_fn; rest are hot.
    times = []
    for i, p in enumerate(params_list):
        step, s0, iqkp = build_step_fn("aimd_ff", p, dt_ctrl=CTRL_PERIOD)
        t0 = time.perf_counter()
        on_logs = batched(*[stacked[k] for k in stacked_keys],
                          step, s0, iqkp)
        on_logs["speed"].block_until_ready()

        eff_B, feel_B, comp_B = score_rides_jax(
            t=on_logs["t"], speed_on=on_logs["speed"],
            speed_base=on_logs["speed_baseline"],
            p_elec=on_logs["p_elec"],
            p_copper=on_logs["p_copper"],
            p_brake=on_logs["p_brake"],
            brake_demand=on_logs["brake_demand"],
            brake_mask=brake_mask_j, n_valid=n_valid_j,
        )
        score = float(np.mean(np.asarray(comp_B)))
        dt = time.perf_counter() - t0
        times.append(dt)
        tag = "compile" if i == 0 else f"hot #{i}"
        print(f"  trial {i:2d}  [{tag:>7}]  {dt*1000:7.1f} ms  score={score:.2f}")

    t_compile = times[0]
    t_hot = np.mean(times[1:])
    print(f"\ncompile trial: {t_compile*1000:.0f} ms")
    print(f"hot mean:      {t_hot*1000:.0f} ms  (over {len(times)-1} trials)")
    print(f"hot p50:       {np.median(times[1:])*1000:.0f} ms")
    print(f"hot p95:       {np.percentile(times[1:], 95)*1000:.0f} ms")

    # For 300 trials × 3 strategies × 3 seeds on the 8-ride screen:
    # total hot trials ≈ 300*3*3 = 2700; compile 9 times.
    est_total_s = (9 * t_compile) + (2700 - 9) * t_hot + (20 * 60 * 0.1)
    print(f"\nOvernight estimate (3 strategies × 3 seeds × 300 trials):")
    print(f"  ≈ {est_total_s:.0f} s  ({est_total_s/60:.1f} min)")


if __name__ == "__main__":
    main()
