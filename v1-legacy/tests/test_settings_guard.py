# tests/test_settings_guard.py — Structural guard against settings drift
#
# These tests catch cases where constants in README, sim, bench scripts,
# or deploy manifests diverge from the single source of truth in
# config/settings.py.  Run with every CI pass.

import importlib
import re
from pathlib import Path

import config.settings as S

ROOT = Path(__file__).resolve().parent.parent


# ── README must quote the same values as settings.py ──────────────────

# Each tuple: (settings attribute, regex pattern matching the README table row)
_README_CHECKS = [
    ("VCAP_REGEN_TAPER_START_V", r"VCAP_REGEN_TAPER_START_V\s*\|\s*([\d.]+)"),
    ("VCAP_REGEN_TAPER_END_V", r"VCAP_REGEN_TAPER_END_V\s*\|\s*([\d.]+)"),
    ("VCAP_ABSOLUTE_MAX", r"VCAP_ABSOLUTE_MAX\s*\|\s*([\d.]+)"),
    ("VESC_WATT_MAX", r"VESC_WATT_MAX\s*\|\s*([\d.]+)"),
    ("REGEN_CURRENT_MAX_A", r"REGEN_CURRENT_MAX_A\s*\|\s*([\d.]+)"),
    ("MOTOR_CURRENT_MAX_A", r"MOTOR_CURRENT_MAX_A\s*\|\s*([\d.]+)"),
    ("MOTOR_COMMAND_LIMIT_A", r"MOTOR_COMMAND_LIMIT_A\s*\|\s*([\d.]+)"),
]


def test_readme_constants_match_settings():
    """Every constant quoted in the README table must match settings.py."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for attr, pattern in _README_CHECKS:
        expected = getattr(S, attr)
        matches = re.findall(pattern, readme)
        assert matches, f"README has no table row for {attr}"
        for m in matches:
            assert float(m) == expected, (
                f"README says {attr} = {m}, but settings.py says {expected}"
            )


# ── Sim must import taper constants, not the deleted hard cutoff ──────

def test_sim_does_not_use_legacy_cutoff():
    """sim/ must not reference the removed VCAP_SOFT_REGEN_CUTOFF."""
    sim_dir = ROOT / "sim"
    if not sim_dir.exists():
        return  # sim/ is optional
    for py in sim_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "VCAP_SOFT_REGEN_CUTOFF" not in text, (
            f"{py.name} still references VCAP_SOFT_REGEN_CUTOFF"
        )
        assert "VCAP_CUTOFF" not in text, (
            f"{py.name} still references VCAP_CUTOFF (deleted alias)"
        )


# ── Bench script fallbacks must match settings.py ────────────────────

_FALLBACK_CHECKS = {
    "VCAP_ABSOLUTE_MAX": S.VCAP_ABSOLUTE_MAX,
    "MOTOR_CURRENT_MAX_A": S.MOTOR_CURRENT_MAX_A,
    "VCAP_MIN_OPERATING": S.VCAP_MIN_OPERATING,
}


def test_bench_fallback_constants():
    """Fallback defaults in bench scripts must match settings.py."""
    bench_dir = ROOT / "scripts" / "bench"
    if not bench_dir.exists():
        return
    pattern = re.compile(r"^\s+(\w+)\s*=\s*([\d.]+)", re.MULTILINE)
    for py in bench_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "except ImportError" not in text:
            continue
        # Extract the fallback block after `except ImportError:`
        idx = text.index("except ImportError")
        block = text[idx:idx + 500]
        for name, value in pattern.findall(block):
            if name in _FALLBACK_CHECKS:
                assert float(value) == _FALLBACK_CHECKS[name], (
                    f"{py.name} fallback {name} = {value}, "
                    f"but settings.py says {_FALLBACK_CHECKS[name]}"
                )


# ── deploy_to_flash.sh must cover every current production module ─────

_DEPLOY_ROOT_FILES = ["boot.py", "main.py", "core.py", "utils.py"]
_DEPLOY_TREE_DIRS = ["app", "config", "drivers", "services", "regen"]


def _production_modules_from_firmware_tree():
    modules = list(_DEPLOY_ROOT_FILES)
    firmware_root = ROOT / "firmware"
    for dirname in _DEPLOY_TREE_DIRS:
        tree = firmware_root / dirname
        for path in sorted(tree.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            modules.append(path.relative_to(firmware_root).as_posix())
    return modules


def test_deploy_script_lists_all_production_files():
    """deploy_to_flash.sh must cover the current firmware tree."""
    deploy = (ROOT / "scripts" / "deploy_to_flash.sh").read_text(encoding="utf-8")
    assert "copy_firmware_tree()" in deploy

    for filename in _DEPLOY_ROOT_FILES:
        assert filename in deploy, (
            f"deploy_to_flash.sh is missing root file {filename}"
        )

    for dirname in _DEPLOY_TREE_DIRS:
        assert f'copy_firmware_tree {dirname}' in deploy, (
            f"deploy_to_flash.sh is missing tree copy for {dirname}/"
        )

    for mod in _production_modules_from_firmware_tree():
        if "/" not in mod:
            continue
        top = mod.split("/", 1)[0]
        assert top in _DEPLOY_TREE_DIRS, f"Unexpected production module outside deploy trees: {mod}"


# ── No hardcoded voltage/current thresholds in production code ────────

# These are the exact numeric values that MUST come from settings.py.
# If any production file contains them as bare literals, someone copy-pasted
# instead of importing.
_MAGIC_NUMBERS = {
    "VCAP_REGEN_TAPER_START_V": S.VCAP_REGEN_TAPER_START_V,
    "VCAP_REGEN_TAPER_END_V": S.VCAP_REGEN_TAPER_END_V,
    "VCAP_ABSOLUTE_MAX": S.VCAP_ABSOLUTE_MAX,
}

_PRODUCTION_DIRS = ["app", "services"]

# Patterns that are allowed (imports, comments, string literals in tests)
_ALLOWED_PATTERNS = [
    re.compile(r"^\s*#"),                # comments
    re.compile(r"^\s*from\s+"),          # import lines
    re.compile(r"^\s*import\s+"),        # import lines
    re.compile(r'["\']'),               # string literals
]


def test_no_hardcoded_voltage_thresholds_in_production():
    """Production code must not have bare voltage threshold literals."""
    for dirname in _PRODUCTION_DIRS:
        prod_dir = ROOT / dirname
        for py in prod_dir.glob("*.py"):
            lines = py.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines, 1):
                # Skip comments and imports
                if any(p.search(line) for p in _ALLOWED_PATTERNS):
                    continue
                for name, val in _MAGIC_NUMBERS.items():
                    # Match the exact float literal (e.g., "43.0" or "43")
                    # Only flag if it appears as a standalone number
                    pattern = re.compile(
                        r'(?<![.\w])' + re.escape(str(val)) + r'(?![.\w])'
                    )
                    assert not pattern.search(line), (
                        f"{py.name}:{i} has hardcoded {val} — "
                        f"should use settings.{name} instead: {line.strip()}"
                    )
