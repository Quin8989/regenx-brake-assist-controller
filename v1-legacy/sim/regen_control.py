"""Compatibility wrapper for sim callers.

The firmware-owned implementation lives in ``firmware/regen`` and is the
single source of truth. Sim imports from here for backward compatibility.
"""

from regen.regen_control import *  # noqa: F401,F403