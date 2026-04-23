"""Diagnostic: dump detailed per-tick traces for the three strategies at
the slider-set in the screenshot (brake=22 Nm, mass=100 kg, v0=21 km/h).

Usage:
    python -m scripts.debug_strategy_traces
"""
import numpy as np

from sim.physics import simulate
from sim.scoring import score
from sim.strategies import STRATEGY_BY_NAME
from config.settings import REGEN_STRATEGY_PARAMS


def run_one(key, brake, v0, mass, t_end=10.0):
    cls = STRATEGY_BY_NAME[key]
    params = REGEN_STRATEGY_PARAMS.get(key, {})
    ctrl = cls(**params)
    r = simulate(ctrl, float(brake), v0_kmh=float(v0), mass_kg=float(mass),
                 t_end=t_end, constant_speed=False)
    sc = score(r, float(mass), emergency=False)
    return ctrl, r, sc


def summarize(key, r, sc):
    t = r['t']
    dt = float(t[1] - t[0])
    n = len(t)
    cur = r['current']
    eta = r['eta']
    locked = r['locked']
    p_elec = r['p_elec']
    p_copper = r['p_copper']
    p_brake = r['p_brake']
    brake_dem = r['brake_demand']
    rpm = r['motor_rpm']
    speed = r['speed']
    spd_base = r['speed_baseline']

    e_elec = float(np.trapezoid(p_elec, t))       # J harvested
    e_copper = float(np.trapezoid(p_copper, t))   # J in copper loss
    e_brake = float(np.trapezoid(p_brake, t))     # J in band
    e_in_mech = e_elec + e_copper + e_brake       # total mech energy

    lock_frac = float(locked.mean())
    mean_eta = float(eta[eta > 0].mean()) if (eta > 0).any() else 0.0
    peak_cur = float(cur.max())
    mean_cur = float(cur.mean())

    # Tracking vs baseline (smaller is better; baseline = direct-brake
    # wheel decel)
    track_err = float(np.sqrt(np.mean((speed - spd_base) ** 2)))

    # Smoothness: RMS jerk of current (proxy for chattery command)
    didt = np.diff(cur) / dt
    rms_jerk_cur = float(np.sqrt(np.mean(didt ** 2)))

    print(f"\n--- {key}  composite={sc['composite']:.2f}")
    print(f"  dt={dt*1000:.1f} ms  n_ticks={n}  t_end={t[-1]:.2f} s")
    print(f"  score breakdown: energy={sc['energy']:.2f}  "
          f"tracking={sc['tracking']:.2f}  smooth={sc['smoothness']:.2f}")
    print(f"  energy:    elec={e_elec:7.1f} J  copper={e_copper:6.1f} J  "
          f"band={e_brake:6.1f} J  total_mech={e_in_mech:7.1f} J")
    print(f"  current:   peak={peak_cur:6.2f} A  mean={mean_cur:6.2f} A  "
          f"rms_jerk={rms_jerk_cur:8.1f} A/s")
    print(f"  carrier:   locked_frac={lock_frac:.3f}  mean_eta(>0)={mean_eta*100:.1f}%")
    print(f"  tracking:  RMS(speed - baseline) = {track_err:.3f} km/h")
    print(f"  final spd: actual={speed[-1]:.2f} km/h  baseline={spd_base[-1]:.2f} km/h")
    print(f"  brake_demand: mean={brake_dem.mean():.2f} Nm  "
          f"max={brake_dem.max():.2f} Nm")


def dump_first_second(key, r):
    """Print the first 1 s of trace at 10 ms resolution for controller debug."""
    t = r['t']
    mask = t < 1.0
    print(f"\n  [{key}] first 1.0 s (dt between prints ≈ {(t[1]-t[0])*1000:.0f} ms):")
    print(f"    {'t(s)':>5}  {'spd':>6}  {'rpm':>6}  {'iA':>6}  "
          f"{'brkNm':>6}  {'lock':>4}  {'eta%':>5}")
    idx = np.arange(len(t))[mask][::5]  # every 5 ticks
    for i in idx:
        print(f"    {t[i]:5.2f}  {r['speed'][i]:6.2f}  {r['motor_rpm'][i]:6.0f}  "
              f"{r['current'][i]:6.2f}  {r['brake_demand'][i]:6.2f}  "
              f"{int(r['locked'][i]):>4}  {r['eta'][i]*100:5.1f}")


def main():
    brake, mass, v0 = 22.0, 100.0, 21.0
    print(f"scenario: brake={brake} Nm, mass={mass} kg, v0={v0} km/h")
    for key in ['pi_controller', 'aimd_ff']:
        ctrl, r, sc = run_one(key, brake, v0, mass)
        summarize(key, r, sc)

    print("\n" + "=" * 70)
    print("current-command peaks (>16 A) for each strategy — slip events:")
    for key in ['pi_controller', 'aimd_ff']:
        _, r, _ = run_one(key, brake, v0, mass)
        t = r['t']
        cur = r['current']
        # Find local peaks > 16 A.
        peaks = []
        for i in range(1, len(cur) - 1):
            if cur[i] > 16.0 and cur[i] > cur[i-1] and cur[i] > cur[i+1]:
                peaks.append((t[i], cur[i]))
        print(f"  {key}: {len(peaks)} peaks")
        for pt, pc in peaks[:12]:
            print(f"    t={pt:5.2f}s  i={pc:5.2f} A")
        if len(peaks) > 12:
            print(f"    ... and {len(peaks) - 12} more")

    # Look for unlocks (band slipping) — carrier rotating.
    print("\n" + "=" * 70)
    print("unlock events (carrier slipping):")
    for key in ['pi_controller', 'aimd_ff']:
        _, r, _ = run_one(key, brake, v0, mass)
        t = r['t']
        locked = r['locked']
        # Runs of False.
        unlocks = []
        i = 0
        while i < len(locked):
            if not locked[i]:
                j = i
                while j < len(locked) and not locked[j]:
                    j += 1
                unlocks.append((t[i], t[j-1] - t[i] + (t[1] - t[0])))
                i = j
            else:
                i += 1
        print(f"  {key}: {len(unlocks)} unlock events")
        for ut, ud in unlocks[:10]:
            print(f"    t={ut:5.2f}s  duration={ud*1000:6.1f} ms")


if __name__ == '__main__':
    main()
