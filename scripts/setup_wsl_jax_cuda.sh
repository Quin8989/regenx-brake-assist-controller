#!/usr/bin/env bash
# scripts/setup_wsl_jax_cuda.sh — run this INSIDE WSL Ubuntu 24.04
# from /mnt/c/VSProjects/regenx-brake-assist-controller.
#
# Idempotent: re-running reuses the existing .venv-linux.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "[1/6] Workspace: $REPO_ROOT"

# ── Sanity: are we actually in WSL? ──
if ! grep -qi microsoft /proc/version 2>/dev/null; then
    echo "ERROR: this script must run inside WSL, not bare Linux or macOS."
    exit 1
fi

# ── Sanity: is CUDA visible? ──
echo "[2/6] Checking GPU via nvidia-smi ..."
if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "ERROR: nvidia-smi not found.  Update your Windows NVIDIA"
    echo "driver and re-launch WSL (wsl --shutdown then wsl)."
    exit 1
fi
nvidia-smi | head -n 20 || true

# ── Python 3.13 ──
echo "[3/6] Ensuring Python 3.13 + build deps ..."
if ! command -v python3.13 >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y software-properties-common
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update
    sudo apt-get install -y python3.13 python3.13-venv python3.13-dev
fi

# ── Linux venv (separate from Windows .venv/) ──
VENV_DIR="$REPO_ROOT/.venv-linux"
if [[ ! -d "$VENV_DIR" ]]; then
    echo "[4/6] Creating $VENV_DIR ..."
    python3.13 -m venv "$VENV_DIR"
else
    echo "[4/6] Reusing existing $VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip wheel setuptools

# ── JAX with CUDA 12 local support ──
echo "[5/6] Installing jax[cuda12] + sim deps ..."
pip install --upgrade "jax[cuda12]"
# Minimum other deps the sim touches.
pip install numpy sympy numba

# ── Smoke test ──
echo "[6/6] Verifying JAX sees the GPU ..."
python - <<'PY'
import jax
print("jax         :", jax.__version__)
print("default bknd:", jax.default_backend())
print("devices     :", jax.devices())
assert any("cuda" in str(d).lower() or "gpu" in str(d).lower()
           for d in jax.devices()), "No CUDA device!"
print("OK — GPU is visible to JAX.")
PY

cat <<EOF

─────────────────────────────────────────────
WSL + CUDA JAX setup complete.

Activate with:
    source .venv-linux/bin/activate

Then benchmark:
    python scripts/research/benchmark_jax_startup.py fp32
    python scripts/research/benchmark_jax_cvar20.py

Both scripts should report a CudaDevice in the startup summary.
─────────────────────────────────────────────
EOF
