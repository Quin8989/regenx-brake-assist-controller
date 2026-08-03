"""Sim-only strategy wrapper around the neural teacher MLP.

This strategy is NOT part of the firmware runtime.  It lives here so
the gallery / tuner can score the MLP against the classical
controllers (``fixed_ff`` / ``pi_controller`` / ``aimd_ff``) on the
same ride basket.  The MLP is too big to fit on the Pico, so firmware
ships a PySR-distilled symbolic form of it.

Design mirrors :class:`scripts.pysr.validate_candidates.PysrStrategy`:

* Module-level class so ProcessPoolExecutor can pickle it.
* Stores the theta-file *path*, not the array — pickles cheaply and
  lets worker processes load lazily on first ``update`` call.
* Same 13-feature context (``FEATURE_NAMES`` in
  :mod:`sim.jax.physics_strategy`) as the PySR pipeline, so the MLP
  can be swapped with a distilled expression with no further changes.
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


# Same clamps as PysrStrategy / K_FLOOR / K_CEIL in the JAX-side module.
# Duplicated here so we don't import JAX just to read two constants
# (important: workers spawned for the gallery shouldn't pay the JAX
# import cost).
K_FLOOR = 0.0
K_CEIL = 1.2

# Hand-chosen feature scales (must match
# scripts/research/neural_teacher._FEATURE_SCALE).  Kept in numpy here
# so the sim path is JAX-free.
_FEATURE_SCALE = np.array(
    [
        3000.0,   # rpm
        500.0,    # drpm_mean
        1500.0,   # drpm_peak_neg
        30.0,     # iq
        1.0,      # duty_cycle
        50.0,     # vcap
        0.5,      # k_prev
        5.0,      # jerk_mean
        30.0,     # jerk_peak
        0.1,      # slip_delta
        1.0,      # decel_frac
        20.0,     # d_iq
        1500.0,   # power_mech
    ],
    dtype=np.float64,
)

_DELTA_SCALE = 0.6     # must match neural_teacher.DELTA_SCALE
_HIDDEN = (32, 32)
_N_FEATURES = 13


def _layer_sizes():
    dims = [_N_FEATURES, *_HIDDEN, 1]
    return [(dims[i + 1], dims[i]) for i in range(len(dims) - 1)]


def _unflatten(theta: np.ndarray):
    """Return a list of (W, b) pairs matching MLPShape layout."""
    layers = []
    offset = 0
    for rows, cols in _layer_sizes():
        wn = rows * cols
        W = theta[offset:offset + wn].reshape(rows, cols)
        offset += wn
        b = theta[offset:offset + rows]
        offset += rows
        layers.append((W, b))
    return layers


def _mlp_forward(theta_layers, feats_norm: np.ndarray) -> float:
    x = feats_norm
    n = len(theta_layers)
    for i, (W, b) in enumerate(theta_layers):
        x = W @ x + b
        if i != n - 1:
            x = np.tanh(x)
    # Final tanh gate so the residual lives in ±DELTA_SCALE.
    return float(np.tanh(x[0]) * _DELTA_SCALE)


class NeuralTeacherStrategy:
    """Strategy wrapping the neural teacher MLP for sim-only use."""

    key = "neural_teacher"

    def __init__(self, theta_path: str, label: Optional[str] = None):
        self.theta_path = str(theta_path)
        self.label = label or "neural_teacher"
        self.name = f"Neural Teacher ({self.label})"
        self._k = 0.1
        self._drpm_mean_prev = 0.0
        self._drpm_peak_neg_prev = 0.0
        self._iq_prev = 0.0
        # Lazily loaded per-worker so pickling stays cheap.
        self._theta_layers = None

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_theta_layers"] = None
        return state

    def _ensure_loaded(self):
        if self._theta_layers is None:
            with np.load(self.theta_path) as npz:
                # Training script saves under 'theta' (see train_neural_teacher.py).
                theta = np.asarray(
                    npz["theta"] if "theta" in npz.files else npz[npz.files[0]],
                    dtype=np.float64,
                )
            self._theta_layers = _unflatten(theta)
        return self._theta_layers

    def update(self, ctx):
        layers = self._ensure_loaded()
        rpm = ctx.preferred_rpm
        iq = ctx.preferred_iq
        taper = voltage_taper(ctx.vcap, VCAP_TAPER_START, VCAP_TAPER_END)
        if taper == 0.0:
            # Match PysrStrategy reset behaviour when regen is throttled.
            self._k = 0.1
            self._drpm_mean_prev = 0.0
            self._drpm_peak_neg_prev = 0.0
            self._iq_prev = 0.0
            return 0.0

        # Derived features — must match neural_teacher training features
        # which in turn match FEATURE_NAMES / collect_imitation_dataset.
        jerk_mean = ctx.drpm_mean - self._drpm_mean_prev
        jerk_peak = ctx.drpm_peak_neg - self._drpm_peak_neg_prev
        slip_delta = ctx.drpm_peak_neg - ctx.drpm_mean
        decel_frac = ctx.drpm_mean / (rpm + 1.0)
        d_iq = iq - self._iq_prev
        power_mech = rpm * iq
        self._drpm_mean_prev = ctx.drpm_mean
        self._drpm_peak_neg_prev = ctx.drpm_peak_neg
        self._iq_prev = iq

        feats = np.array([
            rpm, ctx.drpm_mean, ctx.drpm_peak_neg,
            iq, ctx.duty_cycle, ctx.vcap, self._k,
            jerk_mean, jerk_peak, slip_delta,
            decel_frac, d_iq, power_mech,
        ], dtype=np.float64)
        feats_norm = feats / _FEATURE_SCALE

        try:
            delta = _mlp_forward(layers, feats_norm)
        except (ValueError, FloatingPointError):
            delta = 0.0
        if not np.isfinite(delta):
            delta = 0.0

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
