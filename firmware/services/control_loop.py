# services/control_loop.py — Motor current command computation
#
# HOW ASSIST AND REGEN WORK (user intent → motor current)
# ========================================================
#
# 1. InputManager reads throttle and motor RPM each cycle:
#      Throttle applied                                → ASSIST request
#      Throttle off + motor RPM above entry threshold  → REGEN request
#      Throttle off + motor RPM below exit threshold   → coasting (no regen)
#
# 2. SystemSupervisor gates transitions with safety checks (cap voltage, faults).
#    Direct ASSIST ↔ REGEN transitions are allowed.
#
# 3. This module (ControlLoop) converts state + intent into current commands:
#
#    ASSIST:
#      requested_level (0–1) × max current → positive assist command.
#
#    REGEN:
#      Delegated to the selected strategy from regen.strategies, controlled
#      by REGEN_STRATEGY in config/settings.py.
#
#      The strategy's .update(ctx) returns a raw current
#      command.  The ControlLoop then applies hardware safety guards:
#        - Bus power limiter (VESC_WATT_MAX)
#        - Duty saturation detection (> 0.95)
#        - Voltage taper (near full caps)
#        - Clamp to [0, REGEN_CURRENT_MAX_A]
#
#    Current commands are applied directly — no slew limiting.  The VESC
#    FOC loop handles electrical ramping at 20+ kHz.
#
#    Any other state: both commands zero.
#
# 4. The computed values are transmitted to the VESC over UART.
#
# Inputs read from shared state:
#   system_state, inhibit_motor_commands
#   requested_level (0..1 for assist), cap_voltage_v, vesc_mech_rpm
#   vesc_iq_current_a, vesc_input_current_a, vesc_duty_cycle
#
# Outputs written to shared state:
#   assist_command_request (A), regen_command_request (A), motor_command_a (A)

from config.settings import (
    MOTOR_COMMAND_LIMIT_A,
    REGEN_CURRENT_MAX_A,
    REGEN_STRATEGY,
    REGEN_STRATEGY_PARAMS,
    VCAP_REGEN_TAPER_END_V,
    VESC_WATT_MAX,
)
from time import ticks_diff, ticks_ms
from core import SystemState
from regen.regen_control import apply_regen_limits
from regen.strategies import STRATEGY_BY_NAME
from regen.strategy_context import StrategyContext
from utils import clamp

# Instantiate the selected regen strategy with its tuned params.
_strat_cls = STRATEGY_BY_NAME[REGEN_STRATEGY]
_strat_params = REGEN_STRATEGY_PARAMS.get(REGEN_STRATEGY, {})

# Bus power limit for regen (watts).
_REGEN_POWER_LIMIT_W = VESC_WATT_MAX

# Duty cycle threshold — above this the VESC is saturated.
_DUTY_SATURATION_THRESHOLD = 0.95

# Control loop period (seconds) — matches the main loop tick.
_DT_CTRL = 0.01

# LispBM push telemetry freshness window. Beyond this age we fall back to
# averaged telemetry so strategies do not misread boot-time zeros or stale data.
_FAST_SIGNAL_STALE_MS = 30


def _build_strategy_context(state):
    """Build StrategyContext with a single fast-signal fallback policy.

    Raw averaged telemetry is always populated. The lower-latency LispBM
    push signals are included only while they are fresh; otherwise they are
    set to None and StrategyContext.preferred_* falls back to averaged data.
    """
    rpm_fast = None
    iq_fast = None
    if state.last_push_iq_rx_ms and ticks_diff(ticks_ms(), state.last_push_iq_rx_ms) <= _FAST_SIGNAL_STALE_MS:
        rpm_fast = state.vesc_mech_rpm_fast
        iq_fast = state.vesc_iq_instantaneous_a

    return StrategyContext(
        rpm=state.vesc_mech_rpm,
        vcap=state.cap_voltage_v,
        dt_ctrl=_DT_CTRL,
        iq_actual=state.vesc_iq_current_a,
        duty_cycle=state.vesc_duty_cycle,
        input_current=state.vesc_input_current_a,
        temp_fet=state.vesc_temp_fet_c,
        temp_motor=state.vesc_temp_motor_c,
        vd=state.vesc_vd,
        vq=state.vesc_vq,
        rpm_fast=rpm_fast,
        iq_instantaneous=iq_fast,
    )


class ControlLoop:
    """Command-shaping layer between state machine and command transmitter.

    Safety/state logic and command transmission remain separate concerns.
    """

    def __init__(self, shared_state):
        self._state = shared_state
        self._strategy = _strat_cls(**_strat_params)

    def update(self):
        """Compute this cycle's motor command.

        Behavior per system state:
        - Inhibited: zero everything.
        - ASSIST: map throttle level to current; regen zeroed.
        - REGEN: delegate to strategy + apply safety guards.
        - Other: hold at zero.
        """
        s = self._state

        s.assist_command_request = 0.0
        s.regen_command_request = 0.0

        if not s.inhibit_motor_commands:
            if s.system_state == SystemState.ASSIST:
                self._compute_assist()
            elif s.system_state == SystemState.REGEN:
                self._compute_regen()

        s.motor_command_a = s.assist_command_request - s.regen_command_request

    def _compute_assist(self):
        """Compute forward-drive current request in ASSIST state."""
        s = self._state
        s.assist_command_request = clamp(s.requested_level, 0.0, 1.0) * MOTOR_COMMAND_LIMIT_A

    def _compute_regen(self):
        """Compute regen current: strategy command + hardware safety guards.

        The strategy handles the control algorithm (feedforward, PI, SMC,
        ADRC, etc.) *including* the voltage taper near full caps.
        This method applies the safety envelope on top:
          - Bus power limiter
          - Duty saturation detection
          - Clamp to [0, REGEN_CURRENT_MAX_A]
        """
        s = self._state

        if s.cap_voltage_v >= VCAP_REGEN_TAPER_END_V:
            return

        # --- Strategy command ---
        ctx = _build_strategy_context(s)
        i_cmd = self._strategy.update(ctx)

        p_bus = None
        if s.cap_voltage_v > 0.0 and abs(s.vesc_input_current_a) > 0.0:
            p_bus = abs(s.vesc_input_current_a) * s.cap_voltage_v

        duty_mag = abs(s.vesc_duty_cycle)
        s.regen_command_request = apply_regen_limits(
            i_cmd,
            current_limit=REGEN_CURRENT_MAX_A,
            power_w=p_bus,
            power_limit_w=_REGEN_POWER_LIMIT_W,
            duty_cycle=duty_mag,
            duty_limit=_DUTY_SATURATION_THRESHOLD,
        )
