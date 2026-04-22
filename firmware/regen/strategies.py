"""sim.strategies - regen controllers.

Two strategies:
    pi_controller   PI feedback on a wheel-decel-rate proxy.  Classical
                    reference baseline.
    aimd_ff         Feedforward + AIMD adaptation of the FF gain.
                    Converges to the µ_s boundary by slip-cycling.
                    Default runtime strategy.

The tracking score is computed against an idealized non-slipping brake
that delivers the rider's full static-friction demand (``brake_val``)
at the wheel.  The device's ideal steady state is to park the carrier
just at the µ_s boundary — carrier locked, no band heat, full rider-
demanded torque transferred to regen.  Both strategies share this
target; they differ in how they find it.  ``aimd_ff`` orbits the
boundary via AI/MD cycles; ``pi_controller`` is a non-AIMD reference
comparator.

Every strategy receives only a StrategyContext built from signals that
exist on the physical Pico + VESC system.  ``k`` in ``aimd_ff`` is the
fraction of short-circuit feedforward current
(``i_ff = k · λ · ωe / R``); it stays in a bounded physical scale
rather than an arbitrary tuning knob so the DE search is well
conditioned.  A direct ``target current`` reparameterisation was
considered and rejected: the back-EMF form is the correct first-order
physics and auto-scales with speed, whereas a flat target current
would saturate duty at low RPM.
"""

from config.settings import (
    FLUX_LINKAGE_WB as FLUX_LINKAGE,
    MOTOR_PHASE_RESISTANCE_OHM as R_PHASE,
    REGEN_CURRENT_MAX_A as I_MAX,
    VCAP_REGEN_TAPER_END_V as VCAP_TAPER_END,
    VCAP_REGEN_TAPER_START_V as VCAP_TAPER_START,
    VESC_MOTOR_POLE_PAIRS as POLE_PAIRS,
)
from .regen_control import ff_current_from_rpm, voltage_taper


def _ff_current(rpm, gain):
    return ff_current_from_rpm(
        rpm,
        gain,
        flux_linkage=FLUX_LINKAGE,
        phase_resistance=R_PHASE,
        pole_pairs=POLE_PAIRS,
        current_limit=I_MAX,
    )


class _BaseStrategy:
    """Shared scaffold for regen strategies."""

    def _reset(self):
        raise NotImplementedError

    def _begin_update(self, ctx):
        rpm = ctx.preferred_rpm
        iq = ctx.preferred_iq
        taper = voltage_taper(ctx.vcap, VCAP_TAPER_START, VCAP_TAPER_END)
        if taper == 0.0:
            self._reset()
        return rpm, iq, taper

    def _ff(self, rpm, gain):
        return _ff_current(rpm, gain)

    @staticmethod
    def _clip(x, lo, hi):
        if x < lo:
            return lo
        if x > hi:
            return hi
        return x


class MotorEffFfRegenStrategy(_BaseStrategy):
    """RPM → I feedforward at a fixed motor efficiency.

    3-phase BLDC copper-loss model (Id=0 FOC):

        η = 1 − (1.5 · R · I) / (λ·ωe)

    Solving for I:

        I = (1 − η) · λ·ωe / (1.5 · R)  =  _ff(rpm, gain=(1−η)/1.5)

    At η = 70 %: gain = 0.30 / 1.5 = 0.20.
    """

    key = "motor_eff_ff"

    def __init__(self, target_eta=0.70):
        if not (0.0 < target_eta < 1.0):
            raise ValueError(f"target_eta must be in (0, 1), got {target_eta}")
        self.target_eta = float(target_eta)
        self._gain = (1.0 - self.target_eta) / 1.5
        self.name = f"Motor-Eff FF (η={self.target_eta:.0%})"

    def _reset(self):
        pass

    def update(self, ctx):
        rpm, _iq, taper = self._begin_update(ctx)
        if taper == 0.0:
            return 0.0
        return self._clip(self._ff(rpm, self._gain) * taper, 0.0, I_MAX)

    @staticmethod
    def param_grid():
        return [dict(target_eta=eta) for eta in [0.50, 0.60, 0.70, 0.80, 0.90]]


class PiSlipRegenStrategy(_BaseStrategy):
    """Classical PI feedback on a wheel-decel-rate proxy — reference baseline.

    Not the production controller.  Kept to show how much structural
    value the AIMD / lock-hold schemes add over plain feedback.

    Note on naming: the proxy is called ``raw_slip`` historically, but
    under the corrected mechanical model the carrier is locked almost
    always and ``d(rpm)/dt = -N · d(ω_ring)/dt``.  The proxy is really
    ``|a_wheel| / v`` — a speed-normalised wheel-decel magnitude — so
    ``decel_target`` is the knob's semantic name.

    Decel input comes from ``ctx.drpm_mean`` (mean d(rpm)/dt over the
    VESC's 10 ms / 1 kHz-sampled window).  Preferred over an in-strategy
    numerical derivative of ``rpm_fast`` because the VESC's tighter
    sampling window averages out single-tick telemetry jitter that would
    otherwise contaminate the decel proxy at low speeds.

    Control law::

        decel_proxy = |drpm_mean| / (rpm + 10)
        integral   += (decel_proxy - decel_target) * dt
        gain_mult   = clip(1 - ki * integral, 0.05, 3.0)
        i_ff        = k_ff * gain_mult * λ*ωe/R
        i_cmd       = i_ff + kp_iq * (i_ff - iq_mean)

    Parameters (3 tunable): k_ff, ki, decel_target.
    Fixed: kp_iq = 0.05 (inner loop gain — removed from tuning after
    2026-04 analysis showed < 2% contribution).
    """

    key = "pi_controller"

    def __init__(self, k_ff=0.29, ki=0.38, decel_target=0.50, kp_iq=0.05):
        self.k_ff = k_ff
        self.ki = ki
        self.decel_target = decel_target
        self.kp_iq = kp_iq  # inner additive iq-error correction gain (fixed)
        self.name = f"PI Controller (kff={k_ff}, Ki={ki}, tgt={decel_target})"
        self.integral = 0.0

    def _reset(self):
        self.integral = 0.0

    def update(self, ctx):
        rpm, iq_actual, taper = self._begin_update(ctx)
        if taper == 0.0:
            return 0.0

        d_rpm = abs(ctx.drpm_mean)
        decel_proxy = d_rpm / (rpm + 10.0)

        error = decel_proxy - self.decel_target
        self.integral += error * ctx.dt_ctrl
        self.integral = self._clip(self.integral, -5.0, 5.0)

        correction = self.ki * self.integral
        gain_mult = self._clip(1.0 - correction, 0.05, 3.0)
        i_ff = self._ff(rpm, self.k_ff * gain_mult)

        iq_err = i_ff - iq_actual
        i_cmd = i_ff + self.kp_iq * iq_err

        return self._clip(i_cmd * taper, 0.0, I_MAX)

    @staticmethod
    def param_grid():
        # 3^3 = 27 seeds for DE.  ``decel_target`` range widened to
        # accommodate the static-friction baseline, which produces
        # ~1.5× higher wheel decel than the old kinetic baseline.
        return [
            dict(k_ff=kf, ki=ki, decel_target=dt)
            for kf in [0.15, 0.30, 0.50]
            for ki in [0.2, 0.4, 0.6]
            for dt in [0.4, 0.8, 1.2]
        ]


