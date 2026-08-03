"""Distill the neural teacher into a symbolic PySR expression.

Stage 2 of the distillation pipeline.  Loads a ``theta.npz`` saved by
:mod:`scripts.research.train_neural_teacher`, rolls the teacher over
a diverse set of simulated rides to build a ``(state, k)`` dataset,
then runs PySR with plain MSE loss on that dataset.  Finally, the
top expressions are re-scored with :class:`PopulationEvaluator` on
the true CVaR-20 metric so we can pick the best deployment form.

Why this works
--------------
Direct PySR against CVaR-20 gives a flat, noisy, non-differentiable
loss surface.  PySR's constant optimizer can't feel the shape.
Distillation replaces that with MSE fitting against a dense teacher
target — a problem where PySR excels and its BFGS constant optimizer
has real gradients to climb.

We get three quantities for each candidate expression:
  * train_mse   — MSE against the teacher on the rollout dataset.
  * test_mse    — MSE on a held-out ride slice.
  * cvar20_true — CVaR-20 from the full physics sim, same as rerank.

Usage
-----
    python scripts/pysr_distill_teacher.py \\
        --theta sim/output/neural_teacher/theta.npz \\
        --rollout-rides-per-profile 5 \\
        --rollout-perts 4 \\
        --dataset-size 5000 \\
        --niterations 5 --populations 8 --population-size 33

Followed by validation:
    python scripts/pysr/rerank_hall_of_fame.py \\
        --hof sim/output/pysr/distill/invent_composite_hall_of_fame.csv \\
        --output sim/output/pysr/distill/rerank.csv
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Same sys.path hygiene as invent_composite.py: strip our own
# directory (scripts/) because it contains ``scripts/pysr/__init__.py``
# which would shadow the installed ``pysr`` package.  Then restore
# the repo root so ``sim`` / ``scripts.research.*`` still import.
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p != _SCRIPT_DIR]
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np
import jax
import jax.numpy as jnp

from sim.jax.env import DEFAULT_FLOAT  # noqa: F401  (configures jax)
from sim.jax.physics_strategy import (
    simulate_ride_strategy_jax, FEATURE_NAMES, K_FLOOR, K_CEIL,
)
from sim.jax.pysr_driver import build_batch
from sim.jax.population import PopulationEvaluator
from sim.ride_generator import generate_ride_set
from sim.scoring import _sample_perturbations
from sim.scoreboard import append_scoreboard

from scripts.research.neural_teacher import MLPShape, policy_k


# =====================================================================
#  Teacher rollout: build the (X, y) dataset
# =====================================================================

def rollout_teacher(
    theta: np.ndarray,
    *,
    rides_per_profile: int,
    n_perts: int,
    seed: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Run the teacher through the sim and harvest ``(state, k)`` pairs.

    The sim has no "emit state vector" hook, so we reconstruct the
    13-dim feature vector from the logged channels.  Not every feature
    is logged directly — some (jerk_mean, jerk_peak, slip_delta, ...)
    would require per-tick computation the sim doesn't expose.  For
    those, we use a zero placeholder.  That's OK: if the teacher
    itself didn't use them meaningfully, the student won't either.

    Returns arrays of shape ``[N, 13]`` and ``[N]``.

    NOTE: this is a first-pass dataset.  We'll want to extend it later
    to include the full feature set once the sim exposes per-tick
    values of the windowed aggregates.
    """
    rng = np.random.default_rng(seed)
    rides = generate_ride_set(
        seeds_per_profile=rides_per_profile, base_seed=seed)
    perts = _sample_perturbations(rng, n_perts)

    static, batched, profile_names, n_valid, brake_mask = build_batch(
        rides, perts, seed_base=seed ^ 0x5A5A)
    static_stripped = {k: v for k, v in static.items() if k != "strategy_fn"}

    shape = MLPShape()
    theta_j = jnp.asarray(theta, dtype=DEFAULT_FLOAT)

    def _sim_with_theta(kw):
        def strat(*feats):
            feats_vec = jnp.stack(feats)
            return policy_k(theta_j, feats_vec, shape)
        return simulate_ride_strategy_jax(
            strategy_fn=strat, **static_stripped, **kw)

    sim_jit = jax.jit(jax.vmap(_sim_with_theta))
    logs = sim_jit(batched)
    logs["speed"].block_until_ready()

    # Pull logged channels into numpy for easy reconstruction.
    k_cmd      = np.asarray(logs["k_cmd"])        # [B, T]  — teacher output
    rpm_log    = np.asarray(logs["motor_rpm"])    # [B, T]
    iq_log     = np.asarray(logs["current"])      # [B, T]
    vcap_log   = np.asarray(logs["vcap"])         # [B, T]
    # duty_cycle is not logged — recompute approximately from rpm + flux
    # if we need it; for now, use 0.
    nv = np.asarray(n_valid)                      # [B]

    # Build per-traj features.  Memory-permitting we keep every tick.
    rows_X = []
    rows_y = []
    for b in range(k_cmd.shape[0]):
        T = int(nv[b])
        if T <= 1:
            continue
        rpm = rpm_log[b, :T]
        iq = iq_log[b, :T]
        vcap = vcap_log[b, :T]
        k = k_cmd[b, :T]

        # Reconstruct drpm aggregates in a 25-tick window (250 ms),
        # matching PysrStrategy's window.  This mirrors what the
        # teacher itself *saw* at each tick: the PREVIOUS window's
        # stats.  We roll back by 25 ticks so k[t] lines up with the
        # features available at decision time.
        W = 25
        drpm = np.diff(rpm, prepend=rpm[:1]) / 0.01  # ≈ rpm_prev=rpm[0]
        drpm_mean = np.zeros(T, dtype=np.float32)
        drpm_peak_neg = np.zeros(T, dtype=np.float32)
        for t in range(W, T):
            win = drpm[t - W:t]
            drpm_mean[t] = float(np.mean(win))
            drpm_peak_neg[t] = float(np.min(win))

        # k_prev: simply k shifted by 1
        k_prev = np.concatenate([[k[0]], k[:-1]])
        d_iq = np.diff(iq, prepend=iq[:1])
        power_mech = rpm * iq  # proxy
        # Stateless placeholders for features we don't cheaply derive:
        zero = np.zeros(T, dtype=np.float32)

        # Match FEATURE_NAMES order:
        # ("rpm", "drpm_mean", "drpm_peak_neg", "iq", "duty_cycle",
        #  "vcap", "k_prev", "jerk_mean", "jerk_peak", "slip_delta",
        #  "decel_frac", "d_iq", "power_mech")
        feats = np.stack([
            rpm.astype(np.float32),
            drpm_mean,
            drpm_peak_neg,
            iq.astype(np.float32),
            zero,                         # duty_cycle (not logged)
            vcap.astype(np.float32),
            k_prev.astype(np.float32),
            zero,                         # jerk_mean
            zero,                         # jerk_peak
            zero,                         # slip_delta
            zero,                         # decel_frac
            d_iq.astype(np.float32),
            power_mech.astype(np.float32),
        ], axis=1)                         # [T, 13]

        # Drop the first W ticks where aggregates are bogus, and any
        # ticks outside brake windows (we really only care about the
        # policy's behaviour during braking).
        bm = np.asarray(brake_mask[b])[:T]
        keep = np.zeros(T, dtype=bool)
        keep[W:] = True
        keep &= bm
        rows_X.append(feats[keep])
        rows_y.append(k[keep].astype(np.float32))

    X = np.concatenate(rows_X, axis=0) if rows_X else np.zeros((0, 13))
    y = np.concatenate(rows_y, axis=0) if rows_y else np.zeros((0,))
    return X, y


def subsample_stratified(
    X: np.ndarray, y: np.ndarray, n: int, *, seed: int = 0,
    strat_feature: int = 2,  # drpm_peak_neg
    n_buckets: int = 10,
) -> tuple[np.ndarray, np.ndarray]:
    """Subsample ``n`` rows, stratified by one feature's percentile.

    Without stratification the rare hard-braking states are swamped.
    """
    if X.shape[0] <= n:
        return X, y
    rng = np.random.default_rng(seed)
    xs = X[:, strat_feature]
    edges = np.quantile(xs, np.linspace(0, 1, n_buckets + 1))
    edges[-1] += 1e-6
    bucket_ids = np.digitize(xs, edges[1:-1])
    per_bucket = n // n_buckets
    out_idx = []
    for b in range(n_buckets):
        mask = bucket_ids == b
        idxs = np.flatnonzero(mask)
        if len(idxs) == 0:
            continue
        pick = rng.choice(idxs, size=min(per_bucket, len(idxs)),
                          replace=False)
        out_idx.append(pick)
    out_idx = np.concatenate(out_idx)
    return X[out_idx], y[out_idx]


# =====================================================================
#  PySR distillation fit
# =====================================================================

def fit_pysr(X: np.ndarray, y: np.ndarray, *, args, output_dir: Path):
    # When this file is launched as ``python scripts/pysr_distill_teacher.py``
    # Python prepends ``scripts/`` to sys.path, which then shadows the
    # real ``pysr`` package with the local ``scripts/pysr/`` subpackage
    # (unrelated tooling).  Evict that entry before importing PySR.
    import sys as _sys
    _scripts_dir = str(Path(__file__).resolve().parent)
    _sys.path[:] = [p for p in _sys.path if p != _scripts_dir and p != ""]
    _sys.modules.pop("pysr", None)

    import sympy as _sympy
    from pysr import PySRRegressor

    model = PySRRegressor(
        niterations=args.niterations,
        populations=args.populations,
        population_size=args.population_size,
        ncycles_per_iteration=args.ncycles,

        maxsize=args.maxsize,
        maxdepth=10,
        binary_operators=["+", "-", "*", "/", "min", "max"],
        unary_operators=[
            "relu(x::T) where {T} = x > 0 ? x : zero(T)",
            "negrelu(x::T) where {T} = x < 0 ? -x : zero(T)",
            "step(x::T) where {T} = x > 0 ? one(T) : zero(T)",
            "safetanh(x::T) where {T} = tanh(clamp(x, T(-30), T(30)))",
            "safeexp(x::T) where {T} = exp(clamp(x, T(-30), T(30)))",
            "safelog(x::T) where {T} = log(abs(x) + T(1e-9))",
        ],
        extra_sympy_mappings={
            "relu":    lambda x: (_sympy.Abs(x) + x) / 2,
            "negrelu": lambda x: (_sympy.Abs(x) - x) / 2,
            "step":    lambda x: _sympy.Heaviside(x),
            "safetanh": lambda x: _sympy.tanh(x),
            "min":     lambda a, b: _sympy.Min(a, b),
            "max":     lambda a, b: _sympy.Max(a, b),
            "safeexp": lambda x: _sympy.exp(_sympy.Min(_sympy.Max(x, -30), 30)),
            "safelog": lambda x: _sympy.log(_sympy.Abs(x) + 1e-9),
        },
        complexity_of_operators={
            "/":        2,
            "safetanh": 3,
            "safeexp":  4,
            "safelog":  4,
        },
        complexity_of_constants=2,
        nested_constraints={
            "relu":     {"relu": 0, "negrelu": 0, "step": 0, "safetanh": 0,
                         "safeexp": 0, "safelog": 0},
            "negrelu":  {"relu": 0, "negrelu": 0, "step": 0, "safetanh": 0,
                         "safeexp": 0, "safelog": 0},
            "step":     {"relu": 0, "negrelu": 0, "step": 0, "safetanh": 0,
                         "safeexp": 0, "safelog": 0},
            "safetanh": {"relu": 0, "negrelu": 0, "step": 0, "safetanh": 0,
                         "safeexp": 0, "safelog": 0},
            "safeexp":  {"relu": 0, "negrelu": 0, "step": 0, "safetanh": 0,
                         "safeexp": 0, "safelog": 0},
            "safelog":  {"relu": 0, "negrelu": 0, "step": 0, "safetanh": 0,
                         "safeexp": 0, "safelog": 0},
        },
        constraints={
            "/":        (-1, 6),
            "safeexp":  8,
            "safelog":  8,
            "safetanh": 8,
        },

        # Plain MSE — no Julia callback, full parallelism.
        elementwise_loss="loss(x, y) = (x - y) ^ 2",
        parsimony=args.parsimony,
        adaptive_parsimony_scaling=20.0,
        weight_optimize=0.10,
        warmup_maxsize_by=0.5,

        # Full parallelism: MSE loss runs on the Julia side, no Python
        # state to synchronise.
        parallelism="multithreading",
        deterministic=False,
        random_state=args.seed,

        output_directory=str(output_dir),
        run_id="distill",
        progress=False,
        verbosity=1,
        model_selection="best",
    )

    print(f"[distill] PySR fit on {X.shape[0]} rows × {X.shape[1]} features")
    t0 = time.time()
    model.fit(X, y, variable_names=list(FEATURE_NAMES))
    print(f"[distill] PySR fit done in {(time.time() - t0) / 60:.1f} min")
    return model


