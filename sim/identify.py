"""sim.identify — Compare simulator physics against a recorded bench trace.

Given a CSV produced by ``scripts/bench/test_*_regen_trace.py`` (columns
``t_ms, mot_rpm, iq, cap_v, ...``) this computes the iq implied by the
measured motor RPM dynamics and the simulator's physics constants:

    tau_motor approx J_CARRIER * domega/dt + T_DRAG_COEFF * omega
    iq_predicted = tau_motor / KT                  where KT = 1.5 * Pp * Phi

It then reports the RMS residual between the measured iq and the
predicted iq over the active portion of the trace (|rpm| > 10 and
|iq| > 0.1).  A small residual means the current physics constants
are a good match; a large residual means J_CARRIER and/or
T_DRAG_COEFF likely need to be re-measured on the rig.

This is a VALIDATION tool, not a blind fitter -- we intentionally do not
back-fit constants that can be measured directly on the bench, because
that would hide hardware problems (wrong pole count, loose grub screw,
wrong flux linkage) as parameter drift.

Usage:
    python -m sim.identify data/drill_trace.csv [data/ride_trace.csv ...]
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

from .physics import (
    FLUX_LINKAGE, J_CARRIER, POLE_PAIRS, T_DRAG_COEFF, _TWO_PI,
)


def _load_trace(path: Path) -> dict:
    t, rpm, iq = [], [], []
    with path.open() as f:
        for row in csv.DictReader(f):
            t.append(float(row["t_ms"]) * 1e-3)
            rpm.append(float(row["mot_rpm"]))
            iq.append(float(row["iq"]))
    return {
        "path": str(path),
        "t": np.asarray(t),
        "rpm": np.asarray(rpm),
        "iq": np.asarray(iq),
    }


def validate(path) -> dict:
    """Return residual statistics for one trace file."""
    tr = _load_trace(Path(path))
    t, rpm, iq_meas = tr["t"], tr["rpm"], tr["iq"]

    # Smooth rpm with a short moving average before differentiating.
    # Without this, the numerical derivative of 27 ms-sampled telemetry
    # amplifies noise to hundreds of rad/s^2 and drowns the signal.
    win = max(3, int(round(0.1 / max(np.median(np.diff(t)), 1e-3))))
    kernel = np.ones(win) / win
    rpm_s = np.convolve(rpm, kernel, mode="same")

    # Electrical angular velocity (rad/s) and its time derivative
    omega_e = rpm_s * POLE_PAIRS * _TWO_PI / 60.0
    domega_dt = np.gradient(omega_e, t)

    # Predicted iq from the inertia + drag model
    kt = 1.5 * POLE_PAIRS * FLUX_LINKAGE
    iq_pred = (J_CARRIER * domega_dt + T_DRAG_COEFF * omega_e) / kt

    # Focus on samples with real motion + real current
    active = (np.abs(rpm) > 10.0) & (np.abs(iq_meas) > 0.1)
    if active.sum() < 10:
        return {"path": tr["path"], "active": int(active.sum()),
                "iq_rms": float("nan"), "iq_bias": float("nan")}

    residual = iq_pred[active] - iq_meas[active]
    return {
        "path": tr["path"],
        "active": int(active.sum()),
        "iq_rms": float(np.sqrt(np.mean(residual ** 2))),
        "iq_bias": float(np.mean(residual)),
    }


def main(argv) -> int:
    if not argv:
        print("Usage: python -m sim.identify TRACE.csv [TRACE2.csv ...]")
        return 1
    print("Using J_CARRIER=%s, T_DRAG_COEFF=%s, FLUX=%s, Pp=%s" %
          (J_CARRIER, T_DRAG_COEFF, FLUX_LINKAGE, POLE_PAIRS))
    print()
    print("%-32s%8s%12s%12s" % ("trace", "active", "iq_rms(A)", "iq_bias(A)"))
    print("-" * 64)
    for p in argv:
        r = validate(p)
        print("%-32s%8d%12.3f%12.3f" %
              (r["path"], r["active"], r["iq_rms"], r["iq_bias"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
