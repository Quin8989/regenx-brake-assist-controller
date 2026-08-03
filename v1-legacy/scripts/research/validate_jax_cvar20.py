"""Stage B6a — validate JAX CVaR-20 scoring vs numpy sim.scoring.

End-to-end parity: pick one PySR-shaped expression, evaluate its
robust score (20 rides × 21 perturbations → cvar20) via both paths
and compare nominal composite + cvar20 + mean.

Also demos the compile cache: running the same expression twice
should skip the ~5 s jit compile on the second call.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import jax
import numpy as np

jax.config.update("jax_enable_x64", True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "firmware"))

from sim.ride_generator import generate_ride_set
from sim.scoring import (
    score_strategy_robust, _sample_perturbations, UNCERTAIN_PARAMS,
)
from sim.jax.pysr_driver import CandidateEvaluator
from scripts.research.benchmark_jax_cvar20 import _NumpyPysr, PYSR_EQ


def main():
    # Rides: same 20 used everywhere else.
    rides = generate_ride_set(seeds_per_profile=5, base_seed=0,
                              duration=60.0)
    # Perturbations: nominal + 20 sampled, seed 42 (matches
    # score_strategy_robust default).
    rng = np.random.default_rng(42)
    nominal_p = {name: nom for name, nom, _, _ in UNCERTAIN_PARAMS}
    perts = [nominal_p] + _sample_perturbations(rng, 20)

    # ── numpy reference ──
    print("[numpy] score_strategy_robust ...")
    t0 = time.perf_counter()
    np_res = score_strategy_robust(
        strat_cls=_NumpyPysr, rides=rides, n_samples=20, seed=42,
    )
    t_np = time.perf_counter() - t0
    print(f"  nominal={np_res['nominal']:.3f}  mean={np_res['mean']:.3f}"
          f"  cvar20={np_res['cvar20']:.3f}"
          f"  ({t_np:.1f} s)")

    # ── JAX ──
    print("[jax] CandidateEvaluator construct (off-baseline compile) ...")
    t0 = time.perf_counter()
    ev = CandidateEvaluator(rides, perts, seed_base=0xB6B6)
    t_ctor = time.perf_counter() - t0
    print(f"  ctor (motor-off compile+run): {t_ctor:.2f} s")

    print("[jax] evaluate #1 (fresh compile) ...")
    t0 = time.perf_counter()
    r1 = ev.evaluate(PYSR_EQ)
    t1 = time.perf_counter() - t0
    print(f"  nominal={r1['nominal']:.3f}  mean={r1['mean']:.3f}"
          f"  cvar20={r1['cvar20']:.3f}"
          f"  wall={t1*1000:.0f} ms  (sim={r1['t_sim_s']*1000:.0f} ms,"
          f" score={r1['t_score_s']*1000:.0f} ms)")

    print("[jax] evaluate #2 (cache hit, same expression) ...")
    t0 = time.perf_counter()
    r2 = ev.evaluate(PYSR_EQ)
    t2 = time.perf_counter() - t0
    print(f"  wall={t2*1000:.0f} ms  (sim={r2['t_sim_s']*1000:.0f} ms)")

    # Compare.
    d_nom = abs(r1['nominal'] - np_res['nominal'])
    d_mean = abs(r1['mean']    - np_res['mean'])
    d_cvar = abs(r1['cvar20']  - np_res['cvar20'])
    speedup = t_np / t1
    print("\n── parity ──")
    print(f"  nominal gap:  {d_nom:.3f}   target ≤ 1.5")
    print(f"  mean    gap:  {d_mean:.3f}   target ≤ 1.5")
    print(f"  cvar20  gap:  {d_cvar:.3f}   target ≤ 2.0")
    print(f"  cache speedup (#2 vs #1): {t1/t2:.1f}x"
          f"  ({t1*1000:.0f} → {t2*1000:.0f} ms)")
    print(f"  JAX vs numpy full wall: {speedup:.1f}x"
          f"  ({t_np:.1f} s → {t1:.2f} s)")

    # cvar20 / composites are bounded in [0, 100]; a 1.5 point
    # absolute gap is <2% of the dynamic range.  Gap comes from:
    #   (i) numpy uses Philox RNG / JAX uses Threefry (different
    #       noise realizations even with matched sigmas)
    #   (ii) brake_mask tick-granularity vs log-timestamp mask (≤10 ms)
    #   (iii) j_carrier / foc_tau / mu_s/mu_k / telem_delay not
    #        perturbed on the JAX path (would require retrace)
    # PySR ranking only uses *relative* cvar20 differences so this
    # tolerance is well within usable range.
    ok = d_nom < 1.5 and d_mean < 1.5 and d_cvar < 2.0
    # Cache must save substantial wall time on re-eval; 2×+ is the
    # practical target (first eval pays compile, second does not).
    ok = ok and (t1 - t2) > 1.5

    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
