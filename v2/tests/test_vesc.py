import random
import struct

import pytest

import config as C
import vesc
from conftest import FakeUART, fw_reply, telem


def link_with():
    u = FakeUART()
    return vesc.Link(u), u


def feed(link, uart, data):
    uart.inject(data)
    while uart._rx:
        link.poll()


# --- CRC and framing -----------------------------------------------------------
def test_crc16_xmodem_vector():
    assert vesc.crc16(b"123456789", 0, 9) == 0x31C3


def test_crc16_matches_bitwise_reference():
    def ref(data):
        crc = 0
        for b in data:
            crc ^= b << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
                crc &= 0xFFFF
        return crc
    rng = random.Random(42)
    for _ in range(50):
        blob = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 80)))
        assert vesc.crc16(blob, 0, len(blob)) == ref(blob)


def test_set_current_frame_on_the_wire():
    link, u = link_with()
    link.send(-12.5)
    f = u.tx[:10]
    assert f[:3] == bytes((2, 5, vesc.COMM_SET_CURRENT)) and f[9] == 3
    assert struct.unpack_from(">i", f, 3)[0] == -12500
    assert vesc.crc16(f, 2, 5) == (f[7] << 8 | f[8])


# --- schedule --------------------------------------------------------------------
def count(tx, frame):
    return bytes(tx).count(bytes(frame))


def test_schedule_keepalive_telemetry_temp_and_fw_retry():
    link, u = link_with()
    for _ in range(100):
        link.send(0.0)
    assert bytes(u.tx).count(bytes((2, 5, vesc.COMM_SET_CURRENT))) == 100
    assert count(u.tx, link._req) + count(u.tx, link._req_t) == 100 // C.TELEM_DIV
    assert count(u.tx, link._req_t) == 1
    assert count(u.tx, link._req_fw) == 100 // C.FW_REQ_DIV       # retried
    feed(link, u, fw_reply(6, 6))
    assert link.fw == pytest.approx(6.06)
    n = count(u.tx, link._req_fw)
    for _ in range(100):
        link.send(0.0)
    assert count(u.tx, link._req_fw) == n                         # answered: stops


def test_observer_mode_never_commands_current(monkeypatch):
    monkeypatch.setattr(C, "SEND_CURRENT", False)
    link, u = link_with()
    for _ in range(20):
        link.send(30.0)
    assert bytes((2, 5, vesc.COMM_SET_CURRENT)) not in bytes(u.tx)
    assert count(u.tx, link._req) == 10


# --- decode and link health ------------------------------------------------------
def test_decode_both_masks():
    link, u = link_with()
    feed(link, u, telem(erpm=-7940, v_in=39.4, i_in=-3.5, i_motor=-12.25))
    assert (link.erpm, link.v_in, link.i_in, link.i_motor, link.fault) == \
        (-7940.0, pytest.approx(39.4), -3.5, -12.25, 0)
    feed(link, u, telem(v_in=20.0, temp=71.5))
    assert link.temp_fet == pytest.approx(71.5) and link.v_in == pytest.approx(20.0)


def test_ok_counts_clean_frames_and_resets_on_fault_or_silence():
    link, u = link_with()
    for _ in range(5):
        feed(link, u, telem())
    assert link.ok == 5
    feed(link, u, telem(fault=3))
    assert link.ok == 0 and link.fault == 3
    feed(link, u, telem())
    assert link.ok == 1
    for _ in range(C.LINK_TIMEOUT_TICKS + 1):
        link.send(0.0)
    assert link.ok == 0


def test_unknown_opcode_is_ignored():
    link, u = link_with()
    feed(link, u, bytes(vesc.frame(bytes((36,)) + bytes(16))) + telem(erpm=100))
    assert link.frames == 1 and link.erpm == 100.0


# --- parser robustness (RGX-2-003 §4; review F06/F26/F35) ------------------------
GOOD = telem(erpm=1234, v_in=30.0, i_motor=5.5)      # i_motor raw 0x226: holds a 0x02


def test_random_chunking_roundtrip():
    rng = random.Random(7)
    frames = [telem(erpm=rng.randrange(-9000, 9000), v_in=rng.uniform(10, 40))
              for _ in range(60)]
    link, u = link_with()
    stream = b"".join(frames)
    i = 0
    while i < len(stream):
        n = rng.randrange(1, 40)
        feed(link, u, stream[i:i + n])
        i += n
    assert link.frames == 60 and link.bad == 0


@pytest.mark.parametrize("junk", [b"\x02", b"\x02\x14", b"\x02\x50\x00", b"\x02\xc8",
                                  b"\x03\x00\x14", b"\x00\x02\x02\x02"])
def test_stray_bytes_cost_no_good_frame(junk):
    link, u = link_with()
    feed(link, u, junk + GOOD + GOOD)
    assert link.frames == 2


def test_every_single_bit_flip_is_rejected_and_the_next_frame_survives():
    good = bytearray(GOOD)
    for pos in range(len(good)):
        for bit in range(8):
            bad = bytearray(good)
            bad[pos] ^= 1 << bit
            link, u = link_with()
            feed(link, u, bytes(bad) + GOOD)
            assert link.frames == 1, (pos, bit)
            assert link.i_motor == 5.5 and link.erpm == 1234.0


@pytest.mark.parametrize("seed", range(200))
def test_garbage_then_the_first_good_frame_is_delivered(seed):
    rng = random.Random(seed)
    link, u = link_with()
    feed(link, u, bytes(rng.randrange(256) for _ in range(rng.randrange(1, 600))))
    before = link.frames
    feed(link, u, GOOD)
    assert link.frames == before + 1 and link.erpm == 1234.0


def test_truncated_frame_then_good():
    link, u = link_with()
    feed(link, u, GOOD[:8] + GOOD)
    assert link.frames == 1
