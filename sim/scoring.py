"""sim.scoring — Score a simulate() result on three dimensions + robustness.

Dimensions (0–100 each):
  energy     — device conversion efficiency:
                   ∫P_elec dt  /  ∫(P_elec + P_copper + P_drg + P_brake) dt
               i.e. what fraction of the power the *device* is
               processing ends up stored electrically.  Losses counted
               in the denominator are motor copper + cap ESR
               (``p_copper``), rotor drag (``p_drg``), and band-slip
               heat (``p_brake``).  Rolling resistance and grade are
               excluded — they are identical on the baseline bike and
               are not the device's job.
  tracking   — closeness of our wheel deceleration to a traditional
               mechanical brake at the same rider clamping force.  The
               baseline bike's pads are continuously sliding, so its
               wheel torque is ``brake_val · µ_k/µ_s`` (kinetic
               level), not ``brake_val`` (static holding).  Baseline
               is integrated in parallel by physics.simulate()
               (``speed_baseline``).
  smoothness — ride quality via RMS wheel jerk.

Weighted scenarios model everyday riding.  Each scenario defines a
non-overlapping speed band so low-speed behaviour is never double-counted.
Emergency scenarios override dimension weights to prioritise braking.

Robustness analysis via Monte Carlo perturbation of physical constants.
"""

import math

import numpy as np

from .physics import (
    DT, R_WHEEL, GEAR_N,
    MU_S, MU_K, ETA_GEAR, J_CARRIER, T_DRAG_COEFF,
    R_PHASE, FLUX_LINKAGE, CAP_ESR, FOC_TAU,
    TELEM_DELAY,
)

# ── Tracking low-speed cutoff ────────────────────────────────────────
# Below this speed (km/h), back-EMF is too low for meaningful regen.
# Tracking error at these speeds is not penalised.
TRACKING_CUTOFF_KMH = 5.0

# ── Jerk reference (m/s³) — e-bike braking context ──────────────────
# A tight reference for controller-induced jerk.  Real pavement jerk
# from bumps alone is 10-50 m/s³, so this metric does not map to
# rider-perceived comfort — it's a stability indicator: a tune that
# scores < 70 here is almost certainly ringing or dithering at a
# frequency that will be audible/felt.
J_REF = 10.0

# ── Energy anti-pathology penalty tuning ─────────────────────────────
# Heavily penalise periods where commanded braking is active but the
# device's net conversion is negative (capture < summed device losses).
#
# Net conversion power proxy:
#   p_net_device = p_elec - (p_copper + p_drg + p_brake)
# Negative net periods are penalised proportionally to both duration and
# magnitude relative to demanded brake power.
NEG_EFFICIENCY_SPEED_CUTOFF_KMH = TRACKING_CUTOFF_KMH
NEG_EFFICIENCY_BRAKE_POWER_MIN_W = 20.0
NEG_EFFICIENCY_PENALTY_GAIN = 220.0

# ── Normal dimension weights ─────────────────────────────────────────
W_ENERGY_NORMAL     = 0.40
W_TRACKING_NORMAL   = 0.40
W_SMOOTHNESS_NORMAL = 0.20

# ── Emergency dimension weights (braking > energy) ───────────────────
W_ENERGY_EMERG     = 0.00
W_TRACKING_EMERG   = 0.80
W_SMOOTHNESS_EMERG = 0.20

# ── System mass distribution (rider + bike, kg) ─────────────────────
# Triangular-ish: most riders 80–100 kg total, tails at 70 and 120.
# Weights sum to 1.0.
MASS_DISTRIBUTION = [
    # (mass_kg, weight)
    ( 70, 0.10),   # light rider + light bike
    ( 80, 0.20),
    ( 90, 0.35),   # most common
    (100, 0.20),
    (110, 0.10),
    (120, 0.05),   # heavy rider or cargo
]

# Coarser 3-point mass grid for screening (DE exploration phase).
# Same total weight = 1.0;  extremes + mode.
SCREEN_MASSES = [
    ( 70, 0.25),
    ( 90, 0.50),
    (120, 0.25),
]

# ── Scenario table ───────────────────────────────────────────────────
# Each tuple: (name, v_start_kmh, v_end_kmh, decel_ms2,
#              scenario_weight, is_emergency, kind)
#
# v_start: initial speed for simulate().
# v_end:   scoring ignores timesteps once speed drops below this.
# decel_ms2: desired deceleration (m/s²).  Converted to band-brake
#            torque at runtime via  τ = a · m · R_wheel · (1+N)/N,
#            so heavier riders squeeze harder for the same decel.
# kind:    "ramp" — constant brake from t=0 (standard deceleration test).
#          "step" — brake=0 until STEP_DELAY, then jumps to constant.
#                   Stresses controller transient response to a step
#                   demand (catches ringing, overshoot, slow rise).
#          "cycle" — continuous run with repeated brake on/off windows.
#                   Models real riding where braking is applied in
#                   bursts without resetting initial conditions.
#          "hold" — constant_speed=True, no KE to dissipate.
#                   Simulates holding speed on a descent where gravity
#                   supplies energy at `mass·g·sin(θ)·v`; decel_ms2 is
#                   the effective grade accel (e.g. 0.5 m/s² ≈ 5% grade).
#                   Scores steady-state energy capture.
# Mass is sampled from MASS_DISTRIBUTION for each scenario.
# Scenario weights sum to 1.0.
#
# Motor max decel: ~1.17 m/s² @ 90 kg (40 A limit).
# Classifications are by motor effort, not rider-perceived intensity.
#   light     = 10-25% of motor capacity
#   medium    = 30-50%
#   heavy     = 55-75%  (heavy riders may see carrier slip)
#   saturated = 80-100%+ (at/beyond motor limit, scored on tracking only)

# Delay before brake step in "step" kind scenarios (s).
STEP_DELAY   = 0.3
# Duration of "hold" kind scenarios (s) — long enough for controller
# transients to settle and steady-state regen to dominate the score.
HOLD_T_END   = 3.0

# Brake lever engagement ramp duration (s).
# Real lever engagement rises from 0 to full clamping force over
# ~80–200 ms (cable compliance, band lead-in, rider dynamics).
# Modelled as a linear ramp applied to all non-hold engagement edges.
# 0.0 disables the ramp (legacy step-function behaviour).
BRAKE_ENGAGE_RAMP_S = 0.10
CYCLE_T_END = 9.0
CYCLE_INITIAL_COAST_S = 0.6
CYCLE_PERIOD_S = 2.0
CYCLE_BRAKE_ON_S = 1.1

# Hill scenario parameters for real-world style scoring.
HILL_ACCEL_MS2 = 0.65     # ~6.6% equivalent downhill grade
HILL_STEP_T_END = 7.0
HILL_CYCLE_T_END = 11.0
HILL_CYCLE_INITIAL_COAST_S = 0.7
HILL_CYCLE_PERIOD_S = 2.4
HILL_CYCLE_BRAKE_ON_S = 1.35

SCENARIOS = [
    # Low speed — motor RPM is low
    ("low_light",        10,  5, 0.15, 0.03, False, "cycle"),
    ("low_medium",       10,  3, 0.40, 0.03, False, "cycle"),
    # City speed — bulk of riding
    ("city_light",       20, 15, 0.20, 0.12, False, "cycle"),
    ("city_medium",      20, 10, 0.40, 0.16, False, "cycle"),
    ("city_heavy",       20,  5, 0.70, 0.08, False, "cycle"),
    # Fast — motor in good operating range
    ("fast_light",       30, 25, 0.25, 0.07, False, "cycle"),
    ("fast_medium",      30, 15, 0.50, 0.11, False, "cycle"),
    ("fast_heavy",       30, 10, 0.80, 0.08, False, "cycle"),
    # Saturated — at/beyond motor limit, tracking-only scoring
    ("saturated_fast",   30,  5, 1.00, 0.04, True,  "ramp"),
    ("saturated_high",   40, 10, 1.10, 0.03, True,  "ramp"),
    # Step demand — rider cruises, then clamps brake fully in one motion
    ("city_step",        25, 10, 0.60, 0.08, False, "step"),
    # Hill hold — long descent at constant speed (~5% grade @ 25 km/h)
    ("hill_hold",        25, 25, 0.50, 0.06, False, "hold"),
    # Downhill acceleration before brake clamp (real descent entry).
    ("hill_accel_step",  22, 10, 0.65, 0.06, False, "hill_step"),
    # Consecutive continuous downhill runs (brake on/off cycles).
    ("hill_cont_runs",   24, 10, 0.60, 0.05, False, "hill_cycle"),
]

# Screening set for DE phase. Focus on dominant continuous-run scenarios,
# plus one step transient and one downhill continuous case.
SCREEN_SCENARIOS = [
    s for s in SCENARIOS
    if s[0] in (
        "city_medium", "city_light", "fast_medium",
        "city_heavy", "fast_heavy", "city_step", "hill_cont_runs"
    )
]


# =====================================================================
#  Helpers
# =====================================================================

def _clamp01(x):
    """Clamp a value to [0, 1]."""
    return max(0.0, min(1.0, float(x)))


def decel_to_brake(decel_ms2, mass_kg):
    """Desired deceleration (m/s²) + mass (kg) → band-brake torque (Nm).

    Derivation (carrier-locked, perfect tracking):
        ring_torque = N/(1+N) · τ_brake
        F_wheel     = ring_torque / R_wheel
        a           = F_wheel / m
      ⇒ τ_brake     = a · m · R_wheel · (1+N) / N
    """
    return decel_ms2 * mass_kg * R_WHEEL * (1.0 + GEAR_N) / GEAR_N


def _crop_to_speed_band(result, v_end_kmh):
    """Return a view of *result* cropped to timesteps where speed >= v_end.

    The sim starts at v_start by definition (first sample).  We cut off
    once speed drops below v_end so that lower-speed behaviour isn't
    scored here.

    Returns a new dict with the same keys, arrays sliced to [0:n].
    """
    speed = result['speed']
    # Find last index where speed is still at or above v_end
    above = np.where(speed >= v_end_kmh)[0]
    if len(above) == 0:
        n = 1                           # keep at least one sample
    else:
        n = int(above[-1]) + 1

    return {k: (v[:n] if isinstance(v, np.ndarray) else v)
            for k, v in result.items()}


def _dt_from_result(result):
    """Infer timestep from result's time array."""
    if len(result['t']) > 1:
        return float(result['t'][1] - result['t'][0])
    return DT


# =====================================================================
#  Per-dimension scorers (operate on already-cropped result)
# =====================================================================

def energy_score(result, mass_kg):
    """Device conversion efficiency (0–100).

    ``score = 100 * ∫P_elec dt / ∫(P_elec + P_copper + P_drg + P_brake) dt``

    where the denominator is the total power the device is processing:
    electrical capture + motor copper + cap ESR (grouped in ``p_copper``)
    + rotor drag + band-slip heat.  Rolling resistance and grade are
    excluded because they are identical on a plain bike with a
    traditional brake and do not represent device-side losses.

    When the denominator is effectively zero (coast without braking
    demand), returns 0.
    """
    dt = _dt_from_result(result)
    p_elec = np.asarray(result['p_elec'], dtype=float)
    p_copper = np.asarray(result['p_copper'], dtype=float)
    p_brake = np.asarray(result['p_brake'], dtype=float)
    speed_kmh = np.asarray(result['speed'], dtype=float)
    brake_demand = np.asarray(result['brake_demand'], dtype=float)
    # p_drg is not logged directly; recover from eta denominator.
    # eta = p_elec / (p_elec + p_copper + p_drg + p_brake), so
    # p_drg = p_elec * (1/eta - 1) - p_copper - p_brake, but easier
    # to just have physics log p_drg.  We reconstruct it instead from
    # eta since we don't want to churn the log schema further.
    eta = np.asarray(result['eta'], dtype=float)
    device_power = np.zeros_like(p_elec)
    valid = eta > 0.0
    device_power[valid] = p_elec[valid] / eta[valid]
    # For samples where eta is 0 (no captured power), denominator is
    # still copper + brake + drag + 0; use p_copper + p_brake as lower
    # bound (p_drg unobserved without eta).  These samples contribute
    # nothing to numerator anyway, so underestimating denominator is
    # conservative (inflates score slightly).  Acceptable trade-off to
    # avoid widening the physics log schema.
    device_power[~valid] = p_copper[~valid] + p_brake[~valid]

    e_num = float(np.sum(p_elec) * dt)
    e_den = float(np.sum(device_power) * dt)
    if e_den <= 1e-6:
        return 0.0

    # Heavy penalty for "negative-efficiency" braking windows:
    # active brake demand + meaningful speed where device losses exceed
    # electrical capture (net conversion negative).
    speed_ms = speed_kmh / 3.6
    demand_power = brake_demand * speed_ms / R_WHEEL
    active = (
        (speed_kmh >= NEG_EFFICIENCY_SPEED_CUTOFF_KMH)
        & (demand_power >= NEG_EFFICIENCY_BRAKE_POWER_MIN_W)
    )
    p_losses = np.maximum(device_power - p_elec, 0.0)
    neg_net = np.maximum(p_losses - p_elec, 0.0)
    d_den = float(np.sum(demand_power[active]) * dt)
    if d_den > 1e-9:
        neg_ratio = float(np.sum(neg_net[active]) * dt) / d_den
    else:
        neg_ratio = 0.0

    base = _clamp01(e_num / e_den) * 100.0
    penalty = min(100.0, NEG_EFFICIENCY_PENALTY_GAIN * max(0.0, neg_ratio))
    return max(0.0, base - penalty)


def tracking_score(result):
    """Closeness of our wheel deceleration to the rider's demand (0–100).

    ``speed_baseline`` is a parallel integration performed by
    physics.simulate() of an *idealized non-slipping brake*: the rider's
    clamping force applied as a wheel torque at the full static-friction
    level (``brake_val``), with the same mass, C_rr, and grade.  This is
    what the rider asked for before any pad slip, and it is exactly the
    µ_s boundary the regen controller tries to target (push carrier just
    to the verge of slipping).  A real mechanical brake would deliver
    only ``brake_val · µ_k/µ_s`` once the pads start sliding — the regen
    aims higher because it can.

    Metric:
        a_ours = -Δspeed_ours / Δt        (m/s²)
        a_base = -Δspeed_base / Δt
        score  = clamp01(1 - mean(|a_ours - a_base|) / mean(|a_base|)) * 100

    Symmetric in under/over-delivery: both braking too weakly and too
    hard ding the score.  When the baseline barely decelerates (e.g.
    constant_speed hill-hold scenario), tracking is defined to be 100
    (neither bike is moving relative to the other).
    """
    dt = _dt_from_result(result)
    speed = np.asarray(result['speed'], dtype=float) / 3.6
    speed_base = np.asarray(result['speed_baseline'], dtype=float) / 3.6
    if len(speed) < 2 or dt <= 0.0:
        return 100.0
    a_ours = -np.diff(speed) / dt
    a_base = -np.diff(speed_base) / dt
    mean_base = float(np.mean(np.abs(a_base)))
    if mean_base < 0.05:   # < 0.05 m/s² baseline decel → no real braking
        return 100.0
    err = float(np.mean(np.abs(a_ours - a_base)))
    return _clamp01(1.0 - err / mean_base) * 100.0


def smoothness_score(result):
    """Ride smoothness via RMS wheel jerk (m/s³).

    Returns 100 at zero jerk and 0 once RMS jerk reaches J_REF.
    """
    speed_ms = result['speed'] / 3.6
    if len(speed_ms) < 3:
        return 100.0

    dt = _dt_from_result(result)
    jerk = np.diff(np.diff(speed_ms) / dt) / dt
    j_rms = float(np.sqrt(np.mean(jerk ** 2)))
    return _clamp01(1.0 - j_rms / J_REF) * 100.0


# =====================================================================
#  Single-scenario composite
# =====================================================================

def score(result, mass_kg, *, emergency=False):
    """Score one sim result (already cropped).

    Args:
        result:    dict from simulate().
        mass_kg:   bike + rider mass (kg).
        emergency: if True, use emergency dimension weights.

    Returns:
        dict with keys: energy, tracking, smoothness, composite.
    """
    e = energy_score(result, mass_kg)
    t = tracking_score(result)
    s = smoothness_score(result)

    if emergency:
        composite = (W_ENERGY_EMERG * e + W_TRACKING_EMERG * t
                     + W_SMOOTHNESS_EMERG * s)
    else:
        composite = (W_ENERGY_NORMAL * e + W_TRACKING_NORMAL * t
                     + W_SMOOTHNESS_NORMAL * s)

    return dict(energy=e, tracking=t, smoothness=s, composite=composite)


# =====================================================================
#  Step-level scoring (for training signals)
# =====================================================================

def step_score_series(result, *, emergency=False, eps=1e-9):
    """Build per-timestep reward components aligned to existing dimensions.

    This is designed for training-time signals where a dense step reward is
    needed. It preserves the same high-level intent as score():
      - energy: reward electrical capture relative to demanded braking power
      - tracking: reward delivered-vs-demanded braking torque
      - smoothness: penalize high jerk

    Returns:
        dict with per-step numpy arrays:
            energy_step, tracking_step, smoothness_step, composite_step,
            demand_power_w, valid_tracking
    """
    speed_ms = np.asarray(result['speed'], dtype=float) / 3.6
    speed_base_ms = np.asarray(result['speed_baseline'], dtype=float) / 3.6
    brake_demand = np.asarray(result['brake_demand'], dtype=float)
    p_elec = np.asarray(result['p_elec'], dtype=float)
    p_copper = np.asarray(result['p_copper'], dtype=float)
    p_brake_arr = np.asarray(result['p_brake'], dtype=float)
    eta_arr = np.asarray(result['eta'], dtype=float)

    n = len(speed_ms)
    if n == 0:
        return dict(
            energy_step=np.array([], dtype=float),
            tracking_step=np.array([], dtype=float),
            smoothness_step=np.array([], dtype=float),
            composite_step=np.array([], dtype=float),
            demand_power_w=np.array([], dtype=float),
            valid_tracking=np.array([], dtype=bool),
        )

    # Instantaneous demanded braking power (W) — for interface compat.
    demand_power_w = brake_demand * speed_ms / R_WHEEL

    # Energy component: local device efficiency.
    energy_step = np.zeros(n, dtype=float)
    # Reconstruct device-power denominator from eta (same trick as energy_score).
    device_power = np.zeros(n, dtype=float)
    valid_eta = eta_arr > 0.0
    device_power[valid_eta] = p_elec[valid_eta] / eta_arr[valid_eta]
    device_power[~valid_eta] = p_copper[~valid_eta] + p_brake_arr[~valid_eta]
    mask_power = device_power > eps
    energy_step[mask_power] = np.clip(
        p_elec[mask_power] / (device_power[mask_power] + eps),
        0.0,
        1.0,
    ) * 100.0

    # Tracking component: per-tick decel error vs baseline bike.
    tracking_step = np.full(n, 100.0, dtype=float)
    dt = _dt_from_result(result)
    if n >= 2 and dt > 0.0:
        a_ours = -np.diff(speed_ms) / dt
        a_base = -np.diff(speed_base_ms) / dt
        mean_base = float(np.mean(np.abs(a_base)))
        if mean_base >= 0.05:
            ratio = np.clip(1.0 - np.abs(a_ours - a_base) / (mean_base + eps), 0.0, 1.0)
            tracking_step[1:] = ratio * 100.0
            tracking_step[0] = tracking_step[1]
    valid_tracking = np.asarray(result['speed'], dtype=float) >= TRACKING_CUTOFF_KMH

    # Smoothness component from local jerk estimate.
    smoothness_step = np.full(n, 100.0, dtype=float)
    dt = _dt_from_result(result)
    if n >= 3 and dt > 0.0:
        accel = np.diff(speed_ms) / dt
        jerk = np.diff(accel) / dt
        # Map jerk sample k (between accel[k] and accel[k+1]) to timestep k+1.
        for k, j in enumerate(jerk):
            idx = k + 1
            smoothness_step[idx] = _clamp01(1.0 - abs(float(j)) / J_REF) * 100.0
        smoothness_step[0] = smoothness_step[1]
        smoothness_step[-1] = smoothness_step[-2]

    if emergency:
        composite_step = (
            W_ENERGY_EMERG * energy_step
            + W_TRACKING_EMERG * tracking_step
            + W_SMOOTHNESS_EMERG * smoothness_step
        )
    else:
        composite_step = (
            W_ENERGY_NORMAL * energy_step
            + W_TRACKING_NORMAL * tracking_step
            + W_SMOOTHNESS_NORMAL * smoothness_step
        )

    return dict(
        energy_step=energy_step,
        tracking_step=tracking_step,
        smoothness_step=smoothness_step,
        composite_step=composite_step,
        demand_power_w=demand_power_w,
        valid_tracking=valid_tracking,
    )


def score_from_step_series(result, *, emergency=False, eps=1e-9):
    """Aggregate step_score_series into a scenario summary.

    This provides a step-level compatible aggregate that remains close to the
    existing score() semantics while exposing dense signals for learning.
    """
    ss = step_score_series(result, emergency=emergency, eps=eps)
    if len(ss['composite_step']) == 0:
        return dict(energy=0.0, tracking=0.0, smoothness=0.0, composite=0.0)

    # Demand-weighted aggregation for energy/tracking; mean for smoothness.
    pw = ss['demand_power_w']
    pw_sum = float(np.sum(pw))
    if pw_sum > eps:
        e = float(np.sum(ss['energy_step'] * pw) / pw_sum)
    else:
        e = 0.0

    tr_mask = ss['valid_tracking']
    tr_denom = float(np.sum(pw[tr_mask]))
    if tr_denom > eps:
        t = float(np.sum(ss['tracking_step'][tr_mask] * pw[tr_mask]) / tr_denom)
    else:
        t = 0.0

    s = float(np.mean(ss['smoothness_step']))

    if emergency:
        c = (W_ENERGY_EMERG * e + W_TRACKING_EMERG * t + W_SMOOTHNESS_EMERG * s)
    else:
        c = (W_ENERGY_NORMAL * e + W_TRACKING_NORMAL * t + W_SMOOTHNESS_NORMAL * s)

    return dict(energy=e, tracking=t, smoothness=s, composite=c)


# =====================================================================
#  Full strategy scoring across all scenarios
# =====================================================================

def _lerp_ramp(t_engage, t_ramp, full_nm):
    """Return a brake value linearly ramping from 0 to full_nm over t_ramp seconds.

    t_engage: time elapsed since engagement started (s).
    Returns full_nm if t_ramp <= 0 (no ramp).
    """
    if t_ramp <= 0.0 or t_engage >= t_ramp:
        return full_nm
    if t_engage <= 0.0:
        return 0.0
    return full_nm * (t_engage / t_ramp)


