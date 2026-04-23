"""Stage B2 — JAX port of the outer control loop in sim.physics.simulate().

Scope:
    * Fixed-gain controller only (k is a float, like simulate(k=...)).
    * Noise disabled (sigma=0) — Stage B2 targets deterministic bit-for-bit
      parity with the numpy/numba path.  Stochastic noise is a B3 concern.
    * Constant brake (float), no ride trace.  simulate_ride parity is B3.

The JAX loop uses ``lax.fori_loop`` over the fixed tick count and freezes
state once the early-stop condition trips, so every trajectory has a
fixed compute shape (vmap-friendly).
"""

from __future__ import annotations

import jax
import jax.numpy as jnp
from jax import lax

from sim.physics_jax import _step as _physics_step

from sim.jax_env import DEFAULT_FLOAT  # noqa: F401  (configures jax)


# ---------------------------------------------------------------------
# Controller helpers (pure JAX, branchless).
# ---------------------------------------------------------------------

_TWO_PI = 2.0 * jnp.pi


def voltage_taper_jax(vcap, taper_start, taper_end):
    below = vcap <= taper_start
    above = vcap >= taper_end
    mid = (taper_end - vcap) / (taper_end - taper_start)
    return jnp.where(below, 1.0, jnp.where(above, 0.0, mid))


def ff_current_jax(rpm, gain, flux_linkage, phase_resistance, pole_pairs, current_limit):
    omega_e = rpm * pole_pairs * _TWO_PI / 60.0
    current = gain * flux_linkage * omega_e / phase_resistance
    current = jnp.maximum(current, 0.0)
    return jnp.minimum(current, current_limit)


def apply_regen_limits_jax(current_cmd, *,
                           current_limit,
                           power_w, power_limit_w,
                           duty_cycle, duty_limit):
    """Branchless regen limits — mirrors firmware.regen.regen_control."""
    # Power scaling.  Numpy branch: `if power_w > power_limit_w`.
    p_scale = jnp.where(power_w > power_limit_w,
                        power_limit_w / jnp.maximum(power_w, 1e-12),
                        1.0)
    current_cmd = current_cmd * p_scale

    # Duty headroom.
    headroom = (1.0 - duty_cycle) / jnp.maximum(1.0 - duty_limit, 1e-12)
    headroom = jnp.clip(headroom, 0.0, 1.0)
    d_scale = jnp.where(duty_cycle > duty_limit, headroom, 1.0)
    current_cmd = current_cmd * d_scale

    current_cmd = jnp.maximum(current_cmd, 0.0)
    current_cmd = jnp.minimum(current_cmd, current_limit)
    return current_cmd


# ---------------------------------------------------------------------
# Outer control-loop body — one 10 ms tick.
# ---------------------------------------------------------------------

