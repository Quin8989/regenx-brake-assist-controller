"""sim.physics — Unified regen brake simulation engine.

Public API:
    simulate()       Run a full regen braking simulation.
    ff_current()     Feedforward current from RPM and gain.

Physical constants, the Numba-JIT inner loop, and the simulate() engine
all live in this single module.

Mechanical model
================
Puyan H01 hub motor, planetary 4.8:1:
    sun gear   ← motor rotor
    ring gear  ← wheel shell
    carrier    ← band-brake drum + one-way clutch to ground
The one-way clutch lets the carrier spin freely in the "motoring"
direction during coast (motor decoupled, zero drag).  The band brake's
*only* job is to resist the carrier's rotation.

Rider input is ``brake_val`` (Nm at the carrier) — the torque the rider
demands the band resist with.  No lever / cable / capstan model.

Regen is created by the motor, not the band.  ``brake_val`` is the
static holding torque the rider's clamping force produces on the drum
(``clamp_force · µ_s``).  As regen current rises, the motor's
back-reaction torque at the sun gear is reflected to the carrier as
``(1+N) · T_em · η_gear``.  While that stays below ``brake_val`` the
band holds the carrier locked and the planetary becomes a rigid
reduction — all motor torque goes straight to the wheel.  If the
motor over-shoots ``brake_val`` the band saturates at the kinetic
level ``brake_val · µ_k/µ_s`` and the carrier slips forward,
dumping the excess as band heat.

A traditional wheel brake (same rider clamping force, same pads) has
the pads continuously sliding on the drum, so its steady-state wheel
torque is ``brake_val · µ_k/µ_s`` — the kinetic level.  Matching
that is the controller's target: command regen so the carrier is
just barely slipping.  The lever-feel is then identical to a
conventional wheel brake, band heat is the small well-defined
µ_k/µ_s fraction, and the rest of the braking energy is captured
electrically.
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

# Rotor drag coefficient (iron loss + bearing + magnetic cogging), applied
# to |w_sun|.  Derived from a bench observation on 2026-04-22: with the
# band brake OFF (freewheel disengaged, rotor decoupled from wheel), the
# motor rotor spins down from 35 km/h road-equivalent (w_rotor ≈ 141 rad/s
# through the 4.8:1 gear) to visible rest in ~1.5 s.  Treating this as a
# first-order decay ω(t) = ω0·exp(-b/J·t), "visibly at rest" ≈ 4τ gives
# τ ≈ 0.38 s.
#
# Rotor inertia estimate for the Puyan H01 (3.2 kg total motor,
# manufacturer spec): hub shell ~1.5 kg, stator ~0.6 kg, gears/bearings
# ~0.4 kg → inner rotor ≈ 0.7 kg.  Rotor is a thin-wall steel can with
# surface magnets, OD ~55 mm, stack ~25 mm, mean radius ~25 mm.  For a
# thin-wall can J ≈ m·r_mean² = 0.7 · 0.025² ≈ 4.4e-4 kg·m².
#
# Therefore b_rotor = J_rotor / τ ≈ 4.4e-4 / 0.38 ≈ 1.2e-3 Nm·s/rad.
# Supersedes the earlier 2026-04-20 estimate (0.5 s → 3.7e-3) which
# was biased short by eyeballing a noisy visual cue.
# Stage 9 of bench_test_notes.md verifies this with a direct τ fit.
T_DRAG_COEFF = 0.0012       # Nm/(rad/s)

# ── Planetary gear ────────────────────────────────────────────────────
# Puyan H01 manufacturer spec: 4.8:1 reduction (motor_rpm / wheel_rpm).
# In this model carrier-fixed-or-locked mode gives w_sun = N · w_ring,
# so GEAR_N = N = Z_ring / Z_sun = 4.8.
GEAR_N   = 4.8
ETA_GEAR = 0.95

# ── Band brake — 3D-printed ABS drum (73 mm OD) ─────────────────
MU_S       = 0.30           # static friction coefficient
MU_K       = 0.20           # kinetic friction coefficient
STICTION_W = 0.5            # rad/s — below this, carrier treated as locked

# ── Supercapacitor ────────────────────────────────────────────────────
VCAP_INIT   = 25.0          # V — initial supercap voltage
CAP_ESR     = 0.050         # Ω — supercap bank ESR (typical 20F/48V series string)

# ── Bike ──────────────────────────────────────────────────────────────
# Rolling resistance coefficient for pneumatic e-bike tyre on asphalt.
# Typical Crr range for 26" bike tyres: 0.004 (slick race) to 0.015
# (knobby MTB at low pressure).  0.008 is mid-range for a commuter tyre
# at correct pressure, which matches the intended use case.  Applied as
# F_rr = C_rr · m · g · cos(θ), always opposing motion.
C_RR       = 0.008
# Gravitational accel (m/s²) — used for grade scenarios and rolling
# resistance normal-force calculation.
G_ACCEL    = 9.81

# J_CARRIER lumps the planetary carrier + planet gears + the motor rotor
# inertia reflected to the carrier side via the planetary.  For an
# effective inertia at the carrier axis (with w_ring held), a torque at
# the sun appears at the carrier scaled by (1+N), so the reflected rotor
# inertia is J_rotor·(1+N)².
#
# Direct measurement (2026-04-20, motor disassembled by user):
#   carrier (with integrated one-way freewheel):  ~250 g, mean radius
#     ~30 mm, roughly annular   →  J_c_phys ≈ 2.5e-4 kg·m²
#   planets: 3 × 12 g nylon, orbital radius ~25 mm, plus spin inertia
#                                              →  J_planets ≈ 3e-5 kg·m² (negligible)
#   rotor reflected:  J_rotor·(1+N)² = 4.4e-4 · 5.8² ≈ 1.48e-2 kg·m²
#   total:                                    J_CARRIER ≈ 0.015 kg·m²
#
# Previous values (0.05 placeholder, later 0.007) were both rough guesses
# before the motor was opened.  0.015 is ~2× the 0.007 estimate and ~3×
# smaller than the original 0.05; tunes produced against either will need
# to be re-run.
#
# LIMITATION: this model treats the sun-gear angular velocity as a
# kinematic constraint of w_ring and w_carrier; it does NOT carry the
# rotor as an independent inertia state.  That is acceptable while the
# band brake is engaged (carrier locked, rotor rigidly coupled to the
# wheel through the planetary) but under-represents the free-spinning
# rotor physics during coast (freewheel disengaged).  sim/identify.py
# residuals on coast traces are dominated by this.
J_CARRIER = 0.015            # kg·m² (reflected rotor + carrier + planets)

# ── Band brake torsional compliance ─────────────────────────────────
# Two series springs dominate the carrier-to-ground compliance:
#
#   1. Band axial stretch (spring-steel strap, 15 mm × ~0.5 mm cross
#      section, 120° wrap over a 73 mm OD drum plus ~50 mm of anchor-
#      to-lever tail → load path L ≈ 0.125 m).  E = 200 GPa.
#      k_band_axial = E·A/L ≈ 12 MN/m
#      k_band_tors  = k_axial·r² ≈ 12e6 · 0.0365² ≈ 16 000 Nm/rad.
#
#   2. ABS 3D-printed drum.  E_abs ≈ 2 GPa, G_abs ≈ 0.7 GPa — 100×
#      softer than steel.  Modelled as a short thin shell (wall ~3 mm,
#      axial length ~15 mm) that must transmit band friction torque
#      from its OD down to the carrier shaft.
#      J_polar ≈ 2πr³·t ≈ 9.2e-7 m⁴
#      k_drum  = G·J/L ≈ 43 000 Nm/rad.
#
# In series: 1/k = 1/16 000 + 1/43 000 → k ≈ 12 000 Nm/rad (lower bound
# at 0.5 mm band thickness; thicker strap moves this up to ~18 000).
# The 50 000 Nm/rad I originally wrote was based on assuming a steel
# drum and does NOT apply to your ABS build.
#
# Natural period at J = 0.015:
#   ω_n = √(k/J) ≈ 894 rad/s → T_n ≈ 7 ms
# That is ~70 % of the 10 ms telemetry window, so band ring-down is
# NOT fully damped inside one drpm_mean aggregate — the strategy sees
# a partially-settled transient, not a clean edge.  This shows up as
# ~150–750 k rpm/s² jerk spikes (5–20× smaller than the instantaneous-
# unlock model predicted).
#
# C_BAND is 0.7 × critical damping, representative of the heavy
# dissipation from steel-on-ABS kinetic friction plus structural
# damping in the filled ABS print:
#   C_crit = 2·√(J·k) ≈ 2·√(0.015·12 000) ≈ 26.8 Nm·s/rad
#   C_BAND = 0.7 · C_crit ≈ 18.8 Nm·s/rad.
# Semi-implicit Euler is used for the spring integrator so stability
# holds for dt·ω_n < 2 (0.89 at ω_n ≈ 894 rad/s, dt = 1 ms).
K_BAND = 1.2e4              # Nm/rad — band torsional stiffness (series band+drum)
C_BAND = 18.8               # Nm·s/rad — 0.7 × critical for J=0.015, K=1.2e4

# ── Simulation timing defaults ────────────────────────────────────────
DT          = 0.001         # 1 ms  (was 0.2 ms — stable for J=0.05)
CTRL_PERIOD = 0.01          # 10 ms (100 Hz)
TELEM_DELAY = 0.015         # 15 ms telemetry round-trip
T_END       = 4.0           # s

# ── VESC FOC model ───────────────────────────────────────────────────
FOC_TAU            = 0.001  # s — current loop time constant (~1 kHz bandwidth)
DUTY_SAT_THRESHOLD = 0.95   # duty cycle above which VESC cannot track command
IQ_KP_DEFAULT      = 0.3    # firmware iq feedback gain

# ── Precomputed helpers ─────────────────────────────────────────────
_RPM_SCALE = 60.0 / (2.0 * math.pi)
_TWO_PI    = 2.0 * math.pi

# ── Fast-path telemetry noise model ─────────────────────────────────
# Nominal sigmas modelling the realistic VESC + LispBM jitter seen on
# our hardware.  Each is overridable per-simulation via simulate() /
# simulate_ride() kwargs; sim.scoring perturbs all of them in its
# Monte-Carlo robustness sweep.
#
# Calibrated against a Puyan H01 hub on a VESC 6.x-class controller:
#   * rpm:   sensorless observer jitter, roughly ±10 rpm (mech)
#            steady-state.  Worsens below ~200 rpm but that regime is
#            excluded by the speed cutoff anyway.
#   * iq:    post-FOC-averaged current readout, ~0.2 A σ after the
#            shunt + ADC chain.  Independent of iq_bias below.
#   * iq_bias: slowly-varying zero offset on the current sensor.
#            VESC's boot-time auto-cal trims the fresh value to ~50 mA,
#            but shunt/amp thermal drift adds up to ~300 mA over a
#            40 °C warm-up.  Treat as constant within a single sim run.
#   * vcap:  resistor-divider + ADC noise on the bus voltage, ~30 mV σ.
#
# drpm_mean / drpm_peak_neg sigmas are not derived from σ_rpm because
# the LispBM script aggregates over the 10 ms window with averaging
# that reduces naive-difference noise substantially.  These numbers
# come from bench traces:
#   σ(drpm_mean)     ≈ 14 rpm/s
#   σ(drpm_peak_neg) ≈ 140 rpm/s   (expected-minimum bias: −300 rpm/s)
RPM_NOISE_SIGMA_DEFAULT           = 10.0    # rpm (mech) — sensorless observer
IQ_NOISE_SIGMA_DEFAULT            = 0.2     # A           — post-FOC readout
IQ_BIAS_DEFAULT                   = 0.0     # A           — current-sensor offset
VCAP_NOISE_SIGMA_DEFAULT          = 0.03    # V           — resistor-divider + ADC
DRPM_MEAN_NOISE_SIGMA_DEFAULT     = 14.0    # rpm/s       — 10 ms window noise
DRPM_PEAK_NEG_NOISE_SIGMA_DEFAULT = 140.0   # rpm/s       — per-sample peak hold
DRPM_PEAK_NEG_NOISE_BIAS_DEFAULT  = -300.0  # rpm/s       — E[min of n normals]

# Legacy aliases — still used by a few callers that import directly.
_RPM_NOISE_SIGMA             = RPM_NOISE_SIGMA_DEFAULT
_IQ_NOISE_SIGMA              = IQ_NOISE_SIGMA_DEFAULT
_DRPM_MEAN_NOISE_SIGMA       = DRPM_MEAN_NOISE_SIGMA_DEFAULT
_DRPM_PEAK_NEG_NOISE_SIGMA   = DRPM_PEAK_NEG_NOISE_SIGMA_DEFAULT
_DRPM_PEAK_NEG_NOISE_BIAS    = DRPM_PEAK_NEG_NOISE_BIAS_DEFAULT

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
    """Run n_sub physics timesteps with fixed i_cmd and brake.

    Integrates two parallel bikes on every substep:
      ours       — w_ring + w_carrier + motor + band (the freen device).
      baseline   — w_ring_base only, representing an *idealized*
                   non-slipping brake: the rider's clamping force
                   applied as a wheel torque at the full static-
                   friction level ``brake_val`` (i.e. what the rider
                   actually asked for before any pad slip).  This is
                   the controller target — the regen's job is to make
                   the carrier just barely slip, which is the
                   measurable µ_s boundary.  Same mass, same C_rr,
                   same grade as ours.  Used by
                   scoring.tracking_score.

    All physical parameters are passed explicitly so this function
    can be JIT-compiled without module-level state.
    """
    p_net_sum = 0.0
    pcu_esr_sum = 0.0
    p_brake_sum = 0.0
    p_drg_sum = 0.0
    motor_rpm = 0.0
    stopped = False
    buf_len = len(rpm_buf)

    # Aggregators mirroring scripts/vesc_lisp_push_iq.lisp (1 kHz → 10 ms window).
    # `rpm_prev_sub_in` is seeded once at _init_state by the caller and carried
    # across window batches, so the first per-sample Δrpm of every window
    # telescopes correctly against the last sample of the previous window.
    # This matches the fixed lisp which seeds rpm-prev once before the main
    # loop and never resets it.
    rpm_prev_sub = rpm_prev_sub_in
    drpm_peak_neg_sub = 0.0   # in rpm/s (peak-held min of per-substep Δrpm/dt)
    iq_sum = 0.0
    n_done = 0

    for i in range(n_sub):
        # Planetary kinematics
        w_sun = one_plus_n * w_carrier - gear_n * w_ring
        motor_rpm = max(0.0, -w_sun) * rpm_scale

        # Window-aggregate bookkeeping (before physics update so we capture
        # the rpm at this substep's start, matching what the VESC's 1 kHz
        # lisp loop would sample).
        delta_rate = (motor_rpm - rpm_prev_sub) / dt
        if delta_rate < drpm_peak_neg_sub:
            drpm_peak_neg_sub = delta_rate
        rpm_prev_sub = motor_rpm
        iq_sum += i_actual
        n_done = i + 1

        # RPM delay buffer (circular)
        rpm_buf[rpm_idx] = motor_rpm
        rpm_idx = (rpm_idx + 1) % buf_len

        # FOC current delivery (first-order lag)
        i_target = i_cmd * inv_cur_gain
        i_actual = i_actual + foc_alpha * (i_target - i_actual)
        if i_actual < 0.0:
            i_actual = 0.0

        # Motor coupling.  The one-way clutch engages whenever the band
        # is clamped (brake_val > 0 → carrier forced to react against
        # ground) or whenever the controller is commanding current (and
        # therefore producing EM torque).  Otherwise the clutch is free
        # and the motor is mechanically decoupled: zero rotor drag
        # coupled to the wheel, zero band torque, carrier follows the
        # freewheel kinematic constraint (w_sun = 0, i.e.
        # w_carrier = N/(1+N) · w_ring) so the rotor spins at the wheel
        # equivalent.  Rotor bearing drag alone is ≈ 0 Nm, confirmed by
        # the 2026-04-22 rotor spin-down bench observation.
        motor_coupled = (brake_val > 0.0) or (i_cmd > 1e-4) or (i_actual > 1e-4)

        # Electromagnetic torque
        t_em = kt * i_actual if w_sun < 0.0 else 0.0
        t_drag = t_drag_coeff * abs(w_sun) if motor_coupled else 0.0

        # Gear torques
        t_em_car = one_plus_n * t_em * eta_gear
        t_em_ring = gear_n * t_em * eta_gear

        if motor_coupled:
            # Band brake — Karnopp Coulomb friction model.
            #
            # Rider input is brake_val, the *static* holding torque the
            # band can react before the drum slides on the pad.  Once
            # sliding, kinetic friction drops to brake_val · µ_k/µ_s.
            # Earlier revisions of this model tried to layer a torsional
            # spring + damper in series with the Coulomb element, but
            # the damping term c_band·ω_carrier is unbounded and lets
            # the motor dump arbitrary torque through the band
            # regardless of brake_val — the exact bug that inflated
            # regen capture ~3× in the 2026-04-23 audit.  The band's
            # torsional compliance is ~7 ms ring-down (documented near
            # K_BAND/C_BAND above), comparable to the 10 ms telemetry
            # window, so the controller never observes the transient.
            # Dropping compliance and keeping just the Coulomb slider
            # gives the same average-case behaviour without the bug.
            #
            # State:
            #   w_carrier ≈ 0 AND t_em_car ≤ static_cap
            #       → band holds, transmits exactly t_em_car, no heat
            #   t_em_car > static_cap
            #       → static breakaway, drops to kinetic_cap
            #   w_carrier > stiction_w
            #       → slipping, transmits kinetic_cap, dissipates
            #         t_brake · ω_carrier as heat
            # Freewheel (one-way clutch): t_brake ≥ 0 always.
            if brake_val > 0.0:
                static_cap = brake_val
                kinetic_cap = brake_val * mu_ratio
                if w_carrier <= stiction_w:
                    # Locked or about to lock.
                    if t_em_car <= static_cap:
                        # Band holds — reaction exactly cancels motor
                        # push, carrier stays stationary.
                        t_brake = t_em_car if t_em_car > 0.0 else 0.0
                    else:
                        # Static breakaway to kinetic regime.
                        t_brake = kinetic_cap
                else:
                    # Slipping.
                    t_brake = kinetic_cap
            else:
                t_brake = 0.0
            # delta_band state retained for log compatibility but no
            # longer influences t_brake.
            delta_band = 0.0

            # Carrier dynamics (inertial integration against motor +
            # band reactions).
            net = t_em_car - t_brake
            if w_carrier <= 0.0:
                w_carrier = 0.0
                if net > 0.0:
                    w_carrier = w_carrier + net * inv_j_carrier * dt
            else:
                w_carrier = w_carrier + net * inv_j_carrier * dt
                if w_carrier < 0.0:
                    w_carrier = 0.0
        else:
            # Freewheel decoupled — kinematic constraint keeps w_sun=0
            # so the rotor coasts at the wheel-equivalent speed with
            # no reaction torque back on the wheel.
            t_brake = 0.0
            delta_band = 0.0
            w_carrier = n_over_np1 * w_ring

        # Wheel dynamics
        if free_decel:
            # Resistive torques at the ring (= wheel axis):
            #   motor regen drag + rotor-drag reflected + rolling resistance
            # Assistive torques: gravity along road (downhill = +) and
            # rider pedal input (positive = adding wheel speed).  Rotor
            # drag only couples to the wheel when the freewheel clutch
            # is engaged (motor_coupled).
            t_rr_signed = t_rr_ring if w_ring > 0.0 else 0.0
            t_drag_to_wheel = (gear_n * t_drag * eta_gear) if motor_coupled else 0.0
            w_ring = w_ring - (
                t_em_ring + t_drag_to_wheel
                + t_rr_signed - t_grav_ring - t_pedal_ring
            ) * inv_j_wheel * dt
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
        # Band heat: t_brake * |slip velocity|.  Zero when carrier locked.
        p_brake_sum += t_brake * abs(w_carrier)

        # Baseline bike (idealized non-slipping brake): wheel torque
        # equals the rider's full static-friction demand ``brake_val``
        # — this is the controller target, i.e. what the regen is
        # trying to emulate by pushing the carrier just to the µ_s
        # boundary.  Same mass, same C_rr, same grade.  ``mu_ratio``
        # is kept as a parameter for telemetry/doc purposes but no
        # longer scales the baseline torque.  Only integrated when
        # free_decel is True; in constant_speed scenarios the baseline
        # is held pinned like our bike.
        if free_decel and w_ring_base > 0.0:
            t_rr_base = t_rr_ring if w_ring_base > 0.0 else 0.0
            w_ring_base = w_ring_base - (
                brake_val + t_rr_base - t_grav_ring
            ) * inv_j_wheel * dt
            if w_ring_base < 0.0:
                w_ring_base = 0.0

        # Early exit
        if free_decel and w_ring <= v_min_w and w_carrier <= 0.0:
            stopped = True
            break

    # Window aggregates (match lisp packet semantics).  drpm_mean uses the
    # telescoping sum equivalent: (rpm_end - rpm_at_start_of_window) / (n*dt).
    # rpm_at_start_of_window == rpm_prev_sub_in (previous window's last
    # sample), which is exactly what the fixed lisp's drpm-sum evaluates to.
    if n_done >= 1:
        drpm_mean = (motor_rpm - rpm_prev_sub_in) / (n_done * dt)
        iq_mean = iq_sum / n_done
    else:
        drpm_mean = 0.0
        iq_mean = iq_sum
    drpm_peak_neg = drpm_peak_neg_sub

    return (w_ring, w_carrier, i_actual, e_cap, w_ring_base,
            motor_rpm, rpm_idx, stopped,
            p_net_sum, pcu_esr_sum, p_brake_sum, p_drg_sum,
            drpm_mean, drpm_peak_neg, iq_mean, rpm_prev_sub,
            delta_band)


# =====================================================================
#  Utility functions
# =====================================================================

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
             telem_delay=None,
             rpm_noise_sigma=RPM_NOISE_SIGMA_DEFAULT,
             iq_noise_sigma=IQ_NOISE_SIGMA_DEFAULT,
             iq_bias=IQ_BIAS_DEFAULT,
             vcap_noise_sigma=VCAP_NOISE_SIGMA_DEFAULT,
             grade_rad=0.0, c_rr=None,
             k_band=None, c_band=None):
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
        grade_rad:   Road grade (rad).  Positive = descending (gravity
                    accelerates bike forward).  Default 0 (flat).
        c_rr:        Rolling resistance coefficient override.  Default C_RR.

    Returns:
        dict of CTRL_PERIOD-sampled (10 ms) time-series arrays:
            t, speed, speed_baseline, motor_rpm, current, carrier_rpm, vcap,
            p_elec, p_copper, p_brake, eta,
            brake_demand, locked
    """
    # ── Resolve overrides ────────────────────────────────────────────
    params = _resolve_params(
        eta_gear, j_carrier_override, t_drag_coeff,
        r_phase_override, flux_linkage_override,
        cap_esr, foc_tau, k_band, c_band,
    )

    # ── Initialise state ─────────────────────────────────────────────
    state = _init_state(v0_kmh, mass_kg, dt, mu_s, mu_k, params,
                        vesc_current_gain, vesc_voltage_gain,
                        constant_speed, v_min_kmh, iq_kp,
                        telem_delay=telem_delay,
                        rpm_noise_sigma=rpm_noise_sigma,
                        iq_noise_sigma=iq_noise_sigma,
                        iq_bias=iq_bias,
                        vcap_noise_sigma=vcap_noise_sigma)

    # ── Controller dispatch ──────────────────────────────────────────
    use_strategy = hasattr(controller, 'update')
    k_fixed = 0.0 if use_strategy else float(controller)

    # ── Brake dispatch ───────────────────────────────────────────────
    brake_callable = callable(brake)
    brake_const = 0.0 if brake_callable else float(brake)

    # ── Road forces (constant for the run) ───────────────────────────
    _c_rr = C_RR if c_rr is None else float(c_rr)
    cos_g = math.cos(grade_rad)
    sin_g = math.sin(grade_rad)
    t_rr_ring   = _c_rr * mass_kg * G_ACCEL * cos_g * R_WHEEL   # Nm, opposes motion
    t_grav_ring = mass_kg * G_ACCEL * sin_g * R_WHEEL           # Nm, + = downhill assist

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
        # Fast-path aggregates come from the previous batch (1-tick lag
        # matches the hardware pipeline: VESC aggregates the last 10 ms,
        # Pico reads it, decides this tick's command).
        i_cmd = _compute_current_command(
            use_strategy, controller, k_fixed,
            delayed_rpm, v_cap_sensed, v_cap,
            state['i_actual'], state['iq_kp'],
            vesc_current_gain, params['flux'], params['watt_max'],
            drpm_mean_prev=state['drpm_mean_prev'],
            drpm_peak_neg_prev=state['drpm_peak_neg_prev'],
            iq_mean_prev=state['iq_mean_prev'],
            noise_rng=state['noise_rng'],
            rpm_noise_sigma=state['rpm_noise_sigma'],
            iq_noise_sigma=state['iq_noise_sigma'],
            iq_bias=state['iq_bias'],
            vcap_noise_sigma=state['vcap_noise_sigma'],
            drpm_mean_noise_sigma=state['drpm_mean_noise_sigma'],
            drpm_peak_neg_noise_sigma=state['drpm_peak_neg_noise_sigma'],
            drpm_peak_neg_noise_bias=state['drpm_peak_neg_noise_bias'],
        )

        # ── Brake for this tick ──────────────────────────────────────
        brake_val = brake(t) if brake_callable else brake_const

        # ── Physics substeps (numba JIT) ─────────────────────────────
        (state['w_ring'], state['w_carrier'], state['i_actual'], state['e_cap'],
         state['w_ring_base'],
         motor_rpm, state['rpm_idx'], stopped,
         p_net_sum, pcu_esr_sum, p_brake_sum, p_drg_sum,
         drpm_mean_new, drpm_peak_neg_new, iq_mean_new,
         state['rpm_prev_sub'],
         state['delta_band']) = _run_physics_batch(
            state['ctrl_steps'], dt,
            state['w_ring'], state['w_carrier'], state['i_actual'], state['e_cap'],
            state['w_ring_base'],
            i_cmd, brake_val,
            state['_1pN'], GEAR_N, params['kt'], params['eta_gear'],
            params['t_drag'], params['_1p5R'],
            state['foc_alpha'], state['inv_cur_gain'],
            state['inv_j_carrier'], state['inv_j_wheel'],
            state['mu_ratio'], STICTION_W, params['cap_esr'], state['inv_cap'],
            state['_NoverNp1'], _RPM_SCALE,
            state['free_decel'], state['v_min_w'],
            t_rr_ring, t_grav_ring, 0.0,
            state['rpm_buf'], state['rpm_idx'],
            state['rpm_prev_sub'],
            state['delta_band'], params['k_band'], params['c_band'],
        )
        state['drpm_mean_prev'] = drpm_mean_new
        state['drpm_peak_neg_prev'] = drpm_peak_neg_new
        state['iq_mean_prev'] = iq_mean_new

        # ── Log ──────────────────────────────────────────────────────
        v_cap = math.sqrt(2.0 * state['e_cap'] * state['inv_cap']) if state['e_cap'] > 0.0 else 0.0
        if ix < n_ticks:
            _record_log(logs, ix, t, state, motor_rpm, v_cap,
                        p_net_sum, pcu_esr_sum, p_brake_sum,
                        p_drg_sum, brake_val,
                        state['inv_sub'])
            ix += 1

        if stopped:
            break
        if state['free_decel'] and state['w_ring'] * state['spd_scale'] < v_min_kmh and state['w_carrier'] <= 0.0:
            break

    s = slice(0, ix)
    return {k: v[s] for k, v in logs.items()}


