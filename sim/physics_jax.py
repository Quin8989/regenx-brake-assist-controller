"""sim.physics_jax — JAX port of sim.physics inner substep batch.

Stage B1 scope (this file):
    * One pure JAX function `run_physics_batch_jax` that matches the
      numba-jitted `_run_physics_batch` in sim.physics bit-for-bit up to
      floating-point reordering tolerance.
    * No outer control loop, no strategy dispatch, no ride harness.
      Those belong to Stage B2 / B3.

The goal is correctness, not speed.  We enable float64 and run on CPU.
Once this substep matches numba to ≤1e-6, the rest of the port can
build on it with confidence.
"""

from __future__ import annotations

import jax
import jax.numpy as jnp
from jax import lax

# Force float64 so we can compare to numba's double precision.
from sim.jax_env import DEFAULT_FLOAT  # noqa: F401  (configures jax)


# ---------------------------------------------------------------------
# Inner physics substep — one timestep.  Pure, takes a "carry" tuple and
# the per-step constants, returns the updated carry.  XLA inlines this
# inside lax.fori_loop below.
# ---------------------------------------------------------------------

def _step(i, carry, consts, buf_len):
    (w_ring, w_carrier, i_actual, e_cap, w_ring_base,
     rpm_buf, rpm_idx,
     rpm_prev_sub, drpm_peak_neg_sub, iq_sum, n_done,
     p_net_sum, pcu_esr_sum, p_brake_sum, p_drg_sum,
     delta_band, motor_rpm, stopped) = carry

    (dt, i_cmd, brake_val,
     one_plus_n, gear_n, kt, eta_gear, t_drag_coeff, r_phase_15,
     foc_alpha, inv_cur_gain,
     inv_j_carrier, inv_j_wheel,
     mu_ratio, cap_esr, inv_cap,
     n_over_np1, rpm_scale,
     free_decel, v_min_w,
     t_rr_ring, t_grav_ring, t_pedal_ring,
     k_band, c_band) = consts

    # Planetary kinematics.
    w_sun = one_plus_n * w_carrier - gear_n * w_ring
    motor_rpm_new = jnp.maximum(0.0, -w_sun) * rpm_scale

    # Window-aggregate bookkeeping (before physics update — match numba).
    delta_rate = (motor_rpm_new - rpm_prev_sub) / dt
    drpm_peak_neg_sub_new = jnp.minimum(drpm_peak_neg_sub, delta_rate)
    rpm_prev_sub_new = motor_rpm_new
    iq_sum_new = iq_sum + i_actual
    n_done_new = n_done + 1

    # RPM delay buffer (circular).
    rpm_buf_new = rpm_buf.at[rpm_idx].set(motor_rpm_new)
    rpm_idx_new = (rpm_idx + 1) % buf_len

    # FOC current delivery (first-order lag).
    i_target = i_cmd * inv_cur_gain
    i_actual_new = i_actual + foc_alpha * (i_target - i_actual)
    i_actual_new = jnp.maximum(i_actual_new, 0.0)

    # Motor coupling decision — branchless.
    motor_coupled = (brake_val > 0.0) | (i_cmd > 1e-4) | (i_actual_new > 1e-4)

    # Electromagnetic torque (t_em is 0 unless w_sun < 0, i.e. motor is
    # regenerating by spinning backwards through the planetary).
    t_em = jnp.where(w_sun < 0.0, kt * i_actual_new, 0.0)
    t_drag = jnp.where(motor_coupled, t_drag_coeff * jnp.abs(w_sun), 0.0)

    # Gear torques.
    t_em_car = one_plus_n * t_em * eta_gear
    t_em_ring = gear_n * t_em * eta_gear

    # ─── Band / carrier update (coupled branch) ───────────────────────
    # Accumulate band deformation; clamp static→kinetic on breakaway.
    delta_band_tentative = delta_band + w_carrier * dt
    delta_band_tentative = jnp.maximum(delta_band_tentative, 0.0)

    static_cap = jnp.where(k_band > 0.0, brake_val / k_band, 0.0)
    break_away = (brake_val > 0.0) & (delta_band_tentative > static_cap)
    delta_band_coupled = jnp.where(
        brake_val > 0.0,
        jnp.where(break_away, brake_val * mu_ratio / k_band, delta_band_tentative),
        0.0,
    )
    t_brake_coupled = jnp.where(
        brake_val > 0.0,
        jnp.maximum(k_band * delta_band_coupled + c_band * w_carrier, 0.0),
        0.0,
    )

    # Carrier dynamics.  Reproduce the two-branch integration in numba.
    net_car = t_em_car - t_brake_coupled
    # Branch: w_carrier <= 0 → stays at 0 unless net > 0 (then integrate).
    # Branch: w_carrier > 0 → integrate, clamp ≥ 0.
    w_carrier_pos = jnp.maximum(w_carrier + net_car * inv_j_carrier * dt, 0.0)
    w_carrier_zero_then_push = jnp.where(
        net_car > 0.0, 0.0 + net_car * inv_j_carrier * dt, 0.0)
    w_carrier_coupled = jnp.where(
        w_carrier <= 0.0, w_carrier_zero_then_push, w_carrier_pos)

    # ─── Decoupled branch ─────────────────────────────────────────────
    w_carrier_decoupled = n_over_np1 * w_ring
    delta_band_decoupled = 0.0
    t_brake_decoupled = 0.0

    # Select branch.
    w_carrier_new = jnp.where(motor_coupled, w_carrier_coupled, w_carrier_decoupled)
    delta_band_new = jnp.where(motor_coupled, delta_band_coupled, delta_band_decoupled)
    t_brake = jnp.where(motor_coupled, t_brake_coupled, t_brake_decoupled)

    # Wheel dynamics (only when free_decel).
    t_rr_signed = jnp.where(w_ring > 0.0, t_rr_ring, 0.0)
    t_drag_to_wheel = jnp.where(motor_coupled, gear_n * t_drag * eta_gear, 0.0)
    w_ring_free = w_ring - (
        t_em_ring + t_drag_to_wheel
        + t_rr_signed - t_grav_ring - t_pedal_ring
    ) * inv_j_wheel * dt
    w_ring_free = jnp.maximum(w_ring_free, 0.0)
    w_ring_new = jnp.where(free_decel, w_ring_free, w_ring)

    # Power accounting.
    abs_w_sun = jnp.abs(w_sun)
    p_mot = jnp.where(w_sun <= 0.0, t_em * abs_w_sun, 0.0)
    p_cu = r_phase_15 * i_actual_new * i_actual_new
    p_drg = t_drag * abs_w_sun
    p_cap = jnp.maximum(p_mot * eta_gear - p_cu - p_drg, 0.0)

    # Cap ESR loss — numba does this only when e_cap > 0.5 AND p_cap > 0.
    have_cap = (e_cap > 0.5) & (p_cap > 0.0)
    v_cap_local = jnp.sqrt(jnp.maximum(2.0 * e_cap * inv_cap, 0.0))
    # Avoid divide-by-zero by guarding v_cap_local; safe value when have_cap is False.
    safe_v = jnp.where(v_cap_local > 0.0, v_cap_local, 1.0)
    i_cap_val = p_cap / safe_v
    p_esr_raw = i_cap_val * i_cap_val * cap_esr
    p_net_raw = jnp.maximum(p_cap - p_esr_raw, 0.0)

    p_esr = jnp.where(have_cap, p_esr_raw, 0.0)
    p_net = jnp.where(have_cap, p_net_raw, p_cap)

    e_cap_new = jnp.maximum(e_cap + p_net * dt, 0.0)

    # Accumulators.
    p_net_sum_new = p_net_sum + p_net
    pcu_esr_sum_new = pcu_esr_sum + p_cu + p_esr
    p_drg_sum_new = p_drg_sum + p_drg
    p_brake_sum_new = p_brake_sum + t_brake * jnp.abs(w_carrier_new)

    # Baseline bike.
    t_rr_base = jnp.where(w_ring_base > 0.0, t_rr_ring, 0.0)
    w_ring_base_free = w_ring_base - (
        brake_val + t_rr_base - t_grav_ring
    ) * inv_j_wheel * dt
    w_ring_base_free = jnp.maximum(w_ring_base_free, 0.0)
    # Only integrate when free_decel AND w_ring_base > 0 (match numba).
    w_ring_base_new = jnp.where(
        free_decel & (w_ring_base > 0.0),
        w_ring_base_free,
        w_ring_base,
    )

    # Early-stop flag — once tripped, subsequent steps are masked below.
    just_stopped = free_decel & (w_ring_new <= v_min_w) & (w_carrier_new <= 0.0)
    stopped_new = stopped | just_stopped

    # Freeze everything once stopped.  motor_rpm retains the last live
    # value (numba reports whatever the last executed iteration set).
    def keep_old(new, old):
        return jnp.where(stopped, old, new)

    out_carry = (
        keep_old(w_ring_new, w_ring),
        keep_old(w_carrier_new, w_carrier),
        keep_old(i_actual_new, i_actual),
        keep_old(e_cap_new, e_cap),
        keep_old(w_ring_base_new, w_ring_base),
        jnp.where(stopped, rpm_buf, rpm_buf_new),
        keep_old(rpm_idx_new, rpm_idx),
        keep_old(rpm_prev_sub_new, rpm_prev_sub),
        keep_old(drpm_peak_neg_sub_new, drpm_peak_neg_sub),
        keep_old(iq_sum_new, iq_sum),
        keep_old(n_done_new, n_done),
        keep_old(p_net_sum_new, p_net_sum),
        keep_old(pcu_esr_sum_new, pcu_esr_sum),
        keep_old(p_brake_sum_new, p_brake_sum),
        keep_old(p_drg_sum_new, p_drg_sum),
        keep_old(delta_band_new, delta_band),
        keep_old(motor_rpm_new, motor_rpm),
        stopped_new,
    )
    return out_carry


