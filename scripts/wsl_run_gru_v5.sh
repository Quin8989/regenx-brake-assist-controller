#!/usr/bin/env bash
# Launch GRU v5 training on WSL GPU (v2-style: no jitter penalty, longer run).
# Foreground run — nohup it from the caller if detach is needed.
set -e
cd /mnt/c/VSProjects/regenx-brake-assist-controller
source .venv-linux/bin/activate
export PYTHONPATH="$PWD:$PWD/firmware"
export TF_CPP_MIN_LOG_LEVEL=2
export JAX_PLATFORMS=cuda
python scripts/research/train_neural_teacher.py \
    --arch gru \
    --steps 800 \
    --pop 32 \
    --rides-per-profile 3 \
    --perts 8 \
    --jitter-weight 0.0 \
    --output sim/output/neural_teacher/gru_theta_v5.npz
