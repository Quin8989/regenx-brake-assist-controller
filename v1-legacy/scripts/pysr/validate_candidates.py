"""Wrap each PySR expression as a real regen strategy and score it.

Pipeline
--------
1.  Read the Pareto front from :mod:`scripts.pysr.imitate_aimd`'s
    ``hall_of_fame.csv`` (or any other equivalent CSV with
    ``equation`` / ``complexity`` / ``loss`` columns).
2.  For each row, parse the equation with sympy and lambdify it into
    a numpy-callable ``f(**features)``.
3.  Wrap it as a tiny stateful strategy:
       ``k_next = clip(f(rpm, ..., k_prev), K_FLOOR, K_CEIL)``
       ``i_cmd  = ff_current(rpm, k_next) * voltage_taper(vcap)``
    The strategy thus becomes a drop-in `_BaseStrategy`-shape object
    compatible with the sim's controller-dispatch contract.
4.  Run it through :func:`sim.scoring.score_strategy_robust` against
    the full 20-ride basket and record composite / energy / linearity.
5.  Write a sorted leaderboard CSV alongside the input hall of fame.

This is the validation step: it answers "does any PySR-found
expression actually beat the hand-written AIMD rule on our canonical
scoring?".  Expressions that survive this are candidates to become
the next shipped :class:`PysrStrategy`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "firmware"))
sys.path.insert(0, str(ROOT))

from config.settings import (  # noqa: E402
    MOTOR_PHASE_RESISTANCE_OHM as R_PHASE,
    FLUX_LINKAGE_WB as FLUX_LINKAGE,
    REGEN_CURRENT_MAX_A as I_MAX,
    VCAP_REGEN_TAPER_END_V as VCAP_TAPER_END,
    VCAP_REGEN_TAPER_START_V as VCAP_TAPER_START,
    VESC_MOTOR_POLE_PAIRS as POLE_PAIRS,
)
from regen.regen_control import ff_current_from_rpm, voltage_taper  # noqa: E402
from sim.scoring import score_strategy_robust  # noqa: E402


# =====================================================================
#  Expression -> callable
# =====================================================================

FEATURE_NAMES = (
    "rpm", "drpm_mean", "drpm_peak_neg",
    "iq", "duty_cycle", "vcap", "k_prev",
    "jerk_mean", "jerk_peak", "slip_delta",
    "decel_frac", "d_iq", "power_mech",
)

# Fixed safety clamps (mirror AimdFfRegenStrategy).
K_FLOOR = 0.0
K_CEIL = 1.2    # slightly above 1.0 in case an expression wants to push
                # harder than pure FF; the i_max clamp catches the rest.


def lambdify_expression(equation: str) -> Callable[..., float]:
    """Parse a PySR equation string into a numpy callable.

    Accepts the operator vocabulary this project uses with PySR:
    ``relu``, ``negrelu``, ``step``.  Each maps to a sympy expression
    that ``lambdify`` knows how to emit against numpy.
    """
    import sympy as sp

    locals_map = {
        "relu":    lambda x: (sp.Abs(x) + x) / 2,
        "negrelu": lambda x: (sp.Abs(x) - x) / 2,
        "step":    sp.Heaviside,
    }
    syms = sp.symbols(FEATURE_NAMES)
    expr = sp.sympify(equation, locals=locals_map)
    # lambdify with Heaviside requires a numpy-backed implementation.
    # Sympy's Heaviside carries a default 2nd arg (value-at-zero), so
    # the callback must accept it even if we don't use it.
    fn = sp.lambdify(
        syms, expr,
        modules=[{"Heaviside": lambda x, _h=0.5: np.heaviside(x, 0.5)},
                 "numpy"],
    )
    return fn


# =====================================================================
#  PysrStrategy (module-level, picklable)
# =====================================================================

class PysrStrategy:
    """Stateful regen strategy wrapping a PySR-discovered expression.

    Module-level so it can be pickled for multiprocessing. Stores only
    the equation string; the lambdified callable is built lazily on
    first use and excluded from the pickle via ``__getstate__``.
    """

    def __init__(self, equation: str, label: str = ""):
        self.equation = equation
        self.label = label or "pysr"
        self.key = self.label
        self.name = f"PySR[{self.label}]"
        self._k = 0.1
        # State for per-tick finite differences.  Reset when the
        # voltage taper disables regen (see update()).
        self._drpm_mean_prev = 0.0
        self._drpm_peak_neg_prev = 0.0
        self._iq_prev = 0.0
        self._predict = None  # built lazily

    def __getstate__(self):
        # Drop the non-picklable lambdified fn; it'll rebuild on demand.
        state = self.__dict__.copy()
        state["_predict"] = None
        return state

    def _ensure_callable(self):
        if self._predict is None:
            self._predict = lambdify_expression(self.equation)
        return self._predict

    def update(self, ctx):
        predict = self._ensure_callable()
        rpm = ctx.preferred_rpm
        iq = ctx.preferred_iq
        taper = voltage_taper(ctx.vcap, VCAP_TAPER_START, VCAP_TAPER_END)
        if taper == 0.0:
            self._k = 0.1
            self._drpm_mean_prev = 0.0
            self._drpm_peak_neg_prev = 0.0
            self._iq_prev = 0.0
            return 0.0
        # Derived features (must match collect_imitation_dataset.py).
        jerk_mean = ctx.drpm_mean - self._drpm_mean_prev
        jerk_peak = ctx.drpm_peak_neg - self._drpm_peak_neg_prev
        slip_delta = ctx.drpm_peak_neg - ctx.drpm_mean
        decel_frac = ctx.drpm_mean / (rpm + 1.0)
        d_iq = iq - self._iq_prev
        power_mech = rpm * iq
        self._drpm_mean_prev = ctx.drpm_mean
        self._drpm_peak_neg_prev = ctx.drpm_peak_neg
        self._iq_prev = iq
        try:
            k_next = float(predict(
                rpm, ctx.drpm_mean, ctx.drpm_peak_neg,
                iq, ctx.duty_cycle, ctx.vcap, self._k,
                jerk_mean, jerk_peak, slip_delta,
                decel_frac, d_iq, power_mech,
            ))
        except (ValueError, ZeroDivisionError, FloatingPointError):
            k_next = self._k
        if not np.isfinite(k_next):
            k_next = self._k
        k_next = max(K_FLOOR, min(K_CEIL, k_next))
        self._k = k_next

        i_cmd = ff_current_from_rpm(
            rpm, k_next,
            flux_linkage=FLUX_LINKAGE,
            phase_resistance=R_PHASE,
            pole_pairs=POLE_PAIRS,
            current_limit=I_MAX,
        )
        return max(0.0, min(I_MAX, i_cmd * taper))


# =====================================================================
#  Main
# =====================================================================

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hall-of-fame",
                   default=str(ROOT / "sim" / "output" / "pysr"
                               / "imitate_aimd" / "hall_of_fame.csv"))
    p.add_argument("--out",
                   default=str(ROOT / "sim" / "output" / "pysr"
                               / "candidate_leaderboard.csv"))
    p.add_argument("--top", type=int, default=0,
                   help="If > 0, only evaluate the top-N by PySR loss.")
    p.add_argument("--workers", type=int, default=11,
                   help="Parallel workers for robust scoring. PysrStrategy "
                        "is picklable so we parallelize across perturbations.")
    p.add_argument("--include-baselines", action="store_true", default=True,
                   help="Also score aimd_ff / pi_controller for comparison.")
    args = p.parse_args()

    hof = Path(args.hall_of_fame)
    if not hof.exists():
        raise SystemExit(f"Hall of fame not found: {hof}")
    eqs = pd.read_csv(hof)
    if eqs.empty:
        raise SystemExit("Hall of fame is empty.")

    # Normalise column names (PySR historically used both).
    if "complexity" not in eqs.columns and "Complexity" in eqs.columns:
        eqs = eqs.rename(columns={"Complexity": "complexity",
                                   "Loss": "loss",
                                   "Equation": "equation"})

    eqs = eqs.sort_values("loss").reset_index(drop=True)
    if args.top > 0:
        eqs = eqs.head(args.top)

    print(f"[validate] evaluating {len(eqs)} candidates against full basket...")
    rows: list[dict] = []

    for _, row in eqs.iterrows():
        label = f"c{int(row['complexity'])}"
        try:
            # Parse once up-front so we catch sympy errors before scoring.
            lambdify_expression(row["equation"])
        except Exception as exc:
            rows.append(dict(
                complexity=int(row["complexity"]),
                imitation_loss=float(row["loss"]),
                composite=float("nan"),
                energy=float("nan"),
                linearity=float("nan"),
                robust_mean=float("nan"),
                robust_cvar20=float("nan"),
                equation=row["equation"],
                error=f"parse: {exc!r}",
            ))
            continue

        try:
            res = score_strategy_robust(
                strat_cls=PysrStrategy,
                strat_params={"equation": row["equation"], "label": label},
                workers=args.workers,
            )
        except Exception as exc:
            rows.append(dict(
                complexity=int(row["complexity"]),
                imitation_loss=float(row["loss"]),
                composite=float("nan"),
                capture=float("nan"),
                fidelity=float("nan"),
                robust_mean=float("nan"),
                robust_cvar20=float("nan"),
                equation=row["equation"],
                error=f"score: {exc!r}",
            ))
            continue

        rows.append(dict(
            complexity=int(row["complexity"]),
            imitation_loss=float(row["loss"]),
            composite=float(res["nominal"]),
            capture=float(res["capture_mean"]),
            fidelity=float(res["fidelity_mean"]),
            robust_mean=float(res["mean"]),
            robust_cvar20=float(res["cvar20"]),
            equation=row["equation"],
            error="",
        ))
        print(f"  c={int(row['complexity']):2d} "
              f"composite={res['nominal']:.2f} "
              f"(eff={res['capture_mean']:.1f} fidelity={res['fidelity_mean']:.1f}) "
              f"cvar20={res['cvar20']:.2f}")

    # Baselines ------------------------------------------------------
    if args.include_baselines:
        from config.settings import REGEN_STRATEGY_PARAMS
        from regen.strategies import STRATEGY_BY_NAME
        for base in ("aimd_ff", "pi_controller", "fixed_ff"):
            cls = STRATEGY_BY_NAME[base]
            params = REGEN_STRATEGY_PARAMS.get(base, {})
            res = score_strategy_robust(
                strat_cls=cls, strat_params=params, workers=args.workers,
            )
            rows.append(dict(
                complexity=-1,
                imitation_loss=float("nan"),
                composite=float(res["nominal"]),
                capture=float(res["capture_mean"]),
                fidelity=float(res["fidelity_mean"]),
                robust_mean=float(res["mean"]),
                robust_cvar20=float(res["cvar20"]),
                equation=f"[baseline: {base}]",
                error="",
            ))
            print(f"  baseline {base:14s} composite={res['nominal']:.2f} "
                  f"cvar20={res['cvar20']:.2f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows).sort_values(
        by="composite", ascending=False, na_position="last"
    )
    df.to_csv(out, index=False)
    print(f"\n[validate] wrote leaderboard to {out}")
    print(df.to_string(index=False,
                       columns=["complexity", "composite", "robust_cvar20",
                                "capture", "fidelity", "equation"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
