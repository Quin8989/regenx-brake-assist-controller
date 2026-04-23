"""Option-2 A/B test: does JAX at fp32 produce usable parity?

Strategy: re-run the B6a cvar20 parity vs numpy, but with
``jax_enable_x64`` disabled.  Compare nominal/mean/cvar20 gaps and
wall time against the fp64 path.

Pass criteria: cvar20 gap < 3.0 (vs 2.0 for fp64), nominal gap < 3.0.
Numerical stability of the 6000-tick fori_loop is the real question.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Disable fp64 BEFORE any jax import.
import os
os.environ["JAX_ENABLE_X64"] = "0"

import jax
import jax.numpy as jnp
import numpy as np

# Double-check we are in fp32.
assert not jax.config.jax_enable_x64, "fp64 leaked in"
print(f"[cfg] x64={jax.config.jax_enable_x64}  backend={jax.default_backend()}")

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "firmware"))

# Monkey-patch every sim.physics_jax* module's top-level x64 toggle
# before importing them (they each call jax.config.update at import).
import sim  # noqa
import importlib

# Force fp32 through the import chain by patching the config update.
_orig_update = jax.config.update
def _patched_update(name, value):
    if name == "jax_enable_x64":
        return  # ignore
    return _orig_update(name, value)
jax.config.update = _patched_update

from sim.ride_generator import generate_ride_set
from sim.scoring import score_strategy_robust, _sample_perturbations, UNCERTAIN_PARAMS
from sim.jax.pysr_driver import CandidateEvaluator
from scripts.research.benchmark_jax_cvar20 import _NumpyPysr, PYSR_EQ

# Restore.
jax.config.update = _orig_update
print(f"[post-import] x64={jax.config.jax_enable_x64}")


def main():
    rides = generate_ride_set(seeds_per_profile=5, base_seed=0,
                              duration=60.0)
    rng = np.random.default_rng(42)
    nominal_p = {name: nom for name, nom, _, _ in UNCERTAIN_PARAMS}
    perts = [nominal_p] + _sample_perturbations(rng, 20)

    print("[numpy] ...")
    t0 = time.perf_counter()
    np_res = score_strategy_robust(
        strat_cls=_NumpyPysr, rides=rides, n_samples=20, seed=42,
    )
    t_np = time.perf_counter() - t0
    print(f"  nominal={np_res['nominal']:.3f}  mean={np_res['mean']:.3f}"
          f"  cvar20={np_res['cvar20']:.3f}  ({t_np:.1f} s)")

    print("[jax fp32] ctor ...")
    t0 = time.perf_counter()
    ev = CandidateEvaluator(rides, perts, seed_base=0xB6B6)
    t_ctor = time.perf_counter() - t0
    print(f"  ctor: {t_ctor:.2f} s")

    print("[jax fp32] eval #1 ...")
    t0 = time.perf_counter()
    r1 = ev.evaluate(PYSR_EQ)
    t1 = time.perf_counter() - t0
    print(f"  nominal={r1['nominal']:.3f}  mean={r1['mean']:.3f}"
          f"  cvar20={r1['cvar20']:.3f}  wall={t1*1000:.0f} ms")

    print("[jax fp32] eval #2 (cache) ...")
    t0 = time.perf_counter()
    r2 = ev.evaluate(PYSR_EQ)
    t2 = time.perf_counter() - t0
    print(f"  wall={t2*1000:.0f} ms")

    d_nom = abs(r1['nominal'] - np_res['nominal'])
    d_mean = abs(r1['mean']   - np_res['mean'])
    d_cvar = abs(r1['cvar20'] - np_res['cvar20'])
    print("\n── fp32 parity ──")
    print(f"  nominal gap: {d_nom:.3f}")
    print(f"  mean    gap: {d_mean:.3f}")
    print(f"  cvar20  gap: {d_cvar:.3f}")
    print(f"  fp32 hot:    {t2*1000:.0f} ms  (fp64 was ~2200 ms)")
    print(f"  vs numpy:    {t_np/t1:.1f}x")

    ok_parity = d_nom < 3.0 and d_mean < 3.0 and d_cvar < 3.0
    print("\n" + ("PARITY OK" if ok_parity else "PARITY FAIL"))
    return 0 if ok_parity else 1


if __name__ == "__main__":
    sys.exit(main())
