import array

import control
import ui


def snap(**kw):
    sn = array.array("f", [0.0] * control.SN_LEN)
    for k, v in kw.items():
        sn[getattr(control, k)] = v
    return sn


def test_record_size_is_24():
    assert ui.REC_SIZE == 24


def test_record_roundtrip_scaling():
    sn = snap(SN_WHEEL_RPM=158.7, SN_ERPM=-7940.0, SN_VBANK=39.42,
              SN_IIN=-3.51, SN_IMOTOR=-12.27, SN_ICMD=-12.3,
              SN_SLIP=0.517, SN_THROTTLE=0.734, SN_VSYS=4.93,
              SN_STATE=control.RUN, SN_FAULT=2)
    buf = bytearray(ui.REC_SIZE)
    ui.pack_record(buf, 0, 123456, sn)
    r = ui.unpack_record(buf, 0)
    assert r["ms"] == 123456
    assert abs(r["wheel_rpm"] - 158.7) < 0.1
    assert abs(r["erpm"] + 7940) < 10
    assert abs(r["v_bank"] - 39.42) < 0.01
    assert abs(r["i_motor"] + 12.27) < 0.01
    assert abs(r["slip"] - 0.517) < 0.001
    assert abs(r["vsys"] - 4.93) < 0.001
    assert r["state"] == control.RUN and r["fault"] == 2


def test_ring_wraps_and_orders():
    ring = ui.Ring(n_records=8)
    sn = snap()
    for i in range(11):                     # 3 past capacity
        sn[control.SN_WHEEL_RPM] = float(i)
        ring.append(i, sn)
    assert ring.count == 8 and ring.dropped == 3
    got = [ui.unpack_record(ring.buf, off)["ms"] for off in ring.records()]
    assert got == list(range(3, 11))        # oldest three overwritten


def test_ring_clear():
    ring = ui.Ring(n_records=4)
    ring.append(1, snap())
    ring.clear()
    assert ring.count == 0
    assert list(ring.records()) == []


def test_record_clamps_out_of_range():
    sn = snap(SN_WHEEL_RPM=99999.0, SN_ERPM=9e6, SN_IIN=999.0)
    buf = bytearray(ui.REC_SIZE)
    ui.pack_record(buf, 0, 1, sn)           # must not raise
    r = ui.unpack_record(buf, 0)
    assert r["wheel_rpm"] <= 6553.5
    assert r["i_in"] <= 327.67
