"""Sim-only strategy wrapping the GRU teacher for the gallery/tuner.

Mirrors :class:`sim.neural_teacher_strategy.NeuralTeacherStrategy` but
wraps the recurrent (GRU) policy defined in
:mod:`scripts.research.neural_teacher_gru`.

Same 13-feature context as the MLP path (the GRU builds its own
temporal memory via the hidden state).  Pure numpy here so worker
processes in the Pool don't drag in JAX.

The firmware port will load the same ``theta.npz`` and run the same
GRU cell in ulab / MicroPython; the `(H=32)` cell is ~32 + 32 + 32
floats of matmul state and comfortably fits the Pico budget.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from config.settings import (
    FLUX_LINKAGE_WB as FLUX_LINKAGE,
    MOTOR_PHASE_RESISTANCE_OHM as R_PHASE,
    REGEN_CURRENT_MAX_A as I_MAX,
    VCAP_REGEN_TAPER_END_V as VCAP_TAPER_END,
    VCAP_REGEN_TAPER_START_V as VCAP_TAPER_START,
    VESC_MOTOR_POLE_PAIRS as POLE_PAIRS,
)
from regen.regen_control import ff_current_from_rpm, voltage_taper


K_FLOOR = 0.0
K_CEIL = 1.2
_DELTA_SCALE = 0.6
_N_FEATURES = 13
_HIDDEN = 32

# Must match scripts/research/neural_teacher_gru._FEATURE_SCALE.
_FEATURE_SCALE = np.array(
    [
        3000.0, 500.0, 1500.0, 30.0, 1.0, 50.0, 0.5,
        5.0, 30.0, 0.1, 1.0, 20.0, 1500.0,
    ],
    dtype=np.float64,
)


def _sigmoid(x):
    return 0.5 * (np.tanh(0.5 * x) + 1.0)


def _unflatten(theta: np.ndarray, H: int = _HIDDEN, D: int = _N_FEATURES):
    o = 0
    Wi = theta[o : o + 3 * H * D].reshape(3 * H, D); o += 3 * H * D
    bi = theta[o : o + 3 * H];                       o += 3 * H
    Wh = theta[o : o + 3 * H * H].reshape(3 * H, H); o += 3 * H * H
    bh = theta[o : o + 3 * H];                       o += 3 * H
    Wo = theta[o : o + H];                           o += H
    bo = theta[o : o + 1];                           o += 1
    return Wi, bi, Wh, bh, Wo, bo


def _gru_step(Wi, bi, Wh, bh, Wo, bo, feats_norm, h_prev):
    H = h_prev.shape[0]
    ix = Wi @ feats_norm + bi
    hx = Wh @ h_prev + bh
    ir, iz, in_ = ix[:H], ix[H : 2 * H], ix[2 * H :]
    hr, hz, hn = hx[:H], hx[H : 2 * H], hx[2 * H :]
    r = _sigmoid(ir + hr)
    z = _sigmoid(iz + hz)
    n = np.tanh(in_ + r * hn)
    h_new = (1.0 - z) * n + z * h_prev
    y = float((Wo * h_new).sum() + bo[0])
    return float(np.tanh(y) * _DELTA_SCALE), h_new


class NeuralTeacherGRUStrategy:
    """Recurrent (GRU) variant of the neural teacher strategy."""

    key = "neural_teacher_gru"

    def __init__(self, theta_path: str, label: Optional[str] = None):
        self.theta_path = str(theta_path)
        self.label = label or "neural_teacher_gru"
        self.name = f"Neural Teacher GRU ({self.label})"
        self._k = 0.1
        self._h = np.zeros(_HIDDEN, dtype=np.float64)
        self._drpm_mean_prev = 0.0
        self._drpm_peak_neg_prev = 0.0
        self._iq_prev = 0.0
        self._theta_parts = None  # lazily loaded per-worker

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_theta_parts"] = None
        return state

    def _ensure_loaded(self):
        if self._theta_parts is None:
            with np.load(self.theta_path) as npz:
                theta = np.asarray(
                    npz["theta"] if "theta" in npz.files else npz[npz.files[0]],
                    dtype=np.float64,
                )
            self._theta_parts = _unflatten(theta)
        return self._theta_parts

    def update(self, ctx):
        Wi, bi, Wh, bh, Wo, bo = self._ensure_loaded()
        rpm = ctx.preferred_rpm
        iq = ctx.preferred_iq
        taper = voltage_taper(ctx.vcap, VCAP_TAPER_START, VCAP_TAPER_END)
        if taper == 0.0:
            self._k = 0.1
            self._h = np.zeros(_HIDDEN, dtype=np.float64)
            self._drpm_mean_prev = 0.0
            self._drpm_peak_neg_prev = 0.0
            self._iq_prev = 0.0
            return 0.0

        jerk_mean = ctx.drpm_mean - self._drpm_mean_prev
        jerk_peak = ctx.drpm_peak_neg - self._drpm_peak_neg_prev
        slip_delta = ctx.drpm_peak_neg - ctx.drpm_mean
        decel_frac = ctx.drpm_mean / (rpm + 1.0)
        d_iq = iq - self._iq_prev
        power_mech = rpm * iq
        self._drpm_mean_prev = ctx.drpm_mean
        self._drpm_peak_neg_prev = ctx.drpm_peak_neg
        self._iq_prev = iq

        feats = np.array(
            [
                rpm, ctx.drpm_mean, ctx.drpm_peak_neg,
                iq, ctx.duty_cycle, ctx.vcap, self._k,
                jerk_mean, jerk_peak, slip_delta,
                decel_frac, d_iq, power_mech,
            ],
            dtype=np.float64,
        )
        feats_norm = feats / _FEATURE_SCALE

        try:
            delta, h_new = _gru_step(Wi, bi, Wh, bh, Wo, bo, feats_norm, self._h)
        except (ValueError, FloatingPointError):
            delta, h_new = 0.0, self._h
        if not np.isfinite(delta):
            delta = 0.0
            h_new = self._h
        self._h = h_new

        k_next = max(K_FLOOR, min(K_CEIL, self._k + delta))
        self._k = k_next

        i_cmd = ff_current_from_rpm(
            rpm, k_next,
            flux_linkage=FLUX_LINKAGE,
            phase_resistance=R_PHASE,
            pole_pairs=POLE_PAIRS,
            current_limit=I_MAX,
        )
        return max(0.0, min(I_MAX, i_cmd * taper))
