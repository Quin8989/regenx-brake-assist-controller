"""Sanity-check step for the PySR path.

Fit a symbolic regressor on the (observation, delta_k) pairs produced by
:mod:`scripts.pysr.collect_imitation_dataset`.  Success criterion: a
compact closed-form expression that explains the AIMD decision rule with
high R^2, at a node count small enough to be a drop-in controller.

What "compact" should look like
-------------------------------
The hand-written ``aimd_ff`` update is, morally:

    delta_k = +k_ai * dt                       if no slip
    delta_k = -beta_md * (0.35 + 0.65*lvl) * k  on rising slip edge
    delta_k =  0                               inside an active slip burst

A good PySR result will recover something close to:

    delta_k ≈ k_ai*dt - (k_prev * beta_md * (...)) * relu(-drpm_peak_neg - thr)

or an equivalent ``max(0, ...)`` / ``min(0, ...)`` piecewise form.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# =====================================================================
#  PySR configuration
# =====================================================================

def build_regressor(niterations: int, populations: int,
                    population_size: int, maxsize: int,
                    workers: int,
                    output_dir: Path):
    """Construct the PySRRegressor.

    Import is deferred so `--help` works without Julia present.
    """
    from pysr import PySRRegressor  # type: ignore[import-not-found]
    import sympy  # type: ignore[import-not-found]

    parallel = workers > 1
    return PySRRegressor(
        # Search budget -----------------------------------------------
        niterations=niterations,
        populations=populations,
        population_size=population_size,
        ncycles_per_iteration=550,
        procs=workers if parallel else 0,

        # Expression shape --------------------------------------------
        maxsize=maxsize,
        maxdepth=12,
        binary_operators=["+", "-", "*", "/"],
        unary_operators=[
            "relu(x::T) where {T} = x > 0 ? x : zero(T)",
            "negrelu(x::T) where {T} = x < 0 ? -x : zero(T)",
            # `step` is the rising-edge / threshold detector.
            "step(x::T) where {T} = x > 0 ? one(T) : zero(T)",
        ],
        # Sympy mappings return numeric expressions (not booleans) so
        # equations that multiply step(...) by another term sympify
        # cleanly when PySR builds its export table.
        extra_sympy_mappings={
            "relu":    lambda x: (sympy.Abs(x) + x) / 2,
            "negrelu": lambda x: (sympy.Abs(x) - x) / 2,
            "step":    lambda x: sympy.Heaviside(x),
        },
        # Modest complexity penalty favours small expressions.
        parsimony=0.003,

        # Fitting behaviour -------------------------------------------
        # Default loss is MSE, which is what we want here.
        model_selection="best",     # Pareto best by validation-scored

        # Output ------------------------------------------------------
        output_directory=str(output_dir),
        run_id="imitate_aimd",
        progress=False,
        verbosity=1,
        random_state=0,
        # Deterministic requires serial; we favour throughput here and
        # set random_state for best-effort reproducibility instead.
        deterministic=False,
        parallelism="multithreading" if parallel else "serial",
    )


# =====================================================================
#  Entry point
# =====================================================================

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset",
                   default=str(ROOT / "data" / "pysr_aimd_imitation.csv"))
    p.add_argument("--target", choices=("delta_k", "k_next"),
                   default="k_next")
    p.add_argument("--rows", type=int, default=2500,
                   help="Row subsample used for fitting (PySR scales poorly "
                        "past a few thousand rows).")
    p.add_argument("--niterations", type=int, default=40)
    p.add_argument("--populations", type=int, default=20)
    p.add_argument("--population-size", type=int, default=33)
    p.add_argument("--maxsize", type=int, default=25)
    p.add_argument("--workers", type=int, default=11,
                   help="Julia worker processes (1 = serial/deterministic).")
    p.add_argument("--output-dir",
                   default=str(ROOT / "sim" / "output" / "pysr"))
    args = p.parse_args()

    df = pd.read_csv(args.dataset)
    if len(df) > args.rows:
        df = df.sample(n=args.rows, random_state=0).reset_index(drop=True)

    # Features the firmware can actually read at runtime.
    # 7 raw telemetry channels + 6 cheap derived quantities (4 FLOPs
    # each), giving PySR strong search primitives without expanding
    # the firmware's measurement surface.
    feature_cols = [
        "rpm", "drpm_mean", "drpm_peak_neg",
        "iq", "duty_cycle", "vcap", "k_prev",
        "jerk_mean", "jerk_peak", "slip_delta",
        "decel_frac", "d_iq", "power_mech",
    ]
    X = df[feature_cols].to_numpy(dtype=np.float64)
    y = df[args.target].to_numpy(dtype=np.float64)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[imitate] fitting PySR on {len(X)} rows | target={args.target}")
    print(f"[imitate] features={feature_cols}")
    print(f"[imitate] output_dir={out_dir}")

    model = build_regressor(
        niterations=args.niterations,
        populations=args.populations,
        population_size=args.population_size,
        maxsize=args.maxsize,
        workers=args.workers,
        output_dir=out_dir,
    )
    try:
        model.fit(X, y, variable_names=feature_cols)
        eqs = model.equations_
    except Exception as exc:  # pragma: no cover - export step best-effort
        # PySR's sympy/jax/torch export sometimes chokes on expressions
        # that contain boolean-valued operators.  The actual search has
        # already written the Pareto front to hall_of_fame.csv, so we
        # recover it from there instead of aborting.
        print(f"[imitate] WARN: PySR export failed ({exc!r}); "
              "falling back to hall_of_fame.csv")
        hof = out_dir / "imitate_aimd" / "hall_of_fame.csv"
        if not hof.exists():
            raise
        eqs = pd.read_csv(hof)
        model = None  # type: ignore[assignment]
    if eqs is None or (hasattr(eqs, "empty") and eqs.empty):
        print("[imitate] PySR returned no equations.")
        return 1

    # Materialise a compact summary.
    keep_cols = [c for c in ("complexity", "loss", "score", "equation")
                 if c in eqs.columns]
    summary = eqs[keep_cols].copy()
    print("\n[imitate] Pareto front:")
    with pd.option_context("display.max_colwidth", 120, "display.width", 200):
        print(summary.to_string(index=False))

    if model is not None:
        best = model.get_best()
        best_expr = best["equation"] if isinstance(best, dict) else best.equation
        y_pred = model.predict(X)
        ss_res = float(np.sum((y - y_pred) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0.0 else float("nan")
    else:
        # Fallback path: approximate R^2 from hall_of_fame loss column.
        # loss is MSE, so R^2 = 1 - loss / var(y).
        var_y = float(np.var(y))
        best_row = eqs.loc[eqs["loss"].idxmin()]
        best_expr = best_row["equation"]
        r2 = 1.0 - float(best_row["loss"]) / var_y if var_y > 0.0 else float("nan")
    print(f"\n[imitate] best expression: {best_expr}")
    print(f"[imitate] full-dataset R^2 (best): {r2:.4f}")

    summary_path = out_dir / "imitate_aimd_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({
            "target": args.target,
            "features": feature_cols,
            "rows": len(X),
            "r2_best": r2,
            "best_expression": str(best_expr),
            "pareto": summary.to_dict(orient="records"),
        }, f, indent=2)
    print(f"[imitate] wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
