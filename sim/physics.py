"""sim.physics — Unified regen brake simulation engine.

Public API:
    simulate()       Run a full regen braking simulation.
    hand_to_brake()  Hand squeeze force (N) → carrier brake torque (Nm).
    brake_to_hand()  Carrier brake torque (Nm) → hand squeeze force (N).
    ff_current()     Feedforward current from RPM and gain.

Physical constants, the Numba-JIT inner loop, and the simulate() engine
all live in this single module.
"""

import math
from typing import Any, cast

import numpy as np

from config.settings import (
    FLUX_LINKAGE_WB as FLUX_LINKAGE,
    MOTOR_PHASE_RESISTANCE_OHM as R_PHASE,
    VESC_MOTOR_POLE_PAIRS as POLE_PAIRS,
    REGEN_CURRENT_MAX_A as I_MAX,
    VCAP_REGEN_TAPER_START_V as VCAP_TAPER_START,
    VCAP_REGEN_TAPER_END_V as VCAP_TAPER_END,
    CAPACITANCE_F as CAP_F,
    WHEEL_RADIUS_M as R_WHEEL,
    VESC_WATT_MAX,
)
from .regen_control import apply_regen_limits, ff_current_from_rpm, voltage_taper
from .strategy_context import StrategyContext

# ── Motor derived constants ───────────────────────────────────────────
KT           = 1.5 * POLE_PAIRS * FLUX_LINKAGE   # Nm/A
T_DRAG_COEFF = 0.0005       # Nm/(rad/s) iron loss + bearing

# ── Planetary gear ────────────────────────────────────────────────────
GEAR_N   = 5.0
ETA_GEAR = 0.95

# ── Band brake — 3D-printed PLA drum (default) ───────────────────────
MU_S       = 0.30           # static friction coefficient
MU_K       = 0.20           # kinetic friction coefficient
STICTION_W = 0.5            # rad/s — below this, carrier treated as locked

# ── Supercapacitor ────────────────────────────────────────────────────
VCAP_INIT   = 25.0          # V — initial supercap voltage
CAP_ESR     = 0.050         # Ω — supercap bank ESR (typical 20F/48V series string)

# ── Bike ──────────────────────────────────────────────────────────────
J_CARRIER = 0.05            # kg·m² (carrier + planets + motor rotor)

# ── Simulation timing defaults ────────────────────────────────────────
DT          = 0.001         # 1 ms  (was 0.2 ms — stable for J=0.05)
CTRL_PERIOD = 0.01          # 10 ms (100 Hz)
TELEM_DELAY = 0.015         # 15 ms telemetry round-trip
T_END       = 4.0           # s

# ── VESC FOC model ───────────────────────────────────────────────────
FOC_TAU            = 0.001  # s — current loop time constant (~1 kHz bandwidth)
DUTY_SAT_THRESHOLD = 0.95   # duty cycle above which VESC cannot track command
IQ_KP_DEFAULT      = 0.3    # firmware iq feedback gain

# ── Brake lever model (70 mm PLA drum, 270° wrap) ────────────────────
LEVER_MA    = 4.0
CABLE_EFF   = 0.85
R_DRUM      = 0.035           # m
WRAP_ANGLE  = 1.5 * math.pi   # 270°
BAND_FACTOR = (1.0 - math.exp(-MU_S * WRAP_ANGLE)) * R_DRUM

# ── Precomputed helpers ──────────────────────────────────────────────
_RPM_SCALE = 60.0 / (2.0 * math.pi)
_TWO_PI    = 2.0 * math.pi

# ── Numba JIT ────────────────────────────────────────────────────────
try:
    from numba import njit
    HAS_NUMBA = True
except ImportError:  # pragma: no cover
    HAS_NUMBA = False
    def njit(**kwargs):
        """Identity decorator when numba is absent."""
        def _wrap(fn):
            return fn
        return _wrap