def _scenario_sim_config(kind, v_start, v_end, decel_ms2, mass_kg):
    """Build simulate() args + crop config for one scenario kind.

    Returns (brake, sim_kwargs, crop_v_end).

        brake        — float (ramp/hold) or callable(t)→Nm (step).
        sim_kwargs   — dict[str, Any] passed to simulate() (v0_kmh,
                       mass_kg, t_end, and kind-specific flags like
                       constant_speed).  Typed loosely because values
                       mix float/bool across kinds.
        crop_v_end   — km/h below which scoring samples are dropped.
                       None means "no speed cropping" (hold scenarios).
    """
    from typing import Any
    brake_nm = decel_to_brake(decel_ms2, mass_kg)
    sim_kw: dict[str, Any] = dict(
        v0_kmh=float(v_start), mass_kg=float(mass_kg),
    )

    if kind == "ramp":
        dv = (v_start - v_end) / 3.6
        sim_kw["t_end"] = max(4.0, min(30.0, dv / decel_ms2 * 2.0))
        sim_kw["v_min_kmh"] = float(v_end)
        ramp = BRAKE_ENGAGE_RAMP_S
        def brake_fn(t, _b=brake_nm, _r=ramp):
            return _lerp_ramp(t, _r, _b)
        return brake_fn, sim_kw, v_end

    if kind == "step":
        dv = (v_start - v_end) / 3.6
        sim_kw["t_end"] = max(4.0, min(30.0, dv / decel_ms2 * 2.0 + STEP_DELAY))
        sim_kw["v_min_kmh"] = float(v_end)
        delay = STEP_DELAY
        ramp = BRAKE_ENGAGE_RAMP_S
        def brake_fn(t, _b=brake_nm, _d=delay, _r=ramp):
            return _lerp_ramp(t - _d, _r, _b) if t >= _d else 0.0
        return brake_fn, sim_kw, v_end

    if kind == "cycle":
        sim_kw["t_end"] = CYCLE_T_END
        sim_kw["v_min_kmh"] = float(v_end)

        def brake_fn(
            t,
            _b=brake_nm,
            _coast=CYCLE_INITIAL_COAST_S,
            _period=CYCLE_PERIOD_S,
            _on=CYCLE_BRAKE_ON_S,
            _ramp=BRAKE_ENGAGE_RAMP_S,
        ):
            if t < _coast:
                return 0.0
            phase = (t - _coast) % _period
            if phase >= _on:
                return 0.0
            if phase < _ramp:
                return _lerp_ramp(phase, _ramp, _b)           # engage ramp
            if phase >= _on - _ramp:
                return _lerp_ramp(_on - phase, _ramp, _b)    # release ramp
            return _b

        return brake_fn, sim_kw, v_end

    if kind == "hold":
        # Real descent physics: gravity supplies forward accel of
        # decel_ms2 m/s², controller + brake demand cancels it to hold
        # speed.  Rolling resistance (in physics) provides a small bias
        # that the controller must absorb.  We use free_decel=True so
        # speed is an actual state, not pinned.
        import math as _math
        from .physics import G_ACCEL as _G
        sin_theta = max(-1.0, min(1.0, decel_ms2 / _G))
        sim_kw["t_end"] = HOLD_T_END
        sim_kw["grade_rad"] = _math.asin(sin_theta)
        return brake_nm, sim_kw, None

    if kind == "hill_step":
        import math as _math
        from .physics import G_ACCEL as _G
        sim_kw["t_end"] = HILL_STEP_T_END
        sim_kw["v_min_kmh"] = float(v_end)
        sim_kw["grade_rad"] = _math.asin(max(-1.0, min(1.0, HILL_ACCEL_MS2 / _G)))
        delay = STEP_DELAY
        ramp = BRAKE_ENGAGE_RAMP_S

        def brake_fn(t, _b=brake_nm, _d=delay, _r=ramp):
            return _lerp_ramp(t - _d, _r, _b) if t >= _d else 0.0

        return brake_fn, sim_kw, v_end

    if kind == "hill_cycle":
        import math as _math
        from .physics import G_ACCEL as _G
        sim_kw["t_end"] = HILL_CYCLE_T_END
        sim_kw["v_min_kmh"] = float(v_end)
        sim_kw["grade_rad"] = _math.asin(max(-1.0, min(1.0, HILL_ACCEL_MS2 / _G)))

        def brake_fn(
            t,
            _b=brake_nm,
            _coast=HILL_CYCLE_INITIAL_COAST_S,
            _period=HILL_CYCLE_PERIOD_S,
            _on=HILL_CYCLE_BRAKE_ON_S,
            _ramp=BRAKE_ENGAGE_RAMP_S,
        ):
            if t < _coast:
                return 0.0
            phase = (t - _coast) % _period
            if phase >= _on:
                return 0.0
            if phase < _ramp:
                return _lerp_ramp(phase, _ramp, _b)           # engage ramp
            if phase >= _on - _ramp:
                return _lerp_ramp(_on - phase, _ramp, _b)    # release ramp
            return _b

        return brake_fn, sim_kw, v_end

    raise ValueError(f"Unknown scenario kind: {kind!r}")


def _unpack_scenario(tup):
    """Accept legacy 6-tuples as well as 7-tuples with explicit kind."""
    if len(tup) == 7:
        return tup
    name, v_start, v_end, decel_ms2, weight, emerg = tup
    return name, v_start, v_end, decel_ms2, weight, emerg, "ramp"


def score_strategy(strategy_factory, scenarios=None, masses=None):
    """Run all scenarios for a strategy and return the weighted composite.

    Each scenario is run at every mass in the mass distribution.  The
    per-scenario score is the mass-weighted average, then scenarios are
    combined by their own weights.

    Args:
        strategy_factory: callable() → strategy object (no args).
            Called once per (scenario, mass) pair.
        scenarios: list of scenario tuples, default SCENARIOS.
        masses: list of (mass_kg, weight) tuples, default MASS_DISTRIBUTION.

    Returns:
        dict with keys:
            per_scenario — list of dicts, one per scenario, each with
                           name, energy, tracking, smoothness, composite,
                           weight.
            weighted     — final weighted composite (0–100).
    """
    from .physics import simulate

    if scenarios is None:
        scenarios = SCENARIOS
    if masses is None:
        masses = MASS_DISTRIBUTION

    per_scenario = []
    weighted_sum = 0.0

    for tup in scenarios:
        name, v_start, v_end, decel_ms2, weight, emerg, kind = _unpack_scenario(tup)

        # Accumulate mass-weighted dimension scores
        e_acc = t_acc = s_acc = 0.0

        for mass_kg, m_weight in masses:
            brake, sim_kw, crop_v = _scenario_sim_config(
                kind, v_start, v_end, decel_ms2, mass_kg,
            )
            controller = strategy_factory()
            result = simulate(controller, brake, **sim_kw)
            cropped = result if crop_v is None else _crop_to_speed_band(result, crop_v)
            sc = score(cropped, mass_kg, emergency=emerg)
            e_acc += m_weight * sc['energy']
            t_acc += m_weight * sc['tracking']
            s_acc += m_weight * sc['smoothness']

        # Recompute composite from mass-averaged dimensions
        if emerg:
            composite = (W_ENERGY_EMERG * e_acc + W_TRACKING_EMERG * t_acc
                         + W_SMOOTHNESS_EMERG * s_acc)
        else:
            composite = (W_ENERGY_NORMAL * e_acc + W_TRACKING_NORMAL * t_acc
                         + W_SMOOTHNESS_NORMAL * s_acc)

        per_scenario.append(dict(
            name=name, energy=e_acc, tracking=t_acc, smoothness=s_acc,
            composite=composite, weight=weight,
        ))
        weighted_sum += weight * composite

    return dict(per_scenario=per_scenario, weighted=weighted_sum)


