"""Compatibility wrapper for sim callers.

The firmware-owned implementation lives in ``firmware/regen`` and is the
single source of truth. Sim imports from here for backward compatibility.
"""

from regen.strategies import (  # noqa: F401
    ALL_STRATEGIES,
    STRATEGY_BY_NAME,
    AimdFfRegenStrategy,
    PiSlipRegenStrategy,
)


DEFAULT_STRATEGY_NAMES = tuple(STRATEGY_BY_NAME.keys())


def parse_strategy_names(raw):
    """Parse and validate a comma-separated strategy list.

    Args:
        raw: comma-separated names, or None/empty for defaults.

    Returns:
        List of validated strategy names.

    Raises:
        SystemExit: if any strategy name is unknown.
    """
    if raw:
        names = [name.strip() for name in raw.split(",") if name.strip()]
    else:
        names = list(DEFAULT_STRATEGY_NAMES)

    bad = [name for name in names if name not in STRATEGY_BY_NAME]
    if bad:
        raise SystemExit(
            f"Unknown strategies: {bad}. Valid: {sorted(STRATEGY_BY_NAME)}")

    return names


def strategy_classes_from_names(names):
    """Return strategy classes for validated names."""
    return [STRATEGY_BY_NAME[name] for name in names]