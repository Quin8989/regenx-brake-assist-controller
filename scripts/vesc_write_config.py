# scripts/vesc_write_config.py — Read, patch, and write VESC motor configuration
#
# Workflow:
#   1. Reads current MCCONF from VESC
#   2. Patches specific fields at known byte offsets
#   3. Sends patched config with COMM_SET_MCCONF (applies to RAM only)
#   4. Re-reads config to verify changes took effect
#   5. Only stores to flash with COMM_STORE_MCCONF after user confirms
#
# Run: mpremote run scripts/vesc_write_config.py
#
# SAFETY: COMM_SET_MCCONF is RAM-only (lost on reboot). Flash write
#         requires the STORE step, which has a confirmation prompt.

import struct
from time import sleep_ms, ticks_ms, ticks_diff
from machine import UART, Pin

# ---- UART setup ----
uart = UART(0, baudrate=115200, tx=Pin(0), rx=Pin(1), rxbuf=1024)

# ---- VESC protocol helpers ----

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


def read_mcconf():
    """Read full MCCONF payload from VESC. Returns payload bytes or None."""
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


def send_mcconf(config_data):
    """Send COMM_SET_MCCONF with modified config. Returns True if ACK received."""
    # Build payload: command byte + config data (skip the original command byte)
    payload = bytes([COMM_SET_MCCONF]) + config_data

    uart.read()
    sleep_ms(20)
    uart.write(wrap_frame(payload))
    sleep_ms(200)

    # VESC doesn't always send an explicit ACK for SET_MCCONF,
    # so we verify by re-reading the config
    return True


def store_mcconf():
    """Send COMM_STORE_MCCONF to save RAM config to flash."""
    uart.read()
    sleep_ms(20)
    uart.write(wrap_frame(bytes([COMM_STORE_MCCONF])))
    sleep_ms(500)
    return True


# ---- Byte offset helpers ----
# These offsets are into the config data blob (payload[1:], after command byte)
# Based on decoded layout from test_vesc_read_config.py output:
#
# Offset  Field                     Current → Target
# ------  -----                     ----------------
#   0-3   Config signature          (don't touch)
#   4     PWM mode (u8)             1 (Sync)     → keep
#   5     Comm mode (u8)            0            → keep
#   6     Motor type (u8)           0 (BLDC)     → 2 (FOC)
#   7     Sensor mode (u8)          0 (Sensorless) → keep
#   8-11  Motor max current (f32)   60.0         → 40.0
#  12-15  Motor min current (f32)   -60.0        → -40.0
#  16-19  Battery max current (f32) 99.0         → 40.0
#  20-23  Battery min current (f32) -60.0        → -40.0
#  24-27  Absolute max current (f32) 130.0       → keep
#  28-31  Min ERPM (f32)            -100000      → keep
#  32-35  Max ERPM (f32)            100000       → keep
#  36-39  ERPM start (f32)          0.8          → keep
#  40-43  Max ERPM fbrake (f32)     300.0        → keep
#  44-47  Max ERPM fbrake cc (f32)  1500.0       → keep
#  48-51  Min input V (f32)         8.0          → 15.0
#  52-55  Max input V (f32)         57.0         → 42.0
#  56-59  Batt cut start (f32)      6.0          → 15.0
#  60-63  Batt cut end (f32)        6.0          → 14.0
#  64     Slow abs current (bool)   1            → keep
#  65-68  FET temp start (f32)      85.0         → keep
#  69-72  FET temp end (f32)        100.0        → keep
#  73-76  Motor temp start (f32)    85.0         → keep
#  77-80  Motor temp end (f32)      100.0        → keep
#  81-84  Temp accel dec (f32)      0.15         → keep
#  85-88  Min duty (f32)            0.005        → keep
#  89-92  Max duty (f32)            0.95         → keep
#  93-96  Max watts (f32)           1500000      → 500.0
#  97-100 Min watts (f32)           -1500000     → -500.0

def patch_f32(data, offset, value):
    """Patch a float32 at the given offset in a bytearray."""
    struct.pack_into(">f", data, offset, value)


