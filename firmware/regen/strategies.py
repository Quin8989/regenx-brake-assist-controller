"""sim.strategies - production regen controllers.

The strategy layer is intentionally small:
    pi_controller  PI feedback on a carrier-slip proxy.
    aimd_ff        Feedforward + AIMD gain state.

Every strategy receives only a StrategyContext built from signals that
exist on the physical Pico + VESC system.
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
from .strategy_context import StrategyContext  # noqa: F401 - re-export


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

    def _begin_update(self, ctx):
        rpm = ctx.preferred_rpm
        iq = ctx.preferred_iq
        taper = voltage_taper(ctx.vcap, VCAP_TAPER_START, VCAP_TAPER_END)
        if taper == 0.0:
            self._reset()
        return rpm, iq, taper

    def _ff(self, rpm, gain):
        return _ff_current(rpm, gain)


class PiSlipRegenStrategy(_BaseStrategy):
    """PI feedback on a carrier-slip proxy."""

    key = "pi_controller"

    def __init__(self, k_ff=0.15, kp=0.5, ki=0.1,
                 slip_target=0.3, alpha=0.3):
        self.k_ff = k_ff
        self.kp = kp
        self.ki = ki
        self.slip_target = slip_target
        self.alpha = alpha
        self.name = (
            f"PI Controller (kff={k_ff}, Kp={kp}, Ki={ki}, tgt={slip_target})"
        )
        self.rpm_prev = 0.0
        self.slip_metric = 0.0
        self.integral = 0.0

    def _reset(self):
        self.rpm_prev = 0.0
        self.slip_metric = 0.0
        self.integral = 0.0

    def update(self, ctx):
        rpm, _iq, taper = self._begin_update(ctx)
        if taper == 0.0:
            return 0.0

        d_rpm = abs(rpm - self.rpm_prev) / ctx.dt_ctrl if ctx.dt_ctrl > 0 else 0.0
        self.rpm_prev = rpm
        raw_slip = d_rpm / (rpm + 10.0)
        self.slip_metric += self.alpha * (raw_slip - self.slip_metric)

        error = self.slip_metric - self.slip_target
        self.integral += error * ctx.dt_ctrl
        self.integral = max(-5.0, min(5.0, self.integral))

        correction = self.kp * error + self.ki * self.integral
        gain_mult = max(0.05, min(1.0, 1.0 - correction))
        return self._ff(rpm, self.k_ff * gain_mult) * taper

    @staticmethod
    def param_grid():
        return [
            dict(k_ff=kf, kp=kp, ki=ki, slip_target=st, alpha=al)
            for kf in [0.10, 0.15, 0.20, 0.25, 0.30]
            for kp in [0.1, 0.2, 0.3, 0.5]
            for ki in [0.1, 0.2, 0.3, 0.5]
            for st in [0.2, 0.3, 0.4, 0.6, 0.8]
            for al in [0.05, 0.1, 0.2, 0.4]
        ]


class AimdFfRegenStrategy(_BaseStrategy):
    """Feedforward current with AIMD (additive-increase / multiplicative-decrease) gain state."""

    key = "aimd_ff"

    def __init__(self,
                 k_init=0.0655283, k_ai=0.00122916, beta_md=0.0660271,
                 unlock_thresh=145.144,
                 rpm_scale=2500.0, drpm_scale=6000.0, iq_scale=60.0,
                 drpm_alpha=0.526797):

        self.k_init = float(k_init)
        self.k_ai = float(k_ai)
        self.beta_md = float(beta_md)
        self.unlock_thresh = float(unlock_thresh)

        self.rpm_scale = float(rpm_scale)
        self.drpm_scale = float(drpm_scale)
        self.iq_scale = float(iq_scale)
        self.drpm_alpha = float(drpm_alpha)

        self.name = "AIMD-FF Regen Controller"

        self._rpm_prev = 0.0
        self._drpm_ema = 0.0
        self._k_state = self.k_init

    @staticmethod
    def _clip(x, lo, hi):
        if x < lo:
            return lo
        if x > hi:
            return hi
        return x

    def _reset(self):
        self._rpm_prev = 0.0
        self._drpm_ema = 0.0
        self._k_state = self.k_init

    def update(self, ctx):
        rpm, _iq, taper = self._begin_update(ctx)
        if taper == 0.0:
            self._reset()
            return 0.0

        dt = ctx.dt_ctrl if ctx.dt_ctrl > 0.0 else 0.01
        drpm = (rpm - self._rpm_prev) / dt
        self._rpm_prev = rpm
        self._drpm_ema += self.drpm_alpha * (drpm - self._drpm_ema)
        unlock_band = max(10.0, 0.25 * self.unlock_thresh)
        unlock_excess = (-self._drpm_ema) - self.unlock_thresh
        unlock_level = self._clip(unlock_excess / unlock_band, 0.0, 1.0)

        if unlock_level > 0.0:
            md = self.beta_md * (0.35 + 0.65 * unlock_level)
            self._k_state *= (1.0 - md)
            if self._k_state < 0.0:
                self._k_state = 0.0
        else:
            self._k_state = min(1.0, self._k_state + self.k_ai)
        base = self._ff(rpm, self._k_state)
        return self._clip(base * taper, 0.0, I_MAX)

    @staticmethod
    def param_grid():
        return [
            dict(k_ai=k_ai, beta_md=beta_md)
            for k_ai in [0.0006, 0.0010, 0.0014]
            for beta_md in [0.06, 0.10, 0.14]
        ]


ALL_STRATEGIES = [PiSlipRegenStrategy, AimdFfRegenStrategy]
STRATEGY_BY_NAME = {cls.key: cls for cls in ALL_STRATEGIES}