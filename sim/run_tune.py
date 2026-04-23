"""sim.run_tune - Unified DE tuning pipeline for any registered strategy.

Single official tuning entry point.

Pipeline:
  Phase 1 (screen):  DE with top-4 scenarios × 3-mass grid → fast.
  Phase 2 (refine):  Full 10-scenario × 6-mass eval on DE best.
  Phase 3 (robust):  Monte-Carlo robustness check (optional --robust).

Usage examples:
    python -m sim.run_tune --strategies pi_controller
    python -m sim.run_tune --strategies pi_controller,aimd_ff --maxiter 30 --popsize 15
    python -m sim.run_tune --strategies aimd_ff --robust
"""

from __future__ import annotations

import argparse
import csv
import gc
import json
import multiprocessing as mp
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import numpy as np
from scipy.optimize import differential_evolution, minimize

try:
    import cma as _cma  # type: ignore
    _HAS_CMA = True
except ImportError:
    _cma = None
    _HAS_CMA = False

from .ride_generator import generate_ride_set
from .scoring import (
    precompute_motor_off_logs,
    score_rides,
    score_strategy_robust,
)
from .strategies import DEFAULT_STRATEGY_NAMES, STRATEGY_BY_NAME, parse_strategy_names

DEFAULT_STRATEGIES = list(DEFAULT_STRATEGY_NAMES)

# ── Ride-set sizing ─────────────────────────────────────────────────
# DE/CMA screen: a smaller basket (8 rides) to keep inner-loop cost low.
# Full / polish / final scoring: the canonical 20-ride basket
# (5 seeds × 4 profiles) that the robustness table is built around.
SCREEN_SEEDS_PER_PROFILE = 2
FULL_SEEDS_PER_PROFILE   = 5


# =====================================================================
#  Worker pool helpers
# =====================================================================

def _pool_worker_init():
    """Silence stdout/stderr in pool workers to keep VS Code's pty clean.

    Python-level redirect is sufficient; do NOT touch OS-level file
    descriptors (os.dup2 on fd 1/2 crashes VS Code's ConPTY host).
    """
    devnull = open(os.devnull, "w")         # noqa: SIM115
    sys.stdout = devnull
    sys.stderr = devnull


# =====================================================================
#  Helpers
# =====================================================================

class _Objective:
    """Pickle-safe objective callable for scipy parallel workers.

    When *sample_schedule* is provided and the objective mode is robust,
    the objective tracks a *per-worker* local evaluation count and uses
    it to pick how many Monte-Carlo samples to run per call.  This lets
    early generations use a cheap estimate and late generations use the
    full robust sample count, roughly halving DE wall-clock for the same
    final quality.

    Each worker process has its own ``_local_evals`` counter (no shared
    state — ``mp.Value`` is incompatible with Windows spawn-pool
    pickling).  Schedule thresholds are therefore expressed in
    *per-worker* evaluations: divide the global threshold by n_workers.
    """

    def __init__(
        self,
        strat_cls,
        names,
        int_flags,
        rides,
        motor_off_logs=None,
        mode="nominal",
        robust_samples=8,
        robust_seed=42,
        robust_lambda=0.5,
        sample_schedule=None,
    ):
        self.strat_cls = strat_cls
        self.names = names
        self.int_flags = int_flags
        self.rides = rides
        # Cached motor-off reference trajectories (nominal physics only
        # — robust objective recomputes them per-perturbation).
        self.motor_off_logs = motor_off_logs
        self.mode = mode
        self.robust_samples = robust_samples
        self.robust_seed = robust_seed
        self.robust_lambda = robust_lambda
        self.sample_schedule = sample_schedule  # list[(per_worker_threshold, n)]
        self._local_evals = 0

    def _samples_for_this_eval(self):
        if not self.sample_schedule:
            return self.robust_samples
        idx = self._local_evals
        self._local_evals += 1
        n = self.robust_samples
        for thr, val in self.sample_schedule:
            if idx < thr:
                n = val
                break
        return max(1, int(n))

    def __call__(self, x):
        params = _vec_to_params(x, self.names, self.int_flags)
        if self.mode == "nominal":
            result = score_rides(
                lambda: self.strat_cls(**params),
                self.rides,
                motor_off_logs=self.motor_off_logs,
            )
            return -result.composite

        n_samples = self._samples_for_this_eval()
        robust = score_strategy_robust(
            strategy_factory=None,
            rides=self.rides,
            n_samples=n_samples,
            seed=self.robust_seed,
            workers=1,
            strat_cls=self.strat_cls,
            strat_params=params,
        )

        if self.mode == "robust_mean":
            score = robust["mean"]
        elif self.mode == "robust_p5":
            score = robust["p5"]
        elif self.mode == "robust_mean_std":
            score = robust["mean"] - self.robust_lambda * robust["std"]
        elif self.mode == "robust_cvar10":
            score = robust["cvar10"]
        elif self.mode == "robust_cvar20":
            score = robust["cvar20"]
        else:
            raise ValueError(f"Unsupported objective mode: {self.mode}")
        return -score


