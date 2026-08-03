"""sim/ride_demo.py — Visualise one ride from each profile.

Produces sim/output/ride_demo.png: a 4-row figure showing brake
torque, pedal torque, and grade for a representative ride of each
profile.  Quick eyeballing tool for checking the generator's
plausibility.
"""
from __future__ import annotations

import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from sim.ride_generator import PROFILES, generate_ride


def plot_ride_grid(out_path: Path) -> None:
    seeds = {"casual": 11, "commuter": 23, "hilly": 42, "fast_commuter": 7}
    names = list(PROFILES)

    fig, axes = plt.subplots(len(names), 1, figsize=(12, 2.6 * len(names)),
                             sharex=True)

    for ax, name in zip(axes, names):
        prof = PROFILES[name]
        ride = generate_ride(prof, seed=seeds[name])
        t = np.arange(ride.n) * ride.dt

        ax2 = ax.twinx()
        ax3 = ax.twinx()
        ax3.spines.right.set_position(("outward", 48))

        lb, = ax.plot(t, ride.brake_torque, color="tab:red", lw=1.0,
                      label="brake τ (Nm)")
        lp, = ax2.plot(t, ride.pedal_torque_pred, color="tab:blue", lw=0.7,
                       alpha=0.6, label="pedal τ @ cruise (Nm)")
        lg, = ax3.plot(t, np.degrees(ride.grade_rad), color="tab:green",
                       lw=0.8, label="grade (°)")

        for s, e in ride.brake_windows:
            ax.axvspan(s, e, alpha=0.08, color="red", lw=0)

        ax.set_ylabel("brake τ (Nm)", color="tab:red")
        ax2.set_ylabel("pedal τ (Nm)", color="tab:blue")
        ax3.set_ylabel("grade (°)", color="tab:green")
        ax.tick_params(axis="y", labelcolor="tab:red")
        ax2.tick_params(axis="y", labelcolor="tab:blue")
        ax3.tick_params(axis="y", labelcolor="tab:green")
        ax.set_ylim(0, 45)

        title = (f"{name}  |  cruise={ride.cruise_kmh:.1f} km/h  "
                 f"m={ride.mass_kg:.0f} kg  events={len(ride.brake_windows)}  "
                 f"grade [{math.degrees(ride.grade_rad.min()):+.1f}°, "
                 f"{math.degrees(ride.grade_rad.max()):+.1f}°]")
        ax.set_title(title, fontsize=9, loc="left")
        ax.grid(True, alpha=0.2)

    axes[-1].set_xlabel("time (s)")
    fig.suptitle("Generated rides — one seed per profile", y=1.0)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    plot_ride_grid(Path("sim/output/ride_demo.png"))