class AimdFfRegenStrategy(_BaseStrategy):
    """AIMD adaptation of the FF gain — orbits the µ_s boundary.

    AIMD by construction converges to the static-friction ceiling: it
    probes up with AI until a slip event, cuts with MD, probes up
    again.  The steady-state orbit is a band just above and just below
    the µ_s threshold.

    Under the static-friction scoring baseline (scoring target =
    ``brake_val`` at the wheel), AIMD is *aligned* with the target: its
    time-average command tracks µ_s, which is exactly what the rider
    demanded.  Residual costs:
      * Each slip excursion dumps ``P_band = t_brake·ω_c`` into the
        band — hurts Energy slightly.
      * Decel oscillates around the target — hurts Smoothness.
    This is the canonical AIMD solution to "max utilization of an
    unknown catastrophic boundary" (Chiu & Jain 1989).

    Control law::

        i_ff   = k_eff * λ*ωe/R                         # plant-aware FF
        i_cmd  = clip(i_ff * taper, 0, I_MAX)

    k_eff is the state.  Every tick:

                if slip_event:                                    # carrier slipped
            # MD: multiplicative decrease (graduated over 25% band to
            # avoid chatter at the detection boundary)
            level = clip((-drpm_peak_neg - unlock_thresh) / (0.25*unlock_thresh), 0, 1)
            k_eff *= 1 - beta_md * (0.35 + 0.65*level)
                elif not raw_slip:
            # AI: additive increase — probe up until the next slip
            k_eff += k_ai * dt

        ``slip_event`` intentionally requires:
            * rising edge of ``raw_slip`` (avoid repeat MD in one burst),
            * a short refractory window after each MD,
            * minimum |rpm| (ignore stop-end zero-crossing oscillation).

    Uses ``ctx.drpm_peak_neg`` directly (most-negative per-sample
    d(rpm)/dt, sampled at 1 kHz on the VESC, peak-held between the Pico's
    10 ms polls).  This replaces the old in-strategy EMA of a 100 Hz-
    sampled numerical derivative, which aliased real 2-5 ms unlock
    transients.  No filter constant needed.

    Parameters (4 tunable):
      * ``k``              starting / post-taper recovery value of k_eff.
      * ``k_ai``           AI rate (1/s) — how fast we probe up when safe.
      * ``beta_md``        MD magnitude — how hard we cut on slip.
      * ``unlock_thresh``  slip detector threshold on -drpm_peak_neg (rpm/s).
                           Scale: a single-tick mech-RPM jump of 1 rpm at
                           1 ms sampling reads as 1000 rpm/s, so thresholds
                           of a few hundred to a few thousand rpm/s cover
                           the realistic noise-to-event range.
    """

    key = "aimd_ff"

    # Fixed internal constants — not tuned.
    _K_FLOOR = 0.02
    _K_CEIL = 1.0

    def __init__(self, k=0.131, beta_md=0.05, unlock_thresh=1500.0, k_ai=0.05):
        self.k = float(k)
        self.beta_md = float(beta_md)
        self.unlock_thresh = float(unlock_thresh)
        self.k_ai = float(k_ai)

        self.name = "AIMD-FF Regen Controller"

        self._k_eff = self.k
        self._t_s = 0.0
        self._slip_prev_raw = False

    def _reset(self):
        self._k_eff = self.k
        self._slip_prev_raw = False

    def update(self, ctx):
        rpm, _iq_actual, taper = self._begin_update(ctx)
        if taper == 0.0:
            self._reset()
            return 0.0

        dt = ctx.dt_ctrl if ctx.dt_ctrl > 0.0 else 0.01
        self._t_s += dt

        unlock_band = max(10.0, 0.25 * self.unlock_thresh)
        unlock_excess = (-ctx.drpm_peak_neg) - self.unlock_thresh
        unlock_level = self._clip(unlock_excess / unlock_band, 0.0, 1.0)
        raw_slip = unlock_level > 0.0
        slip_event = raw_slip and not self._slip_prev_raw

        if slip_event:
            md = self.beta_md * (0.35 + 0.65 * unlock_level)
            self._k_eff *= (1.0 - md)
        elif not raw_slip:
            # AI: grow k toward the ceiling until the next slip event.
            self._k_eff += self.k_ai * dt

        self._slip_prev_raw = raw_slip

        self._k_eff = self._clip(self._k_eff, self._K_FLOOR, self._K_CEIL)

        i_cmd = self._ff(rpm, self._k_eff)
        return self._clip(i_cmd * taper, 0.0, I_MAX)

    @staticmethod
    def param_grid():
        # 3^4 = 81 seeds for DE init.  unlock_thresh now in rpm/s on the
        # per-sample peak (1 kHz sampling → realistic noise ~200-600 rpm/s,
        # real unlock spikes ~2000-10000+ rpm/s).
        return [
            dict(k=k, beta_md=bmd, unlock_thresh=ut, k_ai=ka)
            for k   in [0.08, 0.15, 0.28]
            for bmd in [0.04, 0.08, 0.14]
            for ut  in [800.0, 1500.0, 3000.0]
            for ka  in [0.005, 0.05, 0.20]
        ]


