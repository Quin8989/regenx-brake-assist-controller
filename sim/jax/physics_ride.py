"""Stage B3 — JAX port of sim.physics.simulate_ride() for fixed-gain.

Scope:
    * Fixed-gain controller (no strategy dispatch, no fast-path aggregates).
    * Noise disabled (sigma=0) — deterministic.
    * Takes pre-resampled per-tick brake/grade/pedal arrays.  Resampling
      from 1 ms to 10 ms stays in numpy (one-time cost, not on the hot path).
    * vmap-ready: all inputs are rank-1 tensors with a fixed n_ticks.
      Per-ride variables (mass, cruise, per-tick brake/grade/pedal,
      t_rr_ring pre-factor) are per-trajectory arguments.
"""

from __future__ import annotations

import jax
import jax.numpy as jnp
from jax import lax

from sim.jax.physics import _step as _physics_step
from sim.jax.physics_loop import voltage_taper_jax, ff_current_jax, apply_regen_limits_jax
from sim.physics import STICTION_W as _STICTION_W

from sim.jax.env import DEFAULT_FLOAT  # noqa: F401  (configures jax)

_TWO_PI = 2.0 * jnp.pi
_G = 9.81


def _rider_pedal_torque(
    pedal_on, w_ring, cruise_mps, mass_kg, c_rr, cos_g, sin_g,
    rider_kp_p_per_err, rider_p_sustain_w, rider_p_burst_w,
    rider_t_max_nm,
):
    """Pure-JAX equivalent of the closed-loop rider P-controller in
    simulate_ride().  Matches numpy lines 770–803 exactly.
    """
    v_now = w_ring * (1.0 / (1.0))  # placeholder; caller provides R_WHEEL in conversion
    # Caller will convert w_ring to v_now before calling; keep signature simple.
    # To avoid R_WHEEL dependency here, we accept v_now directly.
    # (See wrapper below.)
    del v_now
    return 0.0  # overridden in main function


def simulate_ride_fixed_gain_jax(
    *,
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
    # Controller
    k_gain,
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
):
    """JAX fixed-gain ride simulator.  Returns log arrays of shape
    [n_ticks].  Samples after early-stop are zero-valued.
    """
    rpm_buf0 = jnp.zeros(delay_slots, dtype=DEFAULT_FLOAT)

    # Rider pedal model constants (match numpy simulate_ride).
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
    )

    zeros_t = jnp.zeros(n_ticks, dtype=DEFAULT_FLOAT)
    init_logs = dict(
        t=zeros_t, speed=zeros_t, speed_baseline=zeros_t,
        motor_rpm=zeros_t, current=zeros_t, carrier_rpm=zeros_t,
        vcap=zeros_t, p_elec=zeros_t, p_copper=zeros_t, p_brake=zeros_t,
        brake_demand=zeros_t, eta=zeros_t, pedal=zeros_t, grade=zeros_t,
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

        # ── Rider pedal P-controller ──────────────────────────────────
        v_now = state["w_ring"] * r_wheel
        # Conditions (from numpy):
        #   pedal_on AND v_now < cruise + 0.1 AND v_now > 0.5
        pedal_engaged = pedal_on & (v_now < cruise_mps + 0.1) & (v_now > 0.5)

        err_ms = jnp.maximum(0.0, cruise_mps - v_now)
        # Steady-state demand — see numpy lines 783-786.
        # p_ss = max(0, (c_rr * m * g * cos_g
        #                + 0.28 * 0.509 * v_cruise * v_cruise / max(1, v_cruise)
        #                + m * g * sin_g) * v_cruise)
        aero_term = 0.28 * 0.509 * cruise_mps * cruise_mps / jnp.maximum(cruise_mps, 1.0)
        p_ss = jnp.maximum(
            0.0,
            (c_rr * mass_kg * _G * cos_g + aero_term + mass_kg * _G * sin_g)
            * cruise_mps,
        )
        p_rider_raw = p_ss + rider_kp_p_per_err * err_ms
        p_rider = jnp.minimum(p_rider_raw, rider_p_burst_w)
        # Soft sag between sustain and burst.
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

        # ── Fixed-gain controller ────────────────────────────────────
        taper = voltage_taper_jax(v_cap_sensed, vcap_taper_start, vcap_taper_end)
        i_cmd = ff_current_jax(
            delayed_rpm, k_gain, flux_linkage, r_phase, pole_pairs, current_limit
        ) * taper

        iq_reported = state["i_actual"] * vesc_current_gain
        i_cmd = jnp.where(iq_kp > 0.0,
                          i_cmd + iq_kp * (i_cmd - iq_reported),
                          i_cmd)

        omega_e_ctrl = delayed_rpm * pole_pairs * _TWO_PI / 60.0
        back_emf = flux_linkage * omega_e_ctrl
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

        # ── Physics substeps ─────────────────────────────────────────
        phys_carry = (
            state["w_ring"], state["w_carrier"], state["i_actual"],
            state["e_cap"], state["w_ring_base"],
            state["rpm_buf"], state["rpm_idx"],
            state["rpm_prev_sub"],
            DEFAULT_FLOAT(0.0),  # drpm_peak_neg_sub
            DEFAULT_FLOAT(0.0),  # iq_sum
            jnp.int32(0),      # n_done
            DEFAULT_FLOAT(0.0),  # p_net_sum
            DEFAULT_FLOAT(0.0),  # pcu_esr_sum
            DEFAULT_FLOAT(0.0),  # p_brake_sum
            DEFAULT_FLOAT(0.0),  # p_drg_sum
            state["delta_band"],
            DEFAULT_FLOAT(0.0),  # motor_rpm placeholder
            jnp.bool_(False),  # inner stopped
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
         delta_band_o, motor_rpm_o, inner_stopped) = phys_final

        v_cap_post = jnp.sqrt(jnp.maximum(2.0 * e_cap_o * inv_cap, 0.0))

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
        )

        return (new_state, new_logs)

    _, final_logs = lax.fori_loop(
        0, n_ticks, tick_body, (init_state, init_logs)
    )

    return final_logs
