"""Diagnostic: find first tick where numpy and JAX diverge."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np

from sim.physics import simulate_ride, CTRL_PERIOD, DT
from sim.ride_generator import generate_ride_set
from scripts.research.validate_jax_ride import build_jax_kwargs
from sim.jax.physics_ride import simulate_ride_fixed_gain_jax


def main():
    rides = generate_ride_set(seeds_per_profile=1, base_seed=42)
    ride = rides[0]

    ctrl_steps = max(1, int(CTRL_PERIOD / DT))
    n_ticks_ride = ride.n // ctrl_steps
    max_n = n_ticks_ride + 1

    logs_np = simulate_ride(
        controller=0.5, ride=ride,
        rpm_noise_sigma=0.0, iq_noise_sigma=0.0,
        iq_bias=0.0, vcap_noise_sigma=0.0,
    )

    kwargs, _ = build_jax_kwargs(ride, n_ticks_padded=max_n, k_gain=0.5)
    out = simulate_ride_fixed_gain_jax(**kwargs)
    logs_jax = {k: np.asarray(v) for k, v in out.items()}

    keys = ["speed", "speed_baseline", "motor_rpm", "current", "pedal", "grade",
            "vcap", "brake_demand"]

    # Per-channel peak diff tick.
    print("Per-channel peak-abs-diff across the whole ride:")
    for key in keys:
        a = np.asarray(logs_np[key])
        b = np.asarray(logs_jax[key])[:n_ticks_ride]
        diff = np.abs(a - b)
        j = int(np.argmax(diff))
        print(f"  {key:14s}  peak={diff[j]:.3e} at tick {j:4d}  "
              f"np={a[j]:.6g}  jax={b[j]:.6g}")
    print()

    first_bad = None
    for i in range(n_ticks_ride):
        for key in keys:
            a = logs_np[key][i]
            b = logs_jax[key][i]
            if abs(a - b) > 1e-6:
                if first_bad is None or i < first_bad[0]:
                    first_bad = (i, key, a, b)

    if first_bad is None:
        print("No divergence found.")
        return
    i, key, a, b = first_bad
    print(f"First divergence at tick {i}:")
    print(f"  channel:  {key}")
    print(f"  numpy  = {a!r}")
    print(f"  jax    = {b!r}")
    print(f"  diff   = {abs(a-b):.3e}")
    print()
    print(f"Context at tick {i}:")
    for key in keys:
        print(f"  {key:14s}  np={logs_np[key][i]:.6g}  jax={logs_jax[key][i]:.6g}")
    # Also look at tick i-1 and i+1.
    print(f"\nTick {i-1}:")
    for key in keys:
        if i-1 >= 0:
            print(f"  {key:14s}  np={logs_np[key][i-1]:.6g}  jax={logs_jax[key][i-1]:.6g}")
    # Ride inputs at tick i.
    print(f"\nRide inputs near tick {i}:")
    ti0 = i * ctrl_steps
    print(f"  brake_torque[1ms]:  {ride.brake_torque[ti0:ti0+10]}")
    print(f"  grade_rad[1ms]:     {ride.grade_rad[ti0:ti0+10]}")
    print(f"  pedal_active[1ms]:  {ride.pedal_active[ti0:ti0+10]}")


if __name__ == "__main__":
    main()
