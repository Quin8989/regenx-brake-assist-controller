"""Stage B3 validation + batching benchmark.

Goal: prove JAX ``simulate_ride_fixed_gain_jax`` matches numpy
``simulate_ride`` deterministically, and that ``vmap`` over a batch
of rides gives a real speedup vs sequential calls.
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
import jax.numpy as jnp

from sim.physics import (
    simulate_ride,
    DT, CTRL_PERIOD, TELEM_DELAY,
    GEAR_N, ETA_GEAR, T_DRAG_COEFF, J_CARRIER, CAP_F, VCAP_INIT,
    MU_S, MU_K, CAP_ESR, FOC_TAU, K_BAND, C_BAND, IQ_KP_DEFAULT,
    KT, _RPM_SCALE, C_RR,
    VCAP_TAPER_START, VCAP_TAPER_END,
    DUTY_SAT_THRESHOLD,
)
from config.settings import (
    MOTOR_PHASE_RESISTANCE_OHM as R_PHASE,
    WHEEL_RADIUS_M as R_WHEEL,
    FLUX_LINKAGE_WB,
    VESC_MOTOR_POLE_PAIRS,
    REGEN_CURRENT_MAX_A,
    VESC_WATT_MAX,
)
from sim.ride_generator import generate_ride, PROFILES
from sim.physics_jax_ride import simulate_ride_fixed_gain_jax


def resample_ride_to_ticks(ride, ctrl_steps):
    """Numpy 1ms→10ms resampler matching simulate_ride lines 702-709."""
    n_ticks_ride = ride.n // ctrl_steps
    idx = np.arange(n_ticks_ride) * ctrl_steps
    brake_ticks = np.add.reduceat(
        ride.brake_torque[:n_ticks_ride * ctrl_steps], idx) / ctrl_steps
    grade_ticks = np.add.reduceat(
        ride.grade_rad[:n_ticks_ride * ctrl_steps], idx) / ctrl_steps
    pedal_ticks = np.add.reduceat(
        ride.pedal_active[:n_ticks_ride * ctrl_steps].astype(np.int32),
        idx) > 0
    return brake_ticks, grade_ticks, pedal_ticks, n_ticks_ride


def build_jax_kwargs(ride, *, n_ticks_padded, k_gain):
    dt = DT
    ctrl_steps = max(1, int(CTRL_PERIOD / dt))
    delay_slots = max(1, int(TELEM_DELAY / dt))

    brake_ticks, grade_ticks, pedal_ticks, n_ride = resample_ride_to_ticks(
        ride, ctrl_steps)

    # Pad to n_ticks_padded so vmap can stack variable-length rides.
    pad = n_ticks_padded - n_ride
    if pad > 0:
        brake_ticks = np.concatenate([brake_ticks, np.zeros(pad)])
        grade_ticks = np.concatenate([grade_ticks, np.zeros(pad)])
        pedal_ticks = np.concatenate([pedal_ticks, np.zeros(pad, dtype=bool)])

    N = GEAR_N
    _1pN = 1.0 + N
    v0_kmh = ride.cruise_kmh
    mass_kg = ride.mass_kg
    w_ring0 = (v0_kmh / 3.6) / R_WHEEL
    w_carrier0 = (N / _1pN) * w_ring0
    e_cap0 = 0.5 * CAP_F * VCAP_INIT ** 2

    return dict(
        w_ring0=w_ring0, w_carrier0=w_carrier0, i_actual0=0.0,
        e_cap0=e_cap0, w_ring_base0=w_ring0,
        delay_slots=delay_slots, n_ticks=n_ticks_padded,
        ctrl_steps=ctrl_steps, dt=dt,
        brake_ticks=jnp.asarray(brake_ticks, dtype=jnp.float64),
        grade_ticks=jnp.asarray(grade_ticks, dtype=jnp.float64),
        pedal_active_ticks=jnp.asarray(pedal_ticks, dtype=jnp.bool_),
        mass_kg=float(mass_kg), cruise_mps=float(v0_kmh) / 3.6,
        c_rr=C_RR, r_wheel=R_WHEEL,
        k_gain=float(k_gain),
        one_plus_n=_1pN, gear_n=GEAR_N, kt=KT, eta_gear=ETA_GEAR,
        t_drag_coeff=T_DRAG_COEFF, r_phase=R_PHASE, r_phase_15=1.5 * R_PHASE,
        foc_alpha=dt / (FOC_TAU + dt), inv_cur_gain=1.0,
        inv_j_carrier=1.0 / J_CARRIER,
        inv_j_wheel=1.0 / (mass_kg * R_WHEEL ** 2),
        mu_ratio=MU_K / MU_S, cap_esr=CAP_ESR, inv_cap=1.0 / CAP_F,
        n_over_np1=N / _1pN, rpm_scale=_RPM_SCALE,
        v_min_w=0.0, k_band=K_BAND, c_band=C_BAND,
        flux_linkage=FLUX_LINKAGE_WB, pole_pairs=VESC_MOTOR_POLE_PAIRS,
        current_limit=REGEN_CURRENT_MAX_A,
        power_limit_w=VESC_WATT_MAX, duty_limit=DUTY_SAT_THRESHOLD,
        vcap_taper_start=VCAP_TAPER_START, vcap_taper_end=VCAP_TAPER_END,
        iq_kp=IQ_KP_DEFAULT, vesc_current_gain=1.0, vcap_gain=1.0,
        spd_scale=R_WHEEL * 3.6,
    ), n_ride


_KEYS = ["t", "speed", "speed_baseline", "motor_rpm", "current",
         "carrier_rpm", "vcap", "p_elec", "p_copper", "p_brake",
         "brake_demand", "eta", "pedal", "grade"]

# Per-channel tolerances.  Aggregate energy (below) is the tight bar —
# scoring consumes integrals, not single-tick values.  Per-tick bounds
# here allow for branch-boundary flips (e.g. pedal engages 1 tick
# earlier in JAX vs numba because v_now≈cruise+0.1 flips under FP noise).
_TOL = {
    "t":              (1e-9, 0.0),
    "speed":          (0.1,  0.0),    # km/h — full-run drift
    "speed_baseline": (1e-6, 0.0),
    "motor_rpm":      (50.0, 0.0),    # branch-boundary transient
    "current":        (15.0, 0.0),    # A — single-tick branch spike
    "carrier_rpm":    (200.0, 0.0),
    "vcap":           (1e-3, 0.0),
    "p_elec":         (200.0, 0.0),   # W — instantaneous, near branch
    "p_copper":       (50.0, 0.0),
    "p_brake":        (50.0, 0.0),
    "brake_demand":   (1e-9, 0.0),
    "eta":            (0.5,  0.0),
    "pedal":          (30.0, 0.0),    # 1-tick pedal toggle
    "grade":          (1e-9, 0.0),
}


def compare_ride(ride, k_gain, n_ticks_padded):
    """Run numpy and JAX fixed-gain on one ride, diff every log channel."""
    logs_np = simulate_ride(
        controller=float(k_gain), ride=ride,
        rpm_noise_sigma=0.0, iq_noise_sigma=0.0,
        iq_bias=0.0, vcap_noise_sigma=0.0,
    )

    kwargs, n_ride = build_jax_kwargs(ride, n_ticks_padded=n_ticks_padded, k_gain=k_gain)
    out = simulate_ride_fixed_gain_jax(**kwargs)
    logs_jax = {k: np.asarray(v) for k, v in out.items()}

    n_np = len(logs_np["t"])
    ok = True
    worst = 0.0
    worst_key = ""
    fail_key = ""
    fail_ratio = 0.0
    for key in _KEYS:
        a = np.asarray(logs_np[key])
        b = np.asarray(logs_jax[key])[:n_np]
        if a.size == 0:
            continue
        diff = float(np.max(np.abs(a - b)))
        atol, rtol = _TOL[key]
        bound = atol + rtol * float(np.max(np.abs(a)))
        ratio = diff / max(bound, 1e-30)
        if ratio > 1.0:
            ok = False
            if ratio > fail_ratio:
                fail_ratio = ratio
                fail_key = key
        if diff > worst:
            worst = diff
            worst_key = key

    # Energy integrals must match tightly — they are the score inputs.
    e_np = float(np.trapezoid(logs_np["p_elec"], logs_np["t"]))
    e_jx = float(np.trapezoid(logs_jax["p_elec"][:n_np], logs_jax["t"][:n_np]))
    e_diff = abs(e_np - e_jx)
    e_tol = max(1e-3, 1e-4 * abs(e_np))   # 0.01 % of total energy
    if e_diff > e_tol:
        ok = False

    return n_np, worst, worst_key, fail_key, e_np, e_jx, ok


def main():
    # Build a small, fully deterministic ride set (skip _mix_seed, which
    # is process-hash-randomized).
    rides = []
    prof_list = list(PROFILES.items())
    for i, (prof_name, prof) in enumerate(prof_list):
        for k in range(2):
            rides.append(generate_ride(prof, seed=100 * i + k + 1, duration=60.0))
    print(f"Generated {len(rides)} rides.\n")

    ctrl_steps = max(1, int(CTRL_PERIOD / DT))
    # Pad to the longest ride's tick count (+1 for the trailing empty tick).
    max_n = max(r.n // ctrl_steps for r in rides) + 1

    # ── Per-ride parity check ───────────────────────────────────────
    print("Per-ride parity (fixed-gain k=0.5, noise off):")
    n_pass = 0
    for i, ride in enumerate(rides):
        n, worst, key, fail_key, e_np, e_jx, ok = compare_ride(
            ride, k_gain=0.5, n_ticks_padded=max_n)
        status = "PASS" if ok else "FAIL"
        tag = key if ok else f"{fail_key}!"
        print(f"  [{status}] ride {i:2d}  ({ride.profile:13s}, m={ride.mass_kg:5.1f} kg, "
              f"v={ride.cruise_kmh:5.2f} km/h)  n={n:4d}  "
              f"worst={worst:.2e} ({tag:13s})  "
              f"E_np={e_np:7.1f} J  E_jx={e_jx:7.1f} J  ΔE={e_np-e_jx:+.2e}")
        if ok:
            n_pass += 1
    print(f"  {n_pass}/{len(rides)} passed\n")

    if n_pass != len(rides):
        return 1

    # ── vmap batching ───────────────────────────────────────────────
    print("vmap batching benchmark:")
    kwargs_list = [build_jax_kwargs(r, n_ticks_padded=max_n, k_gain=0.5)[0]
                   for r in rides]

    # Stackable per-ride tensors / scalars.
    def stack(key):
        return jnp.stack([jnp.asarray(kw[key]) for kw in kwargs_list])

    stacked = dict(
        w_ring0=stack("w_ring0"),
        w_carrier0=stack("w_carrier0"),
        i_actual0=stack("i_actual0"),
        e_cap0=stack("e_cap0"),
        w_ring_base0=stack("w_ring_base0"),
        brake_ticks=stack("brake_ticks"),
        grade_ticks=stack("grade_ticks"),
        pedal_active_ticks=stack("pedal_active_ticks"),
        mass_kg=stack("mass_kg"),
        cruise_mps=stack("cruise_mps"),
        inv_j_wheel=stack("inv_j_wheel"),
    )
    # All other kwargs are identical across rides — take from kwargs_list[0].
    shared = {k: v for k, v in kwargs_list[0].items() if k not in stacked}

    # Build a vmapped function.  Static args are the integer shapes.
    def one(w_ring0, w_carrier0, i_actual0, e_cap0, w_ring_base0,
            brake_ticks, grade_ticks, pedal_active_ticks,
            mass_kg, cruise_mps, inv_j_wheel):
        return simulate_ride_fixed_gain_jax(
            w_ring0=w_ring0, w_carrier0=w_carrier0, i_actual0=i_actual0,
            e_cap0=e_cap0, w_ring_base0=w_ring_base0,
            brake_ticks=brake_ticks, grade_ticks=grade_ticks,
            pedal_active_ticks=pedal_active_ticks,
            mass_kg=mass_kg, cruise_mps=cruise_mps, inv_j_wheel=inv_j_wheel,
            **shared,
        )

    batched = jax.jit(jax.vmap(one))
    # Warm-up compile.
    t0 = time.perf_counter()
    r = batched(
        stacked["w_ring0"], stacked["w_carrier0"], stacked["i_actual0"],
        stacked["e_cap0"], stacked["w_ring_base0"],
        stacked["brake_ticks"], stacked["grade_ticks"], stacked["pedal_active_ticks"],
        stacked["mass_kg"], stacked["cruise_mps"], stacked["inv_j_wheel"],
    )
    r["t"].block_until_ready()
    t_compile = time.perf_counter() - t0
    print(f"  vmap compile+first:   {t_compile*1000:8.1f} ms")

    # Hot batched runs.
    N_RUN = 10
    t0 = time.perf_counter()
    for _ in range(N_RUN):
        r = batched(
            stacked["w_ring0"], stacked["w_carrier0"], stacked["i_actual0"],
            stacked["e_cap0"], stacked["w_ring_base0"],
            stacked["brake_ticks"], stacked["grade_ticks"], stacked["pedal_active_ticks"],
            stacked["mass_kg"], stacked["cruise_mps"], stacked["inv_j_wheel"],
        )
        r["t"].block_until_ready()
    t_batched = (time.perf_counter() - t0) / N_RUN
    print(f"  vmap hot ({len(rides)} rides):   {t_batched*1000:8.2f} ms  "
          f"({t_batched/len(rides)*1000:6.2f} ms/ride)")

    # Numba baseline on same rides.
    # Warm up numba cache.
    simulate_ride(controller=0.5, ride=rides[0],
                  rpm_noise_sigma=0.0, iq_noise_sigma=0.0,
                  iq_bias=0.0, vcap_noise_sigma=0.0)
    t0 = time.perf_counter()
    for _ in range(N_RUN):
        for ride in rides:
            simulate_ride(controller=0.5, ride=ride,
                          rpm_noise_sigma=0.0, iq_noise_sigma=0.0,
                          iq_bias=0.0, vcap_noise_sigma=0.0)
    t_numba = (time.perf_counter() - t0) / N_RUN
    print(f"  numba sequential:     {t_numba*1000:8.2f} ms  "
          f"({t_numba/len(rides)*1000:6.2f} ms/ride)")
    print(f"  batched speedup:      {t_numba/t_batched:6.2f}x")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
