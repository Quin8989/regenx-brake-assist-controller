#!/usr/bin/env bash
set -e
cd /mnt/c/VSProjects/regenx-brake-assist-controller
source .venv-linux/bin/activate
pip install optuna pandas pyarrow 2>&1 | tail -n 25
echo "---"
python -c "import optuna, pandas; print('optuna', optuna.__version__); print('pandas', pandas.__version__)"
