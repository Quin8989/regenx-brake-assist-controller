# scripts/vesc_characterize_motor.py - Apply baseline then run VESC FOC characterization
#
# This script first reads the VESC firmware default MCCONF, applies a
# conservative baseline, verifies that baseline, and then runs the VESC's
# built-in FOC commissioning routine over UART.
#
# It does not apply the ReGenX project overlay; use scripts/vesc_flash_config.py
# afterwards for that step.
#
# Run: mpremote mount . run scripts/vesc_characterize_motor.py

import struct
from time import sleep_ms, ticks_ms, ticks_diff
from machine import UART, Pin

try:
    from config.vesc_config import VESC_CHARACTERIZATION, get_baseline_patches
except ImportError:
    print("FAILED: Could not import config.vesc_config")
    print("Run with: mpremote mount . run scripts/vesc_characterize_motor.py")
    raise SystemExit(1)

# ---- UART setup ----
uart = UART(1, baudrate=115200, tx=Pin(4), rx=Pin(5), rxbuf=2048)


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


def extract_frame(buf):
    if len(buf) < 6:
        return None, 0

    idx = 0
    while idx < len(buf):
        if buf[idx] == 0x02 and idx + 4 < len(buf):
            length = buf[idx + 1]
            frame_size = length + 5
            if idx + frame_size > len(buf):
                return None, idx
            payload = bytes(buf[idx + 2:idx + 2 + length])
            crc_recv = (buf[idx + 2 + length] << 8) | buf[idx + 3 + length]
            if crc16(payload) == crc_recv:
                return payload, idx + frame_size
            idx += 1
            continue

        if buf[idx] == 0x03 and idx + 5 < len(buf):
            length = (buf[idx + 1] << 8) | buf[idx + 2]
            if length <= 0 or length >= 10000:
                idx += 1
                continue
            frame_size = length + 6
            if idx + frame_size > len(buf):
                return None, idx
            payload = bytes(buf[idx + 3:idx + 3 + length])
            crc_recv = (buf[idx + 3 + length] << 8) | buf[idx + 4 + length]
            if crc16(payload) == crc_recv:
                return payload, idx + frame_size
            idx += 1
            continue

        idx += 1

    return None, len(buf)


def flush_rx():
    uart.read()
    sleep_ms(20)


def wait_for_command(expected_cmd, timeout_ms=90000):
    buf = bytearray()
    start = ticks_ms()

    while ticks_diff(ticks_ms(), start) < timeout_ms:
        data = uart.read()
        if data:
            buf.extend(data)
            start = ticks_ms()

        while True:
            payload, consumed = extract_frame(buf)
            if payload is None:
                if consumed > 0:
                    del buf[:consumed]
                break

            del buf[:consumed]
            if payload and payload[0] == expected_cmd:
                return payload
        sleep_ms(5)

    return None


def read_fw_version(timeout_ms=2000):
    flush_rx()
    uart.write(wrap_frame(bytes([COMM_FW_VERSION])))
    return wait_for_command(COMM_FW_VERSION, timeout_ms=timeout_ms)


def read_mcconf(command_id=None, timeout_ms=5000):
    if command_id is None:
        command_id = COMM_GET_MCCONF
    flush_rx()
    uart.write(wrap_frame(bytes([command_id])))
    return wait_for_command(command_id, timeout_ms=timeout_ms)


def send_mcconf(config_data):
    payload = bytes([COMM_SET_MCCONF]) + config_data
    flush_rx()
    uart.write(wrap_frame(payload))
    sleep_ms(200)
    return True


def abort(message):
    print("\nFAILED: %s" % message)
    raise SystemExit(1)


def pack_scaled_f32(value, scale=1000):
    return struct.pack(">i", int(round(value * scale)))


def patch_f32(data, offset, value):
    struct.pack_into(">f", data, offset, value)


def patch_u8(data, offset, value):
    data[offset] = value


def read_f32(data, offset):
    return struct.unpack_from(">f", data, offset)[0]


def read_u8(data, offset):
    return data[offset]


def verify_expected_fields(config_data, patches):
    all_ok = True
    for offset, dtype, new_val, name in patches:
        if dtype == "f32":
            actual = read_f32(config_data, offset)
            ok = abs(actual - new_val) < 0.01
            status = "OK" if ok else "FAILED (got %.1f)" % actual
        else:
            actual = read_u8(config_data, offset)
            ok = actual == new_val
            status = "OK" if ok else "FAILED (got %d)" % actual
        if not ok:
            all_ok = False
        print("    %-32s %s" % (name, status))
    return all_ok


COMM_SET_MCCONF = 13
COMM_GET_MCCONF = 14
COMM_GET_MCCONF_DEFAULT = 15
COMM_FW_VERSION = 0
COMM_DETECT_APPLY_ALL_FOC = 58

RESULTS = {
    0: "Sensorless FOC selected",
    1: "Hall sensors detected and selected",
    2: "Encoder detected and selected",
    -10: "Motor detection did not converge",
    -11: "Persistent fault during detection",
    -50: "CAN detection timed out",
    -51: "At least one CAN node failed detection",
}


