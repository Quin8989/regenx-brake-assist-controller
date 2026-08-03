"""Invent a regen strategy from scratch via PySR with a composite-CVaR loss.

This is the "pure guess and check" path: no imitation target, no
structure prior.  PySR generates random symbolic expressions, and for
*each one* our JAX simulator runs a full B=rides*perts trajectory
ensemble and returns -CVaR20(composite) as the loss.  PySR tries to
minimize that number.

Mechanics
---------
PySR's `loss_function` parameter takes a snippet of **Julia** code.
That Julia code runs in the same process as this Python script (when
`parallelism="serial"`, required).  juliacall exposes any Python
object assigned to ``jl.<name>`` as a callable from Julia via
PythonCall.jl.  So we:

1. Build a `PopulationEvaluator` (holds JAX state, pre-sampled rides
   & perturbations, compile cache).
2. Register a Python scorer on the Julia side:
   ``jl.regenx_score = _score_expression``.
3. Hand PySR a Julia loss function that converts each candidate tree
   to a string via ``string_tree`` and calls ``regenx_score(s)``.
4. PySR's mutation/crossover then drives towards higher CVaR-20.

Serial parallelism is mandatory: the GPU / JAX state cannot be
duplicated across Julia worker processes.  One candidate at a time is
fine because each eval costs ~200 ms at B=1000 on GPU and PySR's
per-candidate overhead is negligible next to that.

Runtime budget
--------------
A rough costing, assuming 200 ms per unique expression on GPU and
80% cache-hit rate on repeated constants-only mutations:
  niterations=20, populations=15, population_size=33
  ≈ 10k candidate evals × 0.04 s effective (with cache) ≈ 7 min
  ≈ 10k candidate evals × 0.20 s cold            ≈ 33 min

Try a short shakedown run first: ``--niterations 2 --populations 4``.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

# Python auto-inserts the script's directory at sys.path[0]
# (``.../scripts/``).  Our repo has ``scripts/pysr/__init__.py`` which
# would shadow the installed ``pysr`` package.  Drop it before any
# pysr import happens.
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", _SCRIPT_DIR)]

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "firmware"))

# Import PySR *before* anything touches PopulationEvaluator — the
# evaluator's lazy import chain inserts ``scripts/pysr/`` into
# sys.path, which would shadow the installed ``pysr`` package.
from pysr import PySRRegressor  # noqa: E402,F401
import sympy as _sympy          # noqa: E402

from sim.ride_generator import generate_ride, PROFILES                # noqa: E402
from sim.scoring import _sample_perturbations, UNCERTAIN_PARAMS       # noqa: E402
from sim.jax.population import PopulationEvaluator                    # noqa: E402
from sim.jax.physics_strategy import FEATURE_NAMES                    # noqa: E402


# =====================================================================
#  Fixture
# =====================================================================

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


# =====================================================================
#  Python-side scorer (called from Julia)
# =====================================================================

# Populated by main() before PySR starts.
_EVALUATOR: PopulationEvaluator | None = None
_CALLS = {"total": 0, "cache_hits": 0, "failed": 0}
_BEST = {"cvar20": -math.inf, "expr": None, "t0": 0.0}


def _score_expression(expr_str: str) -> float:
    """Called from the Julia loss function.  Returns -CVaR20 (PySR minimizes).

    Failures (sympify errors, inf/nan trajectories) return a large
    finite penalty so PySR can still compute gradients / rankings.
    """
    global _EVALUATOR, _CALLS, _BEST
    assert _EVALUATOR is not None, "evaluator not initialised"
    _CALLS["total"] += 1

    # Cache hit = PySR re-visited the same string (e.g. constant
    # optimisation pass).
    if expr_str in _EVALUATOR._cache:
        _CALLS["cache_hits"] += 1

    try:
        res = _EVALUATOR.evaluate(expr_str)
    except Exception:
        _CALLS["failed"] += 1
        return 1e6

    cvar = float(res["cvar20"])
    if not math.isfinite(cvar):
        _CALLS["failed"] += 1
        return 1e6

    if cvar > _BEST["cvar20"]:
        _BEST["cvar20"] = cvar
        _BEST["expr"] = expr_str
        elapsed = time.perf_counter() - _BEST["t0"]
        print(f"  [invent] +{elapsed:6.1f}s  new best cvar20={cvar:6.2f}  "
              f"{expr_str}")

    # PySR minimises; we want to maximise CVaR20.  Encode as negative.
    return -cvar


# =====================================================================
#  PySR plumbing
# =====================================================================

JULIA_LOSS = r"""
function invent_loss(tree, dataset::Dataset{T,L}, options)::L where {T,L}
    local s
    try
        s = string_tree(tree, options)
    catch e
        return L(1.0e6)
    end
    loss_f = 1.0e6
    try
        loss_py = Main.regenx_score(s)
        loss_f = pyconvert(Float64, loss_py)
    catch e
        return L(1.0e6)
    end
    if !isfinite(loss_f)
        return L(1.0e6)
    end
    return L(loss_f)
