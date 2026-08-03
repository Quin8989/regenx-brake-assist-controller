"""Tiny stateless MLP that maps the 13-feature PySR context → k.

Shared by :mod:`scripts.research.train_neural_teacher` (training) and
:mod:`scripts.pysr_distill_teacher` (rollout + distillation).

Design choices
--------------
* **Stateless**: takes the same 13 features PySR sees (``FEATURE_NAMES``
  in ``sim/jax/physics_strategy.py``).  All history is already baked
  into those features (``k_prev``, ``drpm_mean``, ``jerk_mean``, ...),
  so the policy is a pure function.
* **Residual form**: ``k = clip(k_prev + delta * mlp(features))``.
  Initial ``mlp`` output is near zero, so the untrained policy is
  ``k = k_prev`` (pass-through), a sensible starting point.
* **Tanh activations + tanh output gate**: matches an operator PySR
  can express natively, so distillation (Stage 2) has a real shot of
  fitting the teacher to within ~1-2 CVaR-20 points.
* **Feature normalization**: fixed, hand-set scales.  Keeps the sim
  output deterministic and doesn't force the trainer to infer scale.
* **Tiny**: 13 → 32 → 32 → 1 ≈ 1.5k parameters.  Big enough to learn
  non-trivial gating; small enough that ES gradient estimates aren't
  starved by noise.

Everything here is pure JAX.  No Python side-effects, no closures
over numpy arrays.  Safe to ``jit`` / ``vmap`` over either the batch
axis or a population-of-thetas axis.
"""
from __future__ import annotations

from dataclasses import dataclass

import jax
import jax.numpy as jnp

from sim.jax.env import DEFAULT_FLOAT
from sim.jax.physics_strategy import FEATURE_NAMES, K_FLOOR, K_CEIL


# Hidden widths.  Kept as a module-level constant so the flatten/
# unflatten helpers stay simple.
HIDDEN = (32, 32)
N_FEATURES = len(FEATURE_NAMES)  # 13


# Hand-chosen feature scales so raw inputs land roughly in [-2, 2].
# These are the 1-sigma magnitudes observed in sample rides.
_FEATURE_SCALE = jnp.asarray(
    [
        3000.0,   # rpm
        500.0,    # drpm_mean
        1500.0,   # drpm_peak_neg
        30.0,     # iq
        1.0,      # duty_cycle
        50.0,     # vcap
        0.5,      # k_prev    ← not really "scale" but keeps net input bounded
        5.0,      # jerk_mean
        30.0,     # jerk_peak
        0.1,      # slip_delta
        1.0,      # decel_frac
        20.0,     # d_iq
        1500.0,   # power_mech
    ],
    dtype=DEFAULT_FLOAT,
)

# Max step away from k_prev the MLP can command per tick.  Not a hard
# saturation on k (that's still K_FLOOR/K_CEIL below) — just a rate
# limit so the untrained residual can't instantly saturate the clamp.
DELTA_SCALE = DEFAULT_FLOAT(0.6)


# =====================================================================
#  Parameter init / flatten / unflatten
# =====================================================================

@dataclass(frozen=True)
class MLPShape:
    """Layer sizes + helpers to flatten/unflatten a parameter vector.

    We carry theta as a **flat 1-D array** throughout training — ES
    operates on a flat parameter space, and ``jax.vmap`` over a
    population axis is trivial when theta is flat.
    """
    in_features: int = N_FEATURES
    hidden: tuple = HIDDEN

    @property
    def sizes(self) -> list[tuple[int, int]]:
        """List of (rows, cols) for each weight matrix."""
        dims = [self.in_features, *self.hidden, 1]
        return [(dims[i + 1], dims[i]) for i in range(len(dims) - 1)]

    @property
    def n_params(self) -> int:
        return sum(r * c + r for r, c in self.sizes)


def init_theta(shape: MLPShape = MLPShape(), *, seed: int = 0,
               last_layer_scale: float = 0.01) -> jnp.ndarray:
    """He-init all weights except the last layer, which starts tiny.

    A tiny last layer makes the untrained residual ``mlp(x)`` produce
    near-zero outputs, so the initial policy is ``k ≈ k_prev`` — a
    pass-through controller.  ES then *builds up* the deviation.
    """
    key = jax.random.PRNGKey(seed)
    pieces = []
    for i, (rows, cols) in enumerate(shape.sizes):
        key, sub_w, sub_b = jax.random.split(key, 3)
        fan_in = cols
        is_last = (i == len(shape.sizes) - 1)
        std = last_layer_scale if is_last else jnp.sqrt(2.0 / fan_in)
        W = jax.random.normal(sub_w, (rows, cols), dtype=DEFAULT_FLOAT) * std
        b = jnp.zeros((rows,), dtype=DEFAULT_FLOAT)
        pieces.append(W.reshape(-1))
        pieces.append(b)
    return jnp.concatenate(pieces)


def _unflatten(theta: jnp.ndarray, shape: MLPShape):
    """Return a list of ``(W, b)`` tuples for each layer."""
    layers = []
    offset = 0
    for rows, cols in shape.sizes:
        wn = rows * cols
        W = theta[offset:offset + wn].reshape(rows, cols)
        offset += wn
        b = theta[offset:offset + rows]
        offset += rows
        layers.append((W, b))
    return layers


# =====================================================================
#  Forward pass
# =====================================================================

def mlp_forward(theta: jnp.ndarray, feats: jnp.ndarray,
                shape: MLPShape = MLPShape()) -> jnp.ndarray:
    """Apply the MLP to one feature vector ``feats`` of length 13.

    Returns a *scalar* JAX array giving the residual ``delta``.
    """
    x = feats / _FEATURE_SCALE
    layers = _unflatten(theta, shape)
    for i, (W, b) in enumerate(layers):
        x = W @ x + b
        is_last = (i == len(layers) - 1)
        if not is_last:
            x = jnp.tanh(x)
    # Final tanh gate so the residual is bounded to ±DELTA_SCALE.
    return jnp.tanh(x[0]) * DELTA_SCALE


def policy_k(theta: jnp.ndarray, feats: jnp.ndarray,
             shape: MLPShape = MLPShape()) -> jnp.ndarray:
    """Full teacher policy: ``clip(k_prev + mlp(feats), K_FLOOR, K_CEIL)``.

    ``feats`` is length-13 in the order of :data:`FEATURE_NAMES`.
    ``k_prev`` is ``feats[6]``.
    """
    delta = mlp_forward(theta, feats, shape)
    k_prev = feats[6]
    return jnp.clip(k_prev + delta, K_FLOOR, K_CEIL)


def make_strategy_fn(theta: jnp.ndarray, shape: MLPShape = MLPShape()):
    """Wrap :func:`policy_k` into the ``*feats`` positional form the
    sim expects as its ``strategy_fn``.

    The wrapper closes over ``theta``.  When the enclosing function
    is itself jitted and ``theta`` is one of its traced arguments,
    the MLP becomes part of the traced graph — no Python callback.
    """
    def strat(*feats):
        feats_vec = jnp.stack(feats)
        return policy_k(theta, feats_vec, shape)
    return strat