# =====================================================================
#  Validate: re-score top-K expressions on the true scorer
# =====================================================================

def validate_top_k(
    model, *,
    rides_per_profile: int,
    n_perts: int,
    k: int,
    seed: int,
) -> list[dict]:
    """Pick the top-k by MSE rank, run them through PopulationEvaluator."""
    eqs = model.equations_
    if eqs is None or len(eqs) == 0:
        print("[validate] no equations produced")
        return []

    # Sort by loss ascending and keep k.  PySR's `equations_` is a
    # DataFrame with columns: complexity, loss, score, equation, ...
    top = eqs.sort_values("loss", ascending=True).head(k)
    exprs = [str(e) for e in top["equation"].tolist()]

    rng = np.random.default_rng(seed)
    rides = generate_ride_set(
        seeds_per_profile=rides_per_profile, base_seed=seed + 1)
    perts = _sample_perturbations(rng, n_perts)

    ev = PopulationEvaluator(rides, perts, seed_base=seed + 2)
    print(f"[validate] scoring top {len(exprs)} expressions on "
          f"{len(rides)}×{len(perts)} = {len(rides)*len(perts)} trajectories")
    out = []
    for expr in exprs:
        res = ev.evaluate(expr)
        out.append({
            "expression": expr,
            "cvar20": res["cvar20"],
            "mean": res["mean"],
            "nominal": res["nominal"],
        })
    out.sort(key=lambda r: r["cvar20"], reverse=True)
    return out


