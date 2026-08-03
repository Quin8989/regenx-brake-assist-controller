#!/usr/bin/env python
"""Overnight offline compute chain -- 2026-04-22.

Runs entirely offline (Julia and Python deps already cached):
  Stage 1  Deep PySR search (~3-5h)        -> hall_of_fame.csv
  Stage 2  Validate PySR candidates        -> candidate_leaderboard.csv
  Stage 3  Re-tune all 4 firmware          -> tune/<stamp>/summary.csv
           strategies under 40/60 scoring

Invoke from repo root:
  .venv\\Scripts\\python.exe scripts\\overnight_20260422.py

Each stage streams its own log file AND prints to console. If a stage
fails, subsequent stages still run -- failure output goes into its log.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

# Offline switches for the julia/juliapkg resolver -- packages are
# already installed under .venv\julia_env\.
os.environ["PYTHONUTF8"] = "1"
os.environ["JULIA_PKG_OFFLINE"] = "true"
os.environ["PYTHON_JULIAPKG_OFFLINE"] = "yes"

# Below-normal priority so the machine stays usable for Windows idle work.
try:
    import ctypes
    BELOW_NORMAL = 0x00004000
    ctypes.windll.kernel32.SetPriorityClass(
        ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
except Exception:
    pass

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_DIR = ROOT / "sim" / "output" / "pysr" / f"overnight_{STAMP}"
LOG_DIR.mkdir(parents=True, exist_ok=True)
print(f"[overnight] logs -> {LOG_DIR}")


def run_stage(label: str, argv: list[str], log_name: str) -> int:
    """Run a stage, writing output to a log file (no console streaming)."""
    log_path = LOG_DIR / log_name
    print(f"[overnight] {label} -> {log_path}", flush=True)
    t0 = time.monotonic()
    with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
        lf.write(f"# {label}\n# cmd: {' '.join(argv)}\n# started: "
                 f"{datetime.now().isoformat()}\n\n")
        lf.flush()
        # Child inherits env incl. PYTHONUTF8=1 and JULIA offline flags,
        # so its stdout is UTF-8 and we can write straight to the file.
        proc = subprocess.Popen(
            argv, stdout=lf, stderr=subprocess.STDOUT,
        )
        rc = proc.wait()
    dur = timedelta(seconds=int(time.monotonic() - t0))
    print(f"[overnight] {label} done in {dur} (rc={rc})", flush=True)
    return rc


# ====================================================================
# Stage 1: deep PySR search
# ====================================================================
run_stage(
    label="Stage 1 / deep PySR search",
    log_name="stage1_pysr.log",
    argv=[
        PY, "-m", "scripts.pysr.imitate_aimd",
        "--rows", "5000",
        "--niterations", "400",
        "--populations", "30",
        "--population-size", "40",
        "--maxsize", "30",
        "--workers", "11",
    ],
)

# ====================================================================
# Stage 2: validate new hall-of-fame
# ====================================================================
run_stage(
    label="Stage 2 / validate PySR candidates",
    log_name="stage2_validate.log",
    argv=[
        PY, "-m", "scripts.pysr.validate_candidates",
        "--out", str(LOG_DIR / "candidate_leaderboard.csv"),
    ],
)

# ====================================================================
# Stage 3: re-tune all 4 firmware strategies under 40/60 scoring
# ====================================================================
run_stage(
    label="Stage 3 / baseline re-tune",
    log_name="stage3_tune.log",
    argv=[
        PY, "-m", "sim.run_tune",
        "--strategies", "fixed_ff,pi_controller,aimd_ff",
        "--seeds", "7,42,123",
        "--maxiter", "200",
        "--popsize", "36",
        "--workers", "11",
        "--polish-maxiter", "80",
    ],
)

print(f"\n[overnight] === ALL DONE ===")
print(f"[overnight] artifacts in {LOG_DIR}")
print(f"[overnight] tune summary in sim/output/tune/<latest>/summary.csv")