# =====================================================================
#  Ride-trace entry point — continuous 60 s stochastic rides
# =====================================================================

def simulate_ride(controller, ride, *, force_motor_off=False,
                  dt=DT, mu_s=MU_S, mu_k=MU_K,
                  eta_gear=None, j_carrier_override=None,
                  t_drag_coeff=None, r_phase_override=None,
                  flux_linkage_override=None,
                  cap_esr=None, foc_tau=None,
                  iq_kp=IQ_KP_DEFAULT,
                  vesc_current_gain=1.0, vesc_voltage_gain=1.0,
                  telem_delay=None,
                  rpm_noise_sigma=RPM_NOISE_SIGMA_DEFAULT,
                  iq_noise_sigma=IQ_NOISE_SIGMA_DEFAULT,
                  iq_bias=IQ_BIAS_DEFAULT,
                  vcap_noise_sigma=VCAP_NOISE_SIGMA_DEFAULT,
                  c_rr=None,
                  k_band=None, c_band=None):
    """Run one physics sim driven by a ``RideTrace``.

    The ride trace supplies three 1 ms-sampled inputs:
        * ``brake_torque``   — rider's clamping demand at the carrier
        * ``pedal_torque``   — rider's positive torque at the wheel
        * ``grade_rad``      — road grade, +down / −up

    ``force_motor_off=True`` clamps i_cmd to 0 regardless of what the
    strategy returns — used by the linearity-reference pass in
    ``sim.scoring`` to produce the "same ride, no motor" trajectory
    the real controller is benchmarked against.

    Initial wheel speed is the rider's cruise target (the rider is
    mid-ride, not starting from rest).

    Returns the same log-array dict as :func:`simulate`, with length
    equal to the ride duration at ``CTRL_PERIOD`` cadence.
    """
    # ── Resolve overrides ────────────────────────────────────────────
    params = _resolve_params(
        eta_gear, j_carrier_override, t_drag_coeff,
        r_phase_override, flux_linkage_override,
        cap_esr, foc_tau, k_band, c_band,
    )

    mass_kg = float(ride.mass_kg)
    v0_kmh = float(ride.cruise_kmh)

    state = _init_state(v0_kmh, mass_kg, dt, mu_s, mu_k, params,
                        vesc_current_gain, vesc_voltage_gain,
                        constant_speed=False, v_min_kmh=0.0,
                        iq_kp=iq_kp, telem_delay=telem_delay,
                        rpm_noise_sigma=rpm_noise_sigma,
                        iq_noise_sigma=iq_noise_sigma,
                        iq_bias=iq_bias,
                        vcap_noise_sigma=vcap_noise_sigma)

    use_strategy = hasattr(controller, 'update')
    k_fixed = 0.0 if use_strategy else float(controller)

    _c_rr = C_RR if c_rr is None else float(c_rr)

    # ── Resample 1 ms ride arrays onto the CTRL_PERIOD (10 ms) grid ──
    ctrl_steps = max(1, int(round(CTRL_PERIOD / dt)))
    n_ticks_ride = ride.n // ctrl_steps
    n_ticks = n_ticks_ride + 1   # +1 for the final log sample

    # For each control tick, take the mean of the ride arrays over the
    # 10-sample sub-window.  Brake / grade move much more slowly than
    # 100 Hz so this is loss-free in practice.
    idx = np.arange(n_ticks_ride) * ctrl_steps
    brake_ticks = np.add.reduceat(
        ride.brake_torque[:n_ticks_ride * ctrl_steps], idx) / ctrl_steps
    grade_ticks = np.add.reduceat(
        ride.grade_rad[:n_ticks_ride * ctrl_steps], idx) / ctrl_steps
    # Pedal is a bool mask → a tick counts as "pedal active" if any of
    # its 10 ms sub-samples is True.
    pedal_active_ticks = np.add.reduceat(
        ride.pedal_active[:n_ticks_ride * ctrl_steps].astype(np.int32),
        idx) > 0

    # Rider pedal-model constants.  Closed-loop on wheel speed.
    v_cruise = float(ride.cruise_kmh) / 3.6
    rider_kp_p_per_err = 150.0   # W of added drive per (m/s) error
    rider_p_sustain_w  = 150.0
    rider_p_burst_w    = 400.0
    rider_t_max_nm     = 120.0   # physiological torque cap at the wheel

    # ── Log arrays ───────────────────────────────────────────────────
    logs = _make_log_arrays(n_ticks)
    # Extra per-tick diagnostics beyond the standard set.
    logs['pedal'] = np.empty(n_ticks)
    logs['grade'] = np.empty(n_ticks)
    ix = 0

    # ── Main control loop (100 Hz) ──────────────────────────────────
    for tick in range(n_ticks_ride):
        t = tick * CTRL_PERIOD
        brake_val = float(brake_ticks[tick])
        grade_val = float(grade_ticks[tick])
        pedal_on = bool(pedal_active_ticks[tick])

        cos_g = math.cos(grade_val)
        sin_g = math.sin(grade_val)
        t_rr_ring   = _c_rr * mass_kg * G_ACCEL * cos_g * R_WHEEL
        t_grav_ring = mass_kg * G_ACCEL * sin_g * R_WHEEL

        # ── Rider pedal P-controller (closed-loop on wheel speed) ──
        # Rider applies positive drive power to hold cruise.  Zero
        # when on the brakes or when already above cruise.
        v_now = state['w_ring'] * R_WHEEL
        if pedal_on and v_now < v_cruise + 0.1 and v_now > 0.5:
            err_ms = max(0.0, v_cruise - v_now)
            # Steady-state demand at current grade (to counter losses).
            p_ss = max(0.0, (
                _c_rr * mass_kg * G_ACCEL * cos_g
                + 0.28 * 0.509 * v_cruise * v_cruise / max(1.0, v_cruise)
                + mass_kg * G_ACCEL * sin_g
            ) * v_cruise)
            p_rider = p_ss + rider_kp_p_per_err * err_ms
            p_rider = min(p_rider, rider_p_burst_w)
            if p_rider > rider_p_sustain_w:
                # Soft sag between sustained and burst.
                excess = (p_rider - rider_p_sustain_w) / (
                    rider_p_burst_w - rider_p_sustain_w)
                p_rider *= 1.0 - 0.2 * excess
            t_pedal_ring = p_rider / v_now
            if t_pedal_ring > rider_t_max_nm:
                t_pedal_ring = rider_t_max_nm
        else:
            t_pedal_ring = 0.0

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
            drpm_mean_prev=state['drpm_mean_prev'],
            drpm_peak_neg_prev=state['drpm_peak_neg_prev'],
            iq_mean_prev=state['iq_mean_prev'],
            noise_rng=state['noise_rng'],
            rpm_noise_sigma=state['rpm_noise_sigma'],
            iq_noise_sigma=state['iq_noise_sigma'],
            iq_bias=state['iq_bias'],
            vcap_noise_sigma=state['vcap_noise_sigma'],
            drpm_mean_noise_sigma=state['drpm_mean_noise_sigma'],
            drpm_peak_neg_noise_sigma=state['drpm_peak_neg_noise_sigma'],
            drpm_peak_neg_noise_bias=state['drpm_peak_neg_noise_bias'],
        )
        if force_motor_off:
            i_cmd = 0.0

        # ── Physics substeps (numba JIT) ─────────────────────────────
        (state['w_ring'], state['w_carrier'], state['i_actual'], state['e_cap'],
         state['w_ring_base'],
         motor_rpm, state['rpm_idx'], stopped,
         p_net_sum, pcu_esr_sum, p_brake_sum, p_drg_sum,
         drpm_mean_new, drpm_peak_neg_new, iq_mean_new,
         state['rpm_prev_sub'],
         state['delta_band']) = _run_physics_batch(
            state['ctrl_steps'], dt,
            state['w_ring'], state['w_carrier'], state['i_actual'], state['e_cap'],
            state['w_ring_base'],
            i_cmd, brake_val,
            state['_1pN'], GEAR_N, params['kt'], params['eta_gear'],
            params['t_drag'], params['_1p5R'],
            state['foc_alpha'], state['inv_cur_gain'],
            state['inv_j_carrier'], state['inv_j_wheel'],
            state['mu_ratio'], STICTION_W, params['cap_esr'], state['inv_cap'],
            state['_NoverNp1'], _RPM_SCALE,
            state['free_decel'], state['v_min_w'],
            t_rr_ring, t_grav_ring, t_pedal_ring,
            state['rpm_buf'], state['rpm_idx'],
            state['rpm_prev_sub'],
            state['delta_band'], params['k_band'], params['c_band'],
        )
        state['drpm_mean_prev'] = drpm_mean_new
        state['drpm_peak_neg_prev'] = drpm_peak_neg_new
        state['iq_mean_prev'] = iq_mean_new

        # ── Log ──────────────────────────────────────────────────────
        v_cap = math.sqrt(2.0 * state['e_cap'] * state['inv_cap']) if state['e_cap'] > 0.0 else 0.0
        if ix < n_ticks:
            _record_log(logs, ix, t, state, motor_rpm, v_cap,
                        p_net_sum, pcu_esr_sum, p_brake_sum,
                        p_drg_sum, brake_val,
                        state['inv_sub'])
            logs['pedal'][ix] = t_pedal_ring
            logs['grade'][ix] = grade_val
            ix += 1

    s = slice(0, ix)
    return {k: v[s] for k, v in logs.items()}


