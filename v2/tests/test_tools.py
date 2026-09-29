import os
import sys
from array import array

import control as K
import ui

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import decode_log  # noqa: E402


def test_decoder_reads_what_the_logger_writes():
    log = ui.Log(16)
    sn = array("f", [0.0] * K.SN_LEN)
    sn[K.SN_WHEEL] = 150.0
    sn[K.SN_ERPM] = 150.0 * 10 * 5.0 * 0.75       # slip 0.25 at k=5, pp=10
    sn[K.SN_VIN] = 31.25
    sn[K.SN_ICMD] = -12.5
    sn[K.SN_STATE] = K.RUN
    log.add(1000, sn)
    data = ui.header(6.06) + bytes(log.chunk())
    (meta, r), = list(decode_log.records(data))
    assert meta["fw"] == "6.06"
    assert r["ms"] == 1000 and r["v_in"] == 31.25 and r["i_cmd"] == -12.5
    assert r["state"] == "RUN"
    assert abs(r["slip"] - 0.25) < 0.01
    assert abs(r["kmh"] - 150.0 * 2.1 * 0.06) < 1e-6