def run_physics_batch_jax(
    n_sub, dt,
    w_ring, w_carrier, i_actual, e_cap, w_ring_base,
    i_cmd, brake_val,
    one_plus_n, gear_n, kt, eta_gear, t_drag_coeff, r_phase_15,
    foc_alpha, inv_cur_gain,
    inv_j_carrier, inv_j_wheel,
    mu_ratio, stiction_w, cap_esr, inv_cap,
    n_over_np1, rpm_scale,
    free_decel, v_min_w,
    t_rr_ring, t_grav_ring, t_pedal_ring,
    rpm_buf, rpm_idx,
    rpm_prev_sub_in,
    delta_band, k_band, c_band,
):
    """JAX equivalent of sim.physics._run_physics_batch.

    Signature matches the numba version exactly.  `stiction_w` is
    accepted but ignored (numba version also never reads it — it's
    reserved for a carrier-locking refinement that is currently
    implemented kinematically via the `w_carrier <= 0` branch).
    Returns a tuple in the same order numba does.
    """
    del stiction_w  # unused — present only for signature parity.

    # Cast scalar inputs to f64.
    dt_f = DEFAULT_FLOAT(dt)
    w_ring_f = DEFAULT_FLOAT(w_ring)
    w_carrier_f = DEFAULT_FLOAT(w_carrier)
    i_actual_f = DEFAULT_FLOAT(i_actual)
    e_cap_f = DEFAULT_FLOAT(e_cap)
    w_ring_base_f = DEFAULT_FLOAT(w_ring_base)
    i_cmd_f = DEFAULT_FLOAT(i_cmd)
    brake_val_f = DEFAULT_FLOAT(brake_val)
    rpm_prev_sub_f = DEFAULT_FLOAT(rpm_prev_sub_in)
    delta_band_f = DEFAULT_FLOAT(delta_band)

    rpm_buf_j = jnp.asarray(rpm_buf, dtype=DEFAULT_FLOAT)
    rpm_idx_j = jnp.int32(rpm_idx)
    buf_len = rpm_buf_j.shape[0]

    consts = (
        dt_f, i_cmd_f, brake_val_f,
        DEFAULT_FLOAT(one_plus_n), DEFAULT_FLOAT(gear_n), DEFAULT_FLOAT(kt),
        DEFAULT_FLOAT(eta_gear), DEFAULT_FLOAT(t_drag_coeff), DEFAULT_FLOAT(r_phase_15),
        DEFAULT_FLOAT(foc_alpha), DEFAULT_FLOAT(inv_cur_gain),
        DEFAULT_FLOAT(inv_j_carrier), DEFAULT_FLOAT(inv_j_wheel),
        DEFAULT_FLOAT(mu_ratio), DEFAULT_FLOAT(cap_esr), DEFAULT_FLOAT(inv_cap),
        DEFAULT_FLOAT(n_over_np1), DEFAULT_FLOAT(rpm_scale),
        jnp.bool_(free_decel), DEFAULT_FLOAT(v_min_w),
        DEFAULT_FLOAT(t_rr_ring), DEFAULT_FLOAT(t_grav_ring), DEFAULT_FLOAT(t_pedal_ring),
        DEFAULT_FLOAT(k_band), DEFAULT_FLOAT(c_band),
    )

    init_carry = (
        w_ring_f, w_carrier_f, i_actual_f, e_cap_f, w_ring_base_f,
        rpm_buf_j, rpm_idx_j,
        rpm_prev_sub_f,
        DEFAULT_FLOAT(0.0),           # drpm_peak_neg_sub
        DEFAULT_FLOAT(0.0),           # iq_sum
        jnp.int32(0),               # n_done
        DEFAULT_FLOAT(0.0),           # p_net_sum
        DEFAULT_FLOAT(0.0),           # pcu_esr_sum
        DEFAULT_FLOAT(0.0),           # p_brake_sum
        DEFAULT_FLOAT(0.0),           # p_drg_sum
        delta_band_f,
        DEFAULT_FLOAT(0.0),           # motor_rpm (set inside loop body)
        jnp.bool_(False),           # stopped
    )

    def body(i, carry):
        return _step(i, carry, consts, buf_len)

    final = lax.fori_loop(0, int(n_sub), body, init_carry)

    (w_ring_out, w_carrier_out, i_actual_out, e_cap_out, w_ring_base_out,
     rpm_buf_out, rpm_idx_out,
     rpm_prev_sub_out, drpm_peak_neg_sub_out, iq_sum_out, n_done_out,
     p_net_sum_out, pcu_esr_sum_out, p_brake_sum_out, p_drg_sum_out,
     delta_band_out, motor_rpm_out, stopped_out) = final

    # Window aggregates (match numba post-loop math).
    safe_n = jnp.maximum(n_done_out, 1)
    drpm_mean_out = jnp.where(
        n_done_out >= 1,
        (motor_rpm_out - rpm_prev_sub_f) / (safe_n * dt_f),
        0.0,
    )
    iq_mean_out = jnp.where(
        n_done_out >= 1,
        iq_sum_out / safe_n,
        iq_sum_out,
    )
    drpm_peak_neg_out = drpm_peak_neg_sub_out

    return (
        w_ring_out, w_carrier_out, i_actual_out, e_cap_out, w_ring_base_out,
        motor_rpm_out, rpm_idx_out, stopped_out,
        p_net_sum_out, pcu_esr_sum_out, p_brake_sum_out, p_drg_sum_out,
        drpm_mean_out, drpm_peak_neg_out, iq_mean_out, rpm_prev_sub_out,
        delta_band_out,
    )