# =====================================================================
#  Private helpers — each handles one concern
# =====================================================================

def _resolve_params(eta_gear, j_carrier_override, t_drag_coeff,
                    r_phase_override, flux_linkage_override,
                    cap_esr, foc_tau, k_band, c_band):
    """Resolve parameter overrides, falling back to module defaults."""
    _eta_gear = eta_gear if eta_gear is not None else ETA_GEAR
    _j_carrier = j_carrier_override if j_carrier_override is not None else J_CARRIER
    _t_drag = t_drag_coeff if t_drag_coeff is not None else T_DRAG_COEFF
    _r_phase = r_phase_override if r_phase_override is not None else R_PHASE
    _flux = flux_linkage_override if flux_linkage_override is not None else FLUX_LINKAGE
    _cap_esr = cap_esr if cap_esr is not None else CAP_ESR
    _foc_tau = foc_tau if foc_tau is not None else FOC_TAU
    _k_band = k_band if k_band is not None else K_BAND
    _c_band = c_band if c_band is not None else C_BAND
    _kt = 1.5 * POLE_PAIRS * _flux
    return dict(
        eta_gear=_eta_gear, j_carrier=_j_carrier, t_drag=_t_drag,
        r_phase=_r_phase, flux=_flux, cap_esr=_cap_esr, foc_tau=_foc_tau,
        kt=_kt, _1p5R=1.5 * _r_phase, watt_max=VESC_WATT_MAX,
        k_band=_k_band, c_band=_c_band,
    )


