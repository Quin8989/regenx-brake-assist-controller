# tests/test_bench_logger.py — RAM ring-buffer bench logger

import sys
import types

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


def test_snapshot_captures_all_fields():
    """Snapshot records all 9 fields from SharedState."""
    state = SharedState()
    state.system_state = SystemState.REGEN
    state.cap_voltage_v = 30.0
    state.vesc_mech_rpm = 245.0
    state.vesc_motor_current_a = 15.0
    state.requested_mode = CommandMode.REGEN
    state.requested_level = 0.75
    state.assist_command_request = 0.0
    state.regen_command_request = 12.5
    bl = BenchLogger(state, max_records=5)
    _set_tick(999)
    bl.snapshot()
    assert bl.records_stored == 1
    assert bl._buf[0] == (999, "REGEN", 30.0, 245.0, 15.0, "REGEN", 0.75, 0.0, 12.5)


def test_ring_buffer_wraps_and_preserves_order():
    """After overflow, oldest records are overwritten; dump shows correct order."""
    state = SharedState()
    bl = BenchLogger(state, max_records=3)
    for i in range(5):
        _set_tick(i * 100)
        bl.snapshot()
    assert bl.records_stored == 3
    start = bl._idx
    ticks = [bl._buf[(start + j) % 3][0] for j in range(3)]
    assert ticks == [200, 300, 400]


def test_clear_resets_buffer():
    bl = BenchLogger(SharedState(), max_records=10)
    for _ in range(3):
        bl.snapshot()
    bl.clear()
    assert bl.records_stored == 0


def test_dump_empty_and_nonempty(capsys):
    """Empty buffer prints 'empty'; nonempty prints header + rows."""
    bl = BenchLogger(SharedState(), max_records=5)
    bl.dump()
    assert "empty" in capsys.readouterr().out

    state = SharedState()
    state.system_state = SystemState.REGEN
    bl2 = BenchLogger(state, max_records=10)
    _set_tick(0)
    bl2.snapshot()
    _set_tick(500)
    bl2.snapshot()
    bl2.dump()
    out = capsys.readouterr().out
    lines = out.strip().split("\n")
    assert lines[0].startswith("tick_ms,")
    assert len(lines) == 3  # header + 2 data rows


def test_dump_order_after_wrap(capsys):
    bl = BenchLogger(SharedState(), max_records=3)
    for i in range(5):
        _set_tick(i * 10)
        bl.snapshot()
    bl.dump()
    lines = capsys.readouterr().out.strip().split("\n")
    data = lines[1:]
    assert int(data[0].split(",")[0]) == 20
    assert int(data[-1].split(",")[0]) == 40
