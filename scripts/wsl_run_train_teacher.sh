#!/usr/bin/env bash
# Run the neural-teacher ES trainer inside WSL on the CUDA GPU.
# Usage (from PowerShell):
#   wsl -d Ubuntu -- bash scripts/wsl_run_train_teacher.sh \
#     --rides-per-profile 5 --perts 10 --pop 32 --steps 150
set -e
cd /mnt/c/VSProjects/regenx-brake-assist-controller
source .venv-linux/bin/activate
export PYTHONPATH="$PWD:$PWD/firmware"
export TF_CPP_MIN_LOG_LEVEL=2
# Force GPU as the JAX default.  If CUDA isn't present this will
# error out clearly instead of silently falling back to CPU.
export JAX_PLATFORMS=cuda
python scripts/research/train_neural_teacher.py "$@"
