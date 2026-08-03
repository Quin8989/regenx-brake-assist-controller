"""sim.ride_generator — Continuous stochastic bike-ride generator.

Generates a realistic ~60 s ride as per-millisecond arrays of
``(brake_torque_Nm, pedal_torque_Nm, grade_rad)``.  One ride replaces
the old basket-of-isolated-scenarios approach: every strategy is
scored against the same continuous experience a rider would have, so
scoring reflects how the controller copes with transitions
(brake → coast → pedal → brake → descent hold → …) instead of clean
ramps from rest.

Design inputs (locked in with user 2026-04-22):
  • 60 s rides, sampled at 1 ms.
  • Speeds below 5 km/h are out of scope — the regen brake is not a
    stopping brake.  The generator avoids deliberately dropping the
    rider below 5 km/h between events; downstream scoring masks any
    sub-threshold samples anyway.
  • Freewheel fully decouples the motor when the strategy is
    inactive, so pedalling costs the rider only bearing drag (treated
    as 0 Nm here) and the usual rolling + aero + grade resistances.
  • Pedal torque is positive human power at the wheel axis (no crank
    model); P-controller against a target cruise speed, clamped to a
    sustained / burst budget.
  • Riders brake harder, more often, and sometimes continuously on
    descents — grade is coupled into the brake-event sampler.

Rider profiles (sampled per ride):
    casual         — gentle, slow, few brake events
    commuter       — moderate cruise, Poisson events, mild grade
    fast_commuter  — higher cruise, firmer braking
    hilly          — wider grade excursions, grade-driven braking

Rider scoring weights (used by sim.scoring):
    casual 0.35, hilly 0.25, commuter 0.30, fast_commuter 0.10.

Brake intensities follow Beta(α,β) on [0, 1] mapped to
[2 Nm, 40 Nm] of *carrier torque* (what physics.simulate consumes).
Beta parameters were chosen so the mean lands on the "moderate urban
stop" value riders naturally use (Dozza 2015 / Shah 2020 naturalistic
cycling studies report 60-70 % of events as feathering, 25 % moderate,
5 % firm/emergency).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# ── Sampling grid ───────────────────────────────────────────────────
DT = 0.001                 # 1 ms — matches physics.DT
DEFAULT_DURATION = 60.0    # seconds

# ── Rider brake physics (carrier-torque domain) ─────────────────────
# The rest of the stack consumes *carrier torque in Nm* directly, so
# this generator skips the hand-force → lever → capstan → drum chain
# and samples the final carrier torque distribution.  Range 2-40 Nm
# spans feathering (just tickling the band so regen can ride the
# µ_s boundary at low speed) through panic (band fully clamped on,
# motor saturates, carrier locked).
BRAKE_MIN_NM = 2.0
BRAKE_MAX_NM = 40.0

# Below this wheel speed (km/h) the regen brake is out of its useful
# envelope.  Generator will not trigger *new* brake events when the
# (estimated) speed is under the cutoff, and scoring masks it too.
SPEED_CUTOFF_KMH = 5.0

# Pedal / rider power budgets.
P_RIDER_SUSTAINED_W = 150.0
P_RIDER_BURST_W     = 400.0
PEDAL_KP_NM_PER_MS  = 3.0   # P-gain: Nm per (m/s) error, at wheel axis
PEDAL_RELAX_S       = 1.5   # time constant for returning to cruise after a brake event

# Approximate wheel radius (m) — used ONLY for local rider-model
# bookkeeping (estimating speed from a kinematic proxy).  The authoritative
# physics uses physics.R_WHEEL which is the same value.
_R_WHEEL_M = 0.3302

# ── Grade Ornstein-Uhlenbeck parameters ─────────────────────────────
GRADE_TAU_S     = 30.0     # mean-reversion time
GRADE_LIMIT_RAD = math.radians(8.0)  # clamp to ±8°

# ── Brake event timing (baseline, per-profile overrides below) ──────
HOLD_LOGN_MU    = math.log(1.8)    # median ~1.8 s
HOLD_LOGN_SIGMA = 0.5
RAMP_LOGN_MU    = math.log(0.25)   # median ~0.25 s (hand tightening)
RAMP_LOGN_SIGMA = 0.4
RAMP_MIN_S      = 0.08
RAMP_MAX_S      = 0.60

# Grade-coupled sampling.
# Arrival-rate multiplier at −5° descent vs flat.  ~2× more events on a
# moderate descent.  Computed as 1 + k·max(0, −grade).
BRAKE_GRADE_K   = 12.0

# Sustained descent control: when the smoothed grade is steeper than
# this *and* persists, skip event sampling and emit a low-amplitude
# continuous drag instead.  Values chosen so only the hilly profile
# routinely triggers this.
DESCENT_HOLD_THRESH_RAD = math.radians(-3.0)
DESCENT_HOLD_MIN_S      = 3.0      # grade must be sustained this long
DESCENT_HOLD_NM         = (4.0, 10.0)  # low-drag amplitude range
DESCENT_HOLD_DURATION_S = (5.0, 15.0)  # length of one hold


# =====================================================================
# Rider profiles
# =====================================================================

@dataclass(frozen=True)
class Profile:
    """A named bundle of rider-distribution parameters.

    All stochastic ride attributes draw from these distributions;
    every ride samples a fresh value, so two rides from the same
    profile differ in cruise target, mass, grade trajectory, etc.
    """
    name: str
    cruise_kmh_mean: float
    cruise_kmh_sigma: float
    brake_rate_hz: float              # λ for the Poisson arrival process
    brake_beta_a: float               # Beta(α,β) for intensity on [0,1]
    brake_beta_b: float
    grade_sigma_rad: float            # OU steady-state σ
    mass_kg_mean: float
    mass_kg_sigma: float
    weight: float                     # scoring weight


PROFILES: dict[str, Profile] = {
    "casual": Profile(
        name="casual",
        cruise_kmh_mean=13.0, cruise_kmh_sigma=2.0,
        brake_rate_hz=1.0 / 20.0,
        brake_beta_a=1.5, brake_beta_b=6.0,
        grade_sigma_rad=math.radians(1.0),
        mass_kg_mean=80.0, mass_kg_sigma=12.0,
        weight=0.35,
    ),
    "hilly": Profile(
        name="hilly",
        cruise_kmh_mean=16.0, cruise_kmh_sigma=3.0,
        brake_rate_hz=1.0 / 8.0,
        brake_beta_a=2.5, brake_beta_b=3.0,
        grade_sigma_rad=math.radians(3.5),
        mass_kg_mean=85.0, mass_kg_sigma=12.0,
        weight=0.25,
    ),
    "commuter": Profile(
        name="commuter",
        cruise_kmh_mean=18.0, cruise_kmh_sigma=3.0,
        brake_rate_hz=1.0 / 12.0,
        brake_beta_a=1.8, brake_beta_b=4.5,
        grade_sigma_rad=math.radians(1.5),
        mass_kg_mean=85.0, mass_kg_sigma=12.0,
        weight=0.30,
    ),
    "fast_commuter": Profile(
        name="fast_commuter",
        cruise_kmh_mean=27.0, cruise_kmh_sigma=4.0,
        brake_rate_hz=1.0 / 15.0,
        brake_beta_a=2.0, brake_beta_b=3.5,
        grade_sigma_rad=math.radians(1.5),
        mass_kg_mean=85.0, mass_kg_sigma=12.0,
        weight=0.10,
    ),
}


# =====================================================================
# RideTrace dataclass
# =====================================================================

@dataclass
class RideTrace:
    """A generated 60 s bike ride ready to feed into physics.simulate.

    All arrays are 1 ms sampled, length = int(duration / DT).

    ``brake_torque``   is a carrier-torque *demand* — what the rider is
                       asking the band brake to clamp with.  Physics
                       decides how much actually reaches the wheel.
    ``pedal_active``   is a bool mask — True wherever the rider is
                       actively pedalling.  The physics-side rider
                       P-controller decides the actual applied torque
                       based on current wheel speed, cruise target,
                       and the rider's power budget.
    ``grade_rad``      is road grade (+down / −up), sampled from an
                       Ornstein-Uhlenbeck process.
    """
    dt: float
    duration: float
    brake_torque: np.ndarray     # Nm at carrier,  0 between events
    pedal_active: np.ndarray     # bool mask, 1 when rider is on pedals
    grade_rad: np.ndarray        # road grade, +down / -up
    mass_kg: float
    cruise_kmh: float            # rider's pedal-target cruise speed
    profile: str
    seed: int
    brake_windows: list[tuple[float, float]] = field(default_factory=list)
    # Predicted pedal torque at cruise speed, for visualisation only.
    # Physics.simulate_ride ignores this; it computes real torque from
    # a closed-loop rider model.
    pedal_torque_pred: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.float64))

    @property
    def n(self) -> int:
        return self.brake_torque.size


# =====================================================================
# Core generator
# =====================================================================

def generate_ride(
    profile: str | Profile,
    seed: int,
    duration: float = DEFAULT_DURATION,
    dt: float = DT,
) -> RideTrace:
    """Generate one RideTrace.  Deterministic in (profile, seed)."""
    prof = profile if isinstance(profile, Profile) else PROFILES[profile]
    rng = np.random.default_rng(seed)
    n = int(round(duration / dt))

    mass_kg    = _clip(rng.normal(prof.mass_kg_mean, prof.mass_kg_sigma), 60.0, 130.0)
    cruise_kmh = _clip(rng.normal(prof.cruise_kmh_mean, prof.cruise_kmh_sigma), 8.0, 35.0)

    grade = _sample_grade_ou(n, dt, prof.grade_sigma_rad, rng)
    brake, windows = _sample_brake_trace(n, dt, prof, grade, rng)
    pedal_active, pedal_pred = _build_pedal_trace(
        n, dt, cruise_kmh, mass_kg, grade, brake, rng)

    return RideTrace(
        dt=dt,
        duration=duration,
        brake_torque=brake,
        pedal_active=pedal_active,
        grade_rad=grade,
        mass_kg=mass_kg,
        cruise_kmh=cruise_kmh,
        profile=prof.name,
        seed=seed,
        brake_windows=windows,
        pedal_torque_pred=pedal_pred,
    )


def generate_ride_set(
    seeds_per_profile: int = 5,
    duration: float = DEFAULT_DURATION,
    base_seed: int = 0,
) -> list[RideTrace]:
    """Build the full weighted ride set used for a scoring evaluation.

    Returns one ``RideTrace`` per (profile, seed) pair.  Downstream
    scoring weights per-profile via ``PROFILES[...].weight`` and
    averages seeds within a profile.
    """
    rides: list[RideTrace] = []
    for prof_name, prof in PROFILES.items():
        for k in range(seeds_per_profile):
            # Deterministic per (profile, k) seed mixing.
            seed = _mix_seed(base_seed, prof_name, k)
            rides.append(generate_ride(prof, seed=seed, duration=duration))
    return rides


# =====================================================================
# Internal — grade OU process
# =====================================================================

def _sample_grade_ou(n: int, dt: float, sigma_ss: float,
                     rng: np.random.Generator) -> np.ndarray:
    """Ornstein-Uhlenbeck grade with steady-state σ = sigma_ss.

    Discrete update: g[k+1] = g[k]·exp(-dt/τ) + σ_step·N(0,1)
    where σ_step = σ_ss · √(1 - exp(-2·dt/τ)).
    """
    tau = GRADE_TAU_S
    a = math.exp(-dt / tau)
    step_sigma = sigma_ss * math.sqrt(max(0.0, 1.0 - a * a))
    g = np.empty(n, dtype=np.float64)
    g[0] = rng.normal(0.0, sigma_ss)
    noise = rng.normal(0.0, step_sigma, size=n - 1)
    for i in range(1, n):
        g[i] = a * g[i - 1] + noise[i - 1]
    np.clip(g, -GRADE_LIMIT_RAD, GRADE_LIMIT_RAD, out=g)
    return g


# =====================================================================
# Internal — brake trace sampling
# =====================================================================

def _sample_brake_trace(n: int, dt: float, prof: Profile,
                        grade: np.ndarray,
                        rng: np.random.Generator
                        ) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """Populate the brake-torque array and return event windows.

    Three event types:
      1. Sustained descent drag   — low-amplitude continuous hold on
         long downhills.  Triggered when smoothed grade ≤ −3° for ≥ 3 s.
      2. Poisson pulse events     — the usual short brake-to-slow
         pattern, arrival rate modulated by instantaneous grade.
      3. Speed-cutoff suppression — if the rider is (approximately)
         below 5 km/h we don't trigger a new event; in-progress events
         are allowed to end naturally.
    """
    brake = np.zeros(n, dtype=np.float64)
    windows: list[tuple[float, float]] = []

    # Kinematic proxy for rider speed used *only* to gate new events.
    # Full physics determines actual speed during scoring.  Initialise
    # at the profile's mean cruise and decay toward it with each
    # event (crude but good enough for gating).
    speed_kmh = prof.cruise_kmh_mean

    # Smoothed grade over ~1.5 s for descent-hold detection.
    smooth_tau = 1.5
    smooth_a = math.exp(-dt / smooth_tau)
    grade_smooth = 0.0

    # Walk the timeline.  At each ms, ask: am I in a descent-hold? In an
    # event? If neither, roll Bernoulli(λ(grade)·dt) for a new event.
    descent_hold_end = -1
    event_end = -1

    i = 0
    while i < n:
        grade_smooth = smooth_a * grade_smooth + (1.0 - smooth_a) * grade[i]
        t = i * dt

        if i < descent_hold_end or i < event_end:
            # Already inside a brake event; trace was pre-filled when
            # the event was scheduled.  Update speed estimate assuming
            # the rider is decelerating at a mild rate (1 m/s² average
            # for gating purposes) and don't go below cutoff.
            # NOTE: the kinematic decay here is a coarse proxy; the
            # physics sim will determine the real trajectory.
            speed_kmh = max(SPEED_CUTOFF_KMH, speed_kmh - 1.0 * 3.6 * dt)
            i += 1
            continue

        # Descent-hold check (takes priority over pulse events).
        if grade_smooth <= DESCENT_HOLD_THRESH_RAD:
            # Confirm persistence: look back DESCENT_HOLD_MIN_S.
            lookback = int(DESCENT_HOLD_MIN_S / dt)
            if i >= lookback:
                if np.all(grade[i - lookback:i] <= DESCENT_HOLD_THRESH_RAD * 0.7):
                    duration_s = rng.uniform(*DESCENT_HOLD_DURATION_S)
                    amp_nm = rng.uniform(*DESCENT_HOLD_NM)
                    end_i = min(n, i + int(duration_s / dt))
                    ramp_i = int(rng.uniform(0.15, 0.35) / dt)
                    _paint_trapezoid(brake, i, end_i, ramp_i, amp_nm)
                    windows.append((t, end_i * dt))
                    descent_hold_end = end_i
                    # Rider will leak speed on descent faster.
                    speed_kmh = max(SPEED_CUTOFF_KMH, speed_kmh)
                    i = end_i
                    continue

        # Speed gate: no new pulse events if the rider is too slow.
        if speed_kmh < SPEED_CUTOFF_KMH + 1.0:
            # Let the rider rebuild speed via pedalling before we brake
            # again.  Skip ahead a short interval.
            speed_kmh += 5.0 * dt  # ~5 km/h/s recovery, coarse
            i += 1
            continue

        # Grade-modulated arrival rate.  Negative grade (descent) ups λ.
        lam = prof.brake_rate_hz * (1.0 + BRAKE_GRADE_K * max(0.0, -grade[i]))
        if rng.random() < lam * dt:
            hold_s = float(np.clip(
                rng.lognormal(HOLD_LOGN_MU, HOLD_LOGN_SIGMA), 0.3, 8.0))
            ramp_s = float(np.clip(
                rng.lognormal(RAMP_LOGN_MU, RAMP_LOGN_SIGMA),
                RAMP_MIN_S, RAMP_MAX_S))
            amp_nm = _sample_brake_amplitude(prof, grade[i], rng)

            ramp_i = int(ramp_s / dt)
            total_i = int((hold_s + 2.0 * ramp_s) / dt)
            end_i = min(n, i + total_i)
            _paint_trapezoid(brake, i, end_i, ramp_i, amp_nm)
            windows.append((t, end_i * dt))
            event_end = end_i
            # Rough speed drop: τ · hold / (m · R) in m/s, converted.
            speed_kmh = max(SPEED_CUTOFF_KMH,
                            speed_kmh - (amp_nm * hold_s) / 15.0)
            i = end_i
            continue

        i += 1

    return brake, windows


def _sample_brake_amplitude(prof: Profile, grade_rad: float,
                            rng: np.random.Generator) -> float:
    """Draw a single brake event's peak torque (Nm)."""
    u = rng.beta(prof.brake_beta_a, prof.brake_beta_b)
    # Descent bias: shift u upward on downhills (gravity needs braking).
    if grade_rad < 0.0:
        bias = min(0.35, 7.0 * -grade_rad)   # ~0.35 at −3° → saturates
        u = u + (1.0 - u) * bias
    return BRAKE_MIN_NM + u * (BRAKE_MAX_NM - BRAKE_MIN_NM)


