#!/usr/bin/env bash
set -e
cd /mnt/c/VSProjects/regenx-brake-assist-controller
source .venv-linux/bin/activate
export PYTHONPATH="$PWD:$PWD/firmware"
# Silence the cosmetic WSL2 CUDA driver-version warning.
export TF_CPP_MIN_LOG_LEVEL=2
echo "=========================================================="
echo "  RUNNING: sim.run_tune --backend jax (GPU)"
echo "=========================================================="
python -m sim.run_tune --backend jax --strategies aimd_ff \
    --seeds 42 --trials 5 --workers 2 2>&1 | tail -n 60
