#!/usr/bin/env bash
set -e
cd /mnt/c/VSProjects/regenx-brake-assist-controller
source .venv-linux/bin/activate
python - <<'PY'
import jax, jax.numpy as jnp, time
print("devices       :", jax.devices())
print("default bknd  :", jax.default_backend())
x = jnp.arange(1_000_000.0)
t0 = time.perf_counter(); y = (x * x).sum().block_until_ready(); t = time.perf_counter() - t0
print(f"kernel sum    : {float(y):.3e}   time={t*1000:.1f} ms")
PY