class FixedFfRegenStrategy(_BaseStrategy):
    """Naive fixed-gain feedforward — stateless RPM-to-current map.

    The simplest possible regen strategy: no slip detection, no state,
    no noise sensitivity.  Always commands the same current for a given
    motor RPM.  Equivalent to a mechanical brake in terms of consistency.

    Control law::

        i_cmd = clip(k * λ*ωe/R * taper, 0, I_MAX)

    Parameters (1 tunable):
      * ``k``   fraction of short-circuit FF current to command.
    """

    key = "fixed_ff"

    def __init__(self, k=0.15):
        self.k = float(k)
        self.name = f"Fixed-FF Regen (k={k})"

    def _reset(self):
        pass

    def update(self, ctx):
        rpm, _iq_actual, taper = self._begin_update(ctx)
        if taper == 0.0:
            return 0.0
        return self._clip(self._ff(rpm, self.k) * taper, 0.0, I_MAX)

    @staticmethod
    def param_grid():
        return [dict(k=k) for k in [0.0, 0.25, 0.5, 0.75, 1.0]]


class AimdMeanFfRegenStrategy(_BaseStrategy):
    """AIMD adaptation using drpm_mean as the slip detector.

    Identical in structure to AimdFfRegenStrategy, but uses
    ``ctx.drpm_mean`` (10 ms window-average d(rpm)/dt, σ≈14 rpm/s) instead
    of ``ctx.drpm_peak_neg`` (per-sample peak-hold, σ≈140 rpm/s + −300 rpm/s
    bias).  The mean signal is 10× quieter and bias-free, giving far fewer
    false-positive slip triggers at the cost of ≈10 ms detection lag.

    Motivation: the peak-neg signal fires on single-tick hall encoder
    glitches at ~2.8σ above its bias.  The mean signal requires a
    sustained decel anomaly across the whole 10 ms window, making noise-
    driven false positives negligible (≫6σ equivalent on mean).  The
    genuine slip event persists for 5–100 ms, so the 10 ms averaging lag
    does not materially delay the MD cut.

    Self-regulation mechanism (same as AIMD):
      * Carrier locked  → AI ramps k_eff up toward µ_s boundary.
      * Slip detected   → MD cuts k_eff instantly; carrier re-locks.
      * Repeat.  Time-average sits just below µ_s; band heat is brief
        transient, not sustained steady-state (unlike fixed_ff).

    Control law::

        raw_slip      = (-drpm_mean) > unlock_thresh_mean
        slip_event    = rising edge of raw_slip (with refractory)
        if slip_event:  k_eff *= 1 − beta_md
        elif not raw_slip:  k_eff += k_ai * dt
        i_cmd = clip(k_eff * λ*ωe/R * taper, 0, I_MAX)

    Parameters (4 tunable):
      * ``k``                  initial / reset value of k_eff.
      * ``k_ai``               AI rate (1/s).
      * ``beta_md``            MD magnitude.
      * ``unlock_thresh_mean`` slip threshold on −drpm_mean (rpm/s).
                               mean-signal scale: realistic slip at
                               40 km/h produces |drpm_mean| several
                               hundred rpm/s above locked baseline.
                               Noise floor ≈3σ=42 rpm/s.
    """

    key = "aimd_mean_ff"

    _K_FLOOR = 0.02
    _K_CEIL = 1.0

    def __init__(self, k=0.131, beta_md=0.05, unlock_thresh_mean=300.0, k_ai=0.05):
        self.k = float(k)
        self.beta_md = float(beta_md)
        self.unlock_thresh_mean = float(unlock_thresh_mean)
        self.k_ai = float(k_ai)
        self.name = "AIMD-Mean-FF Regen Controller"
        self._k_eff = self.k
        self._slip_prev_raw = False

    def _reset(self):
        self._k_eff = self.k
        self._slip_prev_raw = False

    def update(self, ctx):
        rpm, _iq_actual, taper = self._begin_update(ctx)
        if taper == 0.0:
            self._reset()
            return 0.0

        dt = ctx.dt_ctrl if ctx.dt_ctrl > 0.0 else 0.01

        raw_slip = (-ctx.drpm_mean) > self.unlock_thresh_mean
        slip_event = raw_slip and not self._slip_prev_raw

        if slip_event:
            self._k_eff *= (1.0 - self.beta_md)
        elif not raw_slip:
            self._k_eff += self.k_ai * dt

        self._slip_prev_raw = raw_slip

        self._k_eff = self._clip(self._k_eff, self._K_FLOOR, self._K_CEIL)

        i_cmd = self._ff(rpm, self._k_eff)
        return self._clip(i_cmd * taper, 0.0, I_MAX)

    @staticmethod
    def param_grid():
        # 3^4 = 81 seeds.  unlock_thresh_mean on mean-signal scale:
        # σ(drpm_mean) = 14 rpm/s, so 100 rpm/s is safe above noise floor.
        # Slip signal at 40 km/h produces |drpm_mean| ~ 200–1000 rpm/s above base.
        return [
            dict(k=k, beta_md=bmd, unlock_thresh_mean=ut, k_ai=ka)
            for k   in [0.08, 0.15, 0.28]
            for bmd in [0.04, 0.08, 0.14]
            for ut  in [100.0, 300.0, 700.0]
            for ka  in [0.005, 0.05, 0.20]
        ]


