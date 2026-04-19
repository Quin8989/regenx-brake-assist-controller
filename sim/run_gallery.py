"""sim.run_gallery - Generate interactive HTML gallery for selected strategies.

Charts:
  0. Motor Efficiency Map: RPM × carrier torque → η(%) — static reference.
  1. Trajectory: free-decel speed decay, deceleration & operating-point traces
     — brake, mass & speed sliders.

Usage:
    python -m sim.run_gallery --strategies pi_controller,aimd_ff
    python -m sim.run_gallery                                # all strategies
"""

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

from .physics import simulate
from .scoring import score
from .strategies import STRATEGY_BY_NAME, parse_strategy_names, strategy_classes_from_names
from .plotting import generate_efficiency_gallery_html
from config.settings import REGEN_STRATEGY_PARAMS

# =====================================================================
#  Best-known parameters — read from config/settings.py so they stay
#  in sync with run_tune results.  Strategies not in the settings dict
#  fall back to their class defaults.
# =====================================================================

BEST_PARAMS = dict(REGEN_STRATEGY_PARAMS)


# =====================================================================
#  Configuration
# =====================================================================

MAX_WORKERS = max(1, (os.cpu_count() or 2) - 1)

CHART_SPEEDS = np.arange(1.0, 42.0, 1.0)   # 1-41 km/h
TRAJ_T_END = 10.0
TRAJ_BRAKES = np.arange(2.0, 42.0, 2.0)    # 2-40 Nm
TRAJ_MASSES = np.array([60, 70, 80, 90, 100, 110, 120], dtype=float)

OUTPUT_DIR = "sim/output"
TAG = "eff_"


# =====================================================================
#  Batch worker (top-level for pickling)
# =====================================================================

def _w_trajectory_batch(strat_cls, params, brk, mass, speeds, t_end):
    results = []
    for v0 in speeds:
        s = strat_cls(**params)
        r = simulate(s, float(brk), v0_kmh=float(v0), mass_kg=float(mass),
                     t_end=t_end, constant_speed=False)
        sc = score(r, float(mass), emergency=False)
        results.append({
            'speed': r['speed'].copy(),
            'motor_rpm': r['motor_rpm'].copy(),
            'current': r['current'].copy(),
            'eta': r['eta'].copy(),
            'brake_demand': r['brake_demand'].copy(),
            'eff_brake': r['eff_brake'].copy(),
            'p_elec': r['p_elec'].copy(),
            'locked': r['locked'].copy(),
            'score': sc,
        })
    return results


# =====================================================================
#  Data collection
# =====================================================================

def collect_trajectory(pool, strat_classes, best):
    ref_cls = strat_classes[0]
    ref_p = best[ref_cls.key]
    r_ref = simulate(ref_cls(**ref_p), 5.0, v0_kmh=10.0, mass_kg=100.0,
                     t_end=TRAJ_T_END)
    n_time = len(r_ref['t'])
    t_traj = r_ref['t'].copy()

    speeds_list = [float(v) for v in CHART_SPEEDS]

    futures = {}
    for StratCls in strat_classes:
        strategy_name = StratCls.key
        params = best[strategy_name]
        for bi, brk in enumerate(TRAJ_BRAKES):
            for mi, mass in enumerate(TRAJ_MASSES):
                fut = pool.submit(_w_trajectory_batch, StratCls, params,
                                  float(brk), float(mass), speeds_list,
                                  TRAJ_T_END)
                futures[fut] = (strategy_name, bi, mi)

    n_spd = len(CHART_SPEEDS)
    data = {}
    done = 0
    total = len(futures)

    for fut in as_completed(futures):
        strategy_name, bi, mi = futures[fut]
        results = fut.result()
        if strategy_name not in data:
            data[strategy_name] = {}
        d = {
            'spd':   np.zeros((n_spd, n_time)),
            'rpm':   np.zeros((n_spd, n_time)),
            'cur':   np.zeros((n_spd, n_time)),
            'eta':   np.zeros((n_spd, n_time)),
            'brake_demand': np.zeros((n_spd, n_time)),
            'eff_brake': np.zeros((n_spd, n_time)),
            'p_elec': np.zeros((n_spd, n_time)),
            'lock': np.zeros((n_spd, n_time), dtype=bool),
            'scores': [None] * n_spd,
        }
        for si, result_dict in enumerate(results):
            n = min(len(result_dict['speed']), n_time)
            d['spd'][si, :n] = result_dict['speed'][:n]
            d['rpm'][si, :n] = result_dict['motor_rpm'][:n]
            d['cur'][si, :n] = result_dict['current'][:n]
            d['eta'][si, :n] = result_dict['eta'][:n]
            d['brake_demand'][si, :n] = result_dict['brake_demand'][:n]
            d['eff_brake'][si, :n] = result_dict['eff_brake'][:n]
            d['p_elec'][si, :n] = result_dict['p_elec'][:n]
            d['lock'][si, :n] = result_dict['locked'][:n]
            d['scores'][si] = result_dict['score']
        data[strategy_name][(bi, mi)] = d

        done += 1
        if done % 20 == 0 or done == total:
            sys.stdout.write(
                f"\r  Trajectory: {done}/{total} batches "
                f"({done * 100 // total}%)")
            sys.stdout.flush()

    print()
    return data, t_traj


# =====================================================================
#  CLI + main
# =====================================================================

def _parse_args():
    p = argparse.ArgumentParser(
        description="Generate interactive efficiency gallery for selected strategies.",
    )
    p.add_argument(
        "--strategies",
        default=None,
        help="Comma-separated strategy names (default: all registered strategies).",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    strategy_names = parse_strategy_names(args.strategies)
    strat_classes = strategy_classes_from_names(strategy_names)

    # Build best-params dict (only params, flat)
    best = {}
    for strategy_name in strategy_names:
        best[strategy_name] = BEST_PARAMS.get(strategy_name, {})

    names = {
        strategy_name: STRATEGY_BY_NAME[strategy_name](**best[strategy_name]).name
        for strategy_name in strategy_names
    }

    t0 = time.time()
    n_traj = len(strat_classes) * len(TRAJ_BRAKES) * len(TRAJ_MASSES) * len(CHART_SPEEDS)

    print("=" * 70)
    print("  RegenX \u2014 Efficiency Gallery")
    print(f"  Strategies: {', '.join(strategy_names)}")
    print(f"  Workers: {MAX_WORKERS}")
    print(f"  Trajectory sims: {n_traj}")
    print("=" * 70)

    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        print(f"\n\u2500\u2500 Trajectory ({n_traj} sims) \u2500\u2500")
        traj_data, t_traj = collect_trajectory(pool, strat_classes, best)

    print("\n\u2500\u2500 Generating Gallery \u2500\u2500")

    generate_efficiency_gallery_html(
        strategy_names, names,
        traj_data=traj_data, t_traj=t_traj,
        traj_brakes=TRAJ_BRAKES, traj_masses=TRAJ_MASSES,
        speeds=CHART_SPEEDS,
        output_dir=OUTPUT_DIR, tag=TAG)

    elapsed = time.time() - t0
    print(f"\n  Total: {n_traj} sims in {elapsed:.0f}s")
    print(f"  Gallery: {OUTPUT_DIR}/{TAG}gallery.html")
    print("  Done.")
