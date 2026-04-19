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

from .scoring import (
    SCENARIOS,
    SCREEN_MASSES,
    SCREEN_SCENARIOS,
    score_strategy,
    score_strategy_robust,
)
from .strategies import DEFAULT_STRATEGY_NAMES, STRATEGY_BY_NAME, parse_strategy_names

DEFAULT_STRATEGIES = list(DEFAULT_STRATEGY_NAMES)


# =====================================================================
#  Worker pool helpers
# =====================================================================

def _pool_worker_init():
    """Silence stdout/stderr in pool workers.

    On Windows, multiprocessing 'spawn' workers can inherit the VS Code
    terminal pty as their console.  Redirecting at both Python and OS
    levels ensures nothing leaks back — preventing pty buffer overflows
    that crash the VS Code extension host.
    """
    devnull = open(os.devnull, "w")         # noqa: SIM115
    sys.stdout = devnull
    sys.stderr = devnull
    try:
        os.dup2(devnull.fileno(), 1)
        os.dup2(devnull.fileno(), 2)
    except OSError:
        pass  # some sandboxed envs block dup2


# =====================================================================
#  Helpers
# =====================================================================

class _Objective:
    """Pickle-safe objective callable for scipy parallel workers."""

    def __init__(
        self,
        strat_cls,
        names,
        int_flags,
        scenarios,
        masses,
        mode="nominal",
        robust_samples=8,
        robust_seed=42,
        robust_lambda=0.5,
    ):
        self.strat_cls = strat_cls
        self.names = names
        self.int_flags = int_flags
        self.scenarios = scenarios
        self.masses = masses
        self.mode = mode
        self.robust_samples = robust_samples
        self.robust_seed = robust_seed
        self.robust_lambda = robust_lambda

    def __call__(self, x):
        params = _vec_to_params(x, self.names, self.int_flags)
        if self.mode == "nominal":
            result = score_strategy(
                lambda: self.strat_cls(**params),
                scenarios=self.scenarios,
                masses=self.masses,
            )
            return -result["weighted"]

        robust = score_strategy_robust(
            strategy_factory=None,
            n_samples=self.robust_samples,
            seed=self.robust_seed,
            scenarios=self.scenarios,
            masses=self.masses,
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


def _avg_dims(score_result):
    per_s = score_result["per_scenario"]
    total_wt = sum(s["weight"] for s in per_s)
    e = sum(s["energy"] * s["weight"] for s in per_s) / total_wt
    t = sum(s["tracking"] * s["weight"] for s in per_s) / total_wt
    s_ = sum(s["smoothness"] * s["weight"] for s in per_s) / total_wt
    return e, t, s_


# =====================================================================
#  Per-strategy pipeline
# =====================================================================

def _full_nominal_objective(x, strat_cls, names, int_flags):
    """Full-evaluation nominal objective for optional local polish."""
    params = _vec_to_params(x, names, int_flags)
    result = score_strategy(lambda: strat_cls(**params))
    return -result["weighted"]


def _tune_one(
    strat_cls,
    *,
    maxiter,
    popsize,
    pool,
    seed,
    log_fn,
    objective_mode,
    objective_robust_samples,
    objective_robust_lambda,
    polish,
    polish_maxiter,
):
    """Screen-DE → full-eval for one strategy class.

    *pool* must be a multiprocessing.Pool whose .map is passed to scipy
    so no internal pools are created or destroyed between strategies.
    """
    strategy_name = strat_cls.key
    names, bounds, int_flags = _build_bounds(strat_cls)

    # Phase 1: DE with screen config
    objective = _Objective(
        strat_cls, names, int_flags,
        scenarios=SCREEN_SCENARIOS,
        masses=SCREEN_MASSES,
        mode=objective_mode,
        robust_samples=objective_robust_samples,
        robust_seed=seed,
        robust_lambda=objective_robust_lambda,
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

    t0 = time.time()
    rng = np.random.default_rng(seed)
    result = differential_evolution(
        objective,
        bounds=bounds,
        rng=rng,
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
    screen_elapsed = time.time() - t0

    best_params = _vec_to_params(result.x, names, int_flags)
    screen_score = float(-result.fun)

    polish_used = False
    polish_improved = False
    polish_nit = 0
    polish_score = None

    if polish:
        # Optional local search on the full nominal objective.
        # Powell handles mixed/discontinuous landscapes better than gradient methods
        # when integer-like parameters are rounded in _vec_to_params.
        p0 = np.array(result.x, dtype=float)
        pol = minimize(
            _full_nominal_objective,
            x0=p0,
            args=(strat_cls, names, int_flags),
            method="Powell",
            bounds=bounds,
            options={"maxiter": int(polish_maxiter), "disp": False},
        )
        polish_used = True
        polish_nit = int(getattr(pol, "nit", 0) or 0)
        polish_score = float(-pol.fun)

        # Compare on full nominal objective to decide whether to keep polished params.
        pre_full = score_strategy(lambda: strat_cls(**best_params))["weighted"]
        post_params = _vec_to_params(pol.x, names, int_flags)
        post_full = score_strategy(lambda: strat_cls(**post_params))["weighted"]
        if post_full > pre_full:
            best_params = post_params
            polish_improved = True

    # Phase 2: full scoring on best params
    t1 = time.time()
    full = score_strategy(lambda: strat_cls(**best_params))
    full_elapsed = time.time() - t1

    e_avg, t_avg, s_avg = _avg_dims(full)
    total_elapsed = screen_elapsed + full_elapsed

    return {
        "strategy": strategy_name,
        "name": strat_cls(**best_params).name,
        "params": best_params,
        "seed": int(seed),
        "objective_mode": objective_mode,
        "screen_score": screen_score,
        "composite": float(full["weighted"]),
        "energy": float(e_avg),
        "tracking": float(t_avg),
        "smoothness": float(s_avg),
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
            "energy", "tracking", "smoothness", "elapsed_s", "nfev",
            "success", "polish_used", "polish_improved", "params",
        ])
        for i, r in enumerate(rows_sorted, start=1):
            w.writerow([
                i, r["strategy"],
                r.get("seed", ""),
                r.get("objective_mode", "nominal"),
                f"{r['composite']:.3f}", f"{r['energy']:.3f}",
                f"{r['tracking']:.3f}", f"{r['smoothness']:.3f}",
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
                f"  tracking={r['tracking']:.1f}  smoothness={r['smoothness']:.1f}\n"
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
        default="nominal",
        choices=["nominal", "robust_mean", "robust_p5", "robust_mean_std"],
        help="Optimization objective used during DE screen phase.",
    )
    p.add_argument(
        "--robust-objective-samples",
        type=int,
        default=8,
        help="MC samples per objective eval for robust objectives.",
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
    results: list[dict] = []

    # ── One persistent pool for the entire run ──
    # Workers are spawned once, stay warm (numba JIT cached), and are
    # silenced via _pool_worker_init so nothing leaks to VS Code's pty.
    pool = mp.Pool(args.workers, initializer=_pool_worker_init)

    with log_path.open("w", encoding="utf-8") as out_log:
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

            total_runs = len(strategy_names) * len(seeds)
            run_idx = 0
            for strategy_name in strategy_names:
                strat_cls = STRATEGY_BY_NAME[strategy_name]
                for seed in seeds:
                    run_idx += 1
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
                        objective_mode=args.objective,
                        objective_robust_samples=args.robust_objective_samples,
                        objective_robust_lambda=args.robust_objective_lambda,
                        polish=not args.no_polish,
                        polish_maxiter=args.polish_maxiter,
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

            # Phase 3: Monte-Carlo robustness
            if not args.no_robust:
                from .scoring import score_strategy_robust
                _log_file(f"\nRobustness check ({args.robust_samples} "
                          f"samples, {args.workers} workers)...")
                for row in results:
                    strategy_name = row["strategy"]
                    strat_cls = STRATEGY_BY_NAME[strategy_name]
                    params = row["params"]
                    rob = score_strategy_robust(
                        strategy_factory=None,
                        n_samples=args.robust_samples,
                        workers=1,
                        seed=int(row.get("seed", args.seed)),
                        strat_cls=strat_cls,
                        strat_params=params,
                        scenarios=SCENARIOS,
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
            f"energy={r['energy']:.1f}  tracking={r['tracking']:.1f}  "
            f"smooth={r['smoothness']:.1f}{rob}"
        )
        print(f"    PARAMS = dict({pstr})")

    print(f"\n  Total: {elapsed:.0f}s  Artifacts: {out_dir.as_posix()}/")
    print("  Done.")