def _build_bounds(strat_cls):
    """Infer DE bounds and integer flags from param_grid()."""
    grid = strat_cls.param_grid()
    if not grid:
        raise ValueError(f"{strat_cls.key}: empty param_grid()")

    keys = list(grid[0].keys())
    bounds = []
    is_int = []
    defaults = strat_cls().__dict__

    for key in keys:
        values = [row[key] for row in grid]
        lo, hi = float(min(values)), float(max(values))
        bounds.append((lo, hi))
        all_int = all(float(v).is_integer() for v in values)
        default_is_int = isinstance(defaults.get(key), int)
        is_int.append(all_int or default_is_int)

    return keys, bounds, is_int


def _vec_to_params(x, names, int_flags):
    params = {}
    for n, v, is_i in zip(names, x, int_flags):
        params[n] = int(round(float(v))) if is_i else float(v)
    return params


def _dims_of(score_result):
    """Return (energy, linearity) from a RideSetScore."""
    return score_result.energy, score_result.linearity


# =====================================================================
#  Per-strategy pipeline
# =====================================================================

def _full_nominal_objective(x, strat_cls, names, int_flags, rides,
                            motor_off_logs=None):
    """Full-evaluation nominal objective (used only when objective=nominal)."""
    params = _vec_to_params(x, names, int_flags)
    result = score_rides(lambda: strat_cls(**params), rides,
                         motor_off_logs=motor_off_logs)
    return -result.composite


def _make_polish_objective(strat_cls, names, int_flags, *,
                           rides, motor_off_logs,
                           objective_mode, robust_samples,
                           robust_seed, robust_lambda):
    """Build a polish objective that matches the DE objective mode.

    Always runs on the full 20-ride basket so the polish is comparing
    against the same criterion used in the final report.
    """
    obj = _Objective(
        strat_cls, names, int_flags,
        rides=rides,
        motor_off_logs=motor_off_logs,
        mode=objective_mode,
        robust_samples=robust_samples,
        robust_seed=robust_seed,
        robust_lambda=robust_lambda,
    )
    return obj


def _grid_warm_start_init(strat_cls, names, bounds, popsize, pool,
                          log_fn, rides, motor_off_logs):
    """Screen param_grid() nominally on the screen ride set; top-N as init.

    Cheap one-shot: evaluates every row in ``param_grid()`` on the screen
    rides without robust sampling.  Sorts by score and takes the top
    ``popsize*D`` rows as scipy's DE initial population.
    """
    grid = strat_cls.param_grid()
    if not grid:
        return None

    needed = popsize * len(names)
    int_flags = [False] * len(names)  # not used during warm screen

    # Score every grid point on the cheap screen objective.
    screen_obj = _Objective(
        strat_cls, names, int_flags,
        rides=rides, motor_off_logs=motor_off_logs,
        mode="nominal",
    )
    vecs = np.array([[row[k] for k in names] for row in grid], dtype=float)
    t0 = time.time()
    try:
        scores = list(pool.map(screen_obj, vecs))
    except Exception:
        scores = [screen_obj(v) for v in vecs]
    scores_arr = np.asarray(scores, dtype=float)
    order = np.argsort(scores_arr)  # ascending = best first (objective is negated)
    best = vecs[order[:needed]]

    # Clip to bounds + add tiny jitter to avoid DE deduplication rejects.
    lo = np.array([b[0] for b in bounds])
    hi = np.array([b[1] for b in bounds])
    rng = np.random.default_rng(0)
    best = np.clip(best, lo, hi)
    jitter = 1e-4 * (hi - lo)
    best = np.clip(best + jitter * rng.standard_normal(best.shape), lo, hi)

    # Backfill if grid had too few distinct points.
    if best.shape[0] < needed:
        n_extra = needed - best.shape[0]
        u = rng.random((n_extra, len(names)))
        extra = lo + u * (hi - lo)
        best = np.vstack([best, extra])

    log_fn(f"  warm-start: evaluated {len(grid)} grid points in "
           f"{time.time() - t0:.1f}s, best nominal={-float(min(scores_arr)):.2f}")
    return best


