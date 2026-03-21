# scripts/vesc_store_config.py — Save VESC RAM config to flash
#
# ONLY run this after vesc_write_config.py has verified all changes.
# This writes the current RAM config to permanent flash storage.
#
# Run: mpremote run scripts/vesc_store_config.py

import struct
from time import sleep_ms, ticks_ms, ticks_diff
from machine import UART, Pin

uart = UART(0, baudrate=115200, tx=Pin(0), rx=Pin(1), rxbuf=1024)

def crc16(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc

def wrap_frame(payload):
    length = len(payload)
    if length <= 255:
        frame = bytes([0x02, length]) + payload
    else:
        frame = bytes([0x03, length >> 8, length & 0xFF]) + payload
    c = crc16(payload)
    frame += struct.pack(">H", c)
    frame += bytes([0x03])
    return frame

def try_extract(buf):
    if len(buf) < 6:
        return None
    idx = 0
    while idx < len(buf):
        if buf[idx] == 0x02 and idx + 4 < len(buf):
            length = buf[idx + 1]
            if idx + length + 5 <= len(buf):
                payload = bytes(buf[idx + 2:idx + 2 + length])
                crc_recv = (buf[idx + 2 + length] << 8) | buf[idx + 3 + length]
                if crc16(payload) == crc_recv:
                    return payload
            idx += 1
        elif buf[idx] == 0x03 and idx + 5 < len(buf):
            length = (buf[idx + 1] << 8) | buf[idx + 2]
            if length > 0 and length < 10000:
                if idx + length + 6 <= len(buf):
                    payload = bytes(buf[idx + 3:idx + 3 + length])
                    crc_recv = (buf[idx + 3 + length] << 8) | buf[idx + 4 + length]
                    if crc16(payload) == crc_recv:
                        return payload
            idx += 1
        else:
            idx += 1
    return None

def read_mcconf():
    uart.read()
    sleep_ms(20)
    uart.write(wrap_frame(bytes([14])))  # COMM_GET_MCCONF
    sleep_ms(100)
    buf = bytearray()
    start = ticks_ms()
    while ticks_diff(ticks_ms(), start) < 5000:
        data = uart.read()
        if data:
            buf.extend(data)
            start = ticks_ms()
        sleep_ms(5)
        if len(buf) >= 6 and buf[0] == 0x03:
            expected = ((buf[1] << 8) | buf[2]) + 6
            if len(buf) >= expected:
                break
    payload = try_extract(buf)
    if payload and payload[0] == 14 and len(payload) > 50:
        return payload
    return None

# ---- Sanity check: read current RAM config and verify it has our values ----
EXPECTED = [
    (6,  "u8",  2,     "Motor type (FOC)"),
    (8,  "f32", 40.0,  "Motor max current"),
    (12, "f32", -40.0, "Motor min current"),
    (16, "f32", 40.0,  "Battery max current"),
    (20, "f32", -40.0, "Battery min current"),
    (48, "f32", 15.0,  "Min input voltage"),
    (52, "f32", 42.0,  "Max input voltage"),
    (56, "f32", 15.0,  "Battery cutoff start"),
    (60, "f32", 14.0,  "Battery cutoff end"),
    (93, "f32", 500.0, "Max watts"),
    (97, "f32", -500.0, "Min watts (regen)"),
]

print()
print("=" * 50)
print("  VESC Config — Store to Flash")
print("=" * 50)

print("\n[1/2] Pre-flight check: reading RAM config...")
payload = read_mcconf()
if payload is None:
    print("  FAILED: Could not read MCCONF")
    raise SystemExit

vconfig = payload[1:]
all_ok = True
for offset, dtype, expected, name in EXPECTED:
    if dtype == "f32":
        actual = struct.unpack_from(">f", vconfig, offset)[0]
        ok = abs(actual - expected) < 0.01
        status = "%.1f %s" % (actual, "OK" if ok else "EXPECTED %.1f" % expected)
    else:
        actual = vconfig[offset]
        ok = actual == expected
        status = "%d %s" % (actual, "OK" if ok else "EXPECTED %d" % expected)
    if not ok:
        all_ok = False
    print("  %-28s %s" % (name, status))

if not all_ok:
    print("\n  ABORT: RAM config does not match expected values!")
    print("  Run vesc_write_config.py first to apply changes.")
    raise SystemExit

print("\n  All values match. RAM config is correct.")

print("\n[2/2] Storing config to flash...")
uart.read()
sleep_ms(20)
uart.write(wrap_frame(bytes([15])))  # COMM_STORE_MCCONF
sleep_ms(1000)

# Verify by re-reading (flash → RAM happens on store)
print("  Verifying flash write...")
verify = read_mcconf()
if verify is None:
    print("  WARNING: Could not re-read after store")
    print("  VESC may be rebooting — wait and try reading again")
else:
    vv = verify[1:]
    flash_ok = True
    for offset, dtype, expected, name in EXPECTED:
        if dtype == "f32":
            actual = struct.unpack_from(">f", vv, offset)[0]
            ok = abs(actual - expected) < 0.01
        else:
            actual = vv[offset]
            ok = actual == expected
        if not ok:
            flash_ok = False
            print("  MISMATCH: %s" % name)

    if flash_ok:
        print("  FLASH WRITE VERIFIED — all values persisted!")
        print("\n  Config will survive VESC power cycles.")
    else:
        print("  WARNING: Some values may not have persisted!")

print("\n" + "=" * 50)
print("  Done")
print("=" * 50)
