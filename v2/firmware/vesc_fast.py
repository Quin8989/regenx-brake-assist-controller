# vesc_fast.py — on-target viper CRC16 (RGX-2-003 D11, gate FW-1).
#
# Imported opportunistically by vesc.py; absent/failed import falls back to
# the pure-Python table CRC with identical semantics. This module only
# compiles under MicroPython (viper syntax); host tests never import it.

import micropython
from array import array

_TAB = array("H", [0] * 256)
for _i in range(256):
    _c = _i << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x1021) if (_c & 0x8000) else (_c << 1)
        _c &= 0xFFFF
    _TAB[_i] = _c


@micropython.viper
def _crc16_viper(buf: ptr8, start: int, length: int, tab: ptr16) -> int:
    crc = 0
    i = start
    end = start + length
    while i < end:
        crc = ((crc << 8) & 0xFFFF) ^ int(tab[((crc >> 8) ^ int(buf[i])) & 0xFF])
        i += 1
    return crc


def crc16(buf, start, length):
    return _crc16_viper(buf, start, length, _TAB)