def _run_cma(strat_cls, names, bounds, int_flags, objective,
             maxiter, popsize, seed, log_fn, x0=None, pool=None):
    """Run CMA-ES as the DE replacement; returns a DE-like result dict.

    CMA-ES typically needs 2-4x fewer evaluations than DE on smooth,
    continuous 4D landscapes with correlated parameters.  It respects
    bounds via clipping; integer params are rounded by _vec_to_params.
    """
    if not _HAS_CMA:
        raise RuntimeError("CMA-ES requested but 'cma' package not installed. "
                           "Run: pip install cma")
    lo = np.array([b[0] for b in bounds])
    hi = np.array([b[1] for b in bounds])
    if x0 is None:
        x0 = 0.5 * (lo + hi)
    x0 = np.clip(np.asarray(x0, dtype=float), lo, hi)
    sigma0 = float(np.mean((hi - lo) / 6.0))

    opts = {
        "bounds": [lo.tolist(), hi.tolist()],
        "popsize": int(popsize),
        "maxiter": int(maxiter),
        "seed": int(seed),
        "verbose": -9,
        "verb_log": 0,
        "verb_disp": 0,
        "tolfun": 1e-3,
    }
    es = _cma.CMAEvolutionStrategy(x0.tolist(), sigma0, opts)  # type: ignore[union-attr]
    nit = 0
    nfev = 0
    while not es.stop():
        xs = es.ask()
        xs_np = [np.asarray(x, dtype=float) for x in xs]
        if pool is not None:
            fs = list(pool.map(objective, xs_np))
        else:
            fs = [objective(x) for x in xs_np]
        es.tell(xs, fs)
        nit += 1
        nfev += len(xs)
        params = _vec_to_params(es.result.xbest, names, int_flags)
        pstr = ", ".join(f"{k}={v}" for k, v in params.items())
        log_fn(f"[{strat_cls.key}] cma gen {nit:3d}/{maxiter}  "
               f"best={-es.result.fbest:.4f}  {pstr}")

    from types import SimpleNamespace
    return SimpleNamespace(
        x=np.asarray(es.result.xbest, dtype=float),
        fun=float(es.result.fbest),
        nit=int(nit),
        nfev=int(nfev),
        success=bool(es.result.xbest is not None),
        message="CMA-ES finished",
    )


