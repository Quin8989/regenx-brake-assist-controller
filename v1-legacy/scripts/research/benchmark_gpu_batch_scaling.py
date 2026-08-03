"""Measure jit(vmap(simulate)) throughput as a function of batch width B.

PySR with population=N evaluates N candidates per generation.  The
GPU amortization question is: for a fixed candidate, how does the
wall-clock time of one vmap call scale with B = n_rides * n_pert?

Answers the practical question: "given my population budget, how many
rides × perturbations should I push through each eval?"

Run with the current default backend.  Compare runs on CPU (Windows
venv) vs GPU (WSL .venv-linux).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import jax
import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "firmware"))

from sim.ride_generator import generate_ride, PROFILES          # noqa: E402
from sim.scoring import _sample_perturbations, UNCERTAIN_PARAMS   # noqa: E402
from sim.jax.population import PopulationEvaluator                 # noqa: E402


PYSR_EQ = ("relu(0.1 + 0.0005 * drpm_peak_neg + 0.02 * k_prev) "
           "+ step(rpm - 100) * 0.05")


def make_rides(n_per_profile: int) -> list:
    rides = []
    for i, (name, prof) in enumerate(PROFILES.items()):
        for k in range(n_per_profile):
            rides.append(generate_ride(prof, seed=1000 * i + k + 1,
                                       duration=60.0))
    return rides


def main():
    print(f"[jax] backend={jax.default_backend()}  "
          f"devices={jax.devices()}")
    print()

    # Fixture sweep: (n_rides_per_profile, n_perts) -> B
    configs = [
        (2,   3),    # B =   32
        (2,   6),    # B =   56
        (5,   6),    # B =  140  (rung-1)
        (5,  12),    # B =  260
        (10, 12),    # B =  520
        (10, 25),    # B = 1000
        (20, 25),    # B = 2000
        (20, 50),    # B = 4000
    ]

    nominal_p = {n: nom for n, nom, _, _ in UNCERTAIN_PARAMS}

    print(f"{'B':>5}  {'rides':>5}  {'perts':>5}  "
          f"{'compile_ms':>10}  {'hot_ms':>8}  {'per_traj_us':>11}")
    print("-" * 60)

    for n_per_prof, n_pert in configs:
        rides = make_rides(n_per_prof)
        rng   = np.random.default_rng(42)
        perts = [nominal_p] + _sample_perturbations(rng, n_pert - 1)

        ev = PopulationEvaluator(rides, perts)
        B = ev.batch_width

        # First call: mostly compile.
        t0 = time.perf_counter()
        _ = ev.evaluate(PYSR_EQ)
        t_compile = time.perf_counter() - t0

        # Hot calls: median of 3.
        hot = []
        for _ in range(3):
            t0 = time.perf_counter()
            _ = ev.evaluate(PYSR_EQ)
            hot.append(time.perf_counter() - t0)
        t_hot = float(np.median(hot))
        per_traj_us = 1e6 * t_hot / B

        print(f"{B:>5}  {len(rides):>5}  {n_pert:>5}  "
              f"{t_compile*1000:>10.0f}  {t_hot*1000:>8.0f}  "
              f"{per_traj_us:>11.1f}")

        # Release to avoid holding N evaluators in device memory.
        del ev


if __name__ == "__main__":
    main()