def simulate_fixed_gain_jax(
    *,
    # Initial state
    w_ring0, w_carrier0, i_actual0, e_cap0, w_ring_base0,
    delay_slots,                # int (static)
    n_ticks,                    # int (static)
    ctrl_steps,                 # int (static) — substeps per tick
    dt,                         # float
    # Controller gain
    k_gain,
    # Brake
    brake_const,
    # Physics constants
    one_plus_n, gear_n, kt, eta_gear, t_drag_coeff, r_phase,
    r_phase_15,
    foc_alpha, inv_cur_gain,
    inv_j_carrier, inv_j_wheel,
    mu_ratio, cap_esr, inv_cap,
    n_over_np1, rpm_scale,
    free_decel, v_min_w,
    t_rr_ring, t_grav_ring,
    k_band, c_band,
    # Motor electrical
    flux_linkage, pole_pairs,
    # Regen limits
    current_limit, power_limit_w, duty_limit,
    # Taper
    vcap_taper_start, vcap_taper_end,
    # iq Kp feedback (set 0 to disable)
    iq_kp,
    # vesc sense gains
    vesc_current_gain, vcap_gain,
    spd_scale,
):
    """JAX port of sim.physics.simulate() for the fixed-gain path, noise-free.

    Returns a dict of arrays of shape [n_ticks] (same keys as numpy sim).
    Ticks after early stop are zero-padded so shape is static.  Caller
    can use ``n_valid`` to truncate.
    """
    rpm_buf0 = jnp.zeros(delay_slots, dtype=DEFAULT_FLOAT)

    # Per-tick log carry.
    log_t = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)
    log_speed = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)
    log_speed_base = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)
    log_motor_rpm = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)
    log_current = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)
    log_carrier_rpm = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)
    log_vcap = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)
    log_p_elec = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)
    log_p_copper = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)
    log_p_brake = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)
    log_brake_demand = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)
    log_eta = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)

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
        stopped=jnp.bool_(False),
        n_valid=jnp.int32(0),
    )

    buf_len = delay_slots
    inv_sub = 1.0 / ctrl_steps
    ctrl_period = ctrl_steps * dt

    def tick_body(tick_idx, carry):
        state, logs = carry

        # Sense: delayed rpm from circular buffer.
        delayed_rpm = state["rpm_buf"][state["rpm_idx"]]
        v_cap = jnp.sqrt(jnp.maximum(2.0 * state["e_cap"] * inv_cap, 0.0))
        # numpy uses `if e_cap > 0 else 0.0` — jnp.sqrt on clamped value gives 0.
        v_cap_sensed = v_cap * vcap_gain

        # Fixed-gain controller: ff_current * voltage_taper.
        taper = voltage_taper_jax(v_cap_sensed, vcap_taper_start, vcap_taper_end)
        i_cmd = ff_current_jax(
            delayed_rpm, k_gain, flux_linkage, r_phase, pole_pairs, current_limit
        ) * taper

        # iq Kp feedback (match _compute_current_command).
        iq_reported = state["i_actual"] * vesc_current_gain
        i_cmd = jnp.where(
            iq_kp > 0.0,
            i_cmd + iq_kp * (i_cmd - iq_reported),
            i_cmd,
        )

        # Bus power estimate.
        omega_e_ctrl = delayed_rpm * pole_pairs * _TWO_PI / 60.0
        back_emf = flux_linkage * omega_e_ctrl
        p_bus_est = jnp.where((i_cmd > 0.0) & (back_emf > 0.0),
                              i_cmd * back_emf, 0.0)

        # Duty estimate (only valid when v_cap > 1 and back_emf > 0).
        # Numpy passes duty_cycle=None when not valid → limiter is a no-op
        # (we mirror that with a "duty below limit" surrogate).
        duty_valid = (v_cap > 1.0) & (back_emf > 0.0)
        duty_cycle = jnp.where(duty_valid,
                               back_emf / jnp.maximum(v_cap, 1e-12),
                               0.0)   # 0 ≤ duty_limit → no scaling.

        # Apply limits.  For numpy's power_w=None case (when i_cmd<=0 or
        # back_emf<=0) the power limiter is a no-op, which we encode by
        # setting p_bus_est=0 (0 ≤ power_limit → no scaling).
        i_cmd = apply_regen_limits_jax(
            i_cmd,
            current_limit=current_limit,
            power_w=p_bus_est,
            power_limit_w=power_limit_w,
            duty_cycle=duty_cycle,
            duty_limit=duty_limit,
        )

        # Run physics substeps.
        # Pack carry tuple in the same order as _physics_step expects.
        phys_carry = (
            state["w_ring"], state["w_carrier"], state["i_actual"],
            state["e_cap"], state["w_ring_base"],
            state["rpm_buf"], state["rpm_idx"],
            state["rpm_prev_sub"],
            DEFAULT_FLOAT(0.0),   # drpm_peak_neg_sub — reset each tick
            DEFAULT_FLOAT(0.0),   # iq_sum
            jnp.int32(0),       # n_done
            DEFAULT_FLOAT(0.0),   # p_net_sum
            DEFAULT_FLOAT(0.0),   # pcu_esr_sum
            DEFAULT_FLOAT(0.0),   # p_brake_sum
            DEFAULT_FLOAT(0.0),   # p_drg_sum
            state["delta_band"],
            DEFAULT_FLOAT(0.0),   # motor_rpm placeholder
            jnp.bool_(False),   # inner-step stopped flag
        )

        consts = (
            DEFAULT_FLOAT(dt), DEFAULT_FLOAT(i_cmd), DEFAULT_FLOAT(brake_const),
            DEFAULT_FLOAT(one_plus_n), DEFAULT_FLOAT(gear_n), DEFAULT_FLOAT(kt),
            DEFAULT_FLOAT(eta_gear), DEFAULT_FLOAT(t_drag_coeff), DEFAULT_FLOAT(r_phase_15),
            DEFAULT_FLOAT(foc_alpha), DEFAULT_FLOAT(inv_cur_gain),
            DEFAULT_FLOAT(inv_j_carrier), DEFAULT_FLOAT(inv_j_wheel),
            DEFAULT_FLOAT(mu_ratio), DEFAULT_FLOAT(cap_esr), DEFAULT_FLOAT(inv_cap),
            DEFAULT_FLOAT(n_over_np1), DEFAULT_FLOAT(rpm_scale),
            jnp.bool_(free_decel), DEFAULT_FLOAT(v_min_w),
            DEFAULT_FLOAT(t_rr_ring), DEFAULT_FLOAT(t_grav_ring), DEFAULT_FLOAT(0.0),
            DEFAULT_FLOAT(k_band), DEFAULT_FLOAT(c_band),
        )

        def substep_body(i, c):
            return _physics_step(i, c, consts, buf_len)

        phys_final = lax.fori_loop(0, ctrl_steps, substep_body, phys_carry)

        (w_ring_o, w_carrier_o, i_actual_o, e_cap_o, w_ring_base_o,
         rpm_buf_o, rpm_idx_o,
         rpm_prev_sub_o, drpm_peak_neg_sub_o, iq_sum_o, n_done_o,
         p_net_sum_o, pcu_esr_sum_o, p_brake_sum_o, p_drg_sum_o,
         delta_band_o, motor_rpm_o, inner_stopped) = phys_final

        v_cap_post = jnp.sqrt(jnp.maximum(2.0 * e_cap_o * inv_cap, 0.0))

        # Early-stop check (mirrors both numba's inner `if stopped` and
        # the outer loop's `if free_decel and w_ring * spd_scale < v_min
        # and w_carrier <= 0` check).
        just_stopped = inner_stopped | (
            free_decel & (w_ring_o * spd_scale < v_min_w * spd_scale)
            & (w_carrier_o <= 0.0)
        )
        # Note: numpy compares v_min_kmh directly, so v_min_w*spd_scale
        # == v_min_kmh/3.6*R_WHEEL*R_WHEEL*3.6 = v_min_kmh... wait, let
        # me re-check.  spd_scale = R_WHEEL * 3.6, v_min_w = (v_min_kmh
        # /3.6)/R_WHEEL.  So w_ring*spd_scale in km/h; v_min_w*spd_scale
        # = v_min_kmh.  Good.

        # Only record & advance state if not already stopped.
        active = ~state["stopped"]

        # When stopped, keep previous state.
        def sel(new, old):
            return jnp.where(active, new, old)

        new_state = dict(
            w_ring=sel(w_ring_o, state["w_ring"]),
            w_carrier=sel(w_carrier_o, state["w_carrier"]),
            i_actual=sel(i_actual_o, state["i_actual"]),
            e_cap=sel(e_cap_o, state["e_cap"]),
            w_ring_base=sel(w_ring_base_o, state["w_ring_base"]),
            rpm_buf=jnp.where(active, rpm_buf_o, state["rpm_buf"]),
            rpm_idx=sel(rpm_idx_o, state["rpm_idx"]),
            rpm_prev_sub=sel(rpm_prev_sub_o, state["rpm_prev_sub"]),
            delta_band=sel(delta_band_o, state["delta_band"]),
            stopped=state["stopped"] | just_stopped,
            n_valid=state["n_valid"] + jnp.int32(active),
        )

        # Record log for this tick, but only when active (stopped carry
        # doesn't advance, matching numpy which just breaks the loop
        # and slices at ix).
        t_val = DEFAULT_FLOAT(tick_idx) * ctrl_period
        p_elec = p_net_sum_o * inv_sub
        p_cu = pcu_esr_sum_o * inv_sub
        p_brake_s = p_brake_sum_o * inv_sub
        p_drg_s = p_drg_sum_o * inv_sub
        denom = p_elec + p_cu + p_drg_s + p_brake_s
        eta_val = jnp.where(denom > 1.0, p_elec / denom, 0.0)

        def upd(arr, val):
            return arr.at[tick_idx].set(jnp.where(active, val, arr[tick_idx]))

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
            brake_demand=upd(logs["brake_demand"], DEFAULT_FLOAT(brake_const)),
            eta=upd(logs["eta"], eta_val),
        )

        return (new_state, new_logs)

    init_logs = dict(
        t=log_t, speed=log_speed, speed_baseline=log_speed_base,
        motor_rpm=log_motor_rpm, current=log_current,
        carrier_rpm=log_carrier_rpm, vcap=log_vcap,
        p_elec=log_p_elec, p_copper=log_p_copper, p_brake=log_p_brake,
        brake_demand=jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT),
        eta=log_eta,
    )

    final_state, final_logs = lax.fori_loop(
        0, n_ticks, tick_body, (init_state, init_logs)
    )

    final_logs["n_valid"] = final_state["n_valid"]
    return final_logs
