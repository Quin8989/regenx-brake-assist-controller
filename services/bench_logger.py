# services/bench_logger.py — RAM ring-buffer logger for bench debugging
#
# Captures snapshots of key system variables into a fixed-size circular
# buffer in RAM.  When the buffer is full, oldest entries are overwritten.
# Call dump() to print the entire buffer as CSV to the serial console
# for capture on a connected PC.

from config.settings import BENCH_LOG_FIELDS, BENCH_LOG_MAX_RECORDS
from time import ticks_ms


class BenchLogger:
    """Fixed-size circular buffer for bench-session data capture."""

    def __init__(self, state, max_records=None):
        self._state = state
        self._max = max_records or BENCH_LOG_MAX_RECORDS
        self._buf = [None] * self._max
        self._idx = 0          # next write position
        self._count = 0        # total records written (for wrap detection)

    # ------------------------------------------------------------------
    def snapshot(self):
        """Capture one record from SharedState into the ring buffer."""
        s = self._state
        record = (
            ticks_ms(),
            s.system_state,
            s.cap_voltage_v,
            s.wheel_speed_rpm,
            s.vesc_mech_rpm,
            s.requested_mode,
            s.requested_level,
            s.assist_command_request,
            s.regen_command_request,
            s.gear_carrier_speed_rpm,
            s.regen_speed_error_rpm,
        )
        self._buf[self._idx] = record
        self._idx = (self._idx + 1) % self._max
        self._count += 1

    # ------------------------------------------------------------------
    @property
    def records_stored(self):
        """Number of valid records in the buffer (up to max)."""
        return min(self._count, self._max)

    # ------------------------------------------------------------------
    def dump(self):
        """Print the full buffer as CSV lines (oldest first) to serial."""
        n = self.records_stored
        if n == 0:
            print("bench_log: (empty)")
            return

        # Header
        header = "tick_ms," + ",".join(BENCH_LOG_FIELDS)
        print(header)

        # Determine the read start — oldest record
        if self._count <= self._max:
            start = 0
        else:
            start = self._idx  # oldest record is at current write pos

        for i in range(n):
            rec = self._buf[(start + i) % self._max]
            print(",".join(str(v) for v in rec))

    # ------------------------------------------------------------------
    def clear(self):
        """Reset the buffer without reallocating."""
        self._idx = 0
        self._count = 0
