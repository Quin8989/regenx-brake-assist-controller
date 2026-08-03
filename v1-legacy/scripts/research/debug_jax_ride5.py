"""Focus on ride 5 divergence."""
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

rides = generate_ride_set(seeds_per_profile=2, base_seed=42)
ride = rides[5]
print(f"ride 5: {ride.profile}, m={ride.mass_kg}, v={ride.cruise_kmh}")

ctrl_steps = max(1, int(CTRL_PERIOD / DT))
n_ticks_ride = ride.n // ctrl_steps
max_n = n_ticks_ride + 1

logs_np = simulate_ride(controller=0.5, ride=ride,
    rpm_noise_sigma=0.0, iq_noise_sigma=0.0, iq_bias=0.0, vcap_noise_sigma=0.0)
kwargs, _ = build_jax_kwargs(ride, n_ticks_padded=max_n, k_gain=0.5)
out = simulate_ride_fixed_gain_jax(**kwargs)
logs_jax = {k: np.asarray(v) for k, v in out.items()}

keys = ["speed", "speed_baseline", "motor_rpm", "current", "pedal", "grade",
        "vcap", "brake_demand", "carrier_rpm"]

# First tick where motor_rpm or current diverge meaningfully.
for i in range(n_ticks_ride):
    for key in ["motor_rpm", "current", "pedal"]:
        a = logs_np[key][i]; b = logs_jax[key][i]
        if abs(a - b) > 0.5:
            print(f"\nFirst meaningful divergence at tick {i}: {key}  np={a:.4g}  jax={b:.4g}")
            for j in range(max(0, i-3), min(n_ticks_ride, i+3)):
                print(f"  tick {j}:")
                for k2 in keys:
                    print(f"    {k2:14s}  np={logs_np[k2][j]:.6g}  jax={logs_jax[k2][j]:.6g}  Δ={logs_np[k2][j]-logs_jax[k2][j]:+.3e}")
            # Ride inputs
            ti0 = i * ctrl_steps
            print(f"  pedal_active[{ti0}:{ti0+10}]: {ride.pedal_active[ti0:ti0+10]}")
            print(f"  brake_torque[{ti0}:{ti0+10}]: {ride.brake_torque[ti0:ti0+10]}")
            print(f"  grade[{ti0}:{ti0+10}]: {ride.grade_rad[ti0:ti0+10]}")
            sys.exit(0)

print("No big divergence found.")
