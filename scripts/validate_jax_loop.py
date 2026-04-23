"""Stage B2 validation: does the JAX control loop reproduce
sim.physics.simulate() for the fixed-gain path, end-to-end?

We call simulate(k, brake_const, ...) and the JAX equivalent with the
same physics constants and compare every log array.

Noise is implicitly disabled because the fixed-gain path in
sim.physics._compute_current_command does not inject any noise
(that is strategy-dispatch-only).  So this is a deterministic diff.
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np
import jax

from sim.physics import (
    simulate,
    DT, CTRL_PERIOD, TELEM_DELAY, T_END,
    GEAR_N, ETA_GEAR, T_DRAG_COEFF, J_CARRIER, CAP_F, VCAP_INIT,
    MU_S, MU_K, CAP_ESR, FOC_TAU, K_BAND, C_BAND, IQ_KP_DEFAULT,
    KT, _RPM_SCALE,
    VCAP_TAPER_START, VCAP_TAPER_END,
)
from config.settings import (
    MOTOR_PHASE_RESISTANCE_OHM as R_PHASE,
    WHEEL_RADIUS_M as R_WHEEL,
    FLUX_LINKAGE_WB,
    VESC_MOTOR_POLE_PAIRS,
    REGEN_CURRENT_MAX_A,
    VESC_WATT_MAX,
)
from sim.physics import DUTY_SAT_THRESHOLD
from sim.physics_jax_loop import simulate_fixed_gain_jax


def run_jax(*, k_gain, brake_const, v0_kmh, mass_kg=100.0,
            t_end=T_END, v_min_kmh=0.0, grade_rad=0.0):
    dt = DT
    ctrl_steps = max(1, int(CTRL_PERIOD / dt))
    delay_slots = max(1, int(TELEM_DELAY / dt))
    n_ticks = int(t_end / CTRL_PERIOD) + 1

    N = GEAR_N
    _1pN = 1.0 + N
    w_ring0 = (v0_kmh / 3.6) / R_WHEEL
    w_carrier0 = (N / _1pN) * w_ring0
    e_cap0 = 0.5 * CAP_F * VCAP_INIT ** 2
    mu_ratio = MU_K / MU_S
    foc_alpha = dt / (FOC_TAU + dt)

    cos_g = math.cos(grade_rad)
    sin_g = math.sin(grade_rad)
    t_rr_ring = 0.008 * mass_kg * 9.81 * cos_g * R_WHEEL
    t_grav_ring = mass_kg * 9.81 * sin_g * R_WHEEL

    v_min_w = (v_min_kmh / 3.6) / R_WHEEL if v_min_kmh > 0.0 else 0.0

    # JAX requires static shapes, so we JIT-wrap with n_ticks static.
    @jax.jit
    def _run(k_gain_, brake_const_):
        return simulate_fixed_gain_jax(
            w_ring0=w_ring0, w_carrier0=w_carrier0, i_actual0=0.0,
            e_cap0=e_cap0, w_ring_base0=w_ring0,
            delay_slots=delay_slots, n_ticks=n_ticks,
            ctrl_steps=ctrl_steps, dt=dt,
            k_gain=k_gain_, brake_const=brake_const_,
            one_plus_n=_1pN, gear_n=GEAR_N, kt=KT, eta_gear=ETA_GEAR,
            t_drag_coeff=T_DRAG_COEFF, r_phase=R_PHASE,
            r_phase_15=1.5 * R_PHASE,
            foc_alpha=foc_alpha, inv_cur_gain=1.0,
            inv_j_carrier=1.0 / J_CARRIER,
            inv_j_wheel=1.0 / (mass_kg * R_WHEEL ** 2),
            mu_ratio=mu_ratio, cap_esr=CAP_ESR, inv_cap=1.0 / CAP_F,
            n_over_np1=N / _1pN, rpm_scale=_RPM_SCALE,
            free_decel=True, v_min_w=v_min_w,
            t_rr_ring=t_rr_ring, t_grav_ring=t_grav_ring,
            k_band=K_BAND, c_band=C_BAND,
            flux_linkage=FLUX_LINKAGE_WB, pole_pairs=VESC_MOTOR_POLE_PAIRS,
            current_limit=REGEN_CURRENT_MAX_A,
            power_limit_w=VESC_WATT_MAX, duty_limit=DUTY_SAT_THRESHOLD,
            vcap_taper_start=VCAP_TAPER_START, vcap_taper_end=VCAP_TAPER_END,
            iq_kp=IQ_KP_DEFAULT,
            vesc_current_gain=1.0, vcap_gain=1.0,
            spd_scale=R_WHEEL * 3.6,
        )

    out = _run(float(k_gain), float(brake_const))
    # Realize arrays.
    return {k: np.asarray(v) for k, v in out.items()}


_KEYS = ["t", "speed", "speed_baseline", "motor_rpm", "current",
         "carrier_rpm", "vcap", "p_elec", "p_copper", "p_brake",
         "brake_demand", "eta"]


def compare_case(label, *, k_gain, brake_const, v0_kmh=15.0,
                 mass_kg=100.0, t_end=T_END, v_min_kmh=0.0, grade_rad=0.0):
    tol = 1e-6   # per-sample abs tolerance on speed/rpm/current etc.

    t0 = time.perf_counter()
    logs_np = simulate(
        controller=float(k_gain),
        brake=float(brake_const),
        v0_kmh=v0_kmh, mass_kg=mass_kg, t_end=t_end,
        v_min_kmh=v_min_kmh, grade_rad=grade_rad,
        # Disable noise so fixed-gain path matches even if caller
        # later compares strategy paths.  (Fixed-gain ignores these
        # already, but pass 0 to be safe / document intent.)
        rpm_noise_sigma=0.0, iq_noise_sigma=0.0, iq_bias=0.0,
        vcap_noise_sigma=0.0,
    )
    t_np = time.perf_counter() - t0

    t0 = time.perf_counter()
    logs_jax = run_jax(
        k_gain=k_gain, brake_const=brake_const, v0_kmh=v0_kmh,
        mass_kg=mass_kg, t_end=t_end, v_min_kmh=v_min_kmh,
        grade_rad=grade_rad,
    )
    t_jax = time.perf_counter() - t0

    n_np = len(logs_np["t"])
    n_jax = int(logs_jax["n_valid"])

    if n_np != n_jax:
        print(f"[FAIL] {label}: length mismatch — numpy={n_np}, jax={n_jax}")
        return False

    worst = 0.0
    worst_key = ""
    for key in _KEYS:
        a = np.asarray(logs_np[key])
        b = np.asarray(logs_jax[key])[:n_jax]
        diff = np.max(np.abs(a - b)) if a.size else 0.0
        if diff > worst:
            worst = float(diff)
            worst_key = key

    ok = worst <= tol
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label:38s}  n={n_np:4d}  worst={worst:.3e} ({worst_key})  "
          f"np={t_np*1000:6.1f} ms  jax={t_jax*1000:7.1f} ms")
    if not ok:
        for key in _KEYS:
            a = np.asarray(logs_np[key])
            b = np.asarray(logs_jax[key])[:n_jax]
            d = np.max(np.abs(a - b)) if a.size else 0.0
            if d > tol:
                ix = int(np.argmax(np.abs(a - b)))
                print(f"    {key:18s}  max abs-diff={d:.3e} at i={ix}  "
                      f"np={a[ix]:.6g}  jax={b[ix]:.6g}")
    return ok


def main():
    cases = [
        ("k=0 (no regen), no brake",
            dict(k_gain=0.0, brake_const=0.0)),
        ("k=0, moderate brake",
            dict(k_gain=0.0, brake_const=2.0)),
        ("k=0.5, moderate brake",
            dict(k_gain=0.5, brake_const=2.0)),
        ("k=1.0, heavy brake",
            dict(k_gain=1.0, brake_const=6.0, v0_kmh=25.0)),
        ("k=0.3, long run to low speed",
            dict(k_gain=0.3, brake_const=3.0, v0_kmh=20.0, t_end=6.0, v_min_kmh=1.0)),
        ("k=0.8, downhill",
            dict(k_gain=0.8, brake_const=4.0, grade_rad=-0.05)),
        ("k=0.4, uphill",
            dict(k_gain=0.4, brake_const=1.0, grade_rad=0.03)),
    ]

    n_pass = 0
    for label, kw in cases:
        if compare_case(label, **kw):
            n_pass += 1
    print(f"\n{n_pass}/{len(cases)} cases passed.")
    return 0 if n_pass == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
