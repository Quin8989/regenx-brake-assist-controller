"""Centralised scoreboard for all policy candidates.

One CSV at ``sim/output/scoreboard.csv`` accumulates a row per
candidate -- neural teacher, PySR symbolic, baseline, hand-tuned,
anything we want to compare.  Every producer (ES trainer, PySR
distiller, PySR blind search, ad-hoc scripts) should call
``append_scoreboard`` on exit.

Rows are append-only; sort / filter / report in separate tools.

Columns
-------
timestamp        ISO-8601 local time the row was written.
source           neural_teacher | pysr_distill | pysr_invent |
                 baseline | manual | other
run_id           caller-supplied identifier (e.g. output filename).
cvar20           CVaR-20 of the composite score on the evaluation
                 fixture (higher is better).
composite_mean   Mean composite across the evaluation fixture.
n_features       Feature count if applicable (13 for the MLP, else
                 blank).
fixture          Short description of the evaluation fixture, e.g.
                 ``gpu_3x8_heldout``.
notes            Free-form text.
artifact         Path to the saved artifact (npz, expression, ...).
"""
from __future__ import annotations

import csv
import datetime as _dt
import os
from pathlib import Path
from typing import Any

SCOREBOARD_PATH = (
    Path(__file__).resolve().parent / "output" / "scoreboard.csv"
)

_COLUMNS = [
    "timestamp",
    "source",
    "run_id",
    "cvar20",
    "composite_mean",
    "n_features",
    "fixture",
    "notes",
    "artifact",
]


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def append_scoreboard(
    *,
    source: str,
    run_id: str,
    cvar20: float | None = None,
    composite_mean: float | None = None,
    n_features: int | None = None,
    fixture: str = "",
    notes: str = "",
    artifact: str | os.PathLike = "",
    path: Path = SCOREBOARD_PATH,
) -> None:
    """Append one candidate's scores to the central scoreboard.

    Creates the file with a header row on first write.  Never raises
    on I/O errors (scoreboard persistence is best-effort so it does
    not clobber the surrounding run).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        row = {
            "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "run_id": run_id,
            "cvar20": _fmt(cvar20),
            "composite_mean": _fmt(composite_mean),
            "n_features": _fmt(n_features),
            "fixture": fixture,
            "notes": notes,
            "artifact": str(artifact),
        }
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_COLUMNS)
            if is_new:
                writer.writeheader()
            writer.writerow(row)
    except OSError as e:
        # Scoreboard is best-effort; never crash the producer.
        print(f"[scoreboard] warning: failed to append row: {e}")