# =====================================================================
#  Monte Carlo robustness analysis
# =====================================================================

# Uncertain parameter definitions:
# (name, nominal, rel_error_fraction, description)
# rel_error_fraction is 1-sigma (68%).  We sample ±2σ truncated normal.

UNCERTAIN_PARAMS = [
    ("mu_s",             MU_S,     0.15,   "Static friction coeff"),
    ("mu_k",             MU_K,     0.15,   "Kinetic friction coeff"),
    ("eta_gear",         ETA_GEAR, 0.025,  "Gear efficiency"),
    ("flux_linkage",     FLUX_LINKAGE, 0.05, "Motor flux linkage"),
    ("r_phase",          R_PHASE,  0.08,   "Phase resistance"),
    ("j_carrier",        J_CARRIER, 0.12,  "Carrier inertia"),
    ("t_drag_coeff",     T_DRAG_COEFF, 0.25, "Drag coeff"),
    ("vesc_current_gain", 1.0,     0.10,   "VESC current sensor gain error"),
    ("vesc_voltage_gain", 1.0,     0.015,  "VESC voltage sensor gain error"),
    ("cap_esr",          CAP_ESR, 0.30,   "Supercap bank ESR"),
    ("foc_tau",          FOC_TAU, 0.20,   "FOC current loop time constant"),
    ("telem_delay",      TELEM_DELAY, 0.25, "RPM telemetry transport delay"),
]


def _sample_perturbations(rng, n):
    """Sample n sets of perturbed physical constants.

    Motor-thermal parameters (r_phase, flux_linkage, t_drag_coeff) are
    sampled with a shared latent "thermal" factor so they co-vary the
    way they do in reality:
      - Hot copper  → higher r_phase
      - Hot magnets → lower flux_linkage
      - Hot iron    → higher t_drag_coeff (eddy/hysteresis loss)
    The shared factor accounts for ~60% of each parameter's variance;
    the remaining 40% is independent (manufacturing spread, measurement
    error, bearing friction drift).  Other parameters are sampled
    independently.
    """
    # Correlation weights for thermal latent (fraction of 1σ driven by u_th).
    # Signs encode the physical relationship above.
    THERMAL_COEFF = {
        "r_phase":       +0.60,
        "flux_linkage":  -0.60,
        "t_drag_coeff":  +0.60,
    }
    samples = []
    for _ in range(n):
        u_th = rng.normal(0.0, 1.0)
        p = {}
        for name, nominal, sigma_rel, _desc in UNCERTAIN_PARAMS:
            sigma_abs = nominal * sigma_rel
            tc = THERMAL_COEFF.get(name, 0.0)
            # Draw correlated component + independent residual so the
            # marginal stays N(0, sigma_abs) regardless of tc.
            while True:
                z_indep = rng.normal(0.0, 1.0)
                z = tc * u_th + math.sqrt(max(0.0, 1.0 - tc * tc)) * z_indep
                val = nominal + z * sigma_abs
                if abs(val - nominal) <= 2.0 * sigma_abs:
                    break
            if name in ("mu_s", "mu_k", "eta_gear"):
                val = max(0.01, min(1.0, val))
            elif name in ("j_carrier", "t_drag_coeff", "r_phase", "flux_linkage"):
                val = max(nominal * 0.3, val)
            elif name in ("vesc_current_gain", "vesc_voltage_gain"):
                val = max(0.7, min(1.3, val))
            elif name == "cap_esr":
                val = max(0.01, val)
            elif name == "foc_tau":
                val = max(0.0002, val)
            elif name == "telem_delay":
                val = max(0.005, val)  # at least 5 ms
            p[name] = val
        if p["mu_k"] >= p["mu_s"]:
            p["mu_k"] = p["mu_s"] * 0.67
        samples.append(p)
    return samples


def _build_uncertain_params_table():
    """Build the default full-physics uncertain-parameter table."""
    table = []
    for name, nominal, sigma_rel, desc in UNCERTAIN_PARAMS:
        s = float(sigma_rel)
        if s < 1e-4:
            s = 1e-4
        table.append((name, float(nominal), s, desc))
    return table


def _sample_perturbations_from_table(rng, n, table):
    """Sample n perturbed parameter sets from a provided uncertainty table."""
    samples = []
    for _ in range(n):
        p = {}
        for name, nominal, sigma_rel, _desc in table:
            sigma_abs = nominal * sigma_rel
            if sigma_abs <= 0.0:
                val = nominal
            else:
                while True:
                    val = rng.normal(nominal, sigma_abs)
                    if abs(val - nominal) <= 2.0 * sigma_abs:
                        break
            if name in ("mu_s", "mu_k", "eta_gear"):
                val = max(0.01, min(1.0, val))
            elif name in ("j_carrier", "t_drag_coeff", "r_phase", "flux_linkage"):
                val = max(nominal * 0.3, val)
            elif name in ("vesc_current_gain", "vesc_voltage_gain"):
                val = max(0.7, min(1.3, val))
            elif name == "cap_esr":
                val = max(0.01, val)
            elif name == "foc_tau":
                val = max(0.0002, val)
            elif name == "telem_delay":
                val = max(0.005, val)
            p[name] = val
        if p["mu_k"] >= p["mu_s"]:
            p["mu_k"] = p["mu_s"] * 0.67
        samples.append(p)
    return samples


