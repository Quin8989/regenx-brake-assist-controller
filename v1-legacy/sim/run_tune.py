"""sim.run_tune -- Optuna-based strategy tuner.

One library (Optuna) handles everything the old hand-rolled pipeline did:

  * TPE sampler  -- replaces differential_evolution + Powell polish.
  * MedianPruner -- replaces the adaptive MC sample ramp and the
                    cheap-basket -> full-basket two-phase screen.
                    Rung 0 scores each trial on the cheap 8-ride
                    screen with 3 MC samples; rung 1 scores survivors
                    on the full 20-ride basket with 6 MC samples.
  * SQLite RDB   -- replaces the custom checkpoint.json resume logic.
  * study.enqueue_trial + param_grid() rows -- warm start.
  * study.trials_dataframe()                -- replaces the stability CSVs.

Composite / objective unchanged: maximise robust CVaR-20 of
``sim.scoring.score_strategy_robust`` (composite = 0.40 * capture +
0.60 * fidelity).  After the search each winning trial is rescored with
the full ``--robust-samples`` Monte-Carlo sweep and the results are
written to JSON / CSV.

Usage:
    python -m sim.run_tune --strategies pi_controller
    python -m sim.run_tune --strategies pi_controller,aimd_ff --trials 200
    python -m sim.run_tune --strategies aimd_ff --seeds 7,11,17
    python -m sim.run_tune --resume sim/output/tune/20260423_120000
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import optuna
from optuna.samplers import TPESampler
from optuna.pruners import MedianPruner, NopPruner

from .ride_generator import generate_ride_set
from .scoring import (
    precompute_motor_off_logs,
    score_rides,
    score_strategy_robust,
    _robust_pool_initializer,
)
from .strategies import DEFAULT_STRATEGY_NAMES, STRATEGY_BY_NAME, parse_strategy_names
from .scoreboard import append_scoreboard

DEFAULT_STRATEGIES = list(DEFAULT_STRATEGY_NAMES)

# Ride-set sizing (same as before)
SCREEN_SEEDS_PER_PROFILE = 2     # rung 0 -> 8 rides
FULL_SEEDS_PER_PROFILE   = 5     # rung 1 + final report -> 20 rides

# Pruning-rung MC sample counts
SCREEN_SAMPLES           = 3     # cheap rung
FULL_SAMPLES             = 6     # expensive rung (trial return value)
ROBUST_FINAL_SAMPLES     = 60    # post-search robustness sweep

DEFAULT_TRIALS           = 150   # per (strategy, seed)


# =====================================================================
#  Search space (inferred from strategy.param_grid())
# =====================================================================

def _build_search_space(strat_cls):
    """Return (names, bounds, is_int_flags) inferred from param_grid()."""
    grid = strat_cls.param_grid()
    if not grid:
        raise ValueError(f"{strat_cls.key}: empty param_grid()")
    names = list(grid[0].keys())
    bounds, is_int = [], []
    defaults = strat_cls().__dict__
    for key in names:
        values = [row[key] for row in grid]
        lo, hi = float(min(values)), float(max(values))
        bounds.append((lo, hi))
        all_int = all(float(v).is_integer() for v in values)
        default_is_int = isinstance(defaults.get(key), int)
        is_int.append(all_int or default_is_int)
    return names, bounds, is_int


def _suggest_params(trial, names, bounds, is_int):
    params = {}
    for name, (lo, hi), flag in zip(names, bounds, is_int):
        if flag:
            params[name] = trial.suggest_int(name, int(lo), int(hi))
        else:
            params[name] = trial.suggest_float(name, lo, hi)
    return params


# =====================================================================
#  Objective with 2-rung pruning
# =====================================================================

class _Objective:
    """CVaR-20 objective with cheap-rung pruning.

    Rung 0: 8-ride screen, 3 MC samples  -> report to pruner.
    Rung 1: 20-ride full, 6 MC samples   -> final trial value.

    We return negated CVaR-20 and ``direction="minimize"`` so the
    MedianPruner's "lower is better" comparison matches.  The MC
    perturbations within each rung are parallelised across an
    mp.Pool; Optuna itself runs trials sequentially so storage stays
    thread-safe.

    When ``screen_scorer`` / ``full_scorer`` are supplied (JAX backend),
    they fully replace the numpy ``score_strategy_robust`` path.
    """

    def __init__(self, strat_cls, names, bounds, is_int,
                 screen_rides, full_rides, seed, pool=None,
                 screen_scorer=None, full_scorer=None):
        self.strat_cls = strat_cls
        self.names = names
        self.bounds = bounds
        self.is_int = is_int
        self.screen_rides = screen_rides
        self.full_rides = full_rides
        self.seed = seed
        self.pool = pool
        self.screen_scorer = screen_scorer
        self.full_scorer = full_scorer

    def __call__(self, trial):
        params = _suggest_params(trial, self.names, self.bounds, self.is_int)

        # Rung 0 -- cheap screen.
        if self.screen_scorer is not None:
            screen = self.screen_scorer.score(
                self.strat_cls.key, params,
                n_samples=SCREEN_SAMPLES, seed=self.seed)
        else:
            screen = score_strategy_robust(
                strategy_factory=None,
                rides=self.screen_rides,
                n_samples=SCREEN_SAMPLES,
                seed=self.seed,
                pool=self.pool,
                strat_cls=self.strat_cls,
                strat_params=params,
            )
        trial.report(-screen["cvar20"], step=0)
        if trial.should_prune():
            raise optuna.TrialPruned()

        # Rung 1 -- full basket.
        if self.full_scorer is not None:
            full = self.full_scorer.score(
                self.strat_cls.key, params,
                n_samples=FULL_SAMPLES, seed=self.seed)
        else:
            full = score_strategy_robust(
                strategy_factory=None,
                rides=self.full_rides,
                n_samples=FULL_SAMPLES,
                seed=self.seed,
                pool=self.pool,
                strat_cls=self.strat_cls,
                strat_params=params,
            )
        trial.set_user_attr("capture", float(full["capture_mean"]))
        trial.set_user_attr("fidelity",       float(full["fidelity_mean"]))
        trial.set_user_attr("cvar20",     float(full["cvar20"]))
        trial.set_user_attr("nominal",    float(full["nominal"]))
        return -float(full["cvar20"])


# =====================================================================
#  Per-(strategy, seed) study
# =====================================================================

def _run_study(strat_cls, seed, *, n_trials, storage_url,
               screen_rides, full_rides, pool, log_fn,
               screen_scorer=None, full_scorer=None, pruner_kind="median"):
    names, bounds, is_int = _build_search_space(strat_cls)
    study_name = f"{strat_cls.key}_seed{seed}"

    if pruner_kind == "none":
        pruner = NopPruner()
    elif pruner_kind == "soft":
        # Softer: more warmup trials before pruning kicks in, and
        # require a couple of rungs before allowing a prune.
        pruner = MedianPruner(n_startup_trials=30, n_warmup_steps=1)
    else:
        pruner = MedianPruner(n_startup_trials=10, n_warmup_steps=0)

    study = optuna.create_study(
        direction="minimize",
        sampler=TPESampler(seed=seed, n_startup_trials=max(10, len(names) * 2)),
        pruner=pruner,
        storage=storage_url,
        study_name=study_name,
        load_if_exists=True,
    )

    # Warm start: enqueue every param_grid() row (idempotent on resume).
    grid = strat_cls.param_grid()
    already_enqueued = study.user_attrs.get("warm_started", False)
    if not already_enqueued:
        for row in grid:
            row_clamped = {}
            for name, (lo, hi), flag in zip(names, bounds, is_int):
                v = row[name]
                v = max(lo, min(hi, float(v)))
                row_clamped[name] = int(round(v)) if flag else float(v)
            study.enqueue_trial(row_clamped, skip_if_exists=True)
        study.set_user_attr("warm_started", True)

    objective = _Objective(strat_cls, names, bounds, is_int,
                           screen_rides, full_rides, seed, pool=pool,
                           screen_scorer=screen_scorer,
                           full_scorer=full_scorer)

    completed = len([t for t in study.trials
                     if t.state == optuna.trial.TrialState.COMPLETE
                     or t.state == optuna.trial.TrialState.PRUNED])
    remaining = max(0, n_trials - completed)
    if remaining == 0:
        log_fn(f"  [{study_name}] already at {completed}/{n_trials} trials "
               "-- skipping search")
    else:
        log_fn(f"  [{study_name}] running {remaining} trials "
               f"(warm-start={len(grid)} grid rows)")
        t0 = time.time()
        study.optimize(
            objective, n_trials=remaining, n_jobs=1,
            gc_after_trial=True, show_progress_bar=False,
        )
        log_fn(f"  [{study_name}] done in {time.time() - t0:.0f}s")

    best = study.best_trial
    best_value = float(best.value) if best.value is not None else 0.0
    return {
        "strategy": strat_cls.key,
        "name": strat_cls(**best.params).name,
        "params": dict(best.params),
        "seed": int(seed),
        "composite":  -best_value,                 # trial value is -cvar20
        "capture": float(best.user_attrs.get("capture", 0.0)),
        "fidelity":       float(best.user_attrs.get("fidelity", 0.0)),
        "screen_score": float(best.user_attrs.get("nominal", 0.0)),
        "cvar20":     float(best.user_attrs.get("cvar20", -best_value)),
        "n_trials_completed": int(len([t for t in study.trials
                                       if t.state == optuna.trial.TrialState.COMPLETE])),
        "n_trials_pruned":    int(len([t for t in study.trials
                                       if t.state == optuna.trial.TrialState.PRUNED])),
        "param_names": names,
        "bounds": bounds,
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
            "rank", "strategy", "seed", "composite", "capture", "fidelity",
            "cvar20", "n_complete", "n_pruned", "params",
        ])
        for i, r in enumerate(rows_sorted, start=1):
            w.writerow([
                i, r["strategy"], r.get("seed", ""),
                f"{r['composite']:.3f}", f"{r['capture']:.3f}", f"{r['fidelity']:.3f}",
                f"{r.get('robust_cvar20', r['cvar20']):.3f}",
                r.get("n_trials_completed", ""),
                r.get("n_trials_pruned", ""),
                json.dumps(r["params"], sort_keys=True),
            ])

    with (base_dir / "best_snippets.txt").open("w", encoding="utf-8") as f:
        f.write("# Tuned parameter snippets\n\n")
        for r in rows_sorted:
            pstr = ", ".join(f"{k}={v}" for k, v in r["params"].items())
            f.write(f"({r['strategy']}, dict({pstr})),\n")
            f.write(
                f"  # composite={r['composite']:.1f}  "
                f"capture={r['capture']:.1f}  fidelity={r['fidelity']:.1f}\n"
            )


def _write_stability_artifacts(base_dir: Path, rows: list[dict], seeds: list[int]):
    """Cross-seed stability diagnostics for each strategy."""
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
            "std":  float(np.std(scores)),
            "min":  float(np.min(scores)),
            "max":  float(np.max(scores)),
            "best_seed":      int(items_sorted[0].get("seed", -1)),
            "best_composite": float(items_sorted[0]["composite"]),
            "best_params":    items_sorted[0]["params"],
        }

    winners_by_seed: dict[int, str] = {}
    for seed in seeds:
        seed_rows = [r for r in rows if r.get("seed") == seed]
        if not seed_rows:
            continue
        winners_by_seed[int(seed)] = max(seed_rows, key=lambda r: r["composite"])["strategy"]

    winner_counts: dict[str, int] = defaultdict(int)
    for s in winners_by_seed.values():
        winner_counts[s] += 1

    with (base_dir / "stability.json").open("w", encoding="utf-8") as f:
        json.dump({
            "seeds": seeds,
            "winners_by_seed": winners_by_seed,
            "winner_counts": dict(winner_counts),
            "strategy_stats": strategy_stats,
        }, f, indent=2)

    with (base_dir / "stability_summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "n_runs", "mean", "std", "min", "max",
                    "best_seed", "winner_count"])
        for strategy_name in sorted(strategy_stats):
            s = strategy_stats[strategy_name]
            w.writerow([
                strategy_name, s["n_runs"],
                f"{s['mean']:.3f}", f"{s['std']:.3f}",
                f"{s['min']:.3f}",  f"{s['max']:.3f}",
                s["best_seed"], winner_counts.get(strategy_name, 0),
            ])


def _parse_seeds(seed_text: str | None, seed_default: int) -> list[int]:
    if not seed_text:
        return [int(seed_default)]
    seeds = [int(c.strip()) for c in seed_text.split(",") if c.strip()]
    return seeds or [int(seed_default)]


# =====================================================================
#  CLI
# =====================================================================

def _parse_args():
    p = argparse.ArgumentParser(
        description="Tune RegenX control strategies with Optuna "
                    "(TPE + MedianPruner, SQLite resume).",
    )
    p.add_argument(
        "--strategies",
        default=",".join(DEFAULT_STRATEGIES),
        help="Comma-separated strategy names (default: all registered).",
    )
    p.add_argument("--trials", type=int, default=DEFAULT_TRIALS,
                   help=f"Optuna trials per (strategy, seed) "
                        f"(default: {DEFAULT_TRIALS}).")
    p.add_argument("--seed", type=int, default=42,
                   help="Single tuning seed (default: 42).")
    p.add_argument(
        "--seeds", type=str, default=None,
        help="Comma-separated seeds for multi-seed tuning (overrides --seed).",
    )
    p.add_argument(
        "--workers", type=int,
        default=max(1, (os.cpu_count() or 2) // 2),
        help="Parallel Optuna trial workers "
             "(default: cpu_count//2 to leave VS Code headroom).",
    )
    p.add_argument(
        "--robust-samples", type=int, default=ROBUST_FINAL_SAMPLES,
        help=f"Final robustness MC sample count (default: {ROBUST_FINAL_SAMPLES}).",
    )
    p.add_argument(
        "--resume", type=str, default=None, metavar="DIR",
        help="Resume from a previous run's output dir.  Re-opens the "
             "SQLite study so already-completed trials are kept.",
    )
    p.add_argument(
        "--backend", choices=("numpy", "jax"), default="numpy",
        help="Scoring backend (default: numpy).  'jax' runs the ride "
             "sims as a single vmapped JIT kernel per trial — much "
             "faster but with documented ~1 pt CVaR-20 offset vs numpy "
             "(see sim/jax/robust_scoring.py).  JAX backend ignores "
             "--workers (Optuna runs sequentially on one JIT cache).",
    )
    p.add_argument(
        "--pruner", choices=("median", "soft", "none"), default="median",
        help="Optuna pruner selection.  'median' (default) matches the"
             " original 2-rung MedianPruner; 'soft' delays pruning to"
             " 30 warmup trials + 1 warmup rung to let TPE explore more"
             " before cuts; 'none' disables pruning entirely (all trials"
             " run to completion).",
    )
    # Compat shims (silently ignored): older scripts may pass these.
    p.add_argument("--maxiter",  type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--popsize",  type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--polish-maxiter", type=int, default=None, help=argparse.SUPPRESS)
    return p.parse_args()


def main():
    args = _parse_args()
    strategy_names = parse_strategy_names(args.strategies)
    seeds = _parse_seeds(args.seeds, args.seed)

    # Back-compat: if a legacy caller passed --maxiter/--popsize, use
    # their product as the trial budget unless --trials was overridden.
    if args.trials == DEFAULT_TRIALS and args.maxiter and args.popsize:
        args.trials = int(args.maxiter) * int(args.popsize)

    # Below-normal priority on Windows so VS Code stays responsive.
    if sys.platform == "win32":
        try:
            import ctypes
            BELOW_NORMAL = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
        except Exception:
            pass

    # Quiet Optuna's per-trial logger -- our own log_fn covers progress.
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    if args.resume:
        out_dir = Path(args.resume)
        if not out_dir.is_dir():
            raise SystemExit(f"--resume: directory not found: {out_dir}")
        run_id = out_dir.name
    else:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("sim") / "output" / "tune" / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

    log_path = out_dir / "run.log"
    storage_url = f"sqlite:///{(out_dir / 'tune.db').as_posix()}"

    os.environ.setdefault("NUMBA_DISABLE_PERFORMANCE_WARNINGS", "1")

    pruner_desc = {"median": "MedianPruner (2-rung)",
                   "soft":   "MedianPruner (soft: 30 startup, 1 warmup rung)",
                   "none":   "NopPruner (disabled)"}[args.pruner]
    print(
        f"  Tuning {', '.join(strategy_names)}  "
        f"trials={args.trials} workers={args.workers} seeds={seeds}\n"
        f"  Composite: 0.40 * capture + 0.60 * fidelity\n"
        f"  Optimizer: Optuna TPE + {pruner_desc}\n"
        f"  Storage:   {storage_url}\n"
        f"  Log ->     {log_path.as_posix()}",
        flush=True,
    )

    t_all = time.time()

    log_mode = "a" if args.resume else "w"
    with log_path.open(log_mode, encoding="utf-8") as out_log:
        def _log(line):
            out_log.write(line + "\n")
            out_log.flush()
            print(line, flush=True)

        _log("=" * 70)
        _log(f"  RegenX -- Optuna Tuning (TPE + {pruner_desc})")
        _log("=" * 70)
        _log(f"  Strategies: {', '.join(strategy_names)}")
        _log(f"  Trials:     {args.trials}")
        _log(f"  Workers:    {args.workers}")
        _log(f"  Seeds:      {seeds}")
        _log(f"  Robustness: {args.robust_samples} MC samples (final sweep)")
        _log(f"  Output:     {out_dir.as_posix()}")
        _log("")

        _log(
            f"Building rides: screen={SCREEN_SEEDS_PER_PROFILE}*4="
            f"{SCREEN_SEEDS_PER_PROFILE*4} rides, "
            f"full={FULL_SEEDS_PER_PROFILE}*4={FULL_SEEDS_PER_PROFILE*4} rides"
        )
        t_rides = time.time()
        screen_rides = generate_ride_set(
            seeds_per_profile=SCREEN_SEEDS_PER_PROFILE, base_seed=args.seed)
        full_rides = generate_ride_set(
            seeds_per_profile=FULL_SEEDS_PER_PROFILE, base_seed=args.seed)
        # Motor-off caches are recomputed per-perturbation inside
        # score_strategy_robust, but we prime the nominal cache once so
        # the first trial doesn't eat the warm-up cost.
        precompute_motor_off_logs(screen_rides)
        precompute_motor_off_logs(full_rides)
        _log(f"  ride baskets built in {time.time() - t_rides:.1f}s")

        # ── JAX scorers (study-level) ───────────────────────────────
        screen_scorer = full_scorer = None
        if args.backend == "jax":
            t_jax = time.time()
            _log("  initializing JAX backend (compiles one kernel per "
                 "ride basket)…")
            from sim.jax.robust_scoring import JaxRobustScorer
            screen_scorer = JaxRobustScorer(screen_rides)
            full_scorer   = JaxRobustScorer(full_rides)
            _log(f"  JAX scorers ready in {time.time() - t_jax:.1f}s")

        results: list[dict] = []
        total_runs = len(strategy_names) * len(seeds)
        run_idx = 0
        import multiprocessing as mp
        # Robustness-sweep and numpy-backend rungs use the mp.Pool.  For
        # JAX-backend pure runs the Pool is still created because the
        # final robust sweep re-runs on numpy for side-by-side truth
        # (same code path the numpy tuner uses -- keeps reports apples
        # to apples).
        #
        # Use the 'spawn' context: on Linux the default is 'fork', which
        # JAX warns against because it already spun up worker threads
        # inside this process.  fork() copies a multithreaded process
        # and can deadlock.  'spawn' starts a fresh interpreter, which
        # is safe and portable (it's also the default on Windows).
        mp_ctx = mp.get_context("spawn")
        with mp_ctx.Pool(args.workers,
                         initializer=_robust_pool_initializer) as pool:
            for strategy_name in strategy_names:
                strat_cls = STRATEGY_BY_NAME[strategy_name]
                for seed in seeds:
                    run_idx += 1
                    _log(f"[{run_idx}/{total_runs}] {strategy_name} "
                         f"seed={seed}  backend={args.backend}")
                    row = _run_study(
                        strat_cls, int(seed),
                        n_trials=args.trials,
                        storage_url=storage_url,
                        screen_rides=screen_rides,
                        full_rides=full_rides,
                        pool=pool,
                        log_fn=_log,
                        screen_scorer=screen_scorer,
                        full_scorer=full_scorer,
                        pruner_kind=args.pruner,
                    )
                    results.append(row)
                    _log(f"  -> composite={row['composite']:.2f}  "
                         f"capture={row['capture']:.1f}  fidelity={row['fidelity']:.1f}  "
                         f"(complete={row['n_trials_completed']}, "
                         f"pruned={row['n_trials_pruned']})")

            # Final robustness sweep on each winner.
            _log("")
            _log(f"Robustness sweep ({args.robust_samples} samples, "
                 f"{args.workers} workers)...")
            for row in results:
                strat_cls = STRATEGY_BY_NAME[row["strategy"]]
                rob = score_strategy_robust(
                    strategy_factory=None,
                    rides=full_rides,
                    n_samples=args.robust_samples,
                    pool=pool,
                    seed=int(row.get("seed", args.seed)),
                    strat_cls=strat_cls,
                    strat_params=row["params"],
                )
                row["robust_mean"]   = rob["mean"]
                row["robust_std"]    = rob["std"]
                row["robust_p5"]     = rob["p5"]
                row["robust_p95"]    = rob["p95"]
                row["robust_cvar20"] = rob["cvar20"]
                _log(f"  {row['strategy']} seed={row.get('seed')}: "
                     f"mean={rob['mean']:.1f}  std={rob['std']:.1f}  "
                     f"p5={rob['p5']:.1f}  cvar20={rob['cvar20']:.1f}")

    elapsed = time.time() - t_all
    meta = {
        "run_id": run_id,
        "strategies": strategy_names,
        "trials": args.trials,
        "workers": args.workers,
        "seed": args.seed,
        "seeds": seeds,
        "robust_samples": args.robust_samples,
        "elapsed_s": elapsed,
        "storage": storage_url,
        "optimizer": "optuna.TPESampler + MedianPruner (2-rung)",
    }
    _write_artifacts(out_dir, meta, results)
    _write_stability_artifacts(out_dir, results, seeds)

    # Centralised scoreboard: one row per (strategy, seed) tuned winner.
    for r in results:
        pstr = ", ".join(f"{k}={v!r}" for k, v in r["params"].items())
        append_scoreboard(
            source="tune",
            run_id=f"{run_id}/{r['strategy']}_seed{r.get('seed','')}",
            cvar20=float(r.get("robust_cvar20", r["cvar20"])),
            composite_mean=float(r.get("robust_mean", r["composite"])),
            fixture=f"tune_full_{FULL_SEEDS_PER_PROFILE*4}rides_"
                    f"rob{args.robust_samples}",
            notes=f"backend={args.backend} trials={args.trials} "
                  f"params={{{pstr}}}",
            artifact=str(out_dir / "results.json"),
        )

    ranked = sorted(results, key=lambda r: r["composite"], reverse=True)
    for r in ranked:
        pstr = ", ".join(f"{k}={v!r}" for k, v in r["params"].items())
        rob = f"  cvar20={r['robust_cvar20']:.1f}" if "robust_cvar20" in r else ""
        print(f"  {r['strategy']} seed={r.get('seed', '')}  "
              f"composite={r['composite']:.2f}  "
              f"capture={r['capture']:.1f}  fidelity={r['fidelity']:.1f}{rob}")
        print(f"    PARAMS = dict({pstr})")

    print(f"\n  Total: {elapsed:.0f}s  Artifacts: {out_dir.as_posix()}/")
    print("  Done.")


if __name__ == "__main__":
    main()
"""sim.run_tune - Tune any registered strategy against the canonical fitness.

The pipeline is fixed and there are no objective/optimizer toggles:

  Phase 1  Differential Evolution screen on the cheap basket
           (2 seeds * 4 profiles = 8 rides) with a robust CVaR-20
           objective (Expected Shortfall over Monte-Carlo physics
           perturbations).  Population is warm-started from the
           strategy's ``param_grid()`` and the per-eval MC sample
           count is ramped from half to full across generations.

  Phase 2  Powell polish on the full basket (5 seeds * 4 profiles =
           20 rides) using the same robust objective.

  Phase 3  Final 60-sample Monte-Carlo robustness report on the
           full basket.

The composite score is whatever ``sim.scoring.score_rides`` returns
(0.40 * capture + 0.60 * fidelity) -- the only number any tuner ever
sees.

Usage:
    python -m sim.run_tune --strategies pi_controller
    python -m sim.run_tune --strategies pi_controller,aimd_ff --maxiter 30
    python -m sim.run_tune --strategies aimd_ff --seeds 7,11,17
"""

