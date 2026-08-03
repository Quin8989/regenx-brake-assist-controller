#!/usr/bin/env bash
set -euo pipefail

# Run a lightweight tuning benchmark and append machine-normalized results to CSV.
#
# Usage:
#   bash scripts/cloud/benchmark_and_record.sh
#
# Optional overrides:
#   RUN_LABEL=eecs WORKER_SET=auto,half MAXITER=5 POPSIZE=12 STRATEGY=aimd_ff \
#     bash scripts/cloud/benchmark_and_record.sh

if [[ ! -f "sim/run_tune.py" ]]; then
  echo "Run this from the repo root (regenx-brake-assist-controller)."
  exit 1
fi

if [[ -d ".venv" ]]; then
  source .venv/bin/activate
fi

PYTHON_BIN="${PYTHON_BIN:-python}"

RUN_LABEL="${RUN_LABEL:-$(hostname -s 2>/dev/null || hostname)}"
STRATEGY="${STRATEGY:-aimd_ff}"
OPTIMIZER="${OPTIMIZER:-de}"
MAXITER="${MAXITER:-3}"
POPSIZE="${POPSIZE:-8}"
SEEDS="${SEEDS:-42}"

# Comma-separated list: numeric values or aliases: auto,half,quarter.
WORKER_SET="${WORKER_SET:-auto,half}"

OUT_DIR="sim/output/benchmarks"
RESULT_CSV="${RESULT_CSV:-$OUT_DIR/host_benchmark_results.csv}"
mkdir -p "$OUT_DIR"

if [[ ! -f "$RESULT_CSV" ]]; then
  echo "timestamp_utc,run_label,hostname,platform,python_version,cpu_model,cpu_count,workers,strategy,maxiter,popsize,seeds,elapsed_sec,run_dir" > "$RESULT_CSV"
fi

HOSTNAME_FULL="$(hostname 2>/dev/null || echo unknown-host)"
CPU_MODEL="$(lscpu 2>/dev/null | awk -F: '/Model name/ {gsub(/^ +/, "", $2); print $2; exit}')"
if [[ -z "$CPU_MODEL" ]]; then
  CPU_MODEL="unknown-cpu"
fi
CPU_COUNT="$(nproc 2>/dev/null || echo 1)"

PY_INFO="$($PYTHON_BIN - <<'PY'
import platform, sys
print(platform.platform())
print(sys.version.split()[0])
PY
)"
PLATFORM_LINE="$(printf '%s\n' "$PY_INFO" | sed -n '1p')"
PYTHON_VERSION="$(printf '%s\n' "$PY_INFO" | sed -n '2p')"

resolve_workers() {
  local token="$1"
  local n="$CPU_COUNT"
  case "$token" in
    auto)
      echo "$n"
      ;;
    half)
      awk -v n="$n" 'BEGIN { v=int(n/2); if (v < 1) v=1; print v }'
      ;;
    quarter)
      awk -v n="$n" 'BEGIN { v=int(n/4); if (v < 1) v=1; print v }'
      ;;
    *)
      if [[ "$token" =~ ^[0-9]+$ ]] && (( token >= 1 )); then
        echo "$token"
      else
        echo "Invalid WORKER_SET token: $token" >&2
        exit 1
      fi
      ;;
  esac
}

append_csv_row() {
  local timestamp="$1"
  local workers="$2"
  local elapsed="$3"
  local run_dir="$4"

  "$PYTHON_BIN" - <<'PY' "$RESULT_CSV" "$timestamp" "$RUN_LABEL" "$HOSTNAME_FULL" "$PLATFORM_LINE" "$PYTHON_VERSION" "$CPU_MODEL" "$CPU_COUNT" "$workers" "$STRATEGY" "$MAXITER" "$POPSIZE" "$SEEDS" "$elapsed" "$run_dir"
import csv
import sys

path = sys.argv[1]
row = sys.argv[2:]
with open(path, "a", newline="", encoding="utf-8") as f:
    csv.writer(f).writerow(row)
PY
}

echo "=== Host Benchmark Recorder ==="
echo "run_label=$RUN_LABEL"
echo "host=$HOSTNAME_FULL"
echo "platform=$PLATFORM_LINE"
echo "python=$PYTHON_VERSION"
echo "cpu_model=$CPU_MODEL"
echo "cpu_count=$CPU_COUNT"
echo "worker_set=$WORKER_SET"
echo "strategy=$STRATEGY optimizer=$OPTIMIZER maxiter=$MAXITER popsize=$POPSIZE seeds=$SEEDS"
echo

IFS=',' read -r -a TOKENS <<< "$WORKER_SET"

for token in "${TOKENS[@]}"; do
  workers="$(resolve_workers "$token")"
  echo "--- Benchmark run: workers=$workers (from '$token') ---"

  start_epoch="$(date +%s)"
  "$PYTHON_BIN" -m sim.run_tune \
    --strategies "$STRATEGY" \
    --optimizer "$OPTIMIZER" \
    --workers "$workers" \
    --maxiter "$MAXITER" \
    --popsize "$POPSIZE" \
    --seeds "$SEEDS" \
    --objective nominal \
    --no-robust \
    --no-polish
  end_epoch="$(date +%s)"

  elapsed="$((end_epoch - start_epoch))"
  run_dir="$(ls -1dt sim/output/tune/* 2>/dev/null | head -n1 || true)"
  timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  append_csv_row "$timestamp" "$workers" "$elapsed" "$run_dir"

  echo "elapsed=${elapsed}s"
  echo "run_dir=$run_dir"
  echo "appended -> $RESULT_CSV"
  echo
done

echo "Done. Compare rows across machines in: $RESULT_CSV"
