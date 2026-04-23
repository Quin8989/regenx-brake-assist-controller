#!/usr/bin/env bash
# Launch GRU v6 long training on WSL GPU (~5h target).
# v5 was killed early; v6 is the full-length successor with no band-aids.
set -e
cd /mnt/c/VSProjects/regenx-brake-assist-controller
source .venv-linux/bin/activate
export PYTHONPATH="$PWD:$PWD/firmware"
export TF_CPP_MIN_LOG_LEVEL=2
export JAX_PLATFORMS=cuda
python scripts/research/train_neural_teacher.py \
    --arch gru \
    --steps 2100 \
    --pop 32 \
    --rides-per-profile 3 \
    --perts 8 \
    --jitter-weight 0.0 \
    --output sim/output/neural_teacher/gru_theta_v6.npz \
    2>&1 | tee sim/output/teacher_train_gru_v6.log