@njit(cache=True)
def _run_physics_batch(
    n_sub, dt,
    w_ring, w_carrier, i_actual, e_cap,
    i_cmd, brake_val,
    one_plus_n, gear_n, kt, eta_gear, t_drag_coeff, r_phase_15,
    foc_alpha, inv_cur_gain,
    inv_j_carrier, inv_j_wheel,
    mu_ratio, stiction_w, cap_esr, inv_cap,
    n_over_np1, rpm_scale,
    free_decel, v_min_w,
    rpm_buf, rpm_idx,
):
    """Run n_sub physics timesteps with fixed i_cmd and brake.

    All physical parameters are passed explicitly so this function
    can be JIT-compiled without module-level state.
    """
    p_net_sum = 0.0
    pcu_esr_sum = 0.0
    p_brake_sum = 0.0
    eff_brake_sum = 0.0
    brake_demand_sum = 0.0
    p_drg_sum = 0.0
    motor_rpm = 0.0
    stopped = False
    buf_len = len(rpm_buf)

    for _ in range(n_sub):
        # Planetary kinematics
        w_sun = one_plus_n * w_carrier - gear_n * w_ring
        motor_rpm = max(0.0, -w_sun) * rpm_scale

        # RPM delay buffer (circular)
        rpm_buf[rpm_idx] = motor_rpm
        rpm_idx = (rpm_idx + 1) % buf_len

        # FOC current delivery (first-order lag)
        i_target = i_cmd * inv_cur_gain
        i_actual = i_actual + foc_alpha * (i_target - i_actual)
        if i_actual < 0.0:
            i_actual = 0.0

        # Electromagnetic torque
        t_em = kt * i_actual if w_sun < 0.0 else 0.0
        t_drag = t_drag_coeff * abs(w_sun)

        # Gear torques
        t_em_car = one_plus_n * t_em * eta_gear
        t_em_ring = gear_n * t_em * eta_gear

        # Band brake friction
        if abs(w_carrier) < stiction_w:
            t_brake = brake_val
        else:
            t_brake = brake_val * mu_ratio

        # Carrier dynamics
        net = t_em_car - t_brake
        if w_carrier <= 0.0:
            w_carrier = 0.0
            if net > 0.0:
                w_carrier = w_carrier + net * inv_j_carrier * dt
        else:
            w_carrier = w_carrier + net * inv_j_carrier * dt
            if w_carrier < 0.0:
                w_carrier = 0.0

        # Wheel dynamics
        if free_decel:
            w_ring = w_ring - (t_em_ring + gear_n * t_drag * eta_gear) * inv_j_wheel * dt
            if w_ring < 0.0:
                w_ring = 0.0

        # Power accounting
        abs_w_sun = abs(w_sun)
        p_mot = t_em * abs_w_sun if w_sun <= 0.0 else 0.0
        p_cu = r_phase_15 * i_actual * i_actual
        p_drg = t_drag * abs_w_sun
        p_cap = p_mot * eta_gear - p_cu - p_drg
        if p_cap < 0.0:
            p_cap = 0.0

        # Cap ESR loss
        if e_cap > 0.5 and p_cap > 0.0:
            v_cap_local = (2.0 * e_cap * inv_cap) ** 0.5
            i_cap_val = p_cap / v_cap_local
            p_esr = i_cap_val * i_cap_val * cap_esr
            p_net = p_cap - p_esr
            if p_net < 0.0:
                p_net = 0.0
        else:
            p_esr = 0.0
            p_net = p_cap

        e_cap = e_cap + p_net * dt
        if e_cap < 0.0:
            e_cap = 0.0

        # Accumulate for logging
        p_net_sum += p_net
        pcu_esr_sum += p_cu + p_esr
        p_drg_sum += p_drg
        p_brake_sum += t_brake * w_carrier
        motor_ring = t_em_ring + gear_n * t_drag * eta_gear
        brake_lim = n_over_np1 * t_brake
        eff_brake_sum += min(motor_ring, brake_lim)
        brake_demand_sum += brake_lim

        # Early exit
        if free_decel and w_ring <= v_min_w and w_carrier <= 0.0:
            stopped = True
            break

    return (w_ring, w_carrier, i_actual, e_cap,
            motor_rpm, rpm_idx, stopped,
            p_net_sum, pcu_esr_sum, p_brake_sum,
            eff_brake_sum, brake_demand_sum, p_drg_sum)


# =====================================================================
#  Utility functions
# =====================================================================

def hand_to_brake(force_n):
    """Hand squeeze force (N) → carrier brake torque (Nm)."""
    return np.asarray(force_n, dtype=float) * LEVER_MA * CABLE_EFF * BAND_FACTOR


def brake_to_hand(torque_nm):
    """Carrier brake torque (Nm) → hand squeeze force (N)."""
    return np.asarray(torque_nm, dtype=float) / (LEVER_MA * CABLE_EFF * BAND_FACTOR)


def ff_current(rpm, k):
    """Feedforward current: I = k × λ × ωe / R, clamped to [0, I_MAX]."""
    return ff_current_from_rpm(
        rpm,
        k,
        flux_linkage=FLUX_LINKAGE,
        phase_resistance=R_PHASE,
        pole_pairs=POLE_PAIRS,
        current_limit=I_MAX,
    )


