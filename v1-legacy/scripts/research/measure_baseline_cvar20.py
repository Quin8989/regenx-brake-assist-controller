"""Measure current baseline cvar20 for strategy-invention bake-off.

Reports cvar20 composite (and sub-dims) for every strategy currently
registered in sim.strategies, using the *exact same* 20-ride x 21-pert
basket that `CandidateEvaluator` uses.  This fixes the bar that a
stateful-PySR candidate has to clear.

Run from repo root (Windows venv is fine — numpy+numba path):
    .venv\\Scripts\\python.exe scripts\\research\\measure_baseline_cvar20.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "firmware"))

import numpy as np

from config.settings import REGEN_STRATEGY_PARAMS  # noqa: E402
from sim.ride_generator import generate_ride_set  # noqa: E402
from sim.scoring import score_strategy_robust  # noqa: E402
from sim.strategies import STRATEGY_BY_NAME, DEFAULT_STRATEGY_NAMES  # noqa: E402


def _factory(cls, params):
    def make():
        return cls(**params)
    return make


def main():
    rides = generate_ride_set(seeds_per_profile=5, base_seed=0, duration=60.0)
    print(f"Basket: {len(rides)} rides (5 seeds x 4 profiles)")
    print(f"Perturbations: 20 + nominal = 21")
    print(f"Total trajectories: {len(rides) * 21}\n")

    strategies = list(DEFAULT_STRATEGY_NAMES)
    print(f"{'strategy':<18} {'nominal':>8} {'mean':>8} {'p5':>8} {'cvar20':>8} {'wall_s':>8}")
    print("-" * 68)

    rows = []
    for name in strategies:
        cls = STRATEGY_BY_NAME[name]
        params = REGEN_STRATEGY_PARAMS.get(name, {})
        t0 = time.perf_counter()
        r = score_strategy_robust(
            strategy_factory=_factory(cls, params),
            rides=rides,
            n_samples=20,
            seed=42,
            workers=8,
        )
        wall = time.perf_counter() - t0
        rows.append((name, r))
        print(f"{name:<18} {r['nominal']:8.2f} {r['mean']:8.2f} "
              f"{r['p5']:8.2f} {r['cvar20']:8.2f} {wall:8.1f}")

    # Identify champion.
    champ = max(rows, key=lambda kv: kv[1]["cvar20"])
    print("\n" + "=" * 68)
    print(f"Champion: {champ[0]} with cvar20 = {champ[1]['cvar20']:.2f}")
    print(f"Bar to beat (champ + 2.0): cvar20 > {champ[1]['cvar20'] + 2.0:.2f}")


if __name__ == "__main__":
    main()
