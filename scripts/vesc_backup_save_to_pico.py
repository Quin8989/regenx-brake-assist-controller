# scripts/vesc_backup_save_to_pico.py
#
# Save full VESC MCCONF to Pico filesystem so it can be restored later.
# This is a full binary backup (not just selected fields).
#
# Run: mpremote run scripts/vesc_backup_save_to_pico.py

import struct
from time import sleep_ms, ticks_ms, ticks_diff
from machine import UART, Pin

BACKUP_PATH = "vesc_mcconf_backup.bin"
MAGIC = b"VMCF"  # VESC Motor ConFig
VERSION = 1

COMM_GET_MCCONF = 14

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
        return payload
    return None


print()
print("=" * 50)
print("  VESC Backup Save -> Pico")
print("=" * 50)

payload = read_mcconf_payload()
if payload is None:
    print("FAILED: Could not read MCCONF from VESC")
    raise SystemExit

mcconf_data = payload[1:]  # drop command byte
mc_crc = crc16(mcconf_data)
header = MAGIC + bytes([VERSION]) + struct.pack(">H", len(mcconf_data)) + struct.pack(">H", mc_crc)
blob = header + mcconf_data

with open(BACKUP_PATH, "wb") as f:
    f.write(blob)

print("Saved backup to /%s" % BACKUP_PATH)
print("Config bytes: %d" % len(mcconf_data))
print("Data CRC16: %04X" % mc_crc)
print("Total file bytes: %d" % len(blob))
print("\nDone")