# =====================================================================
#  Main simulate() — outer control loop + inner physics batch
# =====================================================================

def simulate(controller, brake, *, v0_kmh=15.0, mass_kg=100.0,
             t_end=T_END, constant_speed=False,
             dt=DT, mu_s=MU_S, mu_k=MU_K,
             eta_gear=None, j_carrier_override=None,
             t_drag_coeff=None, r_phase_override=None,
             flux_linkage_override=None,
             cap_esr=None, foc_tau=None,
             iq_kp=IQ_KP_DEFAULT,
             v_min_kmh=0.0,
             vesc_current_gain=1.0, vesc_voltage_gain=1.0,
             telem_delay=None):
    """Run the full physics simulation.

    Args:
        controller: Strategy object with .update(ctx: StrategyContext) → A,
                    or float k (feedforward gain).
        brake:      Constant brake torque (Nm) as float/int,
                    or callable(t) → Nm for time-varying brake.
        v0_kmh:     Initial wheel speed (km/h).
        mass_kg:    Total bike + rider mass (kg).
        t_end:      Simulation duration (s).
        constant_speed: If True, pin wheel speed (bench/hill test).
        dt:         Integration timestep (s).
        iq_kp:      iq feedback proportional gain (default 0.3, 0 disables).
        v_min_kmh:  Stop sim when wheel speed drops below this (km/h).
        vesc_current_gain: VESC current sensor gain error (1.0 = perfect).
        vesc_voltage_gain: VESC voltage sensor gain error (1.0 = perfect).
        telem_delay: Telemetry round-trip delay (s).  Default TELEM_DELAY.

    Returns:
        dict of CTRL_PERIOD-sampled (10 ms) time-series arrays:
            t, speed, motor_rpm, current, carrier_rpm, vcap,
            p_elec, p_copper, p_brake, eta, eff_brake,
            brake_demand, locked
    """
    # ── Resolve overrides ────────────────────────────────────────────
    params = _resolve_params(
        eta_gear, j_carrier_override, t_drag_coeff,
        r_phase_override, flux_linkage_override,
        cap_esr, foc_tau,
    )

    # ── Initialise state ─────────────────────────────────────────────
    state = _init_state(v0_kmh, mass_kg, dt, mu_s, mu_k, params,
                        vesc_current_gain, vesc_voltage_gain,
                        constant_speed, v_min_kmh, iq_kp,
                        telem_delay=telem_delay)

    # ── Controller dispatch ──────────────────────────────────────────
    use_strategy = hasattr(controller, 'update')
    k_fixed = 0.0 if use_strategy else float(controller)

    # ── Brake dispatch ───────────────────────────────────────────────
    brake_callable = callable(brake)
    brake_const = 0.0 if brake_callable else float(brake)

    # ── Log arrays ───────────────────────────────────────────────────
    n_ticks = int(t_end / CTRL_PERIOD) + 1
    logs = _make_log_arrays(n_ticks)
    ix = 0

    # ── Main control loop (100 Hz) ──────────────────────────────────
    for tick in range(n_ticks):
        t = tick * CTRL_PERIOD

        # ── Sense ────────────────────────────────────────────────────
        rpm_buf = cast(Any, state['rpm_buf'])
        rpm_idx = int(state['rpm_idx'])
        delayed_rpm = float(rpm_buf[rpm_idx])
        v_cap = math.sqrt(2.0 * state['e_cap'] * state['inv_cap']) if state['e_cap'] > 0.0 else 0.0
        v_cap_sensed = v_cap * state['vcap_gain']

        # ── Compute current command ──────────────────────────────────
        i_cmd = _compute_current_command(
            use_strategy, controller, k_fixed,
            delayed_rpm, v_cap_sensed, v_cap,
            state['i_actual'], state['iq_kp'],
            vesc_current_gain, params['flux'], params['watt_max'],
        )

        # ── Brake for this tick ──────────────────────────────────────
        brake_val = brake(t) if brake_callable else brake_const

        # ── Physics substeps (numba JIT) ─────────────────────────────
        (state['w_ring'], state['w_carrier'], state['i_actual'], state['e_cap'],
         motor_rpm, state['rpm_idx'], stopped,
         p_net_sum, pcu_esr_sum, p_brake_sum,
         eff_brake_sum, brake_demand_sum, p_drg_sum) = _run_physics_batch(
            state['ctrl_steps'], dt,
            state['w_ring'], state['w_carrier'], state['i_actual'], state['e_cap'],
            i_cmd, brake_val,
            state['_1pN'], GEAR_N, params['kt'], params['eta_gear'],
            params['t_drag'], params['_1p5R'],
            state['foc_alpha'], state['inv_cur_gain'],
            state['inv_j_carrier'], state['inv_j_wheel'],
            state['mu_ratio'], STICTION_W, params['cap_esr'], state['inv_cap'],
            state['_NoverNp1'], _RPM_SCALE,
            state['free_decel'], state['v_min_w'],
            state['rpm_buf'], state['rpm_idx'],
        )

        # ── Log ──────────────────────────────────────────────────────
        v_cap = math.sqrt(2.0 * state['e_cap'] * state['inv_cap']) if state['e_cap'] > 0.0 else 0.0
        if ix < n_ticks:
            _record_log(logs, ix, t, state, motor_rpm, v_cap,
                        p_net_sum, pcu_esr_sum, p_brake_sum,
                        p_drg_sum, eff_brake_sum, brake_demand_sum,
                        state['inv_sub'])
            ix += 1

        if stopped:
            break
        if state['free_decel'] and state['w_ring'] * state['spd_scale'] < v_min_kmh and state['w_carrier'] <= 0.0:
            break

    s = slice(0, ix)
    return {k: v[s] for k, v in logs.items()}