def _paint_trapezoid(arr: np.ndarray, start: int, end: int,
                     ramp: int, amplitude: float) -> None:
    """Write a trapezoidal pulse: linear up, plateau, linear down."""
    n = end - start
    if n <= 0 or amplitude <= 0.0:
        return
    ramp = min(ramp, n // 2)
    if ramp <= 0:
        arr[start:end] = amplitude
        return
    # Up-ramp.
    up = np.linspace(amplitude / ramp, amplitude, ramp, endpoint=True)
    arr[start:start + ramp] = up
    # Plateau.
    plateau_end = end - ramp
    if plateau_end > start + ramp:
        arr[start + ramp:plateau_end] = amplitude
    # Down-ramp.
    down = np.linspace(amplitude, amplitude / ramp, ramp, endpoint=True)
    arr[plateau_end:plateau_end + ramp] = down


# =====================================================================
# Internal — pedal trace
# =====================================================================

def _build_pedal_trace(n: int, dt: float, cruise_kmh: float, mass_kg: float,
                       grade: np.ndarray, brake: np.ndarray,
                       rng: np.random.Generator
                       ) -> tuple[np.ndarray, np.ndarray]:
    """Compute (pedal_active, pedal_torque_pred).

    ``pedal_active`` is the bool mask physics actually consumes — True
    where the rider is on the pedals (not braking, not coasting down
    a descent) and thus expects the in-sim P-controller to apply
    positive torque.

    ``pedal_torque_pred`` is the torque the rider would need at cruise
    speed under the current grade, capped to the rider's burst power
    budget.  Used by ride_demo for visualisation; physics ignores it.
    """
    active = np.zeros(n, dtype=bool)
    pred = np.zeros(n, dtype=np.float64)
    g = 9.81
    c_rr = 0.008
    c_aero = 0.28 * 0.509

    v_cruise = cruise_kmh / 3.6

    for i in range(n):
        if brake[i] > 0.0:
            # Off the pedals during braking.
            continue
        gr = grade[i]
        f_roll = c_rr * mass_kg * g * math.cos(gr)
        f_aero = c_aero * v_cruise * v_cruise
        f_grade = mass_kg * g * math.sin(gr)
        p_req = (f_roll + f_aero + f_grade) * v_cruise
        if p_req <= 0.0:
            # Rider coasts on descents.
            continue
        active[i] = True
        p_applied = min(p_req, P_RIDER_BURST_W)
        if p_applied > P_RIDER_SUSTAINED_W:
            excess = (p_applied - P_RIDER_SUSTAINED_W) / (
                P_RIDER_BURST_W - P_RIDER_SUSTAINED_W)
            p_applied *= 1.0 - 0.2 * excess
        pred[i] = p_applied / max(1.0, v_cruise)

    # Small multiplicative cadence ripple (±3 %) at ~1.2 Hz (72 rpm).
    t = np.arange(n) * dt
    ripple = 1.0 + 0.03 * np.sin(2.0 * math.pi * 1.2 * t + rng.uniform(0, 2 * math.pi))
    pred *= ripple

    # Post-brake ramp-back: after every brake window the rider takes
    # ~PEDAL_RELAX_S seconds to remount the pedals.  Apply a sigmoidal
    # gate in the prediction (physics applies its own ramp via the
    # rider P-controller since it tracks real wheel speed).
    brake_active = brake > 0.0
    if np.any(brake_active):
        falling_edges = np.where(np.diff(brake_active.astype(np.int8)) < 0)[0] + 1
        tau_i = PEDAL_RELAX_S / dt
        for edge in falling_edges:
            end = min(n, edge + int(5.0 * tau_i))
            k = np.arange(end - edge)
            gate = 1.0 - np.exp(-k / tau_i)
            pred[edge:end] *= gate

    return active, pred


# =====================================================================
# Internal — small utilities
# =====================================================================

def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _mix_seed(base: int, profile_name: str, k: int) -> int:
    """Deterministically fold (base, profile, k) into a 31-bit seed."""
    h = hash((int(base), profile_name, int(k))) & 0x7FFFFFFF
    return int(h)


# =====================================================================
# CLI — quick smoke test
# =====================================================================

if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="commuter", choices=list(PROFILES))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    args = ap.parse_args()

    ride = generate_ride(args.profile, args.seed, args.duration)
    print(f"profile       : {ride.profile}")
    print(f"duration      : {ride.duration:.1f} s")
    print(f"samples       : {ride.n}")
    print(f"mass          : {ride.mass_kg:.1f} kg")
    print(f"cruise target : {ride.cruise_kmh:.1f} km/h")
    print(f"brake events  : {len(ride.brake_windows)}")
    if ride.brake_windows:
        active_s = sum(e - s for s, e in ride.brake_windows)
        print(f"brake active  : {active_s:.1f} s "
              f"({100.0 * active_s / ride.duration:.0f} %)")
    print(f"peak brake    : {ride.brake_torque.max():.1f} Nm")
    print(f"grade range   : "
          f"{math.degrees(ride.grade_rad.min()):+.1f}° "
          f"to {math.degrees(ride.grade_rad.max()):+.1f}°")
    active_frac = ride.pedal_active.mean()
    print(f"pedal active  : {100.0 * active_frac:.0f} % of ride")
    if ride.pedal_torque_pred.size:
        mean_on = ride.pedal_torque_pred[ride.pedal_active].mean() if active_frac > 0 else 0.0
        print(f"pedal τ (pred): mean={mean_on:.1f} Nm  peak={ride.pedal_torque_pred.max():.1f} Nm")
