"""Recurrent (GRU) teacher policy.

Sibling to :mod:`scripts.research.neural_teacher` (MLP).  Same
13-feature input, same residual output form

    k = clip(k_prev + DELTA_SCALE * tanh(Wo @ h + bo), K_FLOOR, K_CEIL)

but ``h`` is a persistent GRU hidden vector carried across ticks, so
the policy can learn temporal behaviour (integral accumulation,
slip-backoff, phase-locked bite modulation) without needing those to
be hand-engineered as input features.

Parameter layout (flat 1-D vector, for ES friendliness)
-------------------------------------------------------
For hidden size ``H`` and input size ``D = 13``:

    Wi  (3H, D)        input-to-gate weights  (stacked r/z/n)
    bi  (3H,)          input biases
    Wh  (3H, H)        hidden-to-gate weights
    bh  (3H,)          recurrent biases
    Wo  (H,)           hidden-to-output weights
    bo  (1,)           output bias

Total parameters: ``3H(D + H + 2) + H + 1``.  With ``H=32`` this is
4545, comparable to the MLP (1537) but with real recurrence.

The GRU follows the PyTorch/Cho 2014 convention:

    r = sigmoid(Wi_r x + bi_r + Wh_r h + bh_r)
    z = sigmoid(Wi_z x + bi_z + Wh_z h + bh_z)
    n = tanh   (Wi_n x + bi_n + r * (Wh_n h + bh_n))
    h' = (1 - z) * n + z * h

Pure JAX, safe to ``jit`` / ``vmap`` over a theta-population axis.
"""
from __future__ import annotations

from dataclasses import dataclass

import jax
import jax.numpy as jnp

from sim.jax.env import DEFAULT_FLOAT
from sim.jax.physics_strategy import FEATURE_NAMES, K_FLOOR, K_CEIL


HIDDEN = 32
N_FEATURES = len(FEATURE_NAMES)  # 13

# Reuse the MLP feature scales so the two paths are comparable.
_FEATURE_SCALE = jnp.asarray(
    [
        3000.0, 500.0, 1500.0, 30.0, 1.0, 50.0, 0.5,
        5.0, 30.0, 0.1, 1.0, 20.0, 1500.0,
    ],
    dtype=DEFAULT_FLOAT,
)

# Same rate limit on the residual as MLP.
DELTA_SCALE = DEFAULT_FLOAT(0.6)


@dataclass(frozen=True)
class GRUShape:
    in_features: int = N_FEATURES
    hidden: int = HIDDEN

    @property
    def n_params(self) -> int:
        H, D = self.hidden, self.in_features
        return 3 * H * (D + H + 2) + H + 1

    @property
    def h0_size(self) -> int:
        return self.hidden


def init_theta(shape: GRUShape = GRUShape(), *, seed: int = 0,
               last_layer_scale: float = 0.01) -> jnp.ndarray:
    """Orthogonal init for recurrent matrix, Xavier for input, tiny
    output — so the untrained policy is near ``k = k_prev``."""
    key = jax.random.PRNGKey(seed)
    H, D = shape.hidden, shape.in_features

    key, k_wi, k_wh, k_wo = jax.random.split(key, 4)

    # Input→gate: Xavier with fan_in=D.
    wi = jax.random.normal(k_wi, (3 * H, D), dtype=DEFAULT_FLOAT) * jnp.sqrt(1.0 / D)
    bi = jnp.zeros((3 * H,), dtype=DEFAULT_FLOAT)

    # Hidden→gate: orthogonal-ish via scaled normal (JAX has no native
    # orthogonal init; the scaled-normal is close enough for tiny H
    # and doesn't starve the ES gradient).
    wh = jax.random.normal(k_wh, (3 * H, H), dtype=DEFAULT_FLOAT) * jnp.sqrt(1.0 / H)
    bh = jnp.zeros((3 * H,), dtype=DEFAULT_FLOAT)

    # Tiny output so initial residual ≈ 0.
    wo = jax.random.normal(k_wo, (H,), dtype=DEFAULT_FLOAT) * last_layer_scale
    bo = jnp.zeros((1,), dtype=DEFAULT_FLOAT)

    return jnp.concatenate(
        [wi.reshape(-1), bi, wh.reshape(-1), bh, wo, bo]
    )


def _unflatten(theta: jnp.ndarray, shape: GRUShape):
    H, D = shape.hidden, shape.in_features
    o = 0
    wi = theta[o : o + 3 * H * D].reshape(3 * H, D); o += 3 * H * D
    bi = theta[o : o + 3 * H];                       o += 3 * H
    wh = theta[o : o + 3 * H * H].reshape(3 * H, H); o += 3 * H * H
    bh = theta[o : o + 3 * H];                       o += 3 * H
    wo = theta[o : o + H];                           o += H
    bo = theta[o : o + 1];                           o += 1
    return wi, bi, wh, bh, wo, bo


def _sigmoid(x):
    return 0.5 * (jnp.tanh(0.5 * x) + 1.0)


def gru_step(theta: jnp.ndarray, feats: jnp.ndarray, h_prev: jnp.ndarray,
             shape: GRUShape = GRUShape()):
    """One GRU tick.  Returns ``(delta, h_new)``."""
    wi, bi, wh, bh, wo, bo = _unflatten(theta, shape)
    H = shape.hidden
    x = feats / _FEATURE_SCALE

    ix = wi @ x + bi             # (3H,)
    hx = wh @ h_prev + bh        # (3H,)

    # Split into r, z, n chunks.
    ir, iz, in_ = ix[:H], ix[H:2 * H], ix[2 * H:]
    hr, hz, hn = hx[:H], hx[H:2 * H], hx[2 * H:]

    r = _sigmoid(ir + hr)
    z = _sigmoid(iz + hz)
    n = jnp.tanh(in_ + r * hn)
    h_new = (1.0 - z) * n + z * h_prev

    y = (wo * h_new).sum() + bo[0]
    delta = jnp.tanh(y) * DELTA_SCALE
    return delta, h_new


def policy_step(theta: jnp.ndarray, feats: jnp.ndarray,
                h_prev: jnp.ndarray,
                shape: GRUShape = GRUShape()):
    """Returns ``(k_new, h_new)``."""
    delta, h_new = gru_step(theta, feats, h_prev, shape)
    k_prev = feats[6]
    k_new = jnp.clip(k_prev + delta, K_FLOOR, K_CEIL)
    return k_new, h_new


def make_strategy_step_fn(theta: jnp.ndarray, shape: GRUShape = GRUShape()):
    """Return a callable of the shape ``simulate_ride_strategy_jax``
    expects for ``strategy_step_fn``: ``(feats, state) -> (k, state')``.

    ``state`` is the GRU hidden vector of length ``shape.hidden``.
    """
    def step(feats, state):
        feats_vec = jnp.stack(feats)
        k, h_new = policy_step(theta, feats_vec, state, shape)
        return k, h_new
    return step


def initial_state(shape: GRUShape = GRUShape()) -> jnp.ndarray:
    return jnp.zeros((shape.hidden,), dtype=DEFAULT_FLOAT)