def _init_state(v0_kmh, mass_kg, dt, mu_s, mu_k, params,
                vesc_current_gain, vesc_voltage_gain,
                constant_speed, v_min_kmh, iq_kp,
                telem_delay=None,
                rpm_noise_sigma=RPM_NOISE_SIGMA_DEFAULT,
                iq_noise_sigma=IQ_NOISE_SIGMA_DEFAULT,
                iq_bias=IQ_BIAS_DEFAULT,
                vcap_noise_sigma=VCAP_NOISE_SIGMA_DEFAULT,
                drpm_mean_noise_sigma=DRPM_MEAN_NOISE_SIGMA_DEFAULT,
                drpm_peak_neg_noise_sigma=DRPM_PEAK_NEG_NOISE_SIGMA_DEFAULT,
                drpm_peak_neg_noise_bias=DRPM_PEAK_NEG_NOISE_BIAS_DEFAULT):
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
        w_ring_base=w_ring,
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
        # Window aggregates (populated by previous batch, read by next
        # tick's strategy context).  Match lisp packet units: rpm/s,
        # rpm/s, A.  Start at zero — first strategy tick sees no fast
        # signal yet, identical to hardware boot before first packet.
        drpm_mean_prev=0.0,
        drpm_peak_neg_prev=0.0,
        iq_mean_prev=0.0,
        # rpm_prev_sub carries the motor_rpm from the last substep of the
        # previous window, so the first per-sample Δrpm of every window
        # telescopes correctly.  Seeded at 0.0 (same as motor at rest);
        # first window's drpm_mean absorbs the spin-up which matches the
        # lisp behaviour of its first packet on VESC boot.
        rpm_prev_sub=0.0,
        # Deterministic per-run RNG for telemetry-noise injection.  Seed
        # is fixed so tuning/scoring is reproducible; the sim still
        # exposes realistic jitter on the fast-path signals.
        noise_rng=np.random.default_rng(0xC0FFEE),
        # Sensor noise magnitudes for this run (parameterised so
        # sim.scoring can perturb them in the robustness sweep).
        rpm_noise_sigma=float(rpm_noise_sigma),
        iq_noise_sigma=float(iq_noise_sigma),
        iq_bias=float(iq_bias),
        vcap_noise_sigma=float(vcap_noise_sigma),
        drpm_mean_noise_sigma=float(drpm_mean_noise_sigma),
        drpm_peak_neg_noise_sigma=float(drpm_peak_neg_noise_sigma),
        drpm_peak_neg_noise_bias=float(drpm_peak_neg_noise_bias),
        # Band-brake torsional deformation (rad).  Builds up as carrier
        # tries to rotate against clamping force; clamped to the kinetic
        # holding level on static breakaway.  Zero at sim start (no
        # prestress) — realistic for a lever that is initially released.
        delta_band=0.0,
    )


