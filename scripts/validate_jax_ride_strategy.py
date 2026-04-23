"""Validate sim.physics_jax_strategy.simulate_ride_strategy_jax (B4).

Uses a hand-written strategy that touches rpm / drpm_mean / k_prev
so fast-path aggregate threading gets exercised.  Expresses the
*same* formula in numpy (``_PyStrategy``) and JAX (``_strategy_formula``)
and compares log channels.
"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

import jax
import jax.numpy as jnp
import numpy as np

jax.config.update("jax_enable_x64", True)

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "firmware"))

from config.settings import (
    MOTOR_PHASE_RESISTANCE_OHM as R_PHASE,
    WHEEL_RADIUS_M as R_WHEEL,
    FLUX_LINKAGE_WB,
    VESC_MOTOR_POLE_PAIRS,
    REGEN_CURRENT_MAX_A,
    VESC_WATT_MAX,
)
from regen.regen_control import ff_current_from_rpm, voltage_taper
from sim.physics import (
    simulate_ride,
    DT, CTRL_PERIOD, TELEM_DELAY,
    GEAR_N, ETA_GEAR, T_DRAG_COEFF, J_CARRIER, CAP_F, VCAP_INIT,
    MU_S, MU_K, CAP_ESR, FOC_TAU, K_BAND, C_BAND, IQ_KP_DEFAULT,
    KT, _RPM_SCALE, C_RR,
    VCAP_TAPER_START, VCAP_TAPER_END,
    DUTY_SAT_THRESHOLD,
)
from sim.ride_generator import generate_ride, PROFILES
from sim.physics_jax_strategy import (
    simulate_ride_strategy_jax, K_FLOOR, K_CEIL,
)
from sim.jax_env import DEFAULT_FLOAT

# Silence default telemetry noise so numpy matches the noise-free JAX.
# simulate_ride uses positional defaults on _init_state for drpm_*_noise_*
# that can't be overridden through the public kwargs.  Wrap _init_state
# to force noise_rng=None after init (disables ALL noise paths in
# _compute_current_command).
import sim.physics as _sp
_orig_init_state = _sp._init_state
def _init_state_nonoise(*a, **kw):
    state = _orig_init_state(*a, **kw)
    state["noise_rng"] = None
    return state
_sp._init_state = _init_state_nonoise


def _strategy_formula(rpm, drpm_mean, drpm_peak_neg, iq, duty, vcap,
                      k_prev, jerk_mean, jerk_peak, slip_delta,
                      decel_frac, d_iq, power_mech):
    return 0.1 + 0.0001 * rpm - 0.002 * drpm_mean + 0.01 * k_prev


class _PyStrategy:
    key = "py_eval"
    name = "PyEval"

    def __init__(self):
        self._k = 0.1
        self._drpm_mean_prev = 0.0
        self._drpm_peak_neg_prev = 0.0
        self._iq_prev = 0.0

    def update(self, ctx):
        rpm = ctx.preferred_rpm
        iq = ctx.preferred_iq
        taper = voltage_taper(ctx.vcap, VCAP_TAPER_START, VCAP_TAPER_END)
        if taper == 0.0:
            self._k = 0.1
            self._drpm_mean_prev = 0.0
            self._drpm_peak_neg_prev = 0.0
            self._iq_prev = 0.0
            return 0.0
        jerk_mean = ctx.drpm_mean - self._drpm_mean_prev
        jerk_peak = ctx.drpm_peak_neg - self._drpm_peak_neg_prev
        slip_delta = ctx.drpm_peak_neg - ctx.drpm_mean
        decel_frac = ctx.drpm_mean / (rpm + 1.0)
        d_iq = iq - self._iq_prev
        power_mech = rpm * iq
        self._drpm_mean_prev = ctx.drpm_mean
        self._drpm_peak_neg_prev = ctx.drpm_peak_neg
        self._iq_prev = iq
        k_next = _strategy_formula(
            rpm, ctx.drpm_mean, ctx.drpm_peak_neg,
            iq, ctx.duty_cycle, ctx.vcap, self._k,
            jerk_mean, jerk_peak, slip_delta,
            decel_frac, d_iq, power_mech,
        )
        if not np.isfinite(k_next):
            k_next = self._k
        k_next = max(K_FLOOR, min(K_CEIL, k_next))
        self._k = k_next
        i_cmd = ff_current_from_rpm(
            rpm, k_next,
            flux_linkage=FLUX_LINKAGE_WB,
            phase_resistance=R_PHASE,
            pole_pairs=VESC_MOTOR_POLE_PAIRS,
            current_limit=REGEN_CURRENT_MAX_A,
        )
        return max(0.0, min(REGEN_CURRENT_MAX_A, i_cmd * taper))


def resample_ride_to_ticks(ride, ctrl_steps):
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


def build_jax_kwargs(ride, *, n_ticks_padded, strategy_fn):
    dt = DT
    ctrl_steps = max(1, int(CTRL_PERIOD / dt))
    delay_slots = max(1, int(TELEM_DELAY / dt))

    brake_ticks, grade_ticks, pedal_ticks, n_ride = resample_ride_to_ticks(
        ride, ctrl_steps)
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
        strategy_fn=strategy_fn,
        w_ring0=w_ring0, w_carrier0=w_carrier0, i_actual0=0.0,
        e_cap0=e_cap0, w_ring_base0=w_ring0,
        delay_slots=delay_slots, n_ticks=n_ticks_padded,
        ctrl_steps=ctrl_steps, dt=dt,
        brake_ticks=jnp.asarray(brake_ticks, dtype=DEFAULT_FLOAT),
        grade_ticks=jnp.asarray(grade_ticks, dtype=DEFAULT_FLOAT),
        pedal_active_ticks=jnp.asarray(pedal_ticks, dtype=jnp.bool_),
        mass_kg=float(mass_kg), cruise_mps=float(v0_kmh) / 3.6,
        c_rr=C_RR, r_wheel=R_WHEEL,
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
         "brake_demand", "eta", "pedal", "grade", "k_cmd"]

_TOL = {
    "t":              dict(atol=1e-9, rtol=1e-9),
    "speed":          dict(atol=0.1,  rtol=1e-4),
    "speed_baseline": dict(atol=1e-6, rtol=1e-6),
    "motor_rpm":      dict(atol=50.0, rtol=1e-4),
    "current":        dict(atol=15.0, rtol=1e-3),
    "carrier_rpm":    dict(atol=50.0, rtol=1e-4),
    "vcap":           dict(atol=0.2,  rtol=1e-4),
    "p_elec":         dict(atol=50.0, rtol=1e-3),
    "p_copper":       dict(atol=15.0, rtol=1e-3),
    "p_brake":        dict(atol=50.0, rtol=1e-3),
    "brake_demand":   dict(atol=1e-9, rtol=1e-9),
    "eta":            dict(atol=3e-2, rtol=1e-3),
    "pedal":          dict(atol=30.0, rtol=1e-3),
    "grade":          dict(atol=1e-9, rtol=1e-9),
    "k_cmd":          dict(atol=1e-5, rtol=1e-5),
}


def _compare(np_logs, jax_logs, n_valid):
    worst_val = 0.0
    worst_key = None
    fail_key = None
    for k in _KEYS:
        if k not in np_logs or k not in jax_logs:
            continue
        a = np.asarray(np_logs[k])[:n_valid]
        b = np.asarray(jax_logs[k])[:n_valid]
        n = min(len(a), len(b))
        diff = np.abs(a[:n] - b[:n])
        peak = float(diff.max()) if n > 0 else 0.0
        tol = _TOL.get(k, dict(atol=1e-6, rtol=1e-6))
        pass_tol = np.allclose(a[:n], b[:n], atol=tol["atol"], rtol=tol["rtol"])
        if peak > worst_val:
            worst_val = peak
            worst_key = k
        if not pass_tol and fail_key is None:
            fail_key = k
    return worst_val, worst_key, fail_key


def main():
    rides = []
    prof_list = list(PROFILES.items())
    for i, (prof_name, prof) in enumerate(prof_list):
        rides.append((f"{prof_name}_0",
                      generate_ride(prof, seed=100 * i + 1, duration=60.0)))

    ctrl_steps = max(1, int(CTRL_PERIOD / DT))
    max_n = max(r.n // ctrl_steps for _, r in rides)

    jit_fn = jax.jit(
        simulate_ride_strategy_jax,
        static_argnames=("strategy_fn", "delay_slots", "n_ticks",
                         "ctrl_steps", "dt"),
    )

    n_pass = 0
    for label, ride in rides:
        kwargs, n_ride = build_jax_kwargs(ride, n_ticks_padded=max_n,
                                          strategy_fn=_strategy_formula)
        t0 = time.perf_counter()
        jax_logs = jit_fn(**kwargs)
        jax_logs = {k: np.asarray(v) for k, v in jax_logs.items()}
        t_jax = time.perf_counter() - t0

        controller = _PyStrategy()
        t0 = time.perf_counter()
        # Disable noise so numpy matches the noise-free JAX port.
        np_logs = simulate_ride(
            controller, ride,
            rpm_noise_sigma=0.0, iq_noise_sigma=0.0, iq_bias=0.0,
            vcap_noise_sigma=0.0,
        )
        t_np = time.perf_counter() - t0

        n_valid = min(n_ride, len(np_logs["t"]))
        worst, worst_key, fail_key = _compare(np_logs, jax_logs, n_valid)

        e_np = float(np.trapezoid(np_logs["p_elec"][:n_valid],
                                   np_logs["t"][:n_valid]))
        e_jax = float(np.trapezoid(jax_logs["p_elec"][:n_valid],
                                    jax_logs["t"][:n_valid]))
        de = e_jax - e_np
        e_tol = max(1e-3, 1e-4 * abs(e_np))
        energy_ok = abs(de) <= e_tol
        ok = fail_key is None and energy_ok
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {label:20s}  n={n_valid:4d}  worst={worst:.3g} "
              f"({worst_key})  E_np={e_np:8.2f}  dE={de:+.3e}  "
              f"fail={fail_key}  np={t_np*1e3:.1f}ms  jax={t_jax*1e3:.1f}ms")
        if ok:
            n_pass += 1

    print(f"\n{n_pass}/{len(rides)} passed")
    if n_pass != len(rides):
        return 1

    # ── vmap batching benchmark (B4b) ───────────────────────────────
    # Build a PySR-shaped strategy via the lambdifier — this is the
    # realistic workload: thousands of candidate expressions evaluated
    # inside a batched ride loop.
    from sim.physics_jax_strategy import lambdify_expression_jax
    pysr_like_eq = (
        "relu(0.1 + 0.0005 * drpm_peak_neg + 0.02 * k_prev) "
        "+ step(rpm - 100) * 0.05"
    )
    pysr_fn = lambdify_expression_jax(pysr_like_eq)

    # Broader basket: 2 per profile = 8 rides.
    bench_rides = []
    for i, (prof_name, prof) in enumerate(prof_list):
        for k in range(2):
            bench_rides.append(generate_ride(prof, seed=100 * i + k + 1,
                                              duration=60.0))
    bench_max_n = max(r.n // ctrl_steps for r in bench_rides)

    kwargs_list = [build_jax_kwargs(r, n_ticks_padded=bench_max_n,
                                    strategy_fn=pysr_fn)[0]
                   for r in bench_rides]

    def _stack(key):
        return jnp.stack([jnp.asarray(kw[key]) for kw in kwargs_list])

    stacked_keys = ("w_ring0", "w_carrier0", "i_actual0", "e_cap0",
                    "w_ring_base0", "brake_ticks", "grade_ticks",
                    "pedal_active_ticks", "mass_kg", "cruise_mps",
                    "inv_j_wheel")
    stacked = {k: _stack(k) for k in stacked_keys}
    shared = {k: v for k, v in kwargs_list[0].items() if k not in stacked}

    def one(w_ring0, w_carrier0, i_actual0, e_cap0, w_ring_base0,
            brake_ticks, grade_ticks, pedal_active_ticks,
            mass_kg, cruise_mps, inv_j_wheel):
        return simulate_ride_strategy_jax(
            w_ring0=w_ring0, w_carrier0=w_carrier0, i_actual0=i_actual0,
            e_cap0=e_cap0, w_ring_base0=w_ring_base0,
            brake_ticks=brake_ticks, grade_ticks=grade_ticks,
            pedal_active_ticks=pedal_active_ticks,
            mass_kg=mass_kg, cruise_mps=cruise_mps,
            inv_j_wheel=inv_j_wheel, **shared,
        )
    batched = jax.jit(jax.vmap(one))

    print(f"\nvmap benchmark (PySR-shaped expression, {len(bench_rides)} rides):")
    t0 = time.perf_counter()
    r = batched(*[stacked[k] for k in stacked_keys])
    r["t"].block_until_ready()
    t_compile = time.perf_counter() - t0
    print(f"  vmap compile+first:   {t_compile*1000:8.1f} ms")

    N_RUN = 10
    t0 = time.perf_counter()
    for _ in range(N_RUN):
        r = batched(*[stacked[k] for k in stacked_keys])
        r["t"].block_until_ready()
    t_batched = (time.perf_counter() - t0) / N_RUN
    print(f"  vmap hot ({len(bench_rides)} rides):   {t_batched*1000:8.2f} ms  "
          f"({t_batched/len(bench_rides)*1000:6.2f} ms/ride)")

    # Numpy sequential baseline with the same expression.
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "pysr"))
    from validate_candidates import lambdify_expression
    np_pysr = lambdify_expression(pysr_like_eq)

    class _PySRStrategy:
        key = "pysr_like"; name = "PySRLike"
        def __init__(self):
            self._k = 0.1
            self._drpm_mean_prev = 0.0
            self._drpm_peak_neg_prev = 0.0
            self._iq_prev = 0.0
        def update(self, ctx):
            rpm = ctx.preferred_rpm; iq = ctx.preferred_iq
            taper = voltage_taper(ctx.vcap, VCAP_TAPER_START, VCAP_TAPER_END)
            if taper == 0.0:
                self._k = 0.1; self._drpm_mean_prev = 0.0
                self._drpm_peak_neg_prev = 0.0; self._iq_prev = 0.0
                return 0.0
            jerk_mean = ctx.drpm_mean - self._drpm_mean_prev
            jerk_peak = ctx.drpm_peak_neg - self._drpm_peak_neg_prev
            slip_delta = ctx.drpm_peak_neg - ctx.drpm_mean
            decel_frac = ctx.drpm_mean / (rpm + 1.0)
            d_iq = iq - self._iq_prev
            power_mech = rpm * iq
            self._drpm_mean_prev = ctx.drpm_mean
            self._drpm_peak_neg_prev = ctx.drpm_peak_neg
            self._iq_prev = iq
            try:
                k_next = float(np_pysr(
                    rpm, ctx.drpm_mean, ctx.drpm_peak_neg, iq,
                    ctx.duty_cycle, ctx.vcap, self._k,
                    jerk_mean, jerk_peak, slip_delta,
                    decel_frac, d_iq, power_mech))
            except (ValueError, ZeroDivisionError, FloatingPointError):
                k_next = self._k
            if not np.isfinite(k_next):
                k_next = self._k
            k_next = max(K_FLOOR, min(K_CEIL, k_next))
            self._k = k_next
            i_cmd = ff_current_from_rpm(
                rpm, k_next,
                flux_linkage=FLUX_LINKAGE_WB,
                phase_resistance=R_PHASE,
                pole_pairs=VESC_MOTOR_POLE_PAIRS,
                current_limit=REGEN_CURRENT_MAX_A,
            )
            return max(0.0, min(REGEN_CURRENT_MAX_A, i_cmd * taper))

    t0 = time.perf_counter()
    for r_ in bench_rides:
        simulate_ride(_PySRStrategy(), r_,
                      rpm_noise_sigma=0.0, iq_noise_sigma=0.0,
                      iq_bias=0.0, vcap_noise_sigma=0.0)
    t_np_total = time.perf_counter() - t0
    print(f"  numpy sequential:     {t_np_total*1000:8.2f} ms  "
          f"({t_np_total/len(bench_rides)*1000:6.2f} ms/ride)")
    print(f"  batched speedup:      {t_np_total/t_batched:.2f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
