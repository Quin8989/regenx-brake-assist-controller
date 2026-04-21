#!/usr/bin/env bash
set -euo pipefail

# Bootstrap an Ubuntu 22.04+ VM for cloud tuning.
# Usage:
#   bash scripts/cloud/bootstrap_ubuntu_vm.sh
#
# Expected current directory: repo root.

if [[ ! -f "sim/run_tune.py" ]]; then
  echo "Run this from the repo root (regenx-brake-assist-controller)."
  exit 1
fi

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git \
  python3 \
  python3-venv \
  python3-pip \
  build-essential

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools
python -m pip install numpy scipy cma

echo "Bootstrap complete."
echo "Next: source .venv/bin/activate && bash scripts/cloud/run_cloud_tune.sh"