def _tune_one(
    strat_cls,
    *,
    maxiter,
    popsize,
    pool,
    seed,
    log_fn,
    screen_rides,
    full_rides,
    screen_off_logs,
    full_off_logs,
    objective_mode,
    objective_robust_samples,
    objective_robust_lambda,
    polish,
    polish_maxiter,
    optimizer="de",
    warm_start=True,
    adaptive_samples=True,
    n_workers=1,
):
    """Screen-DE/CMA → full-eval for one strategy class."""
    strategy_name = strat_cls.key
    names, bounds, int_flags = _build_bounds(strat_cls)

    # Adaptive robust-sample schedule.  Use a *per-worker* counter so
    # it survives pool.map pickling on Windows.  We halve samples for
    # the first 40% of the expected evaluation budget, then ramp to
    # full.  Global threshold is divided by n_workers because each
    # worker only sees 1/n of the evals.
    sample_schedule = None
    if adaptive_samples and objective_mode != "nominal" and objective_robust_samples >= 4:
        total_evals = popsize * len(names) * max(1, maxiter)
        per_worker_threshold = max(1, int(0.4 * total_evals / max(1, n_workers)))
        half = max(1, objective_robust_samples // 2)
        sample_schedule = [
            (per_worker_threshold, half),
            (10**12,               objective_robust_samples),
        ]
        log_fn(f"  adaptive robust samples: {half} for first "
               f"~{int(0.4 * total_evals)} evals (aggregate), then {objective_robust_samples}")

    # Phase 1: DE/CMA with screen config
    objective = _Objective(
        strat_cls, names, int_flags,
        rides=screen_rides,
        motor_off_logs=screen_off_logs,
        mode=objective_mode,
        robust_samples=objective_robust_samples,
        robust_seed=seed,
        robust_lambda=objective_robust_lambda,
        sample_schedule=sample_schedule,
    )

    gen_counter = [0]

    def callback(xk, convergence):
        gen_counter[0] += 1
        # Log to file only — terminal output during DE causes VS Code freezes
        params = _vec_to_params(xk, names, int_flags)
        pstr = ", ".join(f"{k}={v}" for k, v in params.items())
        log_fn(
            f"[{strategy_name}] gen {gen_counter[0]:3d}/{maxiter}  "
            f"conv={convergence:.4f}  {pstr}",
        )

    # Warm-start: evaluate param_grid() on cheap nominal screen and seed
    # DE/CMA with the top-(popsize*D) points.
    init = None
    if warm_start:
        init = _grid_warm_start_init(
            strat_cls, names, bounds, popsize, pool, log_fn,
            rides=screen_rides, motor_off_logs=screen_off_logs,
        )

    t0 = time.time()

    if optimizer == "cma":
        x0 = None if init is None else init[0]  # CMA uses single mean
        result = _run_cma(
            strat_cls, names, bounds, int_flags, objective,
            maxiter=maxiter, popsize=popsize, seed=seed,
            log_fn=log_fn, x0=x0, pool=pool,
        )
    else:
        de_kwargs: dict = dict(
            bounds=bounds,
            seed=seed,
            maxiter=maxiter,
            popsize=popsize,
            tol=1e-3,
            mutation=(0.5, 1.5),
            recombination=0.8,
            workers=pool.map,
            updating="deferred",
            callback=callback,
            polish=False,
        )
        if init is not None:
            de_kwargs["init"] = init
        result = differential_evolution(objective, **de_kwargs)
    screen_elapsed = time.time() - t0

    best_params = _vec_to_params(result.x, names, int_flags)
    screen_score = float(-result.fun)

    polish_used = False
    polish_improved = False
    polish_nit = 0
    polish_score = None

    if polish:
        # Local Powell search on the FULL 20-ride basket with the same
        # objective mode as DE.  Powell is strictly serial, but each
        # iter evaluates the robust objective whose MC samples are
        # routed through the shared pool so all cores stay busy.
        def polish_obj(x):
            params = _vec_to_params(x, names, int_flags)
            if objective_mode == "nominal":
                res = score_rides(
                    lambda: strat_cls(**params),
                    full_rides,
                    motor_off_logs=full_off_logs,
                )
                return -res.composite
            robust = score_strategy_robust(
                strategy_factory=None,
                rides=full_rides,
                n_samples=objective_robust_samples,
                seed=seed,
                pool=pool,               # parallel MC samples
                strat_cls=strat_cls,
                strat_params=params,
            )
            if objective_mode == "robust_mean":
                val = robust["mean"]
            elif objective_mode == "robust_p5":
                val = robust["p5"]
            elif objective_mode == "robust_mean_std":
                val = robust["mean"] - objective_robust_lambda * robust["std"]
            elif objective_mode == "robust_cvar10":
                val = robust["cvar10"]
            elif objective_mode == "robust_cvar20":
                val = robust["cvar20"]
            else:
                raise ValueError(f"Unsupported objective mode: {objective_mode}")
            return -val

        p0 = np.array(result.x, dtype=float)
        pol = minimize(
            polish_obj,
            x0=p0,
            method="Powell",
            bounds=bounds,
            options={"maxiter": int(polish_maxiter), "disp": False},
        )
        polish_used = True
        polish_nit = int(getattr(pol, "nit", 0) or 0)
        polish_score = float(-pol.fun)

        # Compare pre/post on the SAME objective the polish optimised for.
        pre_val  = float(-polish_obj(np.array(result.x, dtype=float)))
        post_val = float(-pol.fun)
        if post_val > pre_val:
            best_params = _vec_to_params(pol.x, names, int_flags)
            polish_improved = True

    # Phase 2: full scoring on best params
    t1 = time.time()
    full = score_rides(lambda: strat_cls(**best_params), full_rides,
                       motor_off_logs=full_off_logs)
    full_elapsed = time.time() - t1

    e_avg, t_avg = _dims_of(full)
    total_elapsed = screen_elapsed + full_elapsed

    return {
        "strategy": strategy_name,
        "name": strat_cls(**best_params).name,
        "params": best_params,
        "seed": int(seed),
        "objective_mode": objective_mode,
        "screen_score": screen_score,
        "composite": float(full.composite),
        "energy": float(e_avg),
        "linearity": float(t_avg),
        "nfev": int(result.nfev),
        "nit": int(result.nit),
        "success": bool(result.success),
        "message": str(result.message),
        "elapsed_s": float(total_elapsed),
        "screen_elapsed_s": float(screen_elapsed),
        "param_names": names,
        "bounds": bounds,
        "polish_used": bool(polish_used),
        "polish_improved": bool(polish_improved),
        "polish_nit": int(polish_nit),
        "polish_score": polish_score,
    }


# =====================================================================
#  Artifacts
# =====================================================================

def _write_artifacts(base_dir: Path, meta: dict, rows: list[dict]):
    rows_sorted = sorted(rows, key=lambda r: r["composite"], reverse=True)

    with (base_dir / "results.json").open("w", encoding="utf-8") as f:
        json.dump({"meta": meta, "results": rows_sorted}, f, indent=2)

    with (base_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "rank", "strategy", "seed", "objective_mode", "composite",
            "energy", "linearity", "elapsed_s", "nfev",
            "success", "polish_used", "polish_improved", "params",
        ])
        for i, r in enumerate(rows_sorted, start=1):
            w.writerow([
                i, r["strategy"],
                r.get("seed", ""),
                r.get("objective_mode", "nominal"),
                f"{r['composite']:.3f}", f"{r['energy']:.3f}",
                f"{r['linearity']:.3f}",
                f"{r['elapsed_s']:.1f}", r["nfev"], r["success"],
                r.get("polish_used", False),
                r.get("polish_improved", False),
                json.dumps(r["params"], sort_keys=True),
            ])

    with (base_dir / "best_snippets.txt").open("w", encoding="utf-8") as f:
        f.write("# Tuned parameter snippets\n\n")
        for r in rows_sorted:
            pstr = ", ".join(f"{k}={v}" for k, v in r["params"].items())
            f.write(f"({r['strategy']}, dict({pstr})),\n")
            f.write(
                f"  # composite={r['composite']:.1f}  energy={r['energy']:.1f}"
                f"  linearity={r['linearity']:.1f}\n"
            )


