# services/bench_logger.py — RAM ring-buffer logger for bench debugging
#
# Captures snapshots of key system variables into a fixed-size circular
# buffer in RAM.  When the buffer is full, oldest entries are overwritten.
# Call dump() to print the entire buffer as CSV to the serial console
# for capture on a connected PC.

from config.settings import (
    BENCH_LOG_ACTIVE_CMD_A_MIN,
    BENCH_LOG_ACTIVE_INPUT_A_MIN,
    BENCH_LOG_ACTIVE_LEVEL_MIN,
    BENCH_LOG_ACTIVE_RPM_MIN,
    BENCH_LOG_FIELDS,
    BENCH_LOG_MAX_RECORDS,
    BENCH_LOG_PERSIST_ENABLE,
    BENCH_LOG_PERSIST_MAX_BYTES,
    BENCH_LOG_PERSIST_PATH,
    BENCH_LOG_SELECTIVE_CAPTURE,
)
from time import ticks_ms

try:
    import os
except Exception:
    os = None


class BenchLogger:
    """Fixed-size circular buffer for bench-session data capture."""

    def __init__(self, state, max_records=None):
        self._state = state
        self._max = max_records or BENCH_LOG_MAX_RECORDS
        self._buf = [None] * self._max
        self._idx = 0          # next write position
        self._count = 0        # total records written (for wrap detection)
        self._persist_enabled = bool(BENCH_LOG_PERSIST_ENABLE)
        self._persist_path = BENCH_LOG_PERSIST_PATH
        self._persist_max_bytes = int(BENCH_LOG_PERSIST_MAX_BYTES)
        self._persist_header = "tick_ms," + ",".join(BENCH_LOG_FIELDS)
        self._selective_capture = bool(BENCH_LOG_SELECTIVE_CAPTURE)
        self._init_persistent_log()

    # ------------------------------------------------------------------
    def _init_persistent_log(self):
        """Create/reset persistent CSV log on Pico flash if enabled."""
        if not self._persist_enabled or os is None:
            return
        try:
            # New firmware boot/session starts a fresh persistent log.
            with open(self._persist_path, "w") as f:
                f.write(self._persist_header + "\n")
        except Exception:
            # Never let filesystem issues affect runtime control.
            self._persist_enabled = False

    # ------------------------------------------------------------------
    def _persist_record(self, record):
        """Append one CSV row to persistent flash log with simple rollover."""
        if not self._persist_enabled or os is None:
            return
        try:
            try:
                size = os.stat(self._persist_path)[6]
            except Exception:
                size = 0
            if size >= self._persist_max_bytes:
                with open(self._persist_path, "w") as f:
                    f.write(self._persist_header + "\n")
            with open(self._persist_path, "a") as f:
                f.write(",".join(str(v) for v in record) + "\n")
        except Exception:
            # Disable persistence on repeated storage failures.
            self._persist_enabled = False

    # ------------------------------------------------------------------
    def snapshot(self):
        """Capture one record from SharedState into the ring buffer."""
        s = self._state
        if self._selective_capture and not self._is_active_sample(s):
            return
        record = (
            ticks_ms(),
            s.system_state,
            s.cap_voltage_v,
            s.vesc_mech_rpm,
            s.vesc_motor_current_a,
            s.vesc_input_current_a,
            s.vesc_duty_cycle,
            s.vesc_fault_code,
            s.requested_mode,
            s.requested_level,
            s.throttle_raw,
            s.throttle_valid,
            s.inhibit_motor_commands,
            s.assist_command_request,
            s.regen_command_request,
            s.motor_command_a,
        )
        self._buf[self._idx] = record
        self._idx = (self._idx + 1) % self._max
        self._count += 1
        self._persist_record(record)

    # ------------------------------------------------------------------
    @staticmethod
    def _is_active_sample(s):
        """Return True when the sample reflects meaningful riding activity."""
        if abs(s.vesc_mech_rpm) >= BENCH_LOG_ACTIVE_RPM_MIN:
            return True
        if abs(s.motor_command_a) >= BENCH_LOG_ACTIVE_CMD_A_MIN:
            return True
        if abs(s.vesc_input_current_a) >= BENCH_LOG_ACTIVE_INPUT_A_MIN:
            return True
        if abs(s.requested_level) >= BENCH_LOG_ACTIVE_LEVEL_MIN:
            return True
        return False

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
