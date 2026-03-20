# tests/test_bench_logger.py — Unit tests for RAM ring-buffer bench logger

import sys
import types

# Stub time module before importing bench_logger
_tick = 0
_time_mod = types.ModuleType("time")
_time_mod.ticks_ms = lambda: _tick
_time_mod.ticks_diff = lambda a, b: a - b
sys.modules.setdefault("time", _time_mod)

from core import SharedState, SystemState, CommandMode
from services.bench_logger import BenchLogger
import services.bench_logger as _bl_mod


def _set_tick(ms):
    global _tick
    _tick = ms
    fn = lambda: _tick
    _time_mod.ticks_ms = fn
    _bl_mod.ticks_ms = fn


class TestBenchLoggerBasic:
    """Core ring-buffer operations."""

    def test_empty_buffer(self):
        bl = BenchLogger(SharedState(), max_records=10)
        assert bl.records_stored == 0

    def test_snapshot_stores_record(self):
        state = SharedState()
        bl = BenchLogger(state, max_records=10)
        bl.snapshot()
        assert bl.records_stored == 1

    def test_snapshot_captures_state(self):
        state = SharedState()
        state.system_state = SystemState.ASSIST
        state.cap_voltage_v = 25.5
        state.wheel_speed_rpm = 100.0
        bl = BenchLogger(state, max_records=10)
        _set_tick(1234)
        bl.snapshot()
        rec = bl._buf[0]
        assert rec[0] == 1234          # tick_ms
        assert rec[1] == "ASSIST"       # system_state
        assert rec[2] == 25.5           # cap_voltage_v
        assert rec[3] == 100.0          # wheel_speed_rpm

    def test_count_up_to_max(self):
        bl = BenchLogger(SharedState(), max_records=5)
        for _ in range(5):
            bl.snapshot()
        assert bl.records_stored == 5

    def test_clear_resets(self):
        bl = BenchLogger(SharedState(), max_records=10)
        for _ in range(3):
            bl.snapshot()
        bl.clear()
        assert bl.records_stored == 0


class TestBenchLoggerWrap:
    """Ring buffer wrap-around behavior."""

    def test_wraps_at_max(self):
        bl = BenchLogger(SharedState(), max_records=3)
        for _ in range(5):
            bl.snapshot()
        # Should only keep latest 3
        assert bl.records_stored == 3

    def test_oldest_overwritten(self):
        state = SharedState()
        bl = BenchLogger(state, max_records=3)
        for i in range(5):
            _set_tick(i * 100)
            bl.snapshot()
        # Buffer should contain ticks 200, 300, 400 (oldest two overwritten)
        n = bl.records_stored
        assert n == 3
        # Oldest is at write pointer (index = 5 % 3 = 2)
        start = bl._idx  # next write pos = oldest
        ticks = [bl._buf[(start + j) % 3][0] for j in range(3)]
        assert ticks == [200, 300, 400]


class TestBenchLoggerDump:
    """CSV dump output."""

    def test_dump_empty(self, capsys):
        bl = BenchLogger(SharedState(), max_records=5)
        bl.dump()
        out = capsys.readouterr().out
        assert "empty" in out

    def test_dump_header_and_rows(self, capsys):
        state = SharedState()
        state.system_state = SystemState.READY
        state.cap_voltage_v = 18.0
        bl = BenchLogger(state, max_records=10)
        _set_tick(0)
        bl.snapshot()
        _set_tick(500)
        bl.snapshot()
        bl.dump()
        out = capsys.readouterr().out
        lines = out.strip().split("\n")
        assert lines[0].startswith("tick_ms,")
        assert len(lines) == 3  # header + 2 data rows

    def test_dump_order_after_wrap(self, capsys):
        state = SharedState()
        bl = BenchLogger(state, max_records=3)
        for i in range(5):
            _set_tick(i * 10)
            bl.snapshot()
        bl.dump()
        out = capsys.readouterr().out
        lines = out.strip().split("\n")
        # 3 data rows (oldest first: 20, 30, 40)
        data_lines = lines[1:]
        assert len(data_lines) == 3
        first_tick = int(data_lines[0].split(",")[0])
        last_tick = int(data_lines[-1].split(",")[0])
        assert first_tick == 20
        assert last_tick == 40


class TestBenchLoggerFields:
    """Verify all 10 logged fields are captured."""

    def test_record_length(self):
        bl = BenchLogger(SharedState(), max_records=5)
        bl.snapshot()
        rec = bl._buf[0]
        # 1 timestamp + 10 state fields = 11
        assert len(rec) == 11

    def test_all_fields_from_state(self):
        state = SharedState()
        state.system_state = SystemState.REGEN
        state.cap_voltage_v = 30.0
        state.wheel_speed_rpm = 50.0
        state.vesc_mech_rpm = 245.0
        state.requested_mode = CommandMode.REGEN
        state.requested_level = 0.75
        state.assist_command_request = 0.0
        state.regen_command_request = 12.5
        state.gear_carrier_speed_rpm = 248.0
        state.regen_speed_error_rpm = 3.0
        bl = BenchLogger(state, max_records=5)
        _set_tick(999)
        bl.snapshot()
        rec = bl._buf[0]
        assert rec == (
            999,
            "REGEN",
            30.0,
            50.0,
            245.0,
            "REGEN",
            0.75,
            0.0,
            12.5,
            248.0,
            3.0,
        )
