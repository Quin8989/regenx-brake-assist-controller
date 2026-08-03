"""Measure the persistent compile-cache + fast-math impact on cold
Python starts.  Run as:

    python scripts/benchmark_jax_startup.py [fp64|fp32] [--clear]

Clears the cache dir when --clear given, then runs the B6a ctor
(which triggers the motor-off baseline compile — the slowest jit
on the hot path).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("fp64", "fp32"), default="fp64",
                    nargs="?")
parser.add_argument("--clear", action="store_true",
                    help="Wipe .jax_cache before running (cold start)")
args = parser.parse_args()

os.environ["JAX_ENABLE_X64"] = "1" if args.mode == "fp64" else "0"

_REPO_ROOT = Path(__file__).resolve().parents[2]
cache_dir = _REPO_ROOT / ".jax_cache"
if args.clear and cache_dir.exists():
    shutil.rmtree(cache_dir)
    print(f"[clear] wiped {cache_dir}")

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "firmware"))

from sim.jax.env import summary  # noqa: E402
print("[jax]", summary())

import numpy as np  # noqa: E402
from sim.ride_generator import generate_ride_set  # noqa: E402
from sim.scoring import _sample_perturbations, UNCERTAIN_PARAMS  # noqa: E402
from sim.jax.pysr_driver import CandidateEvaluator  # noqa: E402
from scripts.research.benchmark_jax_cvar20 import PYSR_EQ  # noqa: E402


def main():
    rides = generate_ride_set(seeds_per_profile=5, base_seed=0,
                              duration=60.0)
    rng = np.random.default_rng(42)
    nominal_p = {name: nom for name, nom, _, _ in UNCERTAIN_PARAMS}
    perts = [nominal_p] + _sample_perturbations(rng, 20)

    t0 = time.perf_counter()
    ev = CandidateEvaluator(rides, perts, seed_base=0xB6B6)
    t_ctor = time.perf_counter() - t0
    print(f"ctor (motor-off jit):  {t_ctor*1000:6.0f} ms")

    t0 = time.perf_counter()
    r1 = ev.evaluate(PYSR_EQ)
    t1 = time.perf_counter() - t0
    print(f"eval #1 (on-path jit): {t1*1000:6.0f} ms  "
          f"(cvar20={r1['cvar20']:.2f})")

    t0 = time.perf_counter()
    _ = ev.evaluate(PYSR_EQ)
    t2 = time.perf_counter() - t0
    print(f"eval #2 (cache hit):   {t2*1000:6.0f} ms")

    # Inspect cache contents.
    if cache_dir.exists():
        entries = list(cache_dir.rglob("*"))
        size = sum(f.stat().st_size for f in entries if f.is_file())
        print(f"[cache] {len(entries)} entries, "
              f"{size/1024/1024:.1f} MB on disk")


if __name__ == "__main__":
    main()
