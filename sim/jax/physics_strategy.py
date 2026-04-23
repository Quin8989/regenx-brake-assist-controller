"""Stage B4 — JAX strategy dispatch + sympy→JAX lambdification.

This extends :mod:`sim.physics_jax_ride` with a strategy-capable
simulator.  The strategy is a pure JAX callable that accepts the
13 features ``PysrStrategy`` expects and returns the next ``k``
gain.  Fast-path aggregates (``drpm_mean``, ``drpm_peak_neg``,
``iq_mean``) are threaded across ticks from the inner physics batch
— they carry one tick of latency, exactly like the numpy sim.

A helper ``lambdify_expression_jax`` converts a PySR hall-of-fame
equation string into a JAX-traceable callable, unlocking Track C:
evaluating thousands of PySR-discovered expressions per second
inside a ``vmap`` over the ride basket.
"""

from __future__ import annotations

from typing import Callable

import jax
import jax.numpy as jnp
from jax import lax

from sim.jax.physics import _step as _physics_step
from sim.physics import STICTION_W as _STICTION_W
from sim.jax.physics_loop import (
    voltage_taper_jax,
    ff_current_jax,
    apply_regen_limits_jax,
)

from sim.jax.env import DEFAULT_FLOAT  # (configures jax)

_TWO_PI = 2.0 * jnp.pi
_G = 9.81

# Features PySR was trained on (must match scripts/pysr/validate_candidates.py).
FEATURE_NAMES = (
    "rpm", "drpm_mean", "drpm_peak_neg",
    "iq", "duty_cycle", "vcap", "k_prev",
    "jerk_mean", "jerk_peak", "slip_delta",
    "decel_frac", "d_iq", "power_mech",
)

# Same clamps as PysrStrategy.
K_FLOOR = 0.0
K_CEIL = 1.2


def lambdify_expression_jax(equation: str) -> Callable:
    """Parse a PySR equation string into a JAX-traceable callable.

    Mirrors :func:`scripts.pysr.validate_candidates.lambdify_expression`
    but emits ``jax.numpy`` instead of ``numpy`` so the result can be
    composed with ``jax.jit`` / ``jax.vmap``.
    """
    import sympy as sp

    locals_map = {
        "relu":    lambda x: (sp.Abs(x) + x) / 2,
        "negrelu": lambda x: (sp.Abs(x) - x) / 2,
        "step":    sp.Heaviside,
        # Expanded op set (2026-04-23): smooth saturation (tanh),
        # native min/max for clamps, and safe transcendentals.
        # ``safeexp`` / ``safelog`` match the Julia-side definitions in
        # scripts/pysr_invent_composite.py so expressions round-trip.
        "tanh":     sp.tanh,
        "safetanh": sp.tanh,
        "min":      sp.Min,
        "max":      sp.Max,
        # Clamp the exp argument to match the Julia side exactly.
        # Using sp.Min/sp.Max keeps it symbolic so lambdify emits jnp.
        "safeexp": lambda x: sp.exp(sp.Min(sp.Max(x, -30), 30)),
        "safelog": lambda x: sp.log(sp.Abs(x) + 1e-9),
    }
    syms = sp.symbols(FEATURE_NAMES)
    expr = sp.sympify(equation, locals=locals_map)
    fn = sp.lambdify(
        syms, expr,
        modules=[{"Heaviside": lambda x, _h=0.5: jnp.heaviside(x, 0.5)},
                 "jax"],
    )
    return fn


