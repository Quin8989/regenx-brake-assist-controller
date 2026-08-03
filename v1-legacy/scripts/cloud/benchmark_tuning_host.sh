#!/usr/bin/env bash
set -euo pipefail

# Benchmark host suitability for sim tuning.
# Usage:
#   bash scripts/cloud/benchmark_tuning_host.sh
# Optional overrides:
#   WORKERS=16 MAXITER=3 POPSIZE=8 bash scripts/cloud/benchmark_tuning_host.sh

WORKERS="${WORKERS:-$(nproc)}"
MAXITER="${MAXITER:-3}"
POPSIZE="${POPSIZE:-8}"
STRATEGY="${STRATEGY:-aimd_ff}"

if [[ ! -f "sim/run_tune.py" ]]; then
  echo "Run this from repo root (regenx-brake-assist-controller)."
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv in repo. Create it first:"
  echo "  python3 -m venv .venv && source .venv/bin/activate"
  exit 1
fi

source .venv/bin/activate

echo "=== Host Info ==="
uname -a || true
if command -v lscpu >/dev/null 2>&1; then
  lscpu | egrep "Model name|Socket|Core\(s\) per socket|Thread\(s\) per core|CPU\(s\)" || true
fi
if command -v free >/dev/null 2>&1; then
  free -h || true
fi
python - <<'PY'
import platform, sys
print("Python:", sys.version.split()[0])
print("Platform:", platform.platform())
PY

echo
printf "=== Quick Tuning Benchmark (strategy=%s workers=%s maxiter=%s popsize=%s) ===\n" \
  "$STRATEGY" "$WORKERS" "$MAXITER" "$POPSIZE"

start=$(date +%s)
python -m sim.run_tune \
  --strategies "$STRATEGY" \
  --optimizer cma \
  --workers "$WORKERS" \
  --maxiter "$MAXITER" \
  --popsize "$POPSIZE" \
  --seeds 42 \
  --objective nominal \
  --no-robust \
  --no-polish
end=$(date +%s)

dur=$((end - start))

latest_dir=$(ls -1dt sim/output/tune/* 2>/dev/null | head -n1 || true)

echo
printf "Elapsed: %ss\n" "$dur"
if [[ -n "$latest_dir" ]]; then
  echo "Latest run dir: $latest_dir"
  if [[ -f "$latest_dir/run.log" ]]; then
    echo "Tail of run.log:"
    tail -n 20 "$latest_dir/run.log" || true
  fi
fi

echo
if (( dur <= 120 )); then
  echo "Verdict: likely good for full robust tuning."
elif (( dur <= 300 )); then
  echo "Verdict: usable; prefer moderate workers/seeds or overnight runs."
else
  echo "Verdict: slow for heavy tuning; reduce objective cost or use cloud VM."
fi
