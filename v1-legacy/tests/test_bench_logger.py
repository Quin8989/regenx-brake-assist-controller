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
    """Snapshot records the full ride-debug field set from SharedState."""
    state = SharedState()
    state.system_state = SystemState.REGEN
    state.cap_voltage_v = 30.0
    state.vesc_mech_rpm = 245.0
    state.vesc_motor_current_a = 15.0
    state.vesc_input_current_a = -8.0
    state.vesc_duty_cycle = 0.42
    state.vesc_fault_code = 3
    state.requested_mode = CommandMode.REGEN
    state.requested_level = 0.75
    state.throttle_raw = 1234
    state.throttle_valid = True
    state.inhibit_motor_commands = False
    state.assist_command_request = 0.0
    state.regen_command_request = 12.5
    state.motor_command_a = -12.5
    bl = BenchLogger(state, max_records=5)
    _set_tick(999)
    bl.snapshot()
    assert bl.records_stored == 1
    assert bl._buf[0] == (
        999,
        "REGEN",
        30.0,
        245.0,
        15.0,
        -8.0,
        0.42,
        3,
        "REGEN",
        0.75,
        1234,
        True,
        False,
        0.0,
        12.5,
        -12.5,
    )


def test_ring_buffer_wraps_and_preserves_order():
    """After overflow, oldest records are overwritten; dump shows correct order."""
    state = SharedState()
    state.requested_level = 0.2
    bl = BenchLogger(state, max_records=3)
    for i in range(5):
        _set_tick(i * 100)
        bl.snapshot()
    assert bl.records_stored == 3
    start = bl._idx
    ticks = [bl._buf[(start + j) % 3][0] for j in range(3)]
    assert ticks == [200, 300, 400]


def test_clear_resets_buffer():
    s = SharedState()
    s.requested_level = 0.2
    bl = BenchLogger(s, max_records=10)
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
    state.requested_level = 0.2
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


def test_dump_header_matches_expanded_field_set(capsys):
    state = SharedState()
    state.requested_level = 0.2
    bl = BenchLogger(state, max_records=2)
    _set_tick(0)
    bl.snapshot()
    bl.dump()

    header = capsys.readouterr().out.strip().split("\n")[0]
    assert header == (
        "tick_ms,system_state,cap_voltage_v,vesc_mech_rpm,vesc_motor_current_a,"
        "vesc_input_current_a,vesc_duty_cycle,vesc_fault_code,requested_mode,"
        "requested_level,throttle_raw,throttle_valid,inhibit_motor_commands,"
        "assist_command_request,regen_command_request,motor_command_a"
    )


def test_dump_order_after_wrap(capsys):
    s = SharedState()
    s.requested_level = 0.2
    bl = BenchLogger(s, max_records=3)
    for i in range(5):
        _set_tick(i * 10)
        bl.snapshot()
    bl.dump()
    lines = capsys.readouterr().out.strip().split("\n")
    data = lines[1:]
    assert int(data[0].split(",")[0]) == 20
    assert int(data[-1].split(",")[0]) == 40


def test_selective_capture_skips_idle_samples():
    s = SharedState()
    bl = BenchLogger(s, max_records=5)
    _set_tick(0)
    bl.snapshot()
    assert bl.records_stored == 0

    # Mark as active via requested level.
    s.requested_level = 0.2
    _set_tick(100)
    bl.snapshot()
    assert bl.records_stored == 1