def simulate_ride_strategy_jax(
    *,
    # Strategy: (rpm, drpm_mean, drpm_peak_neg, iq, duty_cycle, vcap,
    #            k_prev, jerk_mean, jerk_peak, slip_delta,
    #            decel_frac, d_iq, power_mech) -> k_next
    strategy_fn: Callable | None = None,
    # Optional stateful strategy API (preferred for classical controllers
    # like pi_controller / aimd_ff).  Signature:
    #   strategy_step_fn(feats_tuple, strategy_state) -> (k_next, new_strategy_state)
    # where ``feats_tuple`` is the same 13-element tuple passed to the
    # stateless ``strategy_fn`` above.  On taper==0 (regen inhibited) the
    # carried state is reset to ``strategy_state0``.
    strategy_step_fn: Callable | None = None,
    strategy_state0=None,
    # Initial state
    w_ring0, w_carrier0, i_actual0, e_cap0, w_ring_base0,
    delay_slots,               # static
    n_ticks,                   # static
    ctrl_steps,                # static
    dt,                        # static float
    # Per-tick ride arrays (shape [n_ticks])
    brake_ticks, grade_ticks, pedal_active_ticks,
    # Per-ride scalars
    mass_kg, cruise_mps, c_rr, r_wheel,
    # Physics constants
    one_plus_n, gear_n, kt, eta_gear, t_drag_coeff, r_phase,
    r_phase_15,
    foc_alpha, inv_cur_gain,
    inv_j_carrier, inv_j_wheel,
    mu_ratio, cap_esr, inv_cap,
    n_over_np1, rpm_scale,
    v_min_w,
    k_band, c_band,
    # Motor electrical
    flux_linkage, pole_pairs,
    # Regen limits
    current_limit, power_limit_w, duty_limit,
    # Taper
    vcap_taper_start, vcap_taper_end,
    # iq feedback
    iq_kp,
    # VESC sense
    vesc_current_gain, vcap_gain,
    spd_scale,
    # ── Telemetry noise (per-trajectory) ─────────────────────────────
    # Pass a jax.random.PRNGKey to enable stochastic noise.  When sigmas
    # are all 0.0 and iq_bias=0.0, the noise path is mathematically a
    # no-op but still samples (use zero sigmas to keep a stable shape
    # under jit/vmap).
    noise_key=None,
    rpm_noise_sigma=0.0,
    iq_noise_sigma=0.0,
    iq_bias=0.0,
    vcap_noise_sigma=0.0,
    drpm_mean_noise_sigma=0.0,
    drpm_peak_neg_noise_sigma=0.0,
    drpm_peak_neg_noise_bias=0.0,
):
    """JAX ride simulator with strategy dispatch.  Same log layout as
    :func:`simulate_ride_fixed_gain_jax`.
    """
    # ── Resolve strategy dispatch mode ─────────────────────────────
    # Stateless path (PySR default): wrap the pure callable in an
    # adapter that carries a dummy scalar state.  Traced graph is
    # functionally identical to the pre-refactor version.
    if strategy_step_fn is None:
        if strategy_fn is None:
            raise ValueError(
                "simulate_ride_strategy_jax: provide strategy_fn "
                "(stateless) or strategy_step_fn (stateful)."
            )
        _sf = strategy_fn  # bind for closure
        def _stateless_adapter(feats, state):
            return _sf(*feats), state
        strategy_step_fn = _stateless_adapter
        strategy_state0 = jnp.zeros((), dtype=DEFAULT_FLOAT)
    else:
        if strategy_state0 is None:
            raise ValueError(
                "simulate_ride_strategy_jax: strategy_step_fn requires "
                "a non-None strategy_state0 pytree."
            )

    rpm_buf0 = jnp.zeros(delay_slots, dtype=DEFAULT_FLOAT)

    rider_kp_p_per_err = 150.0
    rider_p_sustain_w = 150.0
    rider_p_burst_w = 400.0
    rider_t_max_nm = 120.0

    inv_sub = 1.0 / ctrl_steps
    ctrl_period = ctrl_steps * dt

    init_state = dict(
        w_ring=DEFAULT_FLOAT(w_ring0),
        w_carrier=DEFAULT_FLOAT(w_carrier0),
        i_actual=DEFAULT_FLOAT(i_actual0),
        e_cap=DEFAULT_FLOAT(e_cap0),
        w_ring_base=DEFAULT_FLOAT(w_ring_base0),
        rpm_buf=rpm_buf0,
        rpm_idx=jnp.int32(0),
        rpm_prev_sub=DEFAULT_FLOAT(0.0),
        delta_band=DEFAULT_FLOAT(0.0),
        # ── Strategy state ────────────────────────────────────────────
        # Aggregates from the *previous* physics batch; these become
        # ctx.drpm_mean / ctx.drpm_peak_neg / ctx.iq_mean this tick.
        drpm_mean_agg=DEFAULT_FLOAT(0.0),
        drpm_peak_neg_agg=DEFAULT_FLOAT(0.0),
        iq_mean_agg=DEFAULT_FLOAT(0.0),
        # What the strategy saw last tick — mirrors PysrStrategy's
        # ``self._drpm_mean_prev`` / ``self._iq_prev``.
        drpm_mean_last_used=DEFAULT_FLOAT(0.0),
        drpm_peak_neg_last_used=DEFAULT_FLOAT(0.0),
        iq_last_used=DEFAULT_FLOAT(0.0),
        k_prev=DEFAULT_FLOAT(0.1),
        # Per-strategy extra carry (integral / k_eff / slip_prev_raw / ...).
        # Scalar 0-d array for stateless PySR strategies.
        strategy_state=strategy_state0,
    )

    # ── Noise key bootstrap ───────────────────────────────────────────
    # When disabled we carry a fixed PRNGKey(0) and let the zero-sigma
    # multiplications kill the contribution.  This keeps the tick body
    # jit-trace stable (no Python-None branches).
    noise_enabled = noise_key is not None
    if not noise_enabled:
        noise_key = jax.random.PRNGKey(0)
    init_state["noise_key"] = noise_key

    zeros_t = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)
    init_logs = dict(
        t=zeros_t, speed=zeros_t, speed_baseline=zeros_t,
        motor_rpm=zeros_t, current=zeros_t, carrier_rpm=zeros_t,
        vcap=zeros_t, p_elec=zeros_t, p_copper=zeros_t, p_brake=zeros_t,
        brake_demand=zeros_t, eta=zeros_t, pedal=zeros_t, grade=zeros_t,
        k_cmd=zeros_t,
    )

    def tick_body(tick_idx, carry):
        state, logs = carry

        brake_val = brake_ticks[tick_idx]
        grade_val = grade_ticks[tick_idx]
        pedal_on = pedal_active_ticks[tick_idx]

        cos_g = jnp.cos(grade_val)
        sin_g = jnp.sin(grade_val)
        t_rr_ring = c_rr * mass_kg * _G * cos_g * r_wheel
        t_grav_ring = mass_kg * _G * sin_g * r_wheel

        # ── Rider pedal ──────────────────────────────────────────────
        v_now = state["w_ring"] * r_wheel
        pedal_engaged = pedal_on & (v_now < cruise_mps + 0.1) & (v_now > 0.5)
        err_ms = jnp.maximum(0.0, cruise_mps - v_now)
        aero_term = 0.28 * 0.509 * cruise_mps * cruise_mps / jnp.maximum(cruise_mps, 1.0)
        p_ss = jnp.maximum(
            0.0,
            (c_rr * mass_kg * _G * cos_g + aero_term + mass_kg * _G * sin_g)
            * cruise_mps,
        )
        p_rider_raw = p_ss + rider_kp_p_per_err * err_ms
        p_rider = jnp.minimum(p_rider_raw, rider_p_burst_w)
        excess = (p_rider - rider_p_sustain_w) / jnp.maximum(
            rider_p_burst_w - rider_p_sustain_w, 1e-12)
        p_rider_sagged = p_rider * (1.0 - 0.2 * excess)
        p_rider = jnp.where(p_rider > rider_p_sustain_w, p_rider_sagged, p_rider)
        t_pedal_raw = p_rider / jnp.maximum(v_now, 1e-12)
        t_pedal_clipped = jnp.minimum(t_pedal_raw, rider_t_max_nm)
        t_pedal_ring = jnp.where(pedal_engaged, t_pedal_clipped, 0.0)

        # ── Sense ────────────────────────────────────────────────────
        delayed_rpm = state["rpm_buf"][state["rpm_idx"]]
        v_cap = jnp.sqrt(jnp.maximum(2.0 * state["e_cap"] * inv_cap, 0.0))
        v_cap_sensed = v_cap * vcap_gain

        # Duty estimate for the strategy (same as numpy).
        omega_e_est = delayed_rpm * pole_pairs * _TWO_PI / 60.0
        bemf_est = flux_linkage * omega_e_est
        duty_est = jnp.where(v_cap > 1.0, bemf_est / jnp.maximum(v_cap, 1e-12), 0.0)

        # Taper.
        taper = voltage_taper_jax(v_cap_sensed, vcap_taper_start, vcap_taper_end)
        taper_active = taper > 0.0

        # ── Features & strategy dispatch ────────────────────────────
        ctx_drpm_mean     = state["drpm_mean_agg"]
        ctx_drpm_peak_neg = state["drpm_peak_neg_agg"]
        # Numpy: iq_for_strategy = i_actual * vesc_current_gain + iq_bias;
        # then iq_mean = iq_mean_prev if iq_mean_prev != 0.0 else
        # iq_for_strategy.
        iq_for_strategy = state["i_actual"] * vesc_current_gain + iq_bias
        iq_feat = jnp.where(state["iq_mean_agg"] != 0.0,
                            state["iq_mean_agg"],
                            iq_for_strategy)

        # ── Telemetry noise (matches sim.physics._compute_current_command
        # lines 1050-1064 — 5 independent normals per tick, drawn in the
        # same order).  With zero sigmas this is a no-op identity.
        nkey_next, k_rpm, k_iq, k_dm, k_dpn, k_vc = jax.random.split(
            state["noise_key"], 6)
        rpm_feat = delayed_rpm + rpm_noise_sigma * jax.random.normal(
            k_rpm, dtype=DEFAULT_FLOAT)
        iq_feat = iq_feat + iq_noise_sigma * jax.random.normal(
            k_iq, dtype=DEFAULT_FLOAT)
        ctx_drpm_mean = ctx_drpm_mean + drpm_mean_noise_sigma * jax.random.normal(
            k_dm, dtype=DEFAULT_FLOAT)
        ctx_drpm_peak_neg = (
            ctx_drpm_peak_neg + drpm_peak_neg_noise_bias
            + drpm_peak_neg_noise_sigma * jax.random.normal(
                k_dpn, dtype=DEFAULT_FLOAT)
        )
        # Numpy clamps peak-held minimum to ≤0 after noise.
        ctx_drpm_peak_neg = jnp.minimum(ctx_drpm_peak_neg, 0.0)
        v_cap_sensed = v_cap_sensed + vcap_noise_sigma * jax.random.normal(
            k_vc, dtype=DEFAULT_FLOAT)
        # Re-evaluate taper on the (possibly) noisy v_cap_sensed — matches
        # numpy (voltage_taper is called once on v_cap_sensed prior to
        # strategy dispatch; we moved the call earlier so recompute here).
        taper = voltage_taper_jax(v_cap_sensed, vcap_taper_start, vcap_taper_end)
        taper_active = taper > 0.0

        jerk_mean  = ctx_drpm_mean     - state["drpm_mean_last_used"]
        jerk_peak  = ctx_drpm_peak_neg - state["drpm_peak_neg_last_used"]
        slip_delta = ctx_drpm_peak_neg - ctx_drpm_mean
        decel_frac = ctx_drpm_mean / (rpm_feat + 1.0)
        d_iq       = iq_feat - state["iq_last_used"]
        power_mech = rpm_feat * iq_feat

        feats = (
            rpm_feat, ctx_drpm_mean, ctx_drpm_peak_neg,
            iq_feat, duty_est, v_cap_sensed, state["k_prev"],
            jerk_mean, jerk_peak, slip_delta,
            decel_frac, d_iq, power_mech,
        )
        k_raw, strategy_state_updated = strategy_step_fn(
            feats, state["strategy_state"],
        )
        # Non-finite guard — fall back to k_prev.
        k_raw = jnp.where(jnp.isfinite(k_raw), k_raw, state["k_prev"])
        k_clipped = jnp.clip(k_raw, K_FLOOR, K_CEIL)

        # Taper==0 → reset strategy state & emit zero current (matches
        # PysrStrategy.update short-circuit).
        k_used         = jnp.where(taper_active, k_clipped, 0.1)
        new_k_prev     = k_used   # stored regardless
        new_dm_last    = jnp.where(taper_active, ctx_drpm_mean, 0.0)
        new_dpn_last   = jnp.where(taper_active, ctx_drpm_peak_neg, 0.0)
        new_iq_last    = jnp.where(taper_active, iq_feat, 0.0)

        # Reset per-strategy carry when taper==0 (mirrors each numpy
        # strategy's ``_reset`` on regen inhibit).  tree_map handles
        # arbitrary pytree state (scalar / dict / nested).
        new_strategy_state = jax.tree_util.tree_map(
            lambda new, init: jnp.where(taper_active, new, init),
            strategy_state_updated,
            strategy_state0,
        )

        # ── i_cmd from ff_current * taper, clipped to [0, I_MAX] ────
        i_cmd_pre = ff_current_jax(
            delayed_rpm, k_clipped, flux_linkage, r_phase, pole_pairs, current_limit,
        ) * taper
        i_cmd_pre = jnp.clip(i_cmd_pre, 0.0, current_limit)
        # If taper==0, strategy returns 0 directly (matches numpy).
        i_cmd_pre = jnp.where(taper_active, i_cmd_pre, 0.0)

        # iq_kp feedback.
        iq_reported = state["i_actual"] * vesc_current_gain
        i_cmd = jnp.where(iq_kp > 0.0,
                          i_cmd_pre + iq_kp * (i_cmd_pre - iq_reported),
                          i_cmd_pre)

        # Bus-power + duty limits.
        back_emf = flux_linkage * omega_e_est
        p_bus_est = jnp.where((i_cmd > 0.0) & (back_emf > 0.0),
                              i_cmd * back_emf, 0.0)
        duty_valid = (v_cap > 1.0) & (back_emf > 0.0)
        duty_cycle = jnp.where(duty_valid,
                               back_emf / jnp.maximum(v_cap, 1e-12), 0.0)
        i_cmd = apply_regen_limits_jax(
            i_cmd,
            current_limit=current_limit,
            power_w=p_bus_est,
            power_limit_w=power_limit_w,
            duty_cycle=duty_cycle,
            duty_limit=duty_limit,
        )

        # ── Physics substeps ────────────────────────────────────────
        phys_carry = (
            state["w_ring"], state["w_carrier"], state["i_actual"],
            state["e_cap"], state["w_ring_base"],
            state["rpm_buf"], state["rpm_idx"],
            state["rpm_prev_sub"],
            DEFAULT_FLOAT(0.0),  # drpm_peak_neg_sub
            DEFAULT_FLOAT(0.0),  # iq_sum
            jnp.int32(0),
            DEFAULT_FLOAT(0.0),  # p_net_sum
            DEFAULT_FLOAT(0.0),  # pcu_esr_sum
            DEFAULT_FLOAT(0.0),  # p_brake_sum
            DEFAULT_FLOAT(0.0),  # p_drg_sum
            state["delta_band"],
            DEFAULT_FLOAT(0.0),  # motor_rpm placeholder
            jnp.bool_(False),
        )

        consts = (
            DEFAULT_FLOAT(dt), DEFAULT_FLOAT(i_cmd), DEFAULT_FLOAT(brake_val),
            DEFAULT_FLOAT(one_plus_n), DEFAULT_FLOAT(gear_n), DEFAULT_FLOAT(kt),
            DEFAULT_FLOAT(eta_gear), DEFAULT_FLOAT(t_drag_coeff), DEFAULT_FLOAT(r_phase_15),
            DEFAULT_FLOAT(foc_alpha), DEFAULT_FLOAT(inv_cur_gain),
            DEFAULT_FLOAT(inv_j_carrier), DEFAULT_FLOAT(inv_j_wheel),
            DEFAULT_FLOAT(mu_ratio), DEFAULT_FLOAT(_STICTION_W), DEFAULT_FLOAT(cap_esr), DEFAULT_FLOAT(inv_cap),
            DEFAULT_FLOAT(n_over_np1), DEFAULT_FLOAT(rpm_scale),
            jnp.bool_(True), DEFAULT_FLOAT(v_min_w),
            DEFAULT_FLOAT(t_rr_ring), DEFAULT_FLOAT(t_grav_ring), DEFAULT_FLOAT(t_pedal_ring),
            DEFAULT_FLOAT(k_band), DEFAULT_FLOAT(c_band),
        )

        buf_len = delay_slots

        def substep_body(i, c):
            return _physics_step(i, c, consts, buf_len)

        phys_final = lax.fori_loop(0, ctrl_steps, substep_body, phys_carry)

        (w_ring_o, w_carrier_o, i_actual_o, e_cap_o, w_ring_base_o,
         rpm_buf_o, rpm_idx_o,
         rpm_prev_sub_o, drpm_peak_neg_sub_o, iq_sum_o, n_done_o,
         p_net_sum_o, pcu_esr_sum_o, p_brake_sum_o, p_drg_sum_o,
         delta_band_o, motor_rpm_o, _inner_stopped) = phys_final

        v_cap_post = jnp.sqrt(jnp.maximum(2.0 * e_cap_o * inv_cap, 0.0))

        # Fast-path aggregates from this batch, for next tick.
        n_done_f = DEFAULT_FLOAT(n_done_o)
        drpm_mean_new = jnp.where(
            n_done_o >= 1,
            (motor_rpm_o - state["rpm_prev_sub"]) / jnp.maximum(n_done_f * dt, 1e-30),
            0.0,
        )
        iq_mean_new = jnp.where(n_done_o >= 1, iq_sum_o / jnp.maximum(n_done_f, 1.0), iq_sum_o)

        new_state = dict(
            w_ring=w_ring_o,
            w_carrier=w_carrier_o,
            i_actual=i_actual_o,
            e_cap=e_cap_o,
            w_ring_base=w_ring_base_o,
            rpm_buf=rpm_buf_o,
            rpm_idx=rpm_idx_o,
            rpm_prev_sub=rpm_prev_sub_o,
            delta_band=delta_band_o,
            drpm_mean_agg=drpm_mean_new,
            drpm_peak_neg_agg=drpm_peak_neg_sub_o,
            iq_mean_agg=iq_mean_new,
            drpm_mean_last_used=new_dm_last,
            drpm_peak_neg_last_used=new_dpn_last,
            iq_last_used=new_iq_last,
            k_prev=new_k_prev,
            noise_key=nkey_next,
            strategy_state=new_strategy_state,
        )

        t_val = DEFAULT_FLOAT(tick_idx) * ctrl_period
        p_elec = p_net_sum_o * inv_sub
        p_cu = pcu_esr_sum_o * inv_sub
        p_brake_s = p_brake_sum_o * inv_sub
        p_drg_s = p_drg_sum_o * inv_sub
        denom = p_elec + p_cu + p_drg_s + p_brake_s
        eta_val = jnp.where(denom > 1.0, p_elec / denom, 0.0)

        def upd(arr, val):
            return arr.at[tick_idx].set(val)

        new_logs = dict(
            t=upd(logs["t"], t_val),
            speed=upd(logs["speed"], new_state["w_ring"] * spd_scale),
            speed_baseline=upd(logs["speed_baseline"], new_state["w_ring_base"] * spd_scale),
            motor_rpm=upd(logs["motor_rpm"], motor_rpm_o),
            current=upd(logs["current"], new_state["i_actual"]),
            carrier_rpm=upd(logs["carrier_rpm"], new_state["w_carrier"] * rpm_scale),
            vcap=upd(logs["vcap"], v_cap_post),
            p_elec=upd(logs["p_elec"], p_elec),
            p_copper=upd(logs["p_copper"], p_cu),
            p_brake=upd(logs["p_brake"], p_brake_s),
            brake_demand=upd(logs["brake_demand"], brake_val),
            eta=upd(logs["eta"], eta_val),
            pedal=upd(logs["pedal"], t_pedal_ring),
            grade=upd(logs["grade"], grade_val),
            k_cmd=upd(logs["k_cmd"], k_used),
        )

        return (new_state, new_logs)

    _, final_logs = lax.fori_loop(
        0, n_ticks, tick_body, (init_state, init_logs)
    )
    return final_logs
