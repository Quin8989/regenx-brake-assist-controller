"""Path setup helpers for scripts run via mpremote mount.

Ensures firmware modules are importable after the repo was reorganized under
firmware/ while preserving old import statements like `from core import ...`.
"""

import os
import sys


def ensure_firmware_path():
    """Prepend firmware/ to sys.path if present.

    Works for both:
      - mpremote mount . run scripts/foo.py
      - local CPython runs from repo root
    """
    here = os.path.dirname(__file__)
    scripts_dir = os.path.dirname(here)
    repo_root = os.path.dirname(scripts_dir)
    fw_dir = os.path.join(repo_root, "firmware")

    if fw_dir not in sys.path:
        try:
            if os.stat(fw_dir):
                sys.path.insert(0, fw_dir)
        except OSError:
            pass