# =====================================================================
#  Private helpers — each handles one concern
# =====================================================================

def _resolve_params(eta_gear, j_carrier_override, t_drag_coeff,
                    r_phase_override, flux_linkage_override,
                    cap_esr, foc_tau):
    """Resolve parameter overrides, falling back to module defaults."""
    _eta_gear = eta_gear if eta_gear is not None else ETA_GEAR
    _j_carrier = j_carrier_override if j_carrier_override is not None else J_CARRIER
    _t_drag = t_drag_coeff if t_drag_coeff is not None else T_DRAG_COEFF
    _r_phase = r_phase_override if r_phase_override is not None else R_PHASE
    _flux = flux_linkage_override if flux_linkage_override is not None else FLUX_LINKAGE
    _cap_esr = cap_esr if cap_esr is not None else CAP_ESR
    _foc_tau = foc_tau if foc_tau is not None else FOC_TAU
    _kt = 1.5 * POLE_PAIRS * _flux
    return dict(
        eta_gear=_eta_gear, j_carrier=_j_carrier, t_drag=_t_drag,
        r_phase=_r_phase, flux=_flux, cap_esr=_cap_esr, foc_tau=_foc_tau,
        kt=_kt, _1p5R=1.5 * _r_phase, watt_max=VESC_WATT_MAX,
    )


def _init_state(v0_kmh, mass_kg, dt, mu_s, mu_k, params,
                vesc_current_gain, vesc_voltage_gain,
                constant_speed, v_min_kmh, iq_kp,
                telem_delay=None):
    """Build the mutable simulation state dict."""
    N = GEAR_N
    _1pN = 1.0 + N

    w_ring = (v0_kmh / 3.6) / R_WHEEL
    w_carrier = N / _1pN * w_ring

    ctrl_steps = max(1, int(CTRL_PERIOD / dt))
    _telem_delay = telem_delay if telem_delay is not None else TELEM_DELAY
    delay_slots = max(1, int(_telem_delay / dt))

    return dict(
        w_ring=w_ring,
        w_carrier=w_carrier,
        i_actual=0.0,
        e_cap=0.5 * CAP_F * VCAP_INIT ** 2,
        inv_cap=1.0 / CAP_F,
        rpm_buf=np.zeros(delay_slots, dtype=np.float64),
        rpm_idx=0,
        ctrl_steps=ctrl_steps,
        _1pN=_1pN,
        _NoverNp1=N / _1pN,
        foc_alpha=dt / (params['foc_tau'] + dt),
        inv_cur_gain=1.0 / vesc_current_gain,
        vcap_gain=vesc_voltage_gain,
        inv_j_carrier=1.0 / params['j_carrier'],
        inv_j_wheel=1.0 / (mass_kg * R_WHEEL ** 2),
        mu_ratio=mu_k / mu_s,
        spd_scale=R_WHEEL * 3.6,
        free_decel=not constant_speed,
        v_min_w=(v_min_kmh / 3.6) / R_WHEEL if v_min_kmh > 0.0 else 0.0,
        inv_sub=1.0 / ctrl_steps,
        iq_kp=iq_kp,
    )


