#!/usr/bin/env bash
set -e
cd /mnt/c/VSProjects/regenx-brake-assist-controller
source .venv-linux/bin/activate
export PYTHONPATH="$PWD:$PWD/firmware"
export TF_CPP_MIN_LOG_LEVEL=2
python scripts/pysr_invent_composite.py "$@"