def _sim_kwargs_from_perturbation(p):
    """Convert a perturbation dict to simulate() keyword arguments."""
    return dict(
        mu_s=p["mu_s"],
        mu_k=p["mu_k"],
        eta_gear=p["eta_gear"],
        flux_linkage_override=p["flux_linkage"],
        r_phase_override=p["r_phase"],
        j_carrier_override=p["j_carrier"],
        t_drag_coeff=p["t_drag_coeff"],
        vesc_current_gain=p["vesc_current_gain"],
        vesc_voltage_gain=p["vesc_voltage_gain"],
        cap_esr=p["cap_esr"],
        foc_tau=p["foc_tau"],
        telem_delay=p["telem_delay"],
    )


def _scenario_brake_torque(decel_ms2, mass_kg):
    """Scenario brake torque target (independent of hand/cable path)."""
    return decel_to_brake(decel_ms2, mass_kg)


class _RobustWorker:
    """Pickle-safe callable for parallel Monte-Carlo robustness eval."""

    def __init__(self, strat_cls, strat_params, scenarios, masses):
        self.strat_cls = strat_cls
        self.strat_params = strat_params
        self.scenarios = scenarios
        self.masses = masses

    def __call__(self, perturbation):
        from .physics import simulate

        sim_kw = _sim_kwargs_from_perturbation(perturbation)

        weighted_sum = 0.0
        e_total = t_total = s_total = 0.0

        for tup in self.scenarios:
            name, v_start, v_end, decel_ms2, weight, emerg, kind = _unpack_scenario(tup)

            e_acc = t_acc = s_acc = 0.0

            for mass_kg, m_weight in self.masses:
                brake, scn_kw, crop_v = _scenario_sim_config(
                    kind, v_start, v_end, decel_ms2, mass_kg,
                )
                controller = self.strat_cls(**self.strat_params)
                result = simulate(controller, brake, **scn_kw, **sim_kw)
                cropped = result if crop_v is None else _crop_to_speed_band(result, crop_v)
                sc = score(cropped, mass_kg, emergency=emerg)
                e_acc += m_weight * sc['energy']
                t_acc += m_weight * sc['tracking']
                s_acc += m_weight * sc['smoothness']

            if emerg:
                composite = (W_ENERGY_EMERG * e_acc
                             + W_TRACKING_EMERG * t_acc
                             + W_SMOOTHNESS_EMERG * s_acc)
            else:
                composite = (W_ENERGY_NORMAL * e_acc
                             + W_TRACKING_NORMAL * t_acc
                             + W_SMOOTHNESS_NORMAL * s_acc)

            weighted_sum += weight * composite
            e_total += weight * e_acc
            t_total += weight * t_acc
            s_total += weight * s_acc

        return weighted_sum, e_total, t_total, s_total


def score_strategy_robust(strategy_factory, n_samples=20, seed=42,
                          scenarios=None, masses=None, workers=1,
                          strat_cls=None, strat_params=None, pool=None):
    """Score a strategy across Monte Carlo perturbations of physics.

    For parallel execution (workers > 1), pass strat_cls + strat_params
    instead of strategy_factory (lambdas can't be pickled).

    If *pool* is given (a multiprocessing.Pool), it is used instead of
    creating a temporary one.  The caller is responsible for its lifecycle.

    Returns:
        dict with keys: nominal, mean, std, p5, p95, scores,
                        energy_mean, tracking_mean, smoothness_mean.
    """
    from .physics import simulate

    if scenarios is None:
        scenarios = SCENARIOS
    if masses is None:
        masses = MASS_DISTRIBUTION

    rng = np.random.default_rng(seed)
    table = _build_uncertain_params_table()
    perturbations = _sample_perturbations_from_table(rng, n_samples, table)

    nominal_p = {name: nominal for name, nominal, _, _ in table}
    all_runs = [nominal_p] + perturbations

    # --- Parallel path (pickle-safe) ---
    if (workers > 1 or pool is not None) and strat_cls is not None:
        worker = _RobustWorker(strat_cls, strat_params or {}, scenarios,
                               masses)
        if pool is not None:
            results = pool.map(worker, all_runs)
        else:
            import multiprocessing as mp
            with mp.Pool(workers) as _pool:
                results = _pool.map(worker, all_runs)
        all_composites = [r[0] for r in results]
        all_energy = [r[1] for r in results]
        all_tracking = [r[2] for r in results]
        all_smoothness = [r[3] for r in results]
    else:
        # --- Sequential fallback ---
        if strategy_factory is None:
            if strat_cls is None:
                raise ValueError("Sequential robust scoring requires strategy_factory or strat_cls")
            strategy_factory = lambda: strat_cls(**(strat_params or {}))
        all_composites = []
        all_energy = []
        all_tracking = []
        all_smoothness = []

        for p in all_runs:
            sim_kw = _sim_kwargs_from_perturbation(p)

            weighted_sum = 0.0
            e_total = t_total = s_total = 0.0

            for tup in scenarios:
                name, v_start, v_end, decel_ms2, weight, emerg, kind = _unpack_scenario(tup)

                e_acc = t_acc = s_acc = 0.0

                for mass_kg, m_weight in masses:
                    brake, scn_kw, crop_v = _scenario_sim_config(
                        kind, v_start, v_end, decel_ms2, mass_kg,
                    )
                    controller = strategy_factory()
                    result = simulate(controller, brake, **scn_kw, **sim_kw)
                    cropped = result if crop_v is None else _crop_to_speed_band(result, crop_v)
                    sc = score(cropped, mass_kg, emergency=emerg)
                    e_acc += m_weight * sc['energy']
                    t_acc += m_weight * sc['tracking']
                    s_acc += m_weight * sc['smoothness']

                if emerg:
                    composite = (W_ENERGY_EMERG * e_acc
                                 + W_TRACKING_EMERG * t_acc
                                 + W_SMOOTHNESS_EMERG * s_acc)
                else:
                    composite = (W_ENERGY_NORMAL * e_acc
                                 + W_TRACKING_NORMAL * t_acc
                                 + W_SMOOTHNESS_NORMAL * s_acc)

                weighted_sum += weight * composite
                e_total += weight * e_acc
                t_total += weight * t_acc
                s_total += weight * s_acc

            all_composites.append(weighted_sum)
            all_energy.append(e_total)
            all_tracking.append(t_total)
            all_smoothness.append(s_total)

    nominal_score = all_composites[0]
    perturbed = np.array(all_composites[1:])
    energy_arr = np.array(all_energy[1:])
    tracking_arr = np.array(all_tracking[1:])
    smooth_arr = np.array(all_smoothness[1:])

    # CVaR (Conditional Value at Risk, a.k.a. Expected Shortfall):
    # mean of the worst alpha fraction of samples.  Uses at least 1
    # sample to stay well-defined for small n_samples.
    def _cvar(arr, alpha):
        n = max(1, int(np.ceil(alpha * len(arr))))
        return float(np.mean(np.sort(arr)[:n]))

    return dict(
        nominal=nominal_score,
        mean=float(np.mean(perturbed)),
        std=float(np.std(perturbed)),
        p5=float(np.percentile(perturbed, 5)),
        p95=float(np.percentile(perturbed, 95)),
        cvar10=_cvar(perturbed, 0.10),
        cvar20=_cvar(perturbed, 0.20),
        scores=perturbed,
        energy_mean=float(np.mean(energy_arr)),
        tracking_mean=float(np.mean(tracking_arr)),
        smoothness_mean=float(np.mean(smooth_arr)),
    )


