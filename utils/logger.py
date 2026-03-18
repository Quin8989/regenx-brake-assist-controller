# utils/logger.py — Debug and trace logging
#
# Prints structured debug information to console / serial.
# Supports enabling/disabling categories to avoid flooding output.

from time import ticks_ms

# Log categories — set to True to enable
_ENABLED = {
    "startup": True,
    "telemetry": False,
    "faults": True,
    "state": True,
    "precharge": True,
    "commands": False,
    "loop": False,
}


class Logger:
    def _emit(self, level, category, msg):
        if not _ENABLED.get(category, False):
            return
        print("[{:>8d}] {} {}: {}".format(ticks_ms(), level, category, msg))

    def info(self, category, msg):
        self._emit("INFO", category, msg)

    def warn(self, category, msg):
        self._emit("WARN", category, msg)

    def error(self, category, msg):
        self._emit("ERR ", category, msg)

    def debug(self, category, msg):
        self._emit("DBG ", category, msg)

    @staticmethod
    def enable(category):
        _ENABLED[category] = True

    @staticmethod
    def disable(category):
        _ENABLED[category] = False

    # TODO: Decide the primary debug channel (likely USB serial)
    # TODO: Define log rate limits
    # TODO: Define a simple log format for bench testing
    # TODO: Decide whether logs can be disabled entirely for demo mode
