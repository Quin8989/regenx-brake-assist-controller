"""JAX ports of the three tunable regen strategies (traced-params form).

Each step function is defined at module scope, so its Python identity is
stable across Optuna trials.  Params live inside the ``strategy_state``
pytree alongside runtime state; the ride simulator's ``tree_map`` reset
on taper==0 preserves params (state0 carries the same values) and
clears runtime accumulators.

This shape makes the whole thing JIT-stable: one compilation per
strategy key across an entire study.

Signature expected by :func:`sim.jax.physics_strategy.simulate_ride_strategy_jax`:

    step_fn(feats_tuple, strategy_state) -> (k_next, new_strategy_state)

``feats_tuple`` is the 13-element tuple
    (rpm, drpm_mean, drpm_peak_neg, iq, duty_cycle, vcap, k_prev,
     jerk_mean, jerk_peak, slip_delta, decel_frac, d_iq, power_mech).
"""
from __future__ import annotations

from typing import Callable, Tuple

import jax.numpy as jnp

from sim.jax.env import DEFAULT_FLOAT

# Must match numpy AimdFfRegenStrategy internal clamps.
_AIMD_K_FLOOR = 0.02
_AIMD_K_CEIL = 1.0

# PI internal clamps from numpy.
_PI_INTEGRAL_CLIP = 5.0
_PI_GAIN_MULT_LO = 0.05
_PI_GAIN_MULT_HI = 3.0


# ─────────────────────────────────────────────────────────────────────
# fixed_ff — stateless apart from the param itself
# ─────────────────────────────────────────────────────────────────────

def _fixed_ff_step(feats, state):
    # state = {"k": <traced scalar>}
    return state["k"], state


def _fixed_ff_state0(k: float) -> dict:
    return {"k": jnp.asarray(k, dtype=DEFAULT_FLOAT)}


# ─────────────────────────────────────────────────────────────────────
# pi_controller
# ─────────────────────────────────────────────────────────────────────

def _pi_controller_step(feats, state):
    """Observation-only PI on the rider's external-decel proxy.

    ``decel_proxy``  = |drpm_mean| / (rpm + 10)  — total speed-normalised decel.
    ``motor_decel``  = alpha · iq / (rpm + 10)    — decel attributable to regen.
    ``excess``       = decel_proxy - motor_decel — proxy for band-brake / rider
                                                    contribution.

    Integrator tracks the excess; positive-polarity gain pushes regen up
    when the rider pulls harder than motor alone can explain.  Uses only
    signals StrategyContext already carries on firmware (rpm, drpm_mean,
    iq_actual), so sim/firmware parity is preserved.
    """
    rpm = feats[0]
    drpm_mean = feats[1]
    iq_feat = feats[3]

    denom = rpm + 10.0
    decel_proxy = jnp.abs(drpm_mean) / denom
    motor_decel = state["alpha"] * iq_feat / denom
    excess = decel_proxy - motor_decel

    integral = jnp.clip(state["integral"] + excess * state["dt_ctrl"],
                        -_PI_INTEGRAL_CLIP, _PI_INTEGRAL_CLIP)
    correction = state["ki"] * integral
    gain_mult = jnp.clip(1.0 + correction,
                         _PI_GAIN_MULT_LO, _PI_GAIN_MULT_HI)
    k_next = state["k_ff"] * gain_mult

    new_state = dict(state)
    new_state["integral"] = integral
    return k_next, new_state


def _pi_controller_state0(k_ff: float, ki: float, alpha: float,
                          dt_ctrl: float) -> dict:
    return {
        "k_ff":     jnp.asarray(k_ff,     dtype=DEFAULT_FLOAT),
        "ki":       jnp.asarray(ki,       dtype=DEFAULT_FLOAT),
        "alpha":    jnp.asarray(alpha,    dtype=DEFAULT_FLOAT),
        "dt_ctrl":  jnp.asarray(dt_ctrl,  dtype=DEFAULT_FLOAT),
        "integral": jnp.zeros((), dtype=DEFAULT_FLOAT),
    }


