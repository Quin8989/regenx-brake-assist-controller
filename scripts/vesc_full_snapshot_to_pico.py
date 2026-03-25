# scripts/vesc_full_snapshot_to_pico.py
#
# Capture a full VESC snapshot to Pico filesystem:
# - Firmware version / hardware string
# - MCCONF raw blob
# - APPCONF raw blob (if supported)
# - Metadata report with payload sizes and CRC16 checksums
#
# Run: mpremote run scripts/vesc_full_snapshot_to_pico.py

import struct
from time import sleep_ms, ticks_ms, ticks_diff
from machine import UART, Pin

# Command IDs used in this project / common VESC FW 5.x mappings
COMM_FW_VERSION = 0
COMM_GET_MCCONF = 14

# APPCONF command IDs can vary by firmware branch.
# Try known candidates and keep the first valid large response.
APPCONF_CANDIDATES = [17, 16, 18]

uart = UART(0, baudrate=115200, tx=Pin(0), rx=Pin(1), rxbuf=1024)

MCCONF_FILE = "vesc_snapshot_mcconf.bin"
APPCONF_FILE = "vesc_snapshot_appconf.bin"
META_FILE = "vesc_snapshot_meta.txt"


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


def send_and_receive(cmd_payload, timeout_ms=1500):
    uart.read()
    sleep_ms(20)
    uart.write(wrap_frame(cmd_payload))
    sleep_ms(60)

    buf = bytearray()
    start = ticks_ms()
    while ticks_diff(ticks_ms(), start) < timeout_ms:
        data = uart.read()
        if data:
            buf.extend(data)
            payload = try_extract(buf)
            if payload is not None:
                return payload
        sleep_ms(5)

    return try_extract(buf)


def get_fw_info():
    payload = send_and_receive(bytes([COMM_FW_VERSION]), timeout_ms=1000)
    if not payload or payload[0] != COMM_FW_VERSION or len(payload) < 3:
        return None, None, ""

    major = payload[1]
    minor = payload[2]
    hw_name = ""
    if len(payload) > 3:
        rest = payload[3:]
        nul = rest.find(b"\x00")
        if nul >= 0:
            rest = rest[:nul]
        try:
            hw_name = rest.decode("utf-8", "replace")
        except Exception:
            hw_name = ""
    return major, minor, hw_name


def get_conf_blob(cmd_id):
    payload = send_and_receive(bytes([cmd_id]), timeout_ms=2500)
    if not payload:
        return None
    if payload[0] != cmd_id:
        return None
    if len(payload) < 30:
        return None
    return payload[1:]  # strip command byte


print()
print("=" * 50)
print("  VESC Full Snapshot -> Pico")
print("=" * 50)

fw_major, fw_minor, hw_name = get_fw_info()
if fw_major is None:
    print("FAILED: Could not read firmware version")
    raise SystemExit

print("Firmware: %d.%d" % (fw_major, fw_minor))
print("Hardware: %s" % hw_name)

mcconf = get_conf_blob(COMM_GET_MCCONF)
if mcconf is None:
    print("FAILED: Could not read MCCONF")
    raise SystemExit

with open(MCCONF_FILE, "wb") as f:
    f.write(mcconf)

mc_crc = crc16(mcconf)
print("Saved %s (%d bytes, crc=%04X)" % (MCCONF_FILE, len(mcconf), mc_crc))

appconf = None
app_cmd_used = None
for cmd in APPCONF_CANDIDATES:
    blob = get_conf_blob(cmd)
    if blob is not None:
        appconf = blob
        app_cmd_used = cmd
        break

app_len = 0
app_crc = 0
if appconf is not None:
    with open(APPCONF_FILE, "wb") as f:
        f.write(appconf)
    app_len = len(appconf)
    app_crc = crc16(appconf)
    print("Saved %s (%d bytes, crc=%04X, cmd=%d)" % (APPCONF_FILE, app_len, app_crc, app_cmd_used))
else:
    print("WARNING: APPCONF read not supported by tested command IDs")

with open(META_FILE, "w") as f:
    f.write("vesc_snapshot_version=1\n")
    f.write("fw_major=%d\n" % fw_major)
    f.write("fw_minor=%d\n" % fw_minor)
    f.write("hw_name=%s\n" % hw_name)
    f.write("mcconf_file=%s\n" % MCCONF_FILE)
    f.write("mcconf_len=%d\n" % len(mcconf))
    f.write("mcconf_crc16=%04X\n" % mc_crc)
    if appconf is not None:
        f.write("appconf_file=%s\n" % APPCONF_FILE)
        f.write("appconf_cmd_id=%d\n" % app_cmd_used)
        f.write("appconf_len=%d\n" % app_len)
        f.write("appconf_crc16=%04X\n" % app_crc)
    else:
        f.write("appconf_file=\n")
        f.write("appconf_cmd_id=\n")
        f.write("appconf_len=0\n")
        f.write("appconf_crc16=\n")

print("Saved %s" % META_FILE)
print("\nDone")
