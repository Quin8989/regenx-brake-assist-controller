import random
import struct

import config
import vesc
from conftest import make_frame, selective_payload, fw_payload


# --- CRC ---------------------------------------------------------------------
def test_crc16_xmodem_vector():
    data = b"123456789"
    assert vesc.crc16(data, 0, len(data)) == 0x31C3  # CRC-16/XMODEM check


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


# --- framing -----------------------------------------------------------------
def test_set_current_frame_layout():
    out = bytearray(16)
    n = vesc.pack_set_current(out, -12.5)
    assert n == 10
    assert out[0] == 0x02 and out[1] == 5
    assert out[2] == vesc.COMM_SET_CURRENT
    assert struct.unpack_from(">i", out, 3)[0] == -12500
    assert out[n - 1] == 0x03


def test_telemetry_request_mask():
    out = bytearray(16)
    n = vesc.pack_telemetry_req(out, config.TELEM_MASK)
    assert n == 10
    assert struct.unpack_from(">I", out, 3)[0] == 0x818C


# --- parser round trips ------------------------------------------------------
def collect_parser():
    got = []
    p = vesc.FrameParser(lambda mv, n: got.append(bytes(mv[:n])))
    return p, got


def test_parser_roundtrip_random_chunking():
    rng = random.Random(7)
    payloads = [bytes([vesc.COMM_GET_VALUES_SELECTIVE]) +
                bytes(rng.randrange(256) for _ in range(rng.randrange(1, 60)))
                for _ in range(40)]
    stream = b"".join(make_frame(p) for p in payloads)
    p, got = collect_parser()
    i = 0
    while i < len(stream):
        n = rng.randrange(1, 17)
        chunk = stream[i:i + n]
        p.feed(chunk, len(chunk))
        i += n
    assert got == payloads
    assert p.crc_fail == 0 and p.resync == 0


def test_parser_ignores_long_frames_quietly():
    # 0x03-start (long) frames are junk by design (see FrameParser docstring)
    payload = bytes(range(200))
    stream = b"\x03" + bytes([0, len(payload)]) + payload + b"\x00\x00\x03"
    p, got = collect_parser()
    p.feed(stream, len(stream))
    assert got == []
    assert p.long_seen >= 1
    good = make_frame(selective_payload(config.TELEM_MASK, v_in=20.0))
    p.feed(good, len(good))
    p.feed(good, len(good))
    assert len(got) >= 1              # and it recovers


def test_parser_never_false_accepts_corruption():
    # CRC16 detects every single-bit error: any delivered frame from a
    # 1-bit-corrupted stream must be byte-identical to the true payload,
    # and recovery must cost at most one following good frame.
    rng = random.Random(99)
    payload = selective_payload(config.TELEM_MASK, erpm=1234, v_in=30.0)
    frame = bytearray(make_frame(payload))
    good = make_frame(payload)
    for pos in range(len(frame)):
        for _ in range(2):
            mutated = bytearray(frame)
            mutated[pos] ^= 1 << rng.randrange(8)
            if bytes(mutated) == good:
                continue
            p, got = collect_parser()
            p.feed(mutated, len(mutated))
            assert all(g == payload for g in got)
            p.feed(good, len(good))
            p.feed(good, len(good))
            assert got[-1] == payload
            assert all(g == payload for g in got)


def test_parser_recovers_from_garbage_flood():
    rng = random.Random(5)
    garbage = bytes(rng.randrange(256) for _ in range(5000))
    p, got = collect_parser()
    p.feed(garbage, len(garbage))
    good = make_frame(selective_payload(config.TELEM_MASK, v_in=20.0))
    p.feed(good, len(good))
    p.feed(good, len(good))       # at worst one frame lost to a torn state
    assert len(got) >= 1


def test_parser_truncated_then_good():
    payload = selective_payload(config.TELEM_MASK, erpm=500)
    frame = make_frame(payload)
    p, got = collect_parser()
    p.feed(frame[:8], 8)               # truncated: parser left mid-frame
    good = make_frame(payload)
    p.feed(good, len(good))
    p.feed(good, len(good))
    assert payload in got


# --- selective parse ---------------------------------------------------------
def test_parse_selective_values():
    v = vesc.Values()
    payload = selective_payload(config.TELEM_MASK, erpm=-7940, v_in=39.4,
                                i_in=-3.5, i_motor=-12.25, fault=0)
    assert vesc.parse_selective(payload, len(payload), v)
    assert v.erpm == -7940
    assert abs(v.v_in - 39.4) < 1e-6
    assert abs(v.i_in + 3.5) < 1e-6
    assert abs(v.i_motor + 12.25) < 1e-6
    assert v.fault == 0


def test_parse_selective_with_temp_mask():
    v = vesc.Values()
    payload = selective_payload(config.TELEM_MASK_TEMP, temp_fet=71.5,
                                v_in=20.0)
    assert vesc.parse_selective(payload, len(payload), v)
    assert abs(v.temp_fet - 71.5) < 1e-6


def test_parse_selective_short_payload_rejected():
    v = vesc.Values()
    payload = selective_payload(config.TELEM_MASK)[:-3]
    assert not vesc.parse_selective(payload, len(payload), v)


# --- link scheduler ----------------------------------------------------------
def test_link_schedule_and_values(uart, link):
    for t in range(20):
        link.tick(t * 10, 1.0)
    # 20 commands + 10 telemetry requests
    assert uart.tx.count(bytes([0x02, 5, vesc.COMM_SET_CURRENT])) == 20
    assert uart.tx.count(bytes([0x02, 5,
                                vesc.COMM_GET_VALUES_SELECTIVE])) == 10
    uart.inject(make_frame(selective_payload(config.TELEM_MASK, erpm=1000,
                                             v_in=25.0)))
    link.tick(210, 0.0)
    assert link.values.erpm == 1000
    assert link.age_ms(215) == 5


def test_link_rtt_and_fw(uart, link):
    link.request_fw()
    uart.inject(make_frame(fw_payload(5, 2)))
    link.poll(30)
    assert (link.values.fw_major, link.values.fw_minor) == (5, 2)
