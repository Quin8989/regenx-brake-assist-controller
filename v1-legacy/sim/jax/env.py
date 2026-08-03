"""Centralised JAX configuration for the sim package.

Importing this module (which every sim.physics_jax* module does at
the top) applies three optimisations:

1. **fp64 precision** is the default so energy integrals over 6000-
   tick fori_loops stay numerically stable.  Opt into fp32 for a
   ~1.7× speedup by setting ``JAX_ENABLE_X64=0`` in the environment
   *before* the first import of anything under ``sim``.  Parity vs
   numpy stays within 1 point on 100-point composite scores
   (validated in ``scripts/benchmark_fp32.py``).

2. **Persistent XLA compile cache** at ``.jax_cache/`` in the
   workspace root — skips ~5 s of jit compile on every cold Python
   start after the first run.  Set ``JAX_COMPILATION_CACHE_DIR`` to
   override the location, or ``REGENX_JAX_CACHE=0`` to disable.

3. **XLA CPU fast-math** via ``--xla_cpu_enable_fast_math`` — enables
   reassociation, reciprocals, and FMA fusion.  Safe for this code
   (no NaN propagation tests, no denormal inputs).  Set
   ``REGENX_JAX_FAST_MATH=0`` to disable.

All three are idempotent — importing this module more than once is
a no-op after the first import.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Fast-math BEFORE jax import (XLA flag is parsed at startup) ──
_FAST_MATH = os.environ.get("REGENX_JAX_FAST_MATH", "1") != "0"
if _FAST_MATH:
    existing = os.environ.get("XLA_FLAGS", "")
    if "xla_cpu_enable_fast_math" not in existing:
        flag = "--xla_cpu_enable_fast_math=true"
        os.environ["XLA_FLAGS"] = (existing + " " + flag).strip()

import jax  # noqa: E402

# ── fp64 default, unless JAX_ENABLE_X64=0 ──
_ENABLE_X64 = os.environ.get("JAX_ENABLE_X64", "1") != "0"
# Only call update when needed; double-calling is fine but pointless.
if jax.config.jax_enable_x64 != _ENABLE_X64:
    jax.config.update("jax_enable_x64", _ENABLE_X64)

# ── Persistent compile cache ──
_CACHE_ENABLED = os.environ.get("REGENX_JAX_CACHE", "1") != "0"
if _CACHE_ENABLED:
    cache_dir = Path(os.environ.get(
        "JAX_COMPILATION_CACHE_DIR",
        str(Path(__file__).resolve().parents[1] / ".jax_cache"),
    ))
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        from jax.experimental.compilation_cache import compilation_cache as _cc
        _cc.set_cache_dir(str(cache_dir))
        # Cache entries smaller than this many bytes / faster than this
        # many ms are skipped — defaults are conservative; loosen to
        # capture our ~2-4 s compiles.
        jax.config.update("jax_compilation_cache_dir", str(cache_dir))
        jax.config.update(
            "jax_persistent_cache_min_entry_size_bytes", 0)
        jax.config.update(
            "jax_persistent_cache_min_compile_time_secs", 0.5)
    except Exception:  # pragma: no cover — old jax
        pass


# Stable float dtype for downstream modules (lets them drop the hard-
# coded jnp.float64 that warns when x64 is off).
import jax.numpy as jnp  # noqa: E402
DEFAULT_FLOAT = jnp.float64 if jax.config.jax_enable_x64 else jnp.float32


def summary() -> dict:
    """Return the active JAX config snapshot (for logging)."""
    return dict(
        enable_x64=jax.config.jax_enable_x64,
        default_float=str(DEFAULT_FLOAT.__name__),
        fast_math=_FAST_MATH,
        cache_enabled=_CACHE_ENABLED,
        backend=jax.default_backend(),
        devices=[str(d) for d in jax.devices()],
    )