# =====================================================================
#  CLI
# =====================================================================

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--theta", type=Path, required=True)
    p.add_argument("--rollout-rides-per-profile", type=int, default=5)
    p.add_argument("--rollout-perts", type=int, default=4)
    p.add_argument("--dataset-size", type=int, default=5000)
    p.add_argument("--dataset-seed", type=int, default=42)
    # PySR params
    p.add_argument("--niterations", type=int, default=5)
    p.add_argument("--populations", type=int, default=8)
    p.add_argument("--population-size", type=int, default=33)
    p.add_argument("--ncycles", type=int, default=300)
    p.add_argument("--maxsize", type=int, default=35)
    p.add_argument("--parsimony", type=float, default=0.0005)
    p.add_argument("--seed", type=int, default=0)
    # Validation
    p.add_argument("--validate-top-k", type=int, default=5)
    p.add_argument("--validate-rides-per-profile", type=int, default=10)
    p.add_argument("--validate-perts", type=int, default=20)
    # Output
    p.add_argument("--output-dir", type=Path,
                   default=Path("sim/output/pysr/distill"))
    return p.parse_args()


def main():
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    blob = np.load(args.theta)
    theta = blob["theta"]
    print(f"[distill] loaded theta from {args.theta}  "
          f"({theta.size} params)")

    # ── Stage 2a: rollout ────────────────────────────────────────────
    print("[distill] rolling out teacher to build dataset...")
    t0 = time.time()
    X, y = rollout_teacher(
        theta,
        rides_per_profile=args.rollout_rides_per_profile,
        n_perts=args.rollout_perts,
        seed=args.dataset_seed,
    )
    print(f"[distill] rollout produced {X.shape[0]} samples in "
          f"{time.time() - t0:.1f} s")

    X_ss, y_ss = subsample_stratified(
        X, y, args.dataset_size, seed=args.dataset_seed)
    print(f"[distill] subsampled to {X_ss.shape[0]} rows (stratified)")

    # Save the dataset for reproducibility / retries.
    np.savez(args.output_dir / "dataset.npz", X=X_ss, y=y_ss)

    # ── Stage 2b: PySR fit ───────────────────────────────────────────
    model = fit_pysr(X_ss, y_ss, args=args, output_dir=args.output_dir)

    # ── Stage 2c: validate on the true scorer ───────────────────────
    top = validate_top_k(
        model,
        rides_per_profile=args.validate_rides_per_profile,
        n_perts=args.validate_perts,
        k=args.validate_top_k,
        seed=args.seed + 1000,
    )

    print("\n[distill] === Final leaderboard (by CVaR-20) ===")
    for i, r in enumerate(top, 1):
        print(f"  #{i}  cvar20={r['cvar20']:6.2f}  "
              f"mean={r['mean']:6.2f}  nom={r['nominal']:6.2f}")
        print(f"       {r['expression']}")

    # Save leaderboard.
    import csv
    with open(args.output_dir / "leaderboard.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "cvar20", "mean", "nominal", "expression"])
        for i, r in enumerate(top, 1):
            w.writerow([i, r["cvar20"], r["mean"], r["nominal"],
                        r["expression"]])
    print(f"[distill] saved leaderboard -> {args.output_dir / 'leaderboard.csv'}")

    # Append the top-K to the centralised scoreboard so we can compare
    # distilled expressions across runs + against neural / baseline
    # entries in one place.
    fixture_tag = (
        f"distill_val_{args.validate_rides_per_profile}x"
        f"{args.validate_perts}"
    )
    for i, r in enumerate(top, 1):
        append_scoreboard(
            source="pysr_distill",
            run_id=f"{args.output_dir.name}#top{i}",
            cvar20=float(r["cvar20"]),
            composite_mean=float(r["mean"]),
            fixture=fixture_tag,
            notes=f"rank={i} nominal={r['nominal']:.2f}",
            artifact=str(args.output_dir / "leaderboard.csv"),
        )


if __name__ == "__main__":
    main()
