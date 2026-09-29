import struct
from array import array

import pytest

import config as C
import control as K
import ui


def snap(**kw):
    sn = array("f", [0.0] * K.SN_LEN)
    for k, v in kw.items():
        sn[getattr(K, k)] = v
    return sn


def test_record_is_24_bytes():
    assert ui.REC_SIZE == 24


def test_record_roundtrip_and_flags():
    sn = snap(SN_WHEEL=158.7, SN_ERPM=7940.0, SN_VIN=39.42, SN_IIN=-3.51,
              SN_IMOTOR=-12.27, SN_ICMD=-12.3, SN_THR=0.734, SN_VSYS=4.93,
              SN_TFET=41.5, SN_STATE=K.LIMP_FAULT, SN_BRAKE=1.0, SN_FAULT=2)
    buf = bytearray(ui.REC_SIZE)
    ui.pack(buf, 0, 123456, sn)
    (ms, wr, e, vin, iin, im, ic, thr, vs, tf, st, fault) = struct.unpack(ui.REC_FMT, buf)
    assert ms == 123456 and wr == 1587 and e == 794 and vin == 3942
    assert (iin, im, ic, thr, vs, tf) == (-351, -1227, -1230, 734, 4930, 415)
    assert st & 3 == K.LIMP_FAULT and st >> 2 == 1 and fault == 2


def test_record_clamps_out_of_range():
    buf = bytearray(ui.REC_SIZE)
    ui.pack(buf, 0, 1, snap(SN_WHEEL=99999.0, SN_ERPM=9e6, SN_IIN=-999.0))
    _, wr, e, _, iin, *_ = struct.unpack(ui.REC_FMT, buf)
    assert (wr, e, iin) == (65535, 32767, -32768)


def test_header_names_the_format():
    h = ui.header(6.06)
    assert h.startswith(b"RGX2 rec=" + ui.REC_FMT.encode()) and b"fw=6.06" in h
    assert h.endswith(b"\n")


def records(mv):
    return [struct.unpack_from(ui.REC_FMT, mv, o)[0] for o in range(0, len(mv), ui.REC_SIZE)]


def test_log_flushes_everything_new_in_order_across_the_wrap():
    log = ui.Log(8)
    for t in range(5):
        log.add(t, snap())
    assert records(log.chunk()) == [0, 1, 2, 3, 4] and log.chunk() is None
    for t in range(5, 11):                    # wraps the 8-slot ring
        log.add(t, snap())
    got = []
    while True:
        mv = log.chunk()
        if mv is None:
            break
        got += records(mv)
    assert got == list(range(5, 11))


def test_log_overflow_drops_the_oldest():
    log = ui.Log(4)
    for t in range(10):
        log.add(t, snap())
    got = records(log.chunk()) + records(log.chunk())
    assert got == [6, 7, 8, 9]


def test_chunks_are_bounded():
    log = ui.Log(1000)
    for t in range(500):
        log.add(t, snap())
    assert len(log.chunk()) == C.FLUSH_RECORDS * ui.REC_SIZE


@pytest.mark.parametrize("kw, expected", [
    ({}, True),
    ({"SN_WHEEL": 30.0}, False),
    ({"SN_THR": 0.2}, False),
    ({"SN_BRAKE": 1.0}, False),
    ({"SN_ICMD": -3.0}, False),
    ({"SN_ERPM": 800.0}, False),           # rotor turning: carrier held, wheel moving
])
def test_standstill_needs_everything_quiet(kw, expected):
    assert ui.still(snap(**kw)) is expected
