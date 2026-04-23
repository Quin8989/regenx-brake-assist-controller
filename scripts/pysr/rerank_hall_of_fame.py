"""Re-rank a PySR Pareto front with the CVaR-20 composite scorer.

PySR's training loss is MSE against AIMD's decision; the real
question is whether any of its discovered expressions *beat* AIMD on
our canonical scoring surface.  This script:

1.  Loads a ``hall_of_fame.csv`` (complexity / loss / equation).
2.  Builds a :class:`sim.jax.population.PopulationEvaluator` over a
    wide batch (default 40 rides × 25 perts = 1000 trajectories —
    solidly past the GPU crossover at B≈800).
3.  Scores every equation through the evaluator.
4.  Writes a leaderboard sorted by CVaR-20 composite.

The result is a fast, GPU-capable version of
``scripts.pysr.validate_candidates`` (which uses numpy + sim.scoring
and takes minutes per candidate).  Same candidate set, ~20× faster on
GPU at B=1000.

Usage (Windows CPU):
    python -m scripts.pysr.rerank_hall_of_fame

Usage (WSL GPU):
    bash scripts/wsl_run_pysr_rerank.sh
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import jax
import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "firmware"))

from sim.ride_generator import generate_ride, PROFILES                # noqa: E402
from sim.scoring import _sample_perturbations, UNCERTAIN_PARAMS       # noqa: E402
from sim.jax.population import PopulationEvaluator                    # noqa: E402


DEFAULT_HOF = (_REPO_ROOT / "sim" / "output" / "pysr" / "imitate_aimd"
               / "hall_of_fame.csv")


def build_rides(n_per_profile: int, duration: float = 60.0):
    rides = []
    for i, (_, prof) in enumerate(PROFILES.items()):
        for k in range(n_per_profile):
            rides.append(generate_ride(prof, seed=1000 * i + k + 1,
                                       duration=duration))
    return rides


def build_perturbations(n: int, seed: int = 42):
    nominal = {name: nom for name, nom, _, _ in UNCERTAIN_PARAMS}
    rng = np.random.default_rng(seed)
    return [nominal] + _sample_perturbations(rng, n - 1)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hall-of-fame", type=Path, default=DEFAULT_HOF,
                   help="Path to PySR hall_of_fame.csv")
    p.add_argument("--rides-per-profile", type=int, default=10,
                   help="Rides per profile (4 profiles × N)")
    p.add_argument("--perts", type=int, default=25,
                   help="Perturbation count including nominal")
    p.add_argument("--output", type=Path, default=None,
                   help="Output leaderboard CSV "
                        "(default: beside hall-of-fame)")
    p.add_argument("--top", type=int, default=0,
                   help="Only score the first K rows of the HOF "
                        "(0 = all)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    print(f"[jax] backend={jax.default_backend()}  "
          f"devices={jax.devices()}")

    hof_path = args.hall_of_fame
    if not hof_path.exists():
        print(f"ERROR: hall of fame not found: {hof_path}")
        return 2

    hof = pd.read_csv(hof_path)
    if args.top > 0:
        hof = hof.head(args.top).copy()
    if "Equation" not in hof.columns and "equation" in hof.columns:
        hof = hof.rename(columns={"equation": "Equation",
                                  "complexity": "Complexity",
                                  "loss": "Loss"})
    expressions = hof["Equation"].tolist()
    print(f"[rerank] {len(expressions)} candidates from {hof_path}")

    t0 = time.perf_counter()
    rides = build_rides(args.rides_per_profile)
    perts = build_perturbations(args.perts)
    print(f"[rerank] fixture: {len(rides)} rides × "
          f"{len(perts)} perts = B={len(rides) * len(perts)}")
    print(f"[rerank] fixture built in {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    ev = PopulationEvaluator(rides, perts)
    print(f"[rerank] motor-off baseline compiled in "
          f"{time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    results, timing = ev.evaluate_population(expressions)
    wall = time.perf_counter() - t0
    print()
    print(timing.summary())
    print(f"  wall (outer): {wall*1000:.0f} ms")
    print()

    # Build leaderboard.
    rows = []
    for row, res in zip(hof.to_dict(orient="records"), results):
        rows.append({
            "complexity": row.get("Complexity", np.nan),
            "imitation_loss": row.get("Loss", np.nan),
            "cvar20": res["cvar20"],
            "nominal": res["nominal"],
            "mean": res["mean"],
            "std": res["std"],
            "equation": res["expression"],
        })
    leaderboard = pd.DataFrame(rows).sort_values(
        "cvar20", ascending=False).reset_index(drop=True)

    out_path = args.output or hof_path.with_name(
        hof_path.stem + "_rerank.csv")
    leaderboard.to_csv(out_path, index=False)

    print("--- leaderboard (top 10 by CVaR-20 composite) ---")
    with pd.option_context("display.max_colwidth", 100,
                           "display.width", 200,
                           "display.float_format", "{:.3f}".format):
        print(leaderboard.head(10).to_string(index=False))
    print()
    print(f"[rerank] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