def _make_log_arrays(n_ticks):
    """Allocate output arrays for one sample per control tick."""
    return dict(
        t=np.empty(n_ticks),
        speed=np.empty(n_ticks),
        motor_rpm=np.empty(n_ticks),
        current=np.empty(n_ticks),
        carrier_rpm=np.empty(n_ticks),
        vcap=np.empty(n_ticks),
        p_elec=np.empty(n_ticks),
        p_copper=np.empty(n_ticks),
        p_brake=np.empty(n_ticks),
        eta=np.empty(n_ticks),
        eff_brake=np.empty(n_ticks),
        brake_demand=np.empty(n_ticks),
        locked=np.empty(n_ticks, dtype=bool),
    )


def _compute_current_command(use_strategy, controller, k_fixed,
                             delayed_rpm, v_cap_sensed, v_cap,
                             i_actual, iq_kp,
                             vesc_current_gain, flux, watt_max):
    """Compute the current command for this control tick.

    Handles strategy dispatch, fixed-gain fallback, iq feedback,
    bus power limiting, duty saturation, and clamping.
    """
    # ── Strategy or fixed-gain ───────────────────────────────────────
    if use_strategy:
        iq_for_strategy = i_actual * vesc_current_gain
        # Estimate duty from back-EMF and bus voltage (same info VESC reports)
        omega_e_est = delayed_rpm * POLE_PAIRS * _TWO_PI / 60.0
        bemf_est = flux * omega_e_est
        duty_est = bemf_est / v_cap if v_cap > 1.0 else 0.0
        # Sim has no separate transport latency for the LispBM push path, so the
        # preferred low-latency signals are explicit aliases of the averaged ones.
        ctx = StrategyContext(
            rpm=delayed_rpm,
            vcap=v_cap_sensed,
            dt_ctrl=CTRL_PERIOD,
            iq_actual=iq_for_strategy,
            duty_cycle=duty_est,
            input_current=0.0,  # not modelled in sim inner loop
            rpm_fast=delayed_rpm,
            iq_instantaneous=iq_for_strategy,
        )
        i_cmd = controller.update(ctx)
    else:
        taper = voltage_taper(v_cap_sensed, VCAP_TAPER_START, VCAP_TAPER_END)
        i_cmd = ff_current(delayed_rpm, k_fixed) * taper

    # ── iq feedback (matches firmware control_loop.py Kp correction) ─
    if iq_kp > 0.0:
        iq_reported = i_actual * vesc_current_gain
        i_cmd = i_cmd + iq_kp * (i_cmd - iq_reported)

    # ── Bus power limiter ────────────────────────────────────────────
    omega_e_ctrl = delayed_rpm * POLE_PAIRS * _TWO_PI / 60.0
    back_emf = flux * omega_e_ctrl
    p_bus_est = None
    if i_cmd > 0.0 and back_emf > 0.0:
        p_bus_est = i_cmd * back_emf

    # ── Duty saturation ──────────────────────────────────────────────
    duty_cycle = None
    if v_cap > 1.0 and back_emf > 0.0:
        duty_cycle = back_emf / v_cap

    i_cmd = apply_regen_limits(
        i_cmd,
        current_limit=I_MAX,
        power_w=p_bus_est,
        power_limit_w=watt_max,
        duty_cycle=duty_cycle,
        duty_limit=DUTY_SAT_THRESHOLD,
    )

    return i_cmd


def _record_log(logs, ix, t, state, motor_rpm, v_cap,
                p_net_sum, pcu_esr_sum, p_brake_sum,
                p_drg_sum, eff_brake_sum, brake_demand_sum,
                inv_sub):
    """Write one sample into the log arrays."""
    logs['t'][ix] = t
    logs['speed'][ix] = state['w_ring'] * state['spd_scale']
    logs['motor_rpm'][ix] = motor_rpm
    logs['current'][ix] = state['i_actual']
    logs['carrier_rpm'][ix] = state['w_carrier'] * _RPM_SCALE
    logs['vcap'][ix] = v_cap
    logs['p_elec'][ix] = p_net_sum * inv_sub
    logs['p_copper'][ix] = pcu_esr_sum * inv_sub
    logs['p_brake'][ix] = p_brake_sum * inv_sub
    logs['eff_brake'][ix] = eff_brake_sum * inv_sub
    logs['brake_demand'][ix] = brake_demand_sum * inv_sub
    logs['locked'][ix] = abs(state['w_carrier']) < STICTION_W
    denom = logs['p_elec'][ix] + logs['p_copper'][ix] + (p_drg_sum * inv_sub) + logs['p_brake'][ix]
    logs['eta'][ix] = logs['p_elec'][ix] / denom if denom > 1.0 else 0.0