def _write_stability_artifacts(base_dir: Path, rows: list[dict], seeds: list[int]):
    """Write cross-seed stability diagnostics for each strategy."""
    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_strategy[row["strategy"]].append(row)

    strategy_stats = {}
    for strategy_name, items in by_strategy.items():
        items_sorted = sorted(items, key=lambda r: r["composite"], reverse=True)
        scores = np.array([r["composite"] for r in items], dtype=float)
        strategy_stats[strategy_name] = {
            "n_runs": int(len(items)),
            "mean": float(np.mean(scores)),
            "std": float(np.std(scores)),
            "min": float(np.min(scores)),
            "max": float(np.max(scores)),
            "best_seed": int(items_sorted[0].get("seed", -1)),
            "best_composite": float(items_sorted[0]["composite"]),
            "best_params": items_sorted[0]["params"],
        }

    winners_by_seed: dict[int, str] = {}
    for seed in seeds:
        seed_rows = [r for r in rows if r.get("seed") == seed]
        if not seed_rows:
            continue
        winners_by_seed[int(seed)] = max(seed_rows, key=lambda r: r["composite"])["strategy"]

    winner_counts: dict[str, int] = defaultdict(int)
    for strategy_name in winners_by_seed.values():
        winner_counts[strategy_name] += 1

    with (base_dir / "stability.json").open("w", encoding="utf-8") as f:
        json.dump(
            {
                "seeds": seeds,
                "winners_by_seed": winners_by_seed,
                "winner_counts": dict(winner_counts),
                "strategy_stats": strategy_stats,
            },
            f,
            indent=2,
        )

    with (base_dir / "stability_summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "strategy", "n_runs", "mean", "std", "min", "max", "best_seed", "winner_count"
        ])
        for strategy_name in sorted(strategy_stats):
            s = strategy_stats[strategy_name]
            w.writerow([
                strategy_name,
                s["n_runs"],
                f"{s['mean']:.3f}",
                f"{s['std']:.3f}",
                f"{s['min']:.3f}",
                f"{s['max']:.3f}",
                s["best_seed"],
                winner_counts.get(strategy_name, 0),
            ])


def _parse_seeds(seed_text: str | None, seed_default: int) -> list[int]:
    """Parse comma-separated seeds; fall back to the single default seed."""
    if not seed_text:
        return [int(seed_default)]
    seeds = []
    for chunk in seed_text.split(","):
        c = chunk.strip()
        if not c:
            continue
        seeds.append(int(c))
    if not seeds:
        seeds = [int(seed_default)]
    return seeds


# =====================================================================
#  CLI
# =====================================================================