def score_strategy_robust_trials(
    n_trials=40,
    episodes_per_trial=12,
    seed=42,
    center_blend=0.35,
    accuracy_tol_rel=0.10,
    obs_noise_rel=0.10,
):
    """Evaluate friction-estimator learning with fixed friction per trial.

    This utility is intentionally estimator-focused (not controller-focused):
      - each trial samples a true (mu_s, mu_k) once
      - true friction stays fixed for all episodes in that trial
      - estimator receives noisy observations each episode and updates center
      - accuracy is measured as relative error to the trial's true friction

    Returns:
      dict with aggregate and per-trial learning statistics.
    """
    n_trials = max(1, int(n_trials))
    episodes_per_trial = max(1, int(episodes_per_trial))
    center_blend = float(np.clip(center_blend, 0.0, 1.0))
    accuracy_tol_rel = float(max(0.0, accuracy_tol_rel))
    obs_noise_rel = float(max(0.0, obs_noise_rel))

    rng = np.random.default_rng(seed)

    table = _build_uncertain_params_table()
    row = {name: (center, sigma_rel) for name, center, sigma_rel, _ in table}

    mu_s_center, mu_s_sigma = row["mu_s"]
    mu_k_center, mu_k_sigma = row["mu_k"]

    mu_table = [
        ("mu_s", float(mu_s_center), float(mu_s_sigma), "Static friction coeff"),
        ("mu_k", float(mu_k_center), float(mu_k_sigma), "Kinetic friction coeff"),
    ]
    sampled = _sample_perturbations_from_table(rng, n_trials, mu_table)

    per_trial = []
    solved_cycles = []

    for trial_idx, p_true in enumerate(sampled, start=1):
        true_mu_s = float(p_true["mu_s"])
        true_mu_k = float(min(p_true["mu_k"], true_mu_s * 0.95))

        est_mu_s = float(mu_s_center)
        est_mu_k = float(min(mu_k_center, est_mu_s * 0.95))

        cycle_hit = None
        last_err_s = float("inf")
        last_err_k = float("inf")

        for cycle in range(1, episodes_per_trial + 1):
            # Observation noise proxy for realistic lock/slip trace variability.
            obs_mu_s = rng.normal(true_mu_s, max(1e-4, true_mu_s * obs_noise_rel * 1.25))
            obs_mu_k = rng.normal(true_mu_k, max(1e-4, true_mu_k * obs_noise_rel))

            obs_mu_s = float(np.clip(obs_mu_s, 0.05, 1.0))
            obs_mu_k = float(np.clip(obs_mu_k, 0.01, obs_mu_s * 0.95))

            # Keep mu_s slower than mu_k to reflect weaker observability.
            est_mu_s = (1.0 - 0.5 * center_blend) * est_mu_s + (0.5 * center_blend) * obs_mu_s
            est_mu_k = (1.0 - center_blend) * est_mu_k + center_blend * obs_mu_k
            est_mu_k = float(min(est_mu_k, est_mu_s * 0.95))

            last_err_s = abs(est_mu_s - true_mu_s) / max(true_mu_s, 1e-6)
            last_err_k = abs(est_mu_k - true_mu_k) / max(true_mu_k, 1e-6)

            if cycle_hit is None and last_err_s <= accuracy_tol_rel and last_err_k <= accuracy_tol_rel:
                cycle_hit = cycle

        if cycle_hit is not None:
            solved_cycles.append(cycle_hit)

        per_trial.append(
            dict(
                trial=trial_idx,
                true_mu_s=true_mu_s,
                true_mu_k=true_mu_k,
                final_mu_s=float(est_mu_s),
                final_mu_k=float(est_mu_k),
                final_rel_err_mu_s=float(last_err_s),
                final_rel_err_mu_k=float(last_err_k),
                cycles_to_accuracy=cycle_hit,
            )
        )

    solved_count = len(solved_cycles)
    unsolved_count = n_trials - solved_count

    if solved_cycles:
        mean_cycles = float(np.mean(solved_cycles))
        p50_cycles = float(np.percentile(solved_cycles, 50))
        p90_cycles = float(np.percentile(solved_cycles, 90))
    else:
        mean_cycles = float("inf")
        p50_cycles = float("inf")
        p90_cycles = float("inf")

    return dict(
        n_trials=n_trials,
        episodes_per_trial=episodes_per_trial,
        center_blend=center_blend,
        accuracy_tol_rel=accuracy_tol_rel,
        obs_noise_rel=obs_noise_rel,
        solved_count=solved_count,
        unsolved_count=unsolved_count,
        solved_fraction=float(solved_count / n_trials),
        mean_cycles_to_accuracy=mean_cycles,
        p50_cycles_to_accuracy=p50_cycles,
        p90_cycles_to_accuracy=p90_cycles,
        per_trial=per_trial,
    )