# ─────────────────────────────────────────────────────────────────────
# aimd_ff
# ─────────────────────────────────────────────────────────────────────

def _aimd_ff_step(feats, state):
    drpm_peak_neg = feats[2]

    unlock_thresh = state["unlock_thresh"]
    unlock_band = jnp.maximum(10.0, 0.25 * unlock_thresh)
    unlock_excess = (-drpm_peak_neg) - unlock_thresh
    unlock_level = jnp.clip(unlock_excess / unlock_band, 0.0, 1.0)
    raw_slip = unlock_level > 0.0
    prev_raw = state["slip_prev_raw"] > 0.5
    slip_event = raw_slip & (~prev_raw)

    k_eff = state["k_eff"]
    md = state["beta_md"] * (0.35 + 0.65 * unlock_level)
    k_after_md = k_eff * (1.0 - md)
    k_after_ai = k_eff + state["k_ai"] * state["dt_ctrl"]

    k_new = jnp.where(slip_event, k_after_md,
                      jnp.where(~raw_slip, k_after_ai, k_eff))
    k_new = jnp.clip(k_new, _AIMD_K_FLOOR, _AIMD_K_CEIL)

    new_state = dict(state)
    new_state["k_eff"] = k_new
    new_state["slip_prev_raw"] = jnp.where(
        raw_slip,
        jnp.asarray(1.0, dtype=DEFAULT_FLOAT),
        jnp.asarray(0.0, dtype=DEFAULT_FLOAT),
    )
    return k_new, new_state


def _aimd_ff_state0(k: float, beta_md: float, unlock_thresh: float,
                    k_ai: float, dt_ctrl: float) -> dict:
    k_init = jnp.asarray(k, dtype=DEFAULT_FLOAT)
    return {
        "beta_md":       jnp.asarray(beta_md,       dtype=DEFAULT_FLOAT),
        "unlock_thresh": jnp.asarray(unlock_thresh, dtype=DEFAULT_FLOAT),
        "k_ai":          jnp.asarray(k_ai,          dtype=DEFAULT_FLOAT),
        "dt_ctrl":       jnp.asarray(dt_ctrl,       dtype=DEFAULT_FLOAT),
        "k_eff":         k_init,
        "slip_prev_raw": jnp.zeros((), dtype=DEFAULT_FLOAT),
    }


# ─────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────

IQ_KP_BY_STRATEGY = {
    "fixed_ff":      0.0,
    "pi_controller": 0.05,
    "aimd_ff":       0.0,
}

_STEP_FN_BY_STRATEGY = {
    "fixed_ff":      _fixed_ff_step,
    "pi_controller": _pi_controller_step,
    "aimd_ff":       _aimd_ff_step,
}


def build_step_fn(strategy_key: str, params: dict,
                  dt_ctrl: float) -> Tuple[Callable, dict, float]:
    """Return ``(step_fn, state0, iq_kp)`` for a strategy.

    ``step_fn`` is a module-level function (JIT-stable identity).
    ``state0`` is a dict carrying both traced params and zero-init
    runtime state.  ``iq_kp`` is the post-strategy feedback gain the
    ride sim should apply.
    """
    if strategy_key == "fixed_ff":
        state0 = _fixed_ff_state0(**params)
    elif strategy_key == "pi_controller":
        p = {k: v for k, v in params.items() if k != "kp_iq"}
        state0 = _pi_controller_state0(dt_ctrl=dt_ctrl, **p)
    elif strategy_key == "aimd_ff":
        state0 = _aimd_ff_state0(dt_ctrl=dt_ctrl, **params)
    else:
        raise ValueError(f"unknown strategy {strategy_key!r}")
    return (_STEP_FN_BY_STRATEGY[strategy_key],
            state0,
            IQ_KP_BY_STRATEGY[strategy_key])
