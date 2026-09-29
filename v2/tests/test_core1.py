"""Core 1's flash policy, on the host: a fake machine module, a temp log dir."""
import sys
import types
from array import array

import pytest

import config as C
import control as K
import ui


@pytest.fixture
def core1(tmp_path, monkeypatch):
    machine = types.ModuleType("machine")
    machine.Pin = lambda *a, **k: None
    machine.I2C = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "machine", machine)
    monkeypatch.setattr(C, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(ui.Core1, "_render", lambda self, sn: None)
    opened = []

    def counting_open(path, mode):
        f = open(path, mode)
        opened.append(path)
        return f
    monkeypatch.setitem(ui.__dict__, "open", counting_open)
    sn = array("f", [0.0] * K.SN_LEN)
    c = ui.Core1(sn)
    c.opened = opened
    c.t = 0
    return c


def run(c, seconds, moving):
    c.sn[K.SN_WHEEL] = 150.0 if moving else 0.0
    for _ in range(int(seconds * 50)):          # core 1 passes every 20 ms
        c.t += 20
        c.step(c.t, lambda a, b: a - b)


def size(c):
    import os
    p = C.LOG_DIR + "/" + c.name
    return os.path.getsize(p) if os.path.exists(p) else 0


def test_parked_flushes_once_then_stays_off_flash(core1):
    run(core1, 60, moving=True)
    assert core1.opened == []                    # never while moving
    run(core1, 60, moving=False)
    assert len(core1.opened) == 1 and core1.file is None     # one session, closed
    hdr = len(ui.header(0.0))
    # 60 s of riding at 10 Hz + 3 idle records logged during the 3 s dwell
    assert size(core1) == hdr + (600 + 3) * ui.REC_SIZE
    run(core1, 100, moving=False)                # 157 idle records pending
    assert len(core1.opened) == 1                # 1 Hz idle records: no new session
    run(core1, 20, moving=False)                 # passes FLUSH_RECORDS
    assert len(core1.opened) == 2


def test_moving_off_mid_flush_never_closes_while_moving(core1):
    run(core1, 60, moving=True)
    run(core1, 3.04, moving=False)               # 3 of 4 chunks written
    assert core1.file is not None
    closes = []
    real = core1.file.close
    core1.file.close = lambda: (closes.append(1), real())
    run(core1, 30, moving=True)
    assert closes == [] and core1.file is not None
    run(core1, 10, moving=False)                 # resumes at the next stop
    assert closes == [1] and core1.file is None


def test_write_error_closes_the_file_and_counts(core1, monkeypatch):
    run(core1, 60, moving=True)
    run(core1, 3.04, moving=False)
    f = core1.file

    def boom(mv):
        raise OSError(28)
    monkeypatch.setattr(f, "write", boom)
    run(core1, 1, moving=False)
    assert core1.file is None and f.closed and core1.errors >= 1


def test_low_space_deletes_oldest_rides_then_drops(core1, monkeypatch, tmp_path):
    import os
    for name in ("0000.bin", "0001.bin"):
        with open(os.path.join(C.LOG_DIR, name), "wb") as f:
            f.write(b"x")
    core1.name = "0002.bin"
    free = [0]
    monkeypatch.setattr(os, "statvfs", lambda p: (1, 1, 1, free[0]))
    run(core1, 10, moving=True)
    run(core1, 5, moving=False)
    assert sorted(os.listdir(C.LOG_DIR)) == ["0002.bin"]    # others deleted
    assert size(core1) == len(ui.header(0.0))               # chunks dropped, not written
