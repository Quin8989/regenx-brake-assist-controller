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


class PiSlipRegenStrategy(_BaseStrategy):
    """PI feedback on the rider's *external* decel proxy — reference baseline.

    Not the production controller.  Kept to show how much structural
    value the AIMD / lock-hold schemes add over plain feedback.

    The law is purely observation-based (only signals StrategyContext
    already carries on real hardware): total speed-normalised decel is
    partly attributable to regen torque (≈ ``alpha · iq / (rpm + 10)``),
    and the residual is a proxy for band-brake / rider demand.  The PI
    integrates the residual with positive polarity: when the rider pulls
    harder than motor torque alone can explain, gain climbs and regen
    captures the extra energy.

    Decel input comes from ``ctx.drpm_mean`` (mean d(rpm)/dt over the
    VESC's 10 ms / 1 kHz-sampled window).  Preferred over an in-strategy
    numerical derivative of ``rpm_fast`` because the VESC's tighter
    sampling window averages out single-tick telemetry jitter that would
    otherwise contaminate the decel proxy at low speeds.

    Control law::

        decel_proxy = |drpm_mean|        / (rpm + 10)
        motor_decel = alpha · iq_actual  / (rpm + 10)
        excess      = decel_proxy - motor_decel
        integral   += excess * dt                      (clipped ±5.0)
        gain_mult   = clip(1 + ki * integral, 0.05, 3.0)
        i_ff        = k_ff * gain_mult * λ*ωe/R
        i_cmd       = i_ff + kp_iq * (i_ff - iq_mean)

    Parameters (3 tunable): k_ff, ki, alpha.
    Fixed: kp_iq = 0.05 (inner loop gain — removed from tuning after
    2026-04 analysis showed < 2% contribution).
    """

    key = "pi_controller"

    def __init__(self, k_ff=0.29, ki=0.38, alpha=0.50, kp_iq=0.05):
        self.k_ff = k_ff
        self.ki = ki
        self.alpha = alpha
        self.kp_iq = kp_iq  # inner additive iq-error correction gain (fixed)
        self.name = f"PI Controller (kff={k_ff}, Ki={ki}, alpha={alpha})"
        self.integral = 0.0

    def _reset(self):
        self.integral = 0.0

    def update(self, ctx):
        rpm, iq_actual, taper = self._begin_update(ctx)
        if taper == 0.0:
            return 0.0

        denom = rpm + 10.0
        decel_proxy = abs(ctx.drpm_mean) / denom
        motor_decel = self.alpha * iq_actual / denom
        excess = decel_proxy - motor_decel

        self.integral += excess * ctx.dt_ctrl
        self.integral = self._clip(self.integral, -5.0, 5.0)

        correction = self.ki * self.integral
        gain_mult = self._clip(1.0 + correction, 0.05, 3.0)
        i_ff = self._ff(rpm, self.k_ff * gain_mult)

        iq_err = i_ff - iq_actual
        i_cmd = i_ff + self.kp_iq * iq_err

        return self._clip(i_cmd * taper, 0.0, I_MAX)

    @staticmethod
    def param_grid():
        # 4 * 3 * 4 = 48 seeds for DE.  ``alpha`` sets how much observed
        # decel is attributed to motor torque (iq / (rpm+10)) versus
        # rider-induced (band brake) decel; wide bracket until the 1h
        # tune picks a winner.
        return [
            dict(k_ff=kf, ki=ki, alpha=al)
            for kf in [0.10, 0.15, 0.30, 0.50]
            for ki in [0.2, 0.4, 0.6]
            for al in [0.05, 0.2, 0.5, 1.0]
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


ALL_STRATEGIES = [
    FixedFfRegenStrategy,
    PiSlipRegenStrategy,
    AimdFfRegenStrategy,
]
STRATEGY_BY_NAME = {cls.key: cls for cls in ALL_STRATEGIES}