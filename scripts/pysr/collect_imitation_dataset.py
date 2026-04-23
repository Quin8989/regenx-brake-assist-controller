"""Record per-tick (observation, action) pairs from a tuned AIMD-FF run.

Produces the dataset PySR will try to imitate.  We run the tuned
``aimd_ff`` strategy through the full 20-ride basket via
:func:`sim.physics.simulate_ride`, and on every control tick we
capture what the strategy *saw* and what it *decided*.

Features (strategy-visible only — nothing the firmware can't read at
runtime):

    Raw telemetry:
      rpm              motor mechanical RPM (ctx.rpm — averaged telemetry)
      rpm_fast         low-latency LispBM rpm push (equal to rpm in sim)
      drpm_mean        mean d(rpm)/dt over the 10 ms LispBM window
      drpm_peak_neg    most-negative per-sample d(rpm)/dt in the window
      iq               preferred_iq (LispBM mean when fresh, else averaged)
      duty_cycle       duty cycle (-1..1)
      vcap             bus / supercap voltage (V)
      k_prev           strategy's k_eff at the *start* of this tick

    Derived (firmware-reproducible at a few FLOPs each tick):
      drpm_mean_prev      drpm_mean from the previous tick (per-ride)
      drpm_peak_neg_prev  drpm_peak_neg from the previous tick
      iq_prev             iq from the previous tick
      jerk_mean           drpm_mean - drpm_mean_prev
      jerk_peak           drpm_peak_neg - drpm_peak_neg_prev
      d_iq                iq - iq_prev
      slip_delta          drpm_peak_neg - drpm_mean (sample-vs-trend gap)
      decel_frac          drpm_mean / (rpm + 1) (scale-invariant decel)
      power_mech          rpm * iq (mechanical power proxy)

Targets:

    k_next          strategy's k_eff *after* this tick's update
    delta_k         k_next - k_prev  (what AIMD actually commits to)
    i_cmd           final commanded current (A)

CSV is written to data/pysr_aimd_imitation.csv by default.  The script
also prints a short summary (shape, per-ride counts, delta_k histogram)
so you can eyeball it before handing it to PySR.

Usage:
    .venv\\Scripts\\python.exe -m scripts.pysr.collect_imitation_dataset \\
        [--strategy aimd_ff] [--rows 5000] [--out data/pysr_aimd_imitation.csv] \\
        [--brake-only]
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Type

import numpy as np
import pandas as pd

# Repo-local imports (firmware/ and sim/ are not installed packages).
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "firmware"))
sys.path.insert(0, str(ROOT))

from config.settings import REGEN_STRATEGY_PARAMS  # noqa: E402
from regen.strategies import STRATEGY_BY_NAME  # noqa: E402
from sim.physics import simulate_ride  # noqa: E402
from sim.ride_generator import generate_ride_set  # noqa: E402


# =====================================================================
#  Recording wrapper
# =====================================================================

@dataclass
class _Record:
    rpm: float
    rpm_fast: float
    drpm_mean: float
    drpm_peak_neg: float
    iq: float
    duty_cycle: float
    vcap: float
    k_prev: float
    k_next: float
    i_cmd: float
    brake_active: bool       # inferred from k-delta signs — see collect()
    ride_idx: int
    tick: int


def _make_recording_strategy(base_cls: Type, params: dict):
    """Return a subclass that records (ctx state, k_prev, k_next, i_cmd).

    The wrapper is built fresh per ride so that recorded rows carry the
    correct ``ride_idx``.  Records live on a shared list the caller owns.
    """

    class _Recording(base_cls):
        def __init__(self, sink: list[_Record], ride_idx: int, **kw):
            super().__init__(**kw)
            self._sink = sink
            self._ride_idx = ride_idx
            self._tick = 0

        def update(self, ctx):
            k_prev = float(self._k_eff)
            i_cmd = super().update(ctx)
            k_next = float(self._k_eff)
            # LispBM push signals are exposed via rpm_fast / iq_mean
            # when fresh; sim always provides them.
            rpm_fast = ctx.rpm if ctx.rpm_fast is None else ctx.rpm_fast
            iq = ctx.iq_actual if ctx.iq_mean is None else ctx.iq_mean
            self._sink.append(_Record(
                rpm=float(ctx.rpm),
                rpm_fast=float(rpm_fast),
                drpm_mean=float(ctx.drpm_mean),
                drpm_peak_neg=float(ctx.drpm_peak_neg),
                iq=float(iq),
                duty_cycle=float(ctx.duty_cycle),
                vcap=float(ctx.vcap),
                k_prev=k_prev,
                k_next=k_next,
                i_cmd=float(i_cmd),
                brake_active=False,  # filled later from ride.brake_windows
                ride_idx=self._ride_idx,
                tick=self._tick,
            ))
            self._tick += 1
            return i_cmd

    return lambda: _Recording(sink=params["_sink"],
                              ride_idx=params["_ride_idx"],
                              **{k: v for k, v in params.items()
                                 if not k.startswith("_")})


# =====================================================================
#  Collect
# =====================================================================

def collect(strategy_name: str, rows_target: int,
            brake_only: bool) -> pd.DataFrame:
    if strategy_name not in STRATEGY_BY_NAME:
        raise SystemExit(f"Unknown strategy: {strategy_name}")
    cls = STRATEGY_BY_NAME[strategy_name]
    tuned = dict(REGEN_STRATEGY_PARAMS.get(strategy_name, {}))
    if not tuned:
        raise SystemExit(
            f"No tuned params in REGEN_STRATEGY_PARAMS for {strategy_name}"
        )

    rides = generate_ride_set()   # default 20-ride weighted basket
    print(f"[collect] {strategy_name} | {len(rides)} rides | "
          f"tuned params: {tuned}")

    sink: list[_Record] = []
    for ride_idx, ride in enumerate(rides):
        bucket = dict(tuned)
        bucket["_sink"] = sink
        bucket["_ride_idx"] = ride_idx
        factory = _make_recording_strategy(cls, bucket)
        simulate_ride(factory(), ride)

    df = pd.DataFrame([r.__dict__ for r in sink])

    # Per-ride derived features.  Per-tick finite differences (jerk)
    # are reset at ride boundaries so no cross-ride leakage.  The
    # instantaneous combinations (slip_delta, decel_frac, power_mech)
    # are strategy-visible: the firmware can compute them in a handful
    # of FLOPs each tick.
    df = df.sort_values(["ride_idx", "tick"]).reset_index(drop=True)
    g = df.groupby("ride_idx")
    df["drpm_mean_prev"] = g["drpm_mean"].shift(1).fillna(0.0)
    df["drpm_peak_neg_prev"] = g["drpm_peak_neg"].shift(1).fillna(0.0)
    df["iq_prev"] = g["iq"].shift(1).fillna(0.0)
    df["jerk_mean"] = df["drpm_mean"] - df["drpm_mean_prev"]
    df["jerk_peak"] = df["drpm_peak_neg"] - df["drpm_peak_neg_prev"]
    df["d_iq"] = df["iq"] - df["iq_prev"]
    df["slip_delta"] = df["drpm_peak_neg"] - df["drpm_mean"]
    df["decel_frac"] = df["drpm_mean"] / (df["rpm"] + 1.0)
    df["power_mech"] = df["rpm"] * df["iq"]

    # Tag ticks that fall inside a brake window (strategy doesn't see
    # this at runtime; we use it only for sampling bias).
    window_mask = np.zeros(len(df), dtype=bool)
    for ride_idx, ride in enumerate(rides):
        sel = df["ride_idx"].values == ride_idx
        if not sel.any():
            continue
        t = df.loc[sel, "tick"].values * 0.01  # CTRL_PERIOD
        ride_mask = np.zeros_like(t, dtype=bool)
        for t0, t1 in ride.brake_windows:
            ride_mask |= (t >= t0) & (t <= t1)
        window_mask[sel] = ride_mask
    df["brake_active"] = window_mask

    df["delta_k"] = df["k_next"] - df["k_prev"]

    print(f"[collect] raw rows: {len(df)} | brake-active: {int(window_mask.sum())}")

    if brake_only:
        df = df[df["brake_active"]].reset_index(drop=True)
        print(f"[collect] after brake_only filter: {len(df)}")

    # Subsample, stratified by sign(delta_k) so rare MD events survive.
    if len(df) > rows_target:
        rng = np.random.default_rng(0)
        sign = np.sign(df["delta_k"].values)
        # Three groups: MD (neg), flat (zero), AI (pos).  Keep all MD
        # events, and fill the rest proportionally.
        md_mask = sign < 0
        ai_mask = sign > 0
        flat_mask = sign == 0

        keep_md = np.where(md_mask)[0]
        remaining = rows_target - len(keep_md)
        if remaining < 0:
            # Too many MD events (unlikely).  Random-subsample.
            keep_md = rng.choice(keep_md, size=rows_target, replace=False)
            keep_ai: np.ndarray = np.array([], dtype=int)
            keep_flat: np.ndarray = np.array([], dtype=int)
        else:
            ai_idx = np.where(ai_mask)[0]
            flat_idx = np.where(flat_mask)[0]
            ai_quota = int(remaining * len(ai_idx) / max(1, len(ai_idx) + len(flat_idx)))
            flat_quota = remaining - ai_quota
            keep_ai = rng.choice(ai_idx, size=min(ai_quota, len(ai_idx)), replace=False)
            keep_flat = rng.choice(flat_idx, size=min(flat_quota, len(flat_idx)), replace=False)

        keep = np.sort(np.concatenate([keep_md, keep_ai, keep_flat]))
        df = df.iloc[keep].reset_index(drop=True)
        print(f"[collect] subsampled to {len(df)} (MD={len(keep_md)}, "
              f"AI={len(keep_ai)}, flat={len(keep_flat)})")

    # Cheap eyeball report.
    print("[collect] delta_k summary:")
    print(df["delta_k"].describe())
    print("[collect] k_next summary:")
    print(df["k_next"].describe())

    return df


# =====================================================================
#  Entry point
# =====================================================================

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strategy", default="aimd_ff",
                   choices=list(STRATEGY_BY_NAME.keys()))
    p.add_argument("--rows", type=int, default=5000,
                   help="Target row count after subsampling (default 5000).")
    p.add_argument("--out", default=str(ROOT / "data" / "pysr_aimd_imitation.csv"))
    p.add_argument("--brake-only", action="store_true",
                   help="Drop ticks outside any brake window before subsampling.")
    args = p.parse_args()

    df = collect(args.strategy, args.rows, args.brake_only)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"[collect] wrote {len(df)} rows to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
