"""Compare tune results against config/settings.REGEN_STRATEGY_PARAMS.

Usage:
    python -m scripts.sync_tune_params sim/output/tune/<run_id>/results.json

Prints a per-strategy diff of the fitted params vs what the firmware will
actually use.  Exits non-zero if any param drifts by more than 0.5%%.
This is a preflight check — it does NOT modify settings.py.  Copy new
values by hand so the provenance stays in the git history.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make firmware/ importable when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "firmware"))

from config.settings import REGEN_STRATEGY, REGEN_STRATEGY_PARAMS  # noqa: E402


_TOL_REL = 0.005   # 0.5 %%


def _diff(configured: dict, fitted: dict) -> list[tuple[str, float, float, float]]:
    rows = []
    for name, new in fitted.items():
        old = configured.get(name)
        if old is None:
            rows.append((name, float("nan"), float(new), float("inf")))
            continue
        rel = 0.0 if old == 0 else abs(new - old) / abs(old)
        rows.append((name, float(old), float(new), rel))
    return rows


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    path = Path(argv[0])
    data = json.loads(path.read_text())
    any_drift = False

    print("Active firmware strategy: %s" % REGEN_STRATEGY)
    print("Tune run: %s" % data["meta"]["run_id"])
    print()

    for entry in data["results"]:
        key = entry.get("strategy", "")
        configured = REGEN_STRATEGY_PARAMS.get(key)
        if configured is None:
            print("[skip] %s -- not in REGEN_STRATEGY_PARAMS" % key)
            continue

        rows = _diff(configured, entry["params"])
        drift = any(r[3] > _TOL_REL for r in rows)
        any_drift = any_drift or drift
        flag = "DRIFT" if drift else "ok"
        print("[%s] %s  composite=%.2f" % (flag, key, entry.get("composite", float("nan"))))
        print("  %-16s %-16s %-16s %s" % ("param", "configured", "fitted", "drift"))
        for name, old, new, rel in rows:
            print("  %-16s %-16.6g %-16.6g %+6.2f %%" % (name, old, new, 100.0 * rel))
        print()

    return 1 if any_drift else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