end
"""


def build_regressor(args, output_dir: Path):
    return PySRRegressor(
        # Search budget ------------------------------------------------
        niterations=args.niterations,
        populations=args.populations,
        population_size=args.population_size,
        ncycles_per_iteration=args.ncycles,

        # Expression shape --------------------------------------------
        # Goal: small, firmware-friendly closed forms.  Today's best
        # human form was complexity 10, so `maxsize=25` leaves room
        # for a ~2× larger compound without wasting search time on
        # huge trees.
        maxsize=args.maxsize,
        maxdepth=10,
        binary_operators=["+", "-", "*", "/", "min", "max"],
        unary_operators=[
            "relu(x::T) where {T} = x > 0 ? x : zero(T)",
            "negrelu(x::T) where {T} = x < 0 ? -x : zero(T)",
            # Hard threshold — gates on/off decisions.
            "step(x::T) where {T} = x > 0 ? one(T) : zero(T)",
            # Smooth saturating gate — differentiable twin of `step`;
            # PySR's constant optimizer converges much better on
            # tanh-shaped transitions than on Heaviside ones.
            "tanh(x::T) where {T} = tanh(x)",
            # Exponential / logarithmic scaling.  `safeexp` clips the
            # argument to [-30, 30] so candidates can't overflow and
            # poison the constant optimizer.  `safelog` uses |x|+eps
            # so sign-flipped candidates don't NaN mid-search.
            "safeexp(x::T) where {T} = exp(clamp(x, T(-30), T(30)))",
            "safelog(x::T) where {T} = log(abs(x) + T(1e-9))",
        ],
        extra_sympy_mappings={
            "relu":    lambda x: (_sympy.Abs(x) + x) / 2,
            "negrelu": lambda x: (_sympy.Abs(x) - x) / 2,
            "step":    lambda x: _sympy.Heaviside(x),
            "tanh":    lambda x: _sympy.tanh(x),
            "min":     lambda a, b: _sympy.Min(a, b),
            "max":     lambda a, b: _sympy.Max(a, b),
            "safeexp": lambda x: _sympy.exp(x),
            "safelog": lambda x: _sympy.log(_sympy.Abs(x) + 1e-9),
        },
        # Per-operator complexity weights.  Default is 1 per node.
        # We bias the search toward firmware-cheap forms:
        #   +, -, *              : 1  (free on Cortex-M)
        #   min, max             : 1  (single branch / cmov)
        #   relu, negrelu, step  : 1  (trivial branch)
        #   /                    : 2  (FPU divide, or expensive fallback)
        #   tanh                 : 3  (lookup or rational approx)
        #   safeexp, safelog     : 4  (transcendentals, LUT-scale cost)
        # Also penalize CONSTANTS lightly (2) so PySR prefers feature
        # terms over pure-constant padding.
        complexity_of_operators={
            "/":       2,
            "tanh":    3,
            "safeexp": 4,
            "safelog": 4,
        },
        complexity_of_constants=2,
        # Disallow nested unary ops (``relu(relu(x))``, ``tanh(step(x))``):
        # they rarely help and balloon the eval cost.  Transcendentals
        # get zero nesting with anything (incl. each other) — a single
        # `safeexp(a*x + b)` or `tanh(...)` is always enough.
        nested_constraints={
            "relu":    {"relu": 0, "negrelu": 0, "step": 0, "tanh": 0,
                        "safeexp": 0, "safelog": 0},
            "negrelu": {"relu": 0, "negrelu": 0, "step": 0, "tanh": 0,
                        "safeexp": 0, "safelog": 0},
            "step":    {"relu": 0, "negrelu": 0, "step": 0, "tanh": 0,
                        "safeexp": 0, "safelog": 0},
            "tanh":    {"relu": 0, "negrelu": 0, "step": 0, "tanh": 0,
                        "safeexp": 0, "safelog": 0},
            "safeexp": {"relu": 0, "negrelu": 0, "step": 0, "tanh": 0,
                        "safeexp": 0, "safelog": 0},
            "safelog": {"relu": 0, "negrelu": 0, "step": 0, "tanh": 0,
                        "safeexp": 0, "safelog": 0},
        },
        # Divide: restrict divisor complexity so PySR can't build
        # ``1 / (huge subexpression)`` pathologies.  Same for safelog —
        # an 8-node argument to log is almost certainly noise.
        constraints={
            "/":       (-1, 6),
            "safeexp": 8,
            "safelog": 8,
            "tanh":    8,
        },

        # Loss ---------------------------------------------------------
        loss_function=JULIA_LOSS,
        loss_scale="linear",    # our loss goes negative (-CVaR20)
        parsimony=args.parsimony,
        adaptive_parsimony_scaling=20.0,
        # Slight bump on constant-optimization frequency — our winners
        # are heavily constant-tuned (e.g. ``-3.88e-6 * drpm_mean``).
        weight_optimize=0.10,
        # Start search with small expressions, grow the maxsize budget
        # over the first half of iterations.  Classic PySR best-practice.
        warmup_maxsize_by=0.5,

        # Parallelism --------------------------------------------------
        # MUST be serial: Python callback accesses shared JAX state.
        procs=0,
        parallelism="serial",
        deterministic=True,
        random_state=args.seed,

        # Output -------------------------------------------------------
        output_directory=str(output_dir),
        run_id="invent_composite",
        progress=False,
        verbosity=1,
        model_selection="best",
    )


# =====================================================================
#  Entry point
# =====================================================================

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rides-per-profile", type=int, default=2,
                   help="Rides per profile (4 profiles × N). Lower = "
                        "faster per-eval, less signal.")
    p.add_argument("--perts", type=int, default=5,
                   help="Perturbations including nominal.")
    p.add_argument("--niterations", type=int, default=2,
                   help="PySR outer iterations (shakedown default=2).")
    p.add_argument("--populations", type=int, default=4)
    p.add_argument("--population-size", type=int, default=33)
    p.add_argument("--ncycles", type=int, default=200,
                   help="ncycles_per_iteration — inner mutation count.")
    p.add_argument("--maxsize", type=int, default=35)
    p.add_argument("--parsimony", type=float, default=0.0005)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--output-dir", type=Path,
                   default=_REPO_ROOT / "sim" / "output" / "pysr")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    print("=" * 64)
    print("  PySR composite-loss invention run")
    print("=" * 64)

    t0 = time.perf_counter()
    rides = build_rides(args.rides_per_profile)
    perts = build_perturbations(args.perts)
    B = len(rides) * len(perts)
    print(f"  fixture: {len(rides)} rides × {len(perts)} perts = "
          f"B={B} trajectories per candidate")

    global _EVALUATOR
    _EVALUATOR = PopulationEvaluator(rides, perts)
    print(f"  evaluator ready in {time.perf_counter() - t0:.1f}s")

    # Sanity call: make sure the scorer works end-to-end before Julia
    # gets involved.
    _BEST["t0"] = time.perf_counter()
    sanity = _score_expression("k_prev")
    print(f"  sanity score for 'k_prev': {-sanity:.2f} (CVaR20)")
    # Reset best tracker so the Julia callbacks "see" from 0.
    _BEST["t0"] = time.perf_counter()
    _BEST["cvar20"] = -math.inf
    _BEST["expr"] = None
    _CALLS["total"] = 0
    _CALLS["cache_hits"] = 0
    _CALLS["failed"] = 0

    # Expose the scorer to Julia global scope.
    from juliacall import Main as jl
    jl.regenx_score = _score_expression

    # Build regressor and a dummy dataset.  Our loss ignores X/y
    # entirely — PySR just needs *something* with the right shape so
    # the Dataset constructor is happy.
    model = build_regressor(args, args.output_dir)
    n_dummy = 64
    rng = np.random.default_rng(args.seed)
    X_dummy = rng.standard_normal((n_dummy, len(FEATURE_NAMES)))
    y_dummy = np.zeros(n_dummy)

    print()
    print(f"  starting PySR search (loss=composite CVaR-20 on B={B})")
    print(f"  budget: {args.niterations} iterations × "
          f"{args.populations} populations × pop_size "
          f"{args.population_size}")
    print()

    t_search = time.perf_counter()
    model.fit(X_dummy, y_dummy, variable_names=list(FEATURE_NAMES))
    t_search = time.perf_counter() - t_search

    print()
    print(f"  search done in {t_search:.1f}s  "
          f"({_CALLS['total']} candidates, "
          f"{_CALLS['cache_hits']} cache hits, "
          f"{_CALLS['failed']} failed)")
    print()

    eqs = model.equations_
    if eqs is None or (hasattr(eqs, "empty") and eqs.empty):
        print("  no equations returned.")
        return 1

    # Re-rank the Pareto front by our actual loss (not PySR's reported
    # loss, which should agree but let's be explicit).
    rows = []
    for _, row in eqs.iterrows():
        expr = str(row["equation"])
        try:
            res = _EVALUATOR.evaluate(expr)
            rows.append({
                "complexity": row["complexity"],
                "pysr_loss": row["loss"],
                "cvar20": res["cvar20"],
                "nominal": res["nominal"],
                "mean": res["mean"],
                "std": res["std"],
                "equation": expr,
            })
        except Exception as exc:
            print(f"  skip {expr!r}: {exc!r}")

    board = pd.DataFrame(rows).sort_values(
        "cvar20", ascending=False).reset_index(drop=True)

    out_csv = args.output_dir / "invent_composite" / "leaderboard.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    board.to_csv(out_csv, index=False)
    print("--- invention leaderboard (top 10 by CVaR-20) ---")
    with pd.option_context("display.max_colwidth", 100,
                           "display.width", 200,
                           "display.float_format", "{:.3f}".format):
        print(board.head(10).to_string(index=False))
    print()
    print(f"  wrote {out_csv}")
    print(f"  best overall: cvar20={_BEST['cvar20']:.2f}  "
          f"{_BEST['expr']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