def _parse_args():
    p = argparse.ArgumentParser(
        description="Unified DE tuning for RegenX control strategies.",
    )
    p.add_argument(
        "--strategies",
        default=",".join(DEFAULT_STRATEGIES),
        help="Comma-separated strategy names (default: all registered)",
    )
    p.add_argument("--maxiter", type=int, default=25)
    p.add_argument("--popsize", type=int, default=12)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--seeds",
        type=str,
        default=None,
        help="Comma-separated seeds for multi-seed tuning (overrides --seed).",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=max(1, (os.cpu_count() or 2) // 2),
        help="Parallel workers (default: cpu_count//2 to leave headroom for VS Code)",
    )
    p.add_argument(
        "--robust-samples",
        type=int,
        default=60,
        help="Number of Monte-Carlo robustness samples (default: 60).",
    )
    p.add_argument(
        "--objective",
        type=str,
        default="robust_cvar20",
        choices=[
            "nominal", "robust_mean", "robust_p5",
            "robust_mean_std", "robust_cvar10", "robust_cvar20",
        ],
        help="Optimization objective used during DE screen phase. "
             "Default 'robust_cvar20' optimizes the mean of the worst 20%% "
             "of MC samples (Expected Shortfall), giving a tune that holds "
             "up in the tail of parameter uncertainty.  Use 'nominal' for "
             "fast iteration.",
    )
    p.add_argument(
        "--robust-objective-samples",
        type=int,
        default=6,
        help="MC samples per objective eval for robust objectives. "
             "6 samples × 10 scenarios × 6 masses ≈ 6× slower than nominal.",
    )
    p.add_argument(
        "--robust-objective-lambda",
        type=float,
        default=0.5,
        help="Lambda for robust_mean_std objective: mean - lambda*std.",
    )
    p.add_argument(
        "--no-polish",
        action="store_true",
        help="Disable local polish step on full nominal objective.",
    )
    p.add_argument(
        "--polish-maxiter",
        type=int,
        default=40,
        help="Maximum iterations for local polish step.",
    )
    p.add_argument(
        "--no-robust",
        action="store_true",
        help="Skip Monte-Carlo robustness check (faster, for quick iteration).",
    )
    p.add_argument(
        "--optimizer",
        choices=["de", "cma"],
        default="de",
        help="Global optimizer: 'de' (scipy differential_evolution, default) "
             "or 'cma' (CMA-ES from cma package). CMA typically needs 2-4x "
             "fewer evaluations on smooth continuous problems.",
    )
    p.add_argument(
        "--no-warm-start",
        action="store_true",
        help="Disable param_grid() warm-start of the initial population.",
    )
    p.add_argument(
        "--no-adaptive-samples",
        action="store_true",
        help="Disable adaptive ramp of robust_samples across generations.",
    )
    p.add_argument(
        "--resume",
        type=str,
        default=None,
        metavar="DIR",
        help="Resume from a previous run's output dir (e.g. "
             "sim/output/tune/20260422_074732). Reads checkpoint.json, "
             "skips (strategy, seed) pairs already completed, and "
             "appends new results to the same directory. The robustness "
             "phase also skips rows that already have robust_mean.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    strategy_names = parse_strategy_names(args.strategies)
    seeds = _parse_seeds(args.seeds, args.seed)

    # Lower process priority on Windows so VS Code's UI stays responsive
    if sys.platform == "win32":
        try:
            import ctypes
            BELOW_NORMAL = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
        except Exception:
            pass  # non-critical

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("sim") / "output" / "tune" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "run.log"
    ckpt_path = out_dir / "checkpoint.json"

    # ── Resume support: reuse a previous run's directory + checkpoint ──
    # We keep the original run_id (folder name) so summary.csv lands where
    # the user expects, and we preload `results` so the main loop and the
    # robustness phase can skip anything already on disk.
    resumed_results: list[dict] = []
    if args.resume:
        resume_dir = Path(args.resume)
        if not resume_dir.is_dir():
            raise SystemExit(f"--resume: directory not found: {resume_dir}")
        resume_ckpt = resume_dir / "checkpoint.json"
        if not resume_ckpt.is_file():
            raise SystemExit(
                f"--resume: checkpoint.json not found in {resume_dir}")
        with resume_ckpt.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        resumed_results = list(payload.get("completed", []))
        # Adopt the old folder's run_id so new log entries append there.
        out_dir = resume_dir
        run_id = payload.get("run_id", out_dir.name)
        log_path = out_dir / "run.log"
        ckpt_path = out_dir / "checkpoint.json"

    # Suppress Numba C-level JIT diagnostics that bypass sys.stdout.
    # This avoids needing os.dup2 which breaks VS Code's ConPTY.
    os.environ.setdefault("NUMBA_DISABLE_PERFORMANCE_WARNINGS", "1")

    # ── Single startup line; everything else goes to log file only
    #    to prevent VS Code terminal flooding during heavy compute. ──
    print(
        f"  Tuning {', '.join(strategy_names)}  "
        f"maxiter={args.maxiter} pop={args.popsize} "
        f"workers={args.workers} seeds={seeds}\n"
        f"  Objective mode: {args.objective}\n"
        f"  Uncertainty mode: full physics\n"
        f"  Log → {log_path.as_posix()}",
        flush=True,
    )

    _real_stdout = sys.stdout
    _real_stderr = sys.stderr

    t_all = time.time()
    results: list[dict] = list(resumed_results)
    # Fast-lookup set of (strategy, seed) pairs already on disk. The main
    # tuning loop below skips any pair in this set; the robustness phase
    # skips rows that already carry a robust_mean key.
    completed_pairs: set[tuple[str, int]] = {
        (r["strategy"], int(r.get("seed", args.seed))) for r in results
    }

    # ── One persistent pool for the entire run ──
    # Workers are spawned once, stay warm (numba JIT cached), and are
    # silenced via _pool_worker_init so nothing leaks to VS Code's pty.
    pool = mp.Pool(args.workers, initializer=_pool_worker_init)

    # Append to run.log on resume so we keep the full history in one file.
    log_mode = "a" if args.resume else "w"
    with log_path.open(log_mode, encoding="utf-8") as out_log:
        # Python-level redirect catches print(), warnings, and scipy output.
        # We intentionally do NOT use os.dup2 to redirect fd 1/2 — that
        # disconnects VS Code's ConPTY pipe and crashes the extension host.
        sys.stdout = out_log
        sys.stderr = out_log

        try:
            out_log.write("=" * 70 + "\n")
            out_log.write("  RegenX — Strategy Tuning (DE)\n")
            out_log.write("=" * 70 + "\n")
            out_log.write(f"  Strategies: {', '.join(strategy_names)}\n")
            out_log.write(f"  Maxiter:  {args.maxiter}\n")
            out_log.write(f"  Popsize:  {args.popsize}\n")
            out_log.write(f"  Workers:  {args.workers}\n")
            out_log.write(f"  Seeds:    {seeds}\n")
            out_log.write(f"  Objective mode: {args.objective}\n")
            out_log.write(
                f"  Robust objective samples/lambda: "
                f"{args.robust_objective_samples}/{args.robust_objective_lambda}\n"
            )
            out_log.write(
                f"  Polish:   {not args.no_polish}"
                f" (maxiter={args.polish_maxiter})\n"
            )
            out_log.write(f"  Robust:   {not args.no_robust}"
                          f" ({args.robust_samples} samples)\n")
            out_log.write(f"  Output:   {out_dir.as_posix()}\n\n")
            out_log.flush()

            def _log_file(line):
                """Write to log file only — no terminal output."""
                out_log.write(line + "\n")
                out_log.flush()

            # ── Build ride baskets + cache motor-off passes once ──
            # Same rides reused across all strategies and tuning seeds
            # so every controller is scored on the identical experience.
            _log_file(
                f"Building rides: screen={SCREEN_SEEDS_PER_PROFILE}*4="
                f"{SCREEN_SEEDS_PER_PROFILE*4} rides, "
                f"full={FULL_SEEDS_PER_PROFILE}*4={FULL_SEEDS_PER_PROFILE*4} rides"
            )
            t_rides = time.time()
            screen_rides = generate_ride_set(
                seeds_per_profile=SCREEN_SEEDS_PER_PROFILE,
                base_seed=args.seed,
            )
            full_rides = generate_ride_set(
                seeds_per_profile=FULL_SEEDS_PER_PROFILE,
                base_seed=args.seed,
            )
            screen_off_logs = precompute_motor_off_logs(screen_rides)
            full_off_logs   = precompute_motor_off_logs(full_rides)
            _log_file(
                f"  ride baskets + motor-off caches built in "
                f"{time.time() - t_rides:.1f}s"
            )

            total_runs = len(strategy_names) * len(seeds)
            run_idx = 0
            for strategy_name in strategy_names:
                strat_cls = STRATEGY_BY_NAME[strategy_name]
                for seed in seeds:
                    run_idx += 1
                    if (strategy_name, int(seed)) in completed_pairs:
                        _log_file(
                            f"[{run_idx}/{total_runs}] Strategy "
                            f"{strategy_name} seed={seed} - SKIP (resume, "
                            f"already on disk)"
                        )
                        continue
                    _log_file(
                        f"[{run_idx}/{total_runs}] Strategy {strategy_name} "
                        f"seed={seed} - tuning..."
                    )

                    row = _tune_one(
                        strat_cls,
                        maxiter=args.maxiter,
                        popsize=args.popsize,
                        pool=pool,
                        seed=int(seed),
                        log_fn=_log_file,
                        screen_rides=screen_rides,
                        full_rides=full_rides,
                        screen_off_logs=screen_off_logs,
                        full_off_logs=full_off_logs,
                        objective_mode=args.objective,
                        objective_robust_samples=args.robust_objective_samples,
                        objective_robust_lambda=args.robust_objective_lambda,
                        polish=not args.no_polish,
                        polish_maxiter=args.polish_maxiter,
                        optimizer=args.optimizer,
                        warm_start=not args.no_warm_start,
                        adaptive_samples=not args.no_adaptive_samples,
                        n_workers=args.workers,
                    )
                    results.append(row)

                    _log_file(
                        f"  -> {strategy_name} seed={seed} "
                        f"composite={row['composite']:.2f}  "
                        f"(screen={row['screen_score']:.2f})  "
                        f"polish_improved={row.get('polish_improved', False)}  "
                        f"in {row['elapsed_s']:.0f}s"
                    )

                    with ckpt_path.open("w", encoding="utf-8") as f:
                        json.dump({"run_id": run_id, "completed": results},
                                  f, indent=2)

                    gc.collect()   # release DE temporaries before next run

            # Phase 3: Monte-Carlo robustness (on full 20-ride basket)
            if not args.no_robust:
                _log_file(f"\nRobustness check ({args.robust_samples} "
                          f"samples, {args.workers} workers)...")
                for row in results:
                    strategy_name = row["strategy"]
                    if "robust_mean" in row:
                        _log_file(
                            f"  {strategy_name} seed={row.get('seed')}: "
                            f"SKIP (resume, robust already on disk)"
                        )
                        continue
                    strat_cls = STRATEGY_BY_NAME[strategy_name]
                    params = row["params"]
                    rob = score_strategy_robust(
                        strategy_factory=None,
                        rides=full_rides,
                        n_samples=args.robust_samples,
                        pool=pool,
                        seed=int(row.get("seed", args.seed)),
                        strat_cls=strat_cls,
                        strat_params=params,
                    )
                    row["robust_mean"] = rob["mean"]
                    row["robust_std"] = rob["std"]
                    row["robust_p5"] = rob["p5"]
                    row["robust_p95"] = rob["p95"]
                    _log_file(
                        f"  {strategy_name} seed={row.get('seed')}: mean={rob['mean']:.1f}  "
                        f"std={rob['std']:.1f}  "
                        f"p5={rob['p5']:.1f}  p95={rob['p95']:.1f}"
                    )
                    # Persist robustness progress so a crash during this
                    # phase is also recoverable by --resume.
                    with ckpt_path.open("w", encoding="utf-8") as f:
                        json.dump({"run_id": run_id, "completed": results},
                                  f, indent=2)

        finally:
            # Shut down the single pool before restoring streams
            pool.terminate()
            pool.join()
            sys.stdout = _real_stdout
            sys.stderr = _real_stderr

    elapsed = time.time() - t_all
    meta = {
        "run_id": run_id,
        "strategies": strategy_names,
        "maxiter": args.maxiter,
        "popsize": args.popsize,
        "workers": args.workers,
        "seed": args.seed,
        "seeds": seeds,
        "objective_mode": args.objective,
        "robust_objective_samples": args.robust_objective_samples,
        "robust_objective_lambda": args.robust_objective_lambda,
        "polish": not args.no_polish,
        "polish_maxiter": args.polish_maxiter,
        "robust": not args.no_robust,
        "elapsed_s": elapsed,
    }
    _write_artifacts(out_dir, meta, results)
    _write_stability_artifacts(out_dir, results, seeds)

    # ── Compact terminal summary (only output after compute) ──
    ranked = sorted(results, key=lambda r: r["composite"], reverse=True)
    for r in ranked:
        pstr = ", ".join(f"{k}={v!r}" for k, v in r["params"].items())
        rob = ""
        if "robust_p5" in r:
            rob = f"  robust P5={r['robust_p5']:.1f}"
        print(
            f"  {r['strategy']} seed={r.get('seed', '')}  "
            f"composite={r['composite']:.2f}  "
            f"energy={r['energy']:.1f}  linearity={r['linearity']:.1f}"
            f"{rob}"
        )
        print(f"    PARAMS = dict({pstr})")

    print(f"\n  Total: {elapsed:.0f}s  Artifacts: {out_dir.as_posix()}/")
    print("  Done.")
