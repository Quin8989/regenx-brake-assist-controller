"""Path setup helpers for scripts run via mpremote mount.

Ensures firmware modules are importable after the repo was reorganized under
firmware/ while preserving old import statements like `from core import ...`.
"""

import os
import sys


def _dirname(path):
    """Return parent directory for both CPython and MicroPython."""
    if not path:
        return ""
    p = path.replace("\\", "/")
    if p.endswith("/"):
        p = p[:-1]
    idx = p.rfind("/")
    if idx < 0:
        return ""
    if idx == 0:
        return "/"
    return p[:idx]


def _join(a, b):
    if not a:
        return b
    if a.endswith("/"):
        return a + b
    return a + "/" + b


def ensure_firmware_path():
    """Prepend firmware/ to sys.path if present.

    Works for both:
      - mpremote mount . run scripts/foo.py
      - local CPython runs from repo root
    """
    here = _dirname(__file__)
    scripts_dir = _dirname(here)
    repo_root = _dirname(scripts_dir)
    fw_dir = _join(repo_root, "firmware")

    if fw_dir not in sys.path:
        try:
            if os.stat(fw_dir):
                sys.path.insert(0, fw_dir)
        except OSError:
            pass
