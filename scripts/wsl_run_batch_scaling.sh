#!/usr/bin/env bash
set -e
cd /mnt/c/VSProjects/regenx-brake-assist-controller
source .venv-linux/bin/activate
export PYTHONPATH="$PWD:$PWD/firmware"
export TF_CPP_MIN_LOG_LEVEL=2
python scripts/research/benchmark_gpu_batch_scaling.py 2>&1 | tail -n 40