print()
print("=" * 50)
print("  VESC Motor Preparation")
print("  ReGenX Brake-Assist Controller")
print("=" * 50)
print("\nSafety checks before running:")
print("  - Lift the driven wheel off the ground")
print("  - Remove load from the motor")
print("  - Verify phase and hall wiring")
print("  - Use a stable power source")

baseline_patches = get_baseline_patches()
detect_can = bool(VESC_CHARACTERIZATION.get("detect_can", False))
max_power_loss_w = float(VESC_CHARACTERIZATION.get("max_power_loss_w", 30.0))
min_input_current_a = float(VESC_CHARACTERIZATION.get("min_input_current_a", 0.0))
max_input_current_a = float(VESC_CHARACTERIZATION.get("max_input_current_a", 0.0))
openloop_erpm = float(VESC_CHARACTERIZATION.get("openloop_erpm", 0.0))
sensorless_erpm = float(VESC_CHARACTERIZATION.get("sensorless_erpm", 0.0))

if not baseline_patches:
    abort("Shared config does not define any baseline patches")

print("\nShared settings source: config.vesc_config")
print("Baseline and characterization settings loaded from the repo-mounted Python module")

print("\n[1/7] Checking VESC UART communication...")
fw_payload = read_fw_version()
if fw_payload is None or len(fw_payload) < 3:
    abort("Could not read firmware version from the VESC")

fw_major = fw_payload[1]
fw_minor = fw_payload[2]
print("  OK - FW Version: %d.%d" % (fw_major, fw_minor))
if len(fw_payload) > 3:
    hw_name = fw_payload[3:].split(b"\x00")[0]
    if hw_name:
        try:
            print("  Hardware: %s" % hw_name.decode())
        except UnicodeError:
            print("  Hardware bytes: %s" % hw_name.hex())

print("\n[2/7] Reading firmware default MCCONF...")
default_payload = read_mcconf(COMM_GET_MCCONF_DEFAULT)
if default_payload is None:
    abort("Could not read firmware default MCCONF from the VESC")
print("  OK - %d byte payload from firmware defaults" % len(default_payload))
config = bytearray(default_payload[1:])

print("\n[3/7] Planned baseline values:")
print("  %-32s %12s -> %s" % ("Field", "Default", "Baseline"))
print("  " + "-" * 64)
for offset, dtype, new_val, name in baseline_patches:
    if dtype == "f32":
        cur = read_f32(config, offset)
        cur_str = "%.1f" % cur
        new_str = "%.1f" % new_val
    else:
        cur = read_u8(config, offset)
        cur_str = "%d" % cur
        new_str = "%d" % new_val
    changed = " ***" if cur_str != new_str else ""
    print("  %-32s %12s -> %-12s%s" % (name, cur_str, new_str, changed))

print("\n[4/7] Applying baseline config...")
for offset, dtype, new_val, name in baseline_patches:
    if dtype == "f32":
        patch_f32(config, offset, new_val)
    else:
        patch_u8(config, offset, new_val)
print("  %d fields patched" % len(baseline_patches))

send_mcconf(bytes(config))
sleep_ms(500)

print("  Re-reading MCCONF to verify baseline...")
baseline_verify = read_mcconf(COMM_GET_MCCONF)
if baseline_verify is None:
    abort("Could not re-read MCCONF after applying baseline")

print("\n  Verification against immediate baseline read-back:")
if not verify_expected_fields(baseline_verify[1:], baseline_patches):
    abort("Baseline verification failed")

print("\nShared settings source: config.vesc_config.VESC_CHARACTERIZATION")
print("Characterization settings loaded from the repo-mounted Python module")

print("\n[5/7] Checking MCCONF connectivity after baseline...")
before = read_mcconf()
if before is None:
    abort("Could not read MCCONF from the VESC")
print("  OK - %d byte payload" % len(before))

print("\n[6/7] Running COMM_DETECT_APPLY_ALL_FOC...")
payload = bytearray([COMM_DETECT_APPLY_ALL_FOC, 1 if detect_can else 0])
payload.extend(pack_scaled_f32(max_power_loss_w))
payload.extend(pack_scaled_f32(min_input_current_a))
payload.extend(pack_scaled_f32(max_input_current_a))
payload.extend(pack_scaled_f32(openloop_erpm))
payload.extend(pack_scaled_f32(sensorless_erpm))

flush_rx()
uart.write(wrap_frame(bytes(payload)))
result_payload = wait_for_command(COMM_DETECT_APPLY_ALL_FOC, timeout_ms=90000)
if result_payload is None or len(result_payload) < 3:
    abort("No characterization result returned from the VESC")

result = struct.unpack_from(">h", result_payload, 1)[0]
print("  Result: %d (%s)" % (result, RESULTS.get(result, "Unknown result")))
if result < 0:
    print("  Baseline remains applied so the controller is left in a conservative state.")
    abort("Motor characterization failed")

print("\n[7/7] Re-reading MCCONF after characterization...")
after = read_mcconf()
if after is None:
    abort("Could not read MCCONF after characterization")

print("  OK - characterization completed and the VESC still responds")
print("  Captured MCCONF bytes: %d" % len(after[1:]))
print("  Result code: %d" % result)
print("\nNext step:")
print("  Run mpremote mount . run scripts/vesc_flash_config.py to apply the ReGenX overlay.")

print("\n" + "=" * 50)
print("  Done")
print("=" * 50)