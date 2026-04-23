"""Stage B1 validation: does sim.physics_jax.run_physics_batch_jax match
sim.physics._run_physics_batch bit-for-bit?

We call both with a family of realistic input tuples (different brake
levels, current commands, wheel speeds, capacitor states, early-stop
conditions) and diff every output.

Pass criteria: all scalar outputs within 1e-9 absolute tolerance.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np

from sim.physics import (
    _run_physics_batch,
    DT, CTRL_PERIOD, TELEM_DELAY,
    GEAR_N, ETA_GEAR, T_DRAG_COEFF, J_CARRIER, CAP_F, VCAP_INIT,
    MU_S, MU_K, STICTION_W, CAP_ESR, FOC_TAU, K_BAND, C_BAND,
    KT, _RPM_SCALE,
)
from config.settings import (
    MOTOR_PHASE_RESISTANCE_OHM as R_PHASE,
    WHEEL_RADIUS_M as R_WHEEL,
    REGEN_CURRENT_MAX_A,
)
from sim.jax.physics import run_physics_batch_jax


def build_inputs(*, v0_kmh, i_cmd, brake_val, i_actual_init=0.0,
                 w_carrier_init=None, e_cap_init=None,
                 v_min_kmh=0.0, free_decel=True,
                 mass_kg=100.0, grade_rad=0.0):
    """Assemble the full positional-arg tuple for _run_physics_batch."""
    dt = DT
    ctrl_steps = max(1, int(CTRL_PERIOD / dt))

    N = GEAR_N
    _1pN = 1.0 + N
    w_ring = (v0_kmh / 3.6) / R_WHEEL
    if w_carrier_init is None:
        w_carrier_init = (N / _1pN) * w_ring
    w_ring_base = w_ring
    w_carrier = w_carrier_init

    if e_cap_init is None:
        e_cap_init = 0.5 * CAP_F * VCAP_INIT ** 2
    e_cap = e_cap_init

    foc_alpha = dt / (FOC_TAU + dt)
    inv_cur_gain = 1.0 / 1.0
    inv_j_carrier = 1.0 / J_CARRIER
    inv_j_wheel = 1.0 / (mass_kg * R_WHEEL ** 2)
    mu_ratio = MU_K / MU_S
    inv_cap = 1.0 / CAP_F
    n_over_np1 = N / _1pN

    delay_slots = max(1, int(TELEM_DELAY / dt))
    rpm_buf = np.zeros(delay_slots, dtype=np.float64)
    rpm_idx = 0

    cos_g = math.cos(grade_rad)
    sin_g = math.sin(grade_rad)
    t_rr_ring = 0.008 * mass_kg * 9.81 * cos_g * R_WHEEL
    t_grav_ring = mass_kg * 9.81 * sin_g * R_WHEEL
    t_pedal_ring = 0.0

    v_min_w = (v_min_kmh / 3.6) / R_WHEEL if v_min_kmh > 0.0 else 0.0

    return dict(
        n_sub=ctrl_steps, dt=dt,
        w_ring=w_ring, w_carrier=w_carrier, i_actual=i_actual_init,
        e_cap=e_cap, w_ring_base=w_ring_base,
        i_cmd=i_cmd, brake_val=brake_val,
        one_plus_n=_1pN, gear_n=GEAR_N, kt=KT, eta_gear=ETA_GEAR,
        t_drag_coeff=T_DRAG_COEFF, r_phase_15=1.5 * R_PHASE,
        foc_alpha=foc_alpha, inv_cur_gain=inv_cur_gain,
        inv_j_carrier=inv_j_carrier, inv_j_wheel=inv_j_wheel,
        mu_ratio=mu_ratio, stiction_w=STICTION_W,
        cap_esr=CAP_ESR, inv_cap=inv_cap,
        n_over_np1=n_over_np1, rpm_scale=_RPM_SCALE,
        free_decel=free_decel, v_min_w=v_min_w,
        t_rr_ring=t_rr_ring, t_grav_ring=t_grav_ring, t_pedal_ring=t_pedal_ring,
        rpm_buf=rpm_buf, rpm_idx=rpm_idx,
        rpm_prev_sub_in=0.0,
        delta_band=0.0, k_band=K_BAND, c_band=C_BAND,
    )


_OUT_NAMES = [
    "w_ring", "w_carrier", "i_actual", "e_cap", "w_ring_base",
    "motor_rpm", "rpm_idx", "stopped",
    "p_net_sum", "pcu_esr_sum", "p_brake_sum", "p_drg_sum",
    "drpm_mean", "drpm_peak_neg", "iq_mean", "rpm_prev_sub",
    "delta_band",
]


def run_one(label, **kw):
    args_numba = build_inputs(**kw)
    # numba mutates rpm_buf, so pass a copy.
    args_numba_copy = dict(args_numba)
    args_numba_copy["rpm_buf"] = args_numba["rpm_buf"].copy()
    out_numba = _run_physics_batch(**args_numba_copy)

    out_jax = run_physics_batch_jax(**args_numba)

    # Convert JAX outputs to Python scalars for comparison.
    out_jax_np = [np.asarray(x).item() if np.asarray(x).ndim == 0 else np.asarray(x)
                  for x in out_jax]

    diffs = []
    for name, a, b in zip(_OUT_NAMES, out_numba, out_jax_np):
        if isinstance(a, bool) or isinstance(b, bool):
            ok = bool(a) == bool(b)
            diff = 0.0 if ok else 1.0
        else:
            af = float(a)
            bf = float(b)
            diff = abs(af - bf)
            ok = diff <= 1e-9
        diffs.append((name, ok, a, b, diff))

    worst = max(d[4] for d in diffs if not isinstance(d[2], bool))
    all_ok = all(d[1] for d in diffs)
    status = "PASS" if all_ok else "FAIL"
    print(f"[{status}] {label}   worst abs-diff = {worst:.3e}")
    if not all_ok:
        for name, ok, a, b, diff in diffs:
            marker = "  " if ok else "❌"
            print(f"    {marker} {name:16s}  numba={a!r:>22s}  jax={b!r:>22s}  diff={diff:.3e}")
    return all_ok


def main():
    cases = [
        ("coast, no brake, no regen",
            dict(v0_kmh=15.0, i_cmd=0.0, brake_val=0.0)),
        ("moderate brake, no regen",
            dict(v0_kmh=15.0, i_cmd=0.0, brake_val=2.0)),
        ("moderate brake, full regen",
            dict(v0_kmh=15.0, i_cmd=10.0, brake_val=2.0)),
        ("heavy brake, high regen",
            dict(v0_kmh=25.0, i_cmd=30.0, brake_val=8.0)),
        ("low speed near v_min",
            dict(v0_kmh=2.0, i_cmd=1.0, brake_val=0.5, v_min_kmh=1.0)),
        ("constant speed (free_decel=False)",
            dict(v0_kmh=15.0, i_cmd=10.0, brake_val=2.0, free_decel=False)),
        ("downhill grade",
            dict(v0_kmh=15.0, i_cmd=15.0, brake_val=5.0, grade_rad=-0.05)),
        ("uphill grade, motor off",
            dict(v0_kmh=15.0, i_cmd=0.0, brake_val=0.0, grade_rad=0.05)),
        ("cap nearly empty",
            dict(v0_kmh=15.0, i_cmd=20.0, brake_val=4.0, e_cap_init=0.1)),
        ("cap at init, moderate regen",
            dict(v0_kmh=20.0, i_cmd=15.0, brake_val=3.0)),
        ("pre-warmed i_actual",
            dict(v0_kmh=15.0, i_cmd=10.0, brake_val=2.0, i_actual_init=5.0)),
    ]

    n_pass = 0
    for label, kw in cases:
        ok = run_one(label, **kw)
        n_pass += int(ok)
    print(f"\n{n_pass}/{len(cases)} cases passed.")
    return 0 if n_pass == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
