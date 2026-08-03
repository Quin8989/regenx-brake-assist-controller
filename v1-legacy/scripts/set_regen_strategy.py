#!/usr/bin/env python3
"""Set active regen strategy in firmware/config/settings.py.

Usage:
  python scripts/set_regen_strategy.py --show
  python scripts/set_regen_strategy.py aimd_ff
  python scripts/set_regen_strategy.py pi_controller
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

VALID = ("pi_controller", "aimd_ff")


def read_current_strategy(settings_text: str) -> str:
    m = re.search(r'^REGEN_STRATEGY\s*=\s*"([a-z_]+)"\s*$', settings_text, re.MULTILINE)
    if not m:
        raise RuntimeError("Could not find REGEN_STRATEGY in firmware/config/settings.py")
    return m.group(1)


def set_strategy(settings_text: str, strategy: str) -> str:
    if strategy not in VALID:
        raise ValueError(f"Unsupported strategy: {strategy}")

    new_text, n = re.subn(
        r'^(REGEN_STRATEGY\s*=\s*)"([a-z_]+)"(\s*)$',
        rf'\1"{strategy}"\3',
        settings_text,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        raise RuntimeError("Could not update REGEN_STRATEGY in firmware/config/settings.py")
    return new_text


def main() -> int:
    parser = argparse.ArgumentParser(description="Set active regen strategy")
    parser.add_argument("strategy", nargs="?", choices=VALID, help="Strategy to set")
    parser.add_argument("--show", action="store_true", help="Show active strategy and exit")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    settings_path = repo_root / "firmware" / "config" / "settings.py"
    text = settings_path.read_text(encoding="utf-8")

    current = read_current_strategy(text)
    if args.show:
        print(f"Active strategy: {current}")
        return 0

    if not args.strategy:
        parser.error("Provide a strategy or use --show")

    target = args.strategy
    if target == current:
        print(f"Active strategy already set to {current}")
        return 0

    updated = set_strategy(text, target)
    settings_path.write_text(updated, encoding="utf-8")
    print(f"Updated REGEN_STRATEGY: {current} -> {target}")
    print("Restart firmware/app process so ControlLoop re-instantiates the selected strategy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
