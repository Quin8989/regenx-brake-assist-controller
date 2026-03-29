# scripts/bench/vesc_backup_restore_from_pico.py
#
# Restore full VESC MCCONF from Pico filesystem backup.
#
# Run: mpremote run scripts/bench/vesc_backup_restore_from_pico.py

import struct
from time import sleep_ms, ticks_ms, ticks_diff
from machine import UART, Pin

BACKUP_PATH = "vesc_mcconf_backup.bin"
MAGIC = b"VMCF"
EXPECTED_VERSION = 1

COMM_SET_MCCONF = 13
COMM_GET_MCCONF = 14
uart = UART(1, baudrate=115200, tx=Pin(4), rx=Pin(5), rxbuf=1024)


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
    frame += struct.pack(">H", crc16(payload))
    frame += bytes([0x03])
    return frame


def try_extract(buf):
    if len(buf) < 6:
        return None

    idx = 0
    while idx < len(buf):
        if buf[idx] == 0x02 and idx + 4 < len(buf):
            length = buf[idx + 1]
            frame_size = length + 5
            if idx + frame_size <= len(buf):
                payload = bytes(buf[idx + 2:idx + 2 + length])
                crc_recv = (buf[idx + 2 + length] << 8) | buf[idx + 3 + length]
                if crc16(payload) == crc_recv:
                    return payload
            idx += 1
        elif buf[idx] == 0x03 and idx + 5 < len(buf):
            length = (buf[idx + 1] << 8) | buf[idx + 2]
            if length > 0 and length < 10000:
                frame_size = length + 6
                if idx + frame_size <= len(buf):
                    payload = bytes(buf[idx + 3:idx + 3 + length])
                    crc_recv = (buf[idx + 3 + length] << 8) | buf[idx + 4 + length]
                    if crc16(payload) == crc_recv:
                        return payload
            idx += 1
        else:
            idx += 1
    return None


def read_mcconf_payload():
    uart.read()
    sleep_ms(20)
    uart.write(wrap_frame(bytes([COMM_GET_MCCONF])))
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
    if payload and payload[0] == COMM_GET_MCCONF and len(payload) > 50:
        return payload[1:]
    return None


print()
print("=" * 50)
print("  VESC Backup Restore <- Pico")
print("=" * 50)

try:
    with open(BACKUP_PATH, "rb") as f:
        blob = f.read()
except OSError:
    print("FAILED: Backup file /%s not found" % BACKUP_PATH)
    raise SystemExit

if len(blob) < 9:
    print("FAILED: Backup file too small")
    raise SystemExit

magic = blob[0:4]
version = blob[4]
length = struct.unpack_from(">H", blob, 5)[0]
stored_crc = struct.unpack_from(">H", blob, 7)[0]
mcconf_data = blob[9:]

if magic != MAGIC:
    print("FAILED: Invalid backup magic")
    raise SystemExit
if version != EXPECTED_VERSION:
    print("FAILED: Unsupported backup version %d" % version)
    raise SystemExit
if len(mcconf_data) != length:
    print("FAILED: Length mismatch (header=%d, data=%d)" % (length, len(mcconf_data)))
    raise SystemExit

calc_crc = crc16(mcconf_data)
if calc_crc != stored_crc:
    print("FAILED: CRC mismatch (stored=%04X calc=%04X)" % (stored_crc, calc_crc))
    raise SystemExit

print("Backup file valid")
print("Config bytes: %d" % len(mcconf_data))
print("Data CRC16: %04X" % calc_crc)

# Apply config
uart.read()
sleep_ms(20)
uart.write(wrap_frame(bytes([COMM_SET_MCCONF]) + mcconf_data))
sleep_ms(800)
print("Applied backup with COMM_SET_MCCONF")

# Verify by reading back exactly
live = read_mcconf_payload()
if live is None:
    print("WARNING: Could not read back config for verification")
    print("Wait a moment and run scripts/bench/test_vesc_read_config.py")
else:
    if live == mcconf_data:
        print("VERIFY OK: Live MCCONF matches backup exactly")
    else:
        print("VERIFY FAILED: Live MCCONF differs from backup")

print("\nDone")
