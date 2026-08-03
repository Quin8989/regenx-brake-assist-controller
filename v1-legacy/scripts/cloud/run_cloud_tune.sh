#!/usr/bin/env bash
set -euo pipefail

# Run robust cloud tuning with sensible defaults.
#
# Override any variable inline, for example:
#   STRATEGIES=aimd_ff WORKERS=40 MAXITER=80 POPSIZE=24 bash scripts/cloud/run_cloud_tune.sh

STRATEGIES="${STRATEGIES:-aimd_ff}"
OPTIMIZER="${OPTIMIZER:-cma}"
WORKERS="${WORKERS:-$(nproc)}"
MAXITER="${MAXITER:-80}"
POPSIZE="${POPSIZE:-24}"
SEEDS="${SEEDS:-11,22,33,44}"
OBJECTIVE="${OBJECTIVE:-robust_cvar20}"
ROBUST_OBJECTIVE_SAMPLES="${ROBUST_OBJECTIVE_SAMPLES:-10}"
ROBUST_SAMPLES="${ROBUST_SAMPLES:-120}"

if [[ ! -f "sim/run_tune.py" ]]; then
  echo "Run this from the repo root (regenx-brake-assist-controller)."
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv. Run: bash scripts/cloud/bootstrap_ubuntu_vm.sh"
  exit 1
fi

source .venv/bin/activate

echo "Starting cloud tune with:"
echo "  STRATEGIES=$STRATEGIES"
echo "  OPTIMIZER=$OPTIMIZER"
echo "  WORKERS=$WORKERS"
echo "  MAXITER=$MAXITER"
echo "  POPSIZE=$POPSIZE"
echo "  SEEDS=$SEEDS"
echo "  OBJECTIVE=$OBJECTIVE"
echo "  ROBUST_OBJECTIVE_SAMPLES=$ROBUST_OBJECTIVE_SAMPLES"
echo "  ROBUST_SAMPLES=$ROBUST_SAMPLES"

python -m sim.run_tune \
  --strategies "$STRATEGIES" \
  --optimizer "$OPTIMIZER" \
  --workers "$WORKERS" \
  --maxiter "$MAXITER" \
  --popsize "$POPSIZE" \
  --seeds "$SEEDS" \
  --objective "$OBJECTIVE" \
  --robust-objective-samples "$ROBUST_OBJECTIVE_SAMPLES" \
  --robust-samples "$ROBUST_SAMPLES"

echo "Tune run finished."
echo "Artifacts: sim/output/tune/<run_id>/"