def _make_log_arrays(n_ticks):
    """Allocate output arrays for one sample per control tick."""
    return dict(
        t=np.empty(n_ticks),
        speed=np.empty(n_ticks),
        speed_baseline=np.empty(n_ticks),
        motor_rpm=np.empty(n_ticks),
        current=np.empty(n_ticks),
        carrier_rpm=np.empty(n_ticks),
        vcap=np.empty(n_ticks),
        p_elec=np.empty(n_ticks),
        p_copper=np.empty(n_ticks),
        p_brake=np.empty(n_ticks),
        eta=np.empty(n_ticks),
        brake_demand=np.empty(n_ticks),
        locked=np.empty(n_ticks, dtype=bool),
    )


def _compute_current_command(use_strategy, controller, k_fixed,
                             delayed_rpm, v_cap_sensed, v_cap,
                             i_actual, iq_kp,
                             vesc_current_gain, flux, watt_max,
                             *,
                             drpm_mean_prev=0.0,
                             drpm_peak_neg_prev=0.0,
                             iq_mean_prev=0.0,
                             noise_rng=None,
                             rpm_noise_sigma=RPM_NOISE_SIGMA_DEFAULT,
                             iq_noise_sigma=IQ_NOISE_SIGMA_DEFAULT,
                             iq_bias=IQ_BIAS_DEFAULT,
                             vcap_noise_sigma=VCAP_NOISE_SIGMA_DEFAULT,
                             drpm_mean_noise_sigma=DRPM_MEAN_NOISE_SIGMA_DEFAULT,
                             drpm_peak_neg_noise_sigma=DRPM_PEAK_NEG_NOISE_SIGMA_DEFAULT,
                             drpm_peak_neg_noise_bias=DRPM_PEAK_NEG_NOISE_BIAS_DEFAULT):
    """Compute the current command for this control tick.

    Handles strategy dispatch, fixed-gain fallback, iq feedback,
    bus power limiting, duty saturation, and clamping.

    Fast-path telemetry uncertainty
    -------------------------------
    On hardware, the VESC LispBM script aggregates rpm/iq at 1 kHz and
    emits one 16-byte packet every 10 ms (see
    scripts/vesc_lisp_push_iq.lisp).  The Pico reads that packet at
    the start of its next 10 ms tick, so the ``drpm_*`` / ``iq_mean``
    seen by ``strategy.update`` always describe the **previous**
    window.  This matches the 1-tick lag modelled here: aggregates
    from the prior batch are fed to the next call.

    Telemetry noise (σ ≈ 0.1 rpm on mech-RPM, ~0.05 A on iq) is
    injected at the fast-path fields and — crucially — amplified on
    the derivatives to the level strategies must tolerate on hardware:

        σ(drpm_mean)     ≈ σ(rpm) · √2 / (n·dt)    ~= 14 rpm/s
        σ(drpm_peak_neg) ≈ σ(rpm) · √2 / dt        ~= 140 rpm/s
            plus a negative bias ≈ −σ·√(2·ln n)/dt  ~= −215 rpm/s
            (expected minimum of n standard normals).

    These match the orders of magnitude seen on the bench; the
    retuned slip-detector thresholds are set several σ above them.
    """
    # ── Strategy or fixed-gain ───────────────────────────────────────
    if use_strategy:
        # iq_bias is a constant-per-run sensor offset (zero-point error):
        # applied to everything the strategy sees via an iq field, the
        # same way a real offset would bias every reading.
        iq_for_strategy = i_actual * vesc_current_gain + iq_bias
        # Estimate duty from back-EMF and bus voltage (same info VESC reports)
        omega_e_est = delayed_rpm * POLE_PAIRS * _TWO_PI / 60.0
        bemf_est = flux * omega_e_est
        duty_est = bemf_est / v_cap if v_cap > 1.0 else 0.0

        # Fast-path signals from the previous batch's 10 ms window.
        # Inject realistic telemetry noise (see docstring).
        rpm_fast = delayed_rpm
        iq_mean = iq_mean_prev if iq_mean_prev != 0.0 else iq_for_strategy
        drpm_mean = drpm_mean_prev
        drpm_peak_neg = drpm_peak_neg_prev
        if noise_rng is not None:
            rpm_fast = rpm_fast + noise_rng.normal(0.0, rpm_noise_sigma)
            iq_mean = iq_mean + noise_rng.normal(0.0, iq_noise_sigma)
            drpm_mean = drpm_mean + noise_rng.normal(0.0, drpm_mean_noise_sigma)
            drpm_peak_neg = (drpm_peak_neg
                             + drpm_peak_neg_noise_bias
                             + noise_rng.normal(0.0, drpm_peak_neg_noise_sigma))
            if drpm_peak_neg > 0.0:
                # Peak-held minimum is bounded above by 0 on hardware
                # (min starts at 0, only goes more negative).
                drpm_peak_neg = 0.0
            v_cap_sensed = v_cap_sensed + noise_rng.normal(0.0, vcap_noise_sigma)

        ctx = StrategyContext(
            rpm=delayed_rpm,
            vcap=v_cap_sensed,
            dt_ctrl=CTRL_PERIOD,
            iq_actual=iq_for_strategy,
            duty_cycle=duty_est,
            input_current=0.0,  # not modelled in sim inner loop
            rpm_fast=rpm_fast,
            iq_mean=iq_mean,
            drpm_mean=drpm_mean,
            drpm_peak_neg=drpm_peak_neg,
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
                p_drg_sum, brake_val,
                inv_sub):
    """Write one sample into the log arrays."""
    logs['t'][ix] = t
    logs['speed'][ix] = state['w_ring'] * state['spd_scale']
    logs['speed_baseline'][ix] = state['w_ring_base'] * state['spd_scale']
    logs['motor_rpm'][ix] = motor_rpm
    logs['current'][ix] = state['i_actual']
    logs['carrier_rpm'][ix] = state['w_carrier'] * _RPM_SCALE
    logs['vcap'][ix] = v_cap
    logs['p_elec'][ix] = p_net_sum * inv_sub
    logs['p_copper'][ix] = pcu_esr_sum * inv_sub
    logs['p_brake'][ix] = p_brake_sum * inv_sub
    logs['brake_demand'][ix] = brake_val
    logs['locked'][ix] = abs(state['w_carrier']) < STICTION_W
    denom = logs['p_elec'][ix] + logs['p_copper'][ix] + (p_drg_sum * inv_sub) + logs['p_brake'][ix]
    logs['eta'][ix] = logs['p_elec'][ix] / denom if denom > 1.0 else 0.0