def patch_u8(data, offset, value):
    """Patch a uint8 at the given offset."""
    data[offset] = value


def read_f32(data, offset):
    return struct.unpack_from(">f", data, offset)[0]


def read_u8(data, offset):
    return data[offset]


# ---- Command IDs ----
COMM_GET_MCCONF = 14
COMM_SET_MCCONF = 13
COMM_STORE_MCCONF = 15

# ---- Target configuration values ----
# These are the values we want to set for the ReGenX supercap system
PATCHES = [
    # (offset, type, new_value, name)
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


# ================= MAIN =================
print()
print("=" * 50)
print("  VESC Configuration Writer")
print("  ReGenX Brake-Assist Controller")
print("=" * 50)

# ---- Step 1: Read current config ----
print("\n[1/4] Reading current MCCONF...")
payload = read_mcconf()
if payload is None:
    print("  FAILED: Could not read MCCONF from VESC")
    print("  Check UART connection and VESC power")
    raise SystemExit

print("  OK — %d byte payload" % len(payload))
config = bytearray(payload[1:])  # mutable copy, skip command byte

# ---- Step 2: Show current vs target ----
print("\n[2/4] Planned changes:")
print("  %-28s %12s -> %s" % ("Field", "Current", "New"))
print("  " + "-" * 58)

for offset, dtype, new_val, name in PATCHES:
    if dtype == "f32":
        cur = read_f32(config, offset)
        cur_str = "%.1f" % cur
        new_str = "%.1f" % new_val
    else:
        cur = read_u8(config, offset)
        cur_str = "%d" % cur
        new_str = "%d" % new_val

    changed = " ***" if cur_str != new_str else ""
    print("  %-28s %12s -> %-12s%s" % (name, cur_str, new_str, changed))

# ---- Step 3: Apply patches ----
print("\n[3/4] Patching config blob...")
for offset, dtype, new_val, name in PATCHES:
    if dtype == "f32":
        patch_f32(config, offset, new_val)
    else:
        patch_u8(config, offset, new_val)
print("  %d fields patched" % len(PATCHES))

# Verify patches by re-reading from patched blob
print("\n  Verification read-back from patched blob:")
for offset, dtype, new_val, name in PATCHES:
    if dtype == "f32":
        actual = read_f32(config, offset)
        ok = abs(actual - new_val) < 0.01
        print("    %-28s = %-12.1f %s" % (name, actual, "OK" if ok else "MISMATCH!"))
    else:
        actual = read_u8(config, offset)
        ok = actual == new_val
        print("    %-28s = %-12d %s" % (name, actual, "OK" if ok else "MISMATCH!"))

# ---- Step 4: Send to VESC (RAM only) ----
print("\n[4/4] Sending patched config to VESC (RAM only)...")
send_mcconf(bytes(config))
sleep_ms(500)

# Re-read to verify VESC accepted the changes
print("  Re-reading MCCONF to verify...")
verify = read_mcconf()
if verify is None:
    print("  WARNING: Could not re-read config for verification")
    print("  The VESC may have rejected the config or rebooted")
else:
    vconfig = verify[1:]  # skip command byte
    print("\n  Verification against live VESC config:")
    all_ok = True
    for offset, dtype, new_val, name in PATCHES:
        if dtype == "f32":
            actual = struct.unpack_from(">f", vconfig, offset)[0]
            ok = abs(actual - new_val) < 0.01
            status = "OK" if ok else "FAILED (got %.1f)" % actual
        else:
            actual = vconfig[offset]
            ok = actual == new_val
            status = "OK" if ok else "FAILED (got %d)" % actual
        if not ok:
            all_ok = False
        print("    %-28s %s" % (name, status))

    if all_ok:
        print("\n  ALL CHANGES VERIFIED IN VESC RAM")
        print("\n  Config is in RAM only — will be lost on VESC reboot.")
        print("  To save permanently, run: vesc_store_config.py")
    else:
        print("\n  SOME CHANGES FAILED — do NOT store to flash!")
        print("  Power-cycle the VESC to revert to previous config.")

print("\n" + "=" * 50)
print("  Done")
print("=" * 50)