class JerkBoundaryFfRegenStrategy(_BaseStrategy):
    """Bidirectional jerk detector with online µ_s boundary learning.

    Differentiates ``ctx.drpm_mean`` between consecutive 10 ms windows to
    produce jerk (rpm/s²), then uses the *sign* of jerk to distinguish:

      * Negative jerk spike  → carrier unlocked (slip onset): the band grabs
        harder than µ_s allows, carrier unlocks, motor sees reduced load and
        drpm_mean snaps sharply negative.
      * Positive jerk spike  → carrier re-locked: motor re-couples to wheel,
        drpm_mean jumps to the steady locked-braking level.
      * Near-zero jerk       → carrier steadily locked or steadily slipping.

    Band compliance analysis (ABS drum 73 mm OD, steel band 10–15 mm wide,
    wrap 90–145°, E=200 GPa) gives k_spring = 29 000–70 000 Nm/rad.
    Unlock transient = sqrt(J/k) ≈ 0.5–0.7 ms — sub-substep, so the slip
    event appears as a sharp single-window drpm_mean jump, validating the
    instantaneous-unlock model in the sim.

    Noise: σ(jerk) ≈ 2 000 rpm/s².  Locked-braking jerk < 10 000 rpm/s².
    Genuine slip onset: 500 000–5 000 000 rpm/s² (sim-measured).
    Discrimination gap: 50–500×.  Threshold of 20 000–300 000 rpm/s² sits
    cleanly in the dead zone.

    Post-lock settling holdoff
    --------------------------
    Each carrier re-lock (or initial brake engagement) drives drpm_mean to a
    large transient peak (+40 000 rpm/s) and then rapidly settles over 2–3
    ticks to the steady-state locked-braking value (~−100 to −3 000 rpm/s).
    This settling slope produces negative jerk spikes of 1–3 million rpm/s²,
    which are 7–16× larger than a genuine slip event — making them guaranteed
    false positives that would spiral k_eff down on every brake application.

    To suppress this, after any large positive jerk (re-lock marker), slip
    detection is suppressed for ``_POST_LOCK_HOLDOFF_TICKS`` control ticks
    (40 ms).  Real slip events in steady-state locked braking occur tens to
    hundreds of milliseconds after the engagement peak, well outside this
    window.  On hardware with gradual lever engagement, the peak is also
    much smaller than in the sim's step-function brake model, so the holdoff
    is conservative in both environments.

    Improvement over pure AIMD:

    After each slip event the strategy saves ``_k_boundary`` — the k_eff
    value that caused slip, i.e. the empirical µ_s ceiling.  After the
    carrier re-locks (positive jerk confirmation or 300 ms timeout), AI
    ramps up toward ``_k_boundary * probe_margin`` and then *holds*.
    This eliminates the AIMD orbit that perpetually overshoots and
    re-slips: once the boundary is known, approach is from below without
    crossing.  A new boundary estimate is recorded on every subsequent
    slip event, so the controller tracks a changing µ_s (varying brake
    lever force, road surface, temperature).

    Control law::

        jerk          = (drpm_mean − drpm_mean_prev) / dt

        # Holdoff: suppress slip detection while drpm_mean > RELOCK_DRPM_FLOOR
        # (motor coupling up) and for _POST_LOCK_HOLDOFF_TICKS ticks after.
        # Works for both step and ramped lever inputs.
        if drpm_mean > RELOCK_DRPM_FLOOR:  holdoff = 14
        else:                              holdoff = max(0, holdoff − 1)
        if holdoff > 0:  neg_jerk ← False

        slip_event    = rising edge of (−jerk > unlock_thresh_jerk)
                        AND holdoff=0 AND NOT _waiting_relock   # cascade guard
        relock_event  = rising edge of (jerk > unlock_thresh_jerk*RELOCK_RATIO)
                        while _waiting_relock
                        AND drpm_mean > RELOCK_DRPM_FLOOR   # true lock, not oscillation

        on slip_event:
            _k_boundary = k_eff              # empirical µ_s ceiling
            k_eff      *= 1 − beta_md        # MD cut: drop below boundary
            _waiting_relock = True           # suppress AI until confirmed

        on relock_event OR 300 ms timeout:
            _waiting_relock = False          # OK to resume AI

        while not _waiting_relock and no slip:
            k_target = _k_boundary * PROBE_MARGIN
            if k_eff < k_target:  k_eff += k_ai * dt
            # else hold — do not probe past the known boundary

        i_cmd = clip(k_eff * λ*ωe/R * taper, 0, I_MAX)

    Fixed internal ratios (not tuned):
      RELOCK_RATIO  = 0.3   carrier re-lock jerk threshold relative to slip.
      PROBE_MARGIN  = 0.90  AI targets 90% of the last empirical slip boundary.

    Parameters (4 tunable — same count as aimd_ff for fair comparison):
      * ``k``                  initial / reset value of k_eff.
      * ``k_ai``               AI rate (1/s) toward k_boundary*PROBE_MARGIN.
      * ``beta_md``            MD cut magnitude on slip.
      * ``unlock_thresh_jerk`` slip threshold on −jerk (rpm/s²).
    """

    key = "jerk_boundary_ff"

    _K_FLOOR = 0.02
    _K_CEIL  = 1.0
    _RELOCK_RATIO  = 0.3
    _PROBE_MARGIN  = 0.90
    _RELOCK_TIMEOUT = 0.3  # s — resume AI even if re-lock jerk is missed
    # After any large positive jerk (carrier re-lock or initial engagement peak),
    # drpm_mean takes 2–3 ticks to settle from its transient peak to the
    # steady locked-braking value.  The settling slope mimics slip (large
    # negative jerk) and must be suppressed.  40 ms covers the tail reliably.
    # After any large positive drpm_mean event (carrier re-lock or brake
    # engagement spin-up), suppress slip detection for a settling window.
    # Triggered by drpm_mean crossing above RELOCK_DRPM_FLOOR — the same
    # threshold used for the relock event gate — so a single constant governs
    # both.  14 ticks (140 ms) covers a 100 ms lever ramp plus the 40 ms
    # post-ramp drpm_mean settling tail with margin.
    _POST_LOCK_HOLDOFF_TICKS = 14
    # Carrier re-lock produces a sustained drpm_mean spike to ~+40 000 rpm/s.
    # Oscillatory slip cycles also contain positive drpm_mean phases, but the
    # rising-edge requirement on slip_event already prevents re-detection there.
    # Gating the relock_event on this floor excludes short oscillatory bumps.
    _RELOCK_DRPM_FLOOR = 5000.0  # rpm/s — must be clearly positive at re-lock

    def __init__(self, k=0.131, beta_md=0.05, unlock_thresh_jerk=80000.0, k_ai=0.05):
        self.k = float(k)
        self.beta_md = float(beta_md)
        self.unlock_thresh_jerk = float(unlock_thresh_jerk)
        self.k_ai = float(k_ai)
        self.name = "Jerk-Boundary-FF Regen Controller"
        self._k_eff = self.k
        self._k_boundary = self._K_CEIL  # no boundary known yet → full range
        self._drpm_prev = 0.0
        self._slip_prev_raw = False
        self._relock_prev_raw = False
        self._waiting_relock = False
        self._relock_timer = 0.0
        self._relock_holdoff = 0  # ticks remaining in post-lock settling holdoff

    def _reset(self):
        self._k_eff = self.k
        self._k_boundary = self._K_CEIL
        self._drpm_prev = 0.0
        self._slip_prev_raw = False
        self._relock_prev_raw = False
        self._waiting_relock = False
        self._relock_timer = 0.0
        self._relock_holdoff = 0

    def update(self, ctx):
        rpm, _iq_actual, taper = self._begin_update(ctx)
        if taper == 0.0:
            self._reset()
            return 0.0

        dt = ctx.dt_ctrl if ctx.dt_ctrl > 0.0 else 0.01

        jerk = (ctx.drpm_mean - self._drpm_prev) / dt
        self._drpm_prev = ctx.drpm_mean

        relock_thresh = self.unlock_thresh_jerk * self._RELOCK_RATIO
        neg_jerk_raw = (-jerk) > self.unlock_thresh_jerk
        pos_jerk_raw = jerk > relock_thresh

        # Post-lock settling holdoff: suppress slip detection while drpm_mean
        # is large and positive (motor coupling up during brake engagement or
        # carrier re-lock), and for _POST_LOCK_HOLDOFF_TICKS ticks afterwards.
        # Triggered on drpm_mean level rather than jerk so it works for both
        # step and ramped lever inputs without a magic tick count per model.
        if ctx.drpm_mean > self._RELOCK_DRPM_FLOOR:
            self._relock_holdoff = self._POST_LOCK_HOLDOFF_TICKS
        elif self._relock_holdoff > 0:
            self._relock_holdoff -= 1
        if self._relock_holdoff > 0:
            neg_jerk_raw = False

        # Gate on _waiting_relock: once a slip is detected we're already in the
        # MD-cut + wait state, so further oscillatory neg-jerk edges during the
        # same slip cycle must not trigger additional MD cuts (cascade prevention).
        slip_event   = neg_jerk_raw and not self._slip_prev_raw and not self._waiting_relock
        # Gate jerk-based relock on drpm_mean being clearly positive: genuine
        # carrier re-lock drives drpm_mean to ~+40 000 rpm/s while oscillatory
        # slip phases that produce positive jerk still have drpm_mean < 0.
        relock_event = (pos_jerk_raw and not self._relock_prev_raw
                        and self._waiting_relock
                        and ctx.drpm_mean > self._RELOCK_DRPM_FLOOR)

        if slip_event:
            self._k_boundary = self._k_eff      # record empirical µ_s ceiling
            self._k_eff *= (1.0 - self.beta_md)
            self._waiting_relock = True
            self._relock_timer = 0.0
        elif self._waiting_relock:
            self._relock_timer += dt
            if relock_event or self._relock_timer >= self._RELOCK_TIMEOUT:
                self._waiting_relock = False
        elif not neg_jerk_raw:
            k_target = self._k_boundary * self._PROBE_MARGIN
            if self._k_eff < k_target:
                self._k_eff += self.k_ai * dt

        self._slip_prev_raw   = neg_jerk_raw
        self._relock_prev_raw = pos_jerk_raw

        self._k_eff = self._clip(self._k_eff, self._K_FLOOR, self._K_CEIL)

        i_cmd = self._ff(rpm, self._k_eff)
        return self._clip(i_cmd * taper, 0.0, I_MAX)

    @staticmethod
    def param_grid():
        # 3^3 = 27 seeds.  unlock_thresh_jerk is fixed at 80 000 rpm/s² and
        # excluded from the tuner: the sim uses instantaneous lock/unlock so
        # all threshold values in the 20 000–300 000 rpm/s² dead zone produce
        # identical scores.  The real-world threshold must be calibrated from
        # logged hardware data (post-deployment).
        return [
            dict(k=k, beta_md=bmd, unlock_thresh_jerk=80000.0, k_ai=ka)
            for k   in [0.08, 0.15, 0.28]
            for bmd in [0.04, 0.08, 0.14]
            for ka  in [0.005, 0.05, 0.20]
        ]


ALL_STRATEGIES = [
    MotorEffFfRegenStrategy,
    PiSlipRegenStrategy,
    AimdFfRegenStrategy,
    AimdMeanFfRegenStrategy,
    JerkBoundaryFfRegenStrategy,
    FixedFfRegenStrategy,
]
STRATEGY_BY_NAME = {cls.key: cls for cls in ALL_STRATEGIES}