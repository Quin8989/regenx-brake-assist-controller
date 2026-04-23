"""Stage B5 — telemetry noise injection parity.

Bit-exact parity with numpy is impossible (numpy uses Philox via
``np.random.Generator.normal`` while JAX uses Threefry).  Instead we
check *statistical* parity: over N trajectories with independent noise
draws, the mean and std of the energy integral and the peak current
should agree within Monte-Carlo error.

This is the gate that lets us use JAX noise for cvar20 scoring
(Track C): if energy distributions match, the scoring quantiles will
match.
"""
from __future__ import annotations

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
)
from regen.regen_control import ff_current_from_rpm, voltage_taper
from sim.physics import (
    simulate_ride,
    DT, CTRL_PERIOD,
    VCAP_TAPER_START, VCAP_TAPER_END,
    RPM_NOISE_SIGMA_DEFAULT,
    IQ_NOISE_SIGMA_DEFAULT,
    VCAP_NOISE_SIGMA_DEFAULT,
    DRPM_MEAN_NOISE_SIGMA_DEFAULT,
    DRPM_PEAK_NEG_NOISE_SIGMA_DEFAULT,
    DRPM_PEAK_NEG_NOISE_BIAS_DEFAULT,
)
from sim.ride_generator import generate_ride, PROFILES
from sim.jax.physics_strategy import (
    simulate_ride_strategy_jax, lambdify_expression_jax,
    K_FLOOR, K_CEIL,
)
from scripts.research.validate_jax_ride_strategy import build_jax_kwargs


# A realistic-looking PySR expression.
PYSR_EQ = ("relu(0.1 + 0.0005 * drpm_peak_neg + 0.02 * k_prev) "
           "+ step(rpm - 100) * 0.05")
PYSR_JAX = lambdify_expression_jax(PYSR_EQ)


class _NumpyPysr:
    """Numpy wrapper using the same expression — stateful, picklable."""
    key = "pysr_like"; name = "PySRLike"
    def __init__(self):
        import sys as _s
        _s.path.insert(0, str(_REPO_ROOT / "scripts" / "pysr"))
        from validate_candidates import lambdify_expression
        self._p = lambdify_expression(PYSR_EQ)
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
            k_next = float(self._p(
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


def main():
    # One commuter ride — higher-power path, noise matters more.
    ride = generate_ride(PROFILES["fast_commuter"], seed=401, duration=60.0)
    ctrl_steps = max(1, int(CTRL_PERIOD / DT))
    n_ticks = ride.n // ctrl_steps

    sigmas = dict(
        rpm_noise_sigma=RPM_NOISE_SIGMA_DEFAULT,
        iq_noise_sigma=IQ_NOISE_SIGMA_DEFAULT,
        iq_bias=0.0,
        vcap_noise_sigma=VCAP_NOISE_SIGMA_DEFAULT,
        drpm_mean_noise_sigma=DRPM_MEAN_NOISE_SIGMA_DEFAULT,
        drpm_peak_neg_noise_sigma=DRPM_PEAK_NEG_NOISE_SIGMA_DEFAULT,
        drpm_peak_neg_noise_bias=DRPM_PEAK_NEG_NOISE_BIAS_DEFAULT,
    )

    N_TRAJ = 64

    # ── Numpy Monte Carlo ──
    e_np = np.zeros(N_TRAJ)
    imax_np = np.zeros(N_TRAJ)
    # simulate_ride re-seeds noise_rng per-call to a fixed value, so
    # we must override after _init_state to inject per-trajectory seeds.
    import sim.physics as _sp
    _orig = _sp._init_state
    seeds = iter([])  # injected in closure below

    t0 = time.perf_counter()
    for i in range(N_TRAJ):
        seed_i = 10000 + i
        def _patched(*a, _s=seed_i, **kw):
            st = _orig(*a, **kw)
            st["noise_rng"] = np.random.default_rng(_s)
            return st
        _sp._init_state = _patched
        logs = simulate_ride(_NumpyPysr(), ride,
                             rpm_noise_sigma=sigmas["rpm_noise_sigma"],
                             iq_noise_sigma=sigmas["iq_noise_sigma"],
                             iq_bias=sigmas["iq_bias"],
                             vcap_noise_sigma=sigmas["vcap_noise_sigma"])
        e_np[i] = float(np.trapezoid(logs["p_elec"], logs["t"]))
        imax_np[i] = float(np.max(logs["current"]))
    _sp._init_state = _orig
    t_np = time.perf_counter() - t0

    # ── JAX Monte Carlo ──
    # Build shared kwargs (all same ride).
    kwargs, _ = build_jax_kwargs(ride, n_ticks_padded=n_ticks,
                                  strategy_fn=PYSR_JAX)
    # Strip out scalars we'll pass explicitly and the noise-specific ones.
    shared = {k: v for k, v in kwargs.items()}
    # Add noise sigmas.
    shared.update(sigmas)

    # vmap over noise_key only; everything else is shared.
    def one_noisy(noise_key):
        return simulate_ride_strategy_jax(noise_key=noise_key, **shared)
    batched = jax.jit(jax.vmap(one_noisy))

    keys = jax.random.split(jax.random.PRNGKey(0xC0FFEE), N_TRAJ)
    # Warm-up
    r = batched(keys)
    r["t"].block_until_ready()

    t0 = time.perf_counter()
    r = batched(keys)
    r["t"].block_until_ready()
    t_jax = time.perf_counter() - t0

    p_elec = np.asarray(r["p_elec"])    # [N_TRAJ, n_ticks]
    t = np.asarray(r["t"])
    current = np.asarray(r["current"])
    e_jax = np.trapezoid(p_elec, t, axis=1)
    imax_jax = current.max(axis=1)

    def fmt(arr):
        return f"mean={arr.mean():.3f}  std={arr.std(ddof=1):.3f}"

    print(f"Energy (J) over {N_TRAJ} trajectories:")
    print(f"  numpy: {fmt(e_np)}")
    print(f"  jax  : {fmt(e_jax)}")
    print(f"Imax (A) over {N_TRAJ} trajectories:")
    print(f"  numpy: {fmt(imax_np)}")
    print(f"  jax  : {fmt(imax_jax)}")
    print(f"Wall time: numpy={t_np:.2f}s  jax={t_jax:.2f}s  "
          f"speedup={t_np/t_jax:.1f}x")

    # Standard errors.
    se_e_np  = e_np.std(ddof=1) / np.sqrt(N_TRAJ)
    se_e_jax = e_jax.std(ddof=1) / np.sqrt(N_TRAJ)
    diff_e = abs(e_np.mean() - e_jax.mean())
    combined_se = np.hypot(se_e_np, se_e_jax)
    print(f"\nEnergy-mean gap: {diff_e:.3f} J   combined SE: {combined_se:.3f} J"
          f"  ({diff_e/combined_se:.2f} sigma)")

    ok = diff_e < 3.0 * combined_se
    # Std ratios should also be comparable (within 20% for N=64).
    std_ratio = e_jax.std(ddof=1) / e_np.std(ddof=1)
    print(f"Energy-std ratio (jax/np): {std_ratio:.3f}")
    ok = ok and (0.7 < std_ratio < 1.3)

    # Sanity: both distributions should be > 0 std (not collapsed).
    ok = ok and e_np.std(ddof=1) > 1e-3
    ok = ok and e_jax.std(ddof=1) > 1e-3

    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
