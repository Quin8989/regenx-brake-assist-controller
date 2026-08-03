"""Per-scenario breakdown + trace analysis of the 3 tuned strategies.

Prints:
  1. per-scenario (E,T,S,comp) for each strategy, sorted by weight
  2. where each strategy is *worst* relative to the best at that scenario
  3. diagnostic stats per trace: mean |i_cmd - demand|, slip events, stalled
     samples, low-speed tracking fraction
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "firmware"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np

from config.settings import REGEN_STRATEGY_PARAMS
from sim.physics import simulate
from sim.scoring import (
    SCENARIOS, MASS_DISTRIBUTION, _unpack_scenario, _scenario_sim_config,
    _crop_to_speed_band, score, TRACKING_CUTOFF_KMH,
)
from sim.strategies import (
    PiSlipRegenStrategy, AimdFfRegenStrategy,
)

STRATS = {
    "pi_controller": PiSlipRegenStrategy,
    "aimd_ff": AimdFfRegenStrategy,
}


def factory(cls, key):
    params = REGEN_STRATEGY_PARAMS[key]

    def make():
        return cls(**params)
    return make


def trace_diagnostics(result):
    """Extract human-readable per-trace stats."""
    spd = result['speed']
    spd_base = result['speed_baseline']
    dt = float(result['t'][1] - result['t'][0]) if len(result['t']) > 1 else 0.01
    mask = spd >= TRACKING_CUTOFF_KMH
    if not np.any(mask) or len(spd) < 2:
        return dict(undershoot_pct=0.0, overshoot_pct=0.0, jerk_rms=0.0,
                    slip_events=0, sat_frac=0.0)
    # Decel-error buckets vs traditional-brake baseline (m/s^2 integrated over time).
    a_ours = -np.diff(spd / 3.6) / dt
    a_base = -np.diff(spd_base / 3.6) / dt
    mean_base = float(np.mean(np.abs(a_base)))
    if mean_base < 0.05:
        under = over = 0.0
    else:
        err = a_ours - a_base   # +: braked harder than baseline; -: softer
        under = float(np.sum(np.clip(-err, 0.0, None))) / (np.sum(np.abs(a_base)) + 1e-9) * 100.0
        over = float(np.sum(np.clip(err, 0.0, None))) / (np.sum(np.abs(a_base)) + 1e-9) * 100.0
    v_ms = spd / 3.6
    jerk = np.diff(np.diff(v_ms) / dt) / dt
    jerk_rms = float(np.sqrt(np.mean(jerk**2))) if len(jerk) else 0.0
    # slip events = samples where commanded i dropped by >20 % in one step
    i_cmd = result.get('i_cmd', None)
    slip = 0
    if i_cmd is not None and len(i_cmd) > 2:
        di = np.diff(i_cmd)
        slip = int(np.sum((di < -2.0)))  # drops >2 A in one tick
    # saturation fraction: iq_actual near I_MAX
    iq = result.get('iq_actual', None)
    sat_frac = 0.0
    if iq is not None and len(iq):
        sat_frac = float(np.mean(np.abs(iq) > 40.0)) * 100.0
    return dict(undershoot_pct=under, overshoot_pct=over, jerk_rms=jerk_rms,
                slip_events=slip, sat_frac=sat_frac)


def run_all():
    results = {}  # results[name][strat] = dict
    for tup in SCENARIOS:
        name, v_start, v_end, decel, weight, emerg, kind = _unpack_scenario(tup)
        results[name] = dict(weight=weight, emerg=emerg, kind=kind,
                             v_start=v_start, v_end=v_end, decel=decel,
                             strat={})
        for key, cls in STRATS.items():
            e_acc = t_acc = s_acc = 0.0
            diag_acc = dict(undershoot_pct=0.0, overshoot_pct=0.0,
                            jerk_rms=0.0, slip_events=0.0, sat_frac=0.0)
            for mass, mw in MASS_DISTRIBUTION:
                brake, kw, crop_v = _scenario_sim_config(
                    kind, v_start, v_end, decel, mass)
                strat = factory(cls, key)()
                result = simulate(strat, brake, **kw)
                cropped = result if crop_v is None else _crop_to_speed_band(result, crop_v)
                sc = score(cropped, mass, emergency=emerg)
                e_acc += mw * sc['energy']
                t_acc += mw * sc['tracking']
                s_acc += mw * sc['smoothness']
                d = trace_diagnostics(cropped)
                for k in diag_acc:
                    diag_acc[k] += mw * d[k]
            results[name]['strat'][key] = dict(
                E=e_acc, T=t_acc, S=s_acc,
                comp=(0.0 if emerg else 0.40) * e_acc + (0.80 if emerg else 0.40) * t_acc + 0.20 * s_acc,
                **diag_acc,
            )
    return results


def print_report(res):
    scenarios_sorted = sorted(res.items(), key=lambda kv: -kv[1]['weight'])
    print("\n=== PER-SCENARIO COMPOSITE (sorted by scenario weight) ===")
    print(f"{'scenario':<20} {'wt':>5} {'kind':>5}  "
          + "  ".join(f"{k:>18}" for k in STRATS))
    for name, info in scenarios_sorted:
        row = f"{name:<20} {info['weight']:.2f}  {info['kind']:>5}  "
        for k in STRATS:
            s = info['strat'][k]
            row += f"{s['E']:4.0f}/{s['T']:4.0f}/{s['S']:4.0f}={s['comp']:5.1f}  "
        print(row)

    print("\n=== DIMENSION TOTALS (weighted across scenarios) ===")
    tot = {k: dict(E=0, T=0, S=0, comp=0) for k in STRATS}
    for info in res.values():
        for k in STRATS:
            s = info['strat'][k]
            for dim in ('E', 'T', 'S', 'comp'):
                tot[k][dim] += info['weight'] * s[dim]
    print(f"{'strat':<16} {'E':>6} {'T':>6} {'S':>6} {'comp':>6}")
    for k in STRATS:
        t = tot[k]
        print(f"{k:<16} {t['E']:6.1f} {t['T']:6.1f} {t['S']:6.1f} {t['comp']:6.2f}")

    print("\n=== LOSS-BUCKET: where each strategy under-performs the leader ===")
    for name, info in scenarios_sorted:
        best_k = max(STRATS, key=lambda k: info['strat'][k]['comp'])
        best_c = info['strat'][best_k]['comp']
        for k in STRATS:
            gap = best_c - info['strat'][k]['comp']
            if gap > 5.0:
                s = info['strat'][k]
                print(f"  {name:<18} {k:<14} gap=-{gap:4.1f} "
                      f"(E={s['E']:4.0f} T={s['T']:4.0f} S={s['S']:4.0f}) "
                      f"under={s['undershoot_pct']:4.1f}% "
                      f"over={s['overshoot_pct']:4.1f}% "
                      f"jerk={s['jerk_rms']:5.2f} sat={s['sat_frac']:4.1f}%")

    print("\n=== DIAGNOSTICS (weighted avg across scenarios) ===")
    print(f"{'strat':<16} {'under%':>8} {'over%':>8} {'jerk_rms':>10} {'sat%':>8}")
    for k in STRATS:
        u = o = j = sat = 0.0
        for info in res.values():
            s = info['strat'][k]
            w = info['weight']
            u += w * s['undershoot_pct']
            o += w * s['overshoot_pct']
            j += w * s['jerk_rms']
            sat += w * s['sat_frac']
        print(f"{k:<16} {u:8.2f} {o:8.2f} {j:10.2f} {sat:8.2f}")


if __name__ == "__main__":
    res = run_all()
    print_report(res)
