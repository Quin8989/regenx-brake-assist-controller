#!/usr/bin/env python
"""Overnight offline PySR re-run with expanded 13-feature set.

Adds 6 derived features (jerk_mean, jerk_peak, slip_delta, decel_frac,
d_iq, power_mech) to the imitation dataset and re-runs the PySR search
+ validation. Meant to run concurrently with the baseline re-tune
from overnight_20260422.py -- the two contend for CPU but both make
forward progress.

Stages:
  1. Regenerate imitation dataset with 13 features (~30s).
  2. Deep PySR search against the new dataset (~10-15 min).
  3. Validate PySR candidates against the full basket (~5-10 min).

Invoke from repo root:
  .venv\\Scripts\\python.exe scripts\\overnight_jerk_20260422.py
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

os.environ["PYTHONUTF8"] = "1"
os.environ["JULIA_PKG_OFFLINE"] = "true"
os.environ["PYTHON_JULIAPKG_OFFLINE"] = "yes"

# Below-normal priority so the existing Stage 3 tune keeps its share.
try:
    import ctypes
    BELOW_NORMAL = 0x00004000
    ctypes.windll.kernel32.SetPriorityClass(
        ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
except Exception:
    pass

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_DIR = ROOT / "sim" / "output" / "pysr" / f"overnight_jerk_{STAMP}"
LOG_DIR.mkdir(parents=True, exist_ok=True)
print(f"[jerk-overnight] logs -> {LOG_DIR}", flush=True)


def run_stage(label: str, argv: list[str], log_name: str) -> int:
    log_path = LOG_DIR / log_name
    print(f"[jerk-overnight] {label} -> {log_path}", flush=True)
    t0 = time.monotonic()
    with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
        lf.write(f"# {label}\n# cmd: {' '.join(argv)}\n# started: "
                 f"{datetime.now().isoformat()}\n\n")
        lf.flush()
        proc = subprocess.Popen(argv, stdout=lf, stderr=subprocess.STDOUT)
        rc = proc.wait()
    dur = timedelta(seconds=int(time.monotonic() - t0))
    print(f"[jerk-overnight] {label} done in {dur} (rc={rc})", flush=True)
    return rc


# ===================================================================
# Stage 1: regenerate dataset with 13 features
# ===================================================================
run_stage(
    label="Stage 1 / regenerate 13-feature dataset",
    log_name="stage1_collect.log",
    argv=[PY, "-m", "scripts.pysr.collect_imitation_dataset"],
)

# ===================================================================
# Stage 2: deep PySR search on expanded feature set
# ===================================================================
# Separate hall_of_fame path so we don't overwrite last night's winner
# until we validate the new candidates.
PYSR_OUT = LOG_DIR / "pysr_search"
run_stage(
    label="Stage 2 / deep PySR search (13 features)",
    log_name="stage2_pysr.log",
    argv=[
        PY, "-m", "scripts.pysr.imitate_aimd",
        "--rows", "5000",
        "--niterations", "400",
        "--populations", "30",
        "--population-size", "40",
        "--maxsize", "30",
        "--workers", "11",
        "--output-dir", str(PYSR_OUT),
    ],
)

# ===================================================================
# Stage 3: validate the new hall-of-fame
# ===================================================================
run_stage(
    label="Stage 3 / validate PySR candidates",
    log_name="stage3_validate.log",
    argv=[
        PY, "-m", "scripts.pysr.validate_candidates",
        "--hall-of-fame", str(PYSR_OUT / "imitate_aimd" / "hall_of_fame.csv"),
        "--out", str(LOG_DIR / "candidate_leaderboard.csv"),
    ],
)

print(f"\n[jerk-overnight] === ALL DONE ===", flush=True)
print(f"[jerk-overnight] artifacts in {LOG_DIR}", flush=True)
