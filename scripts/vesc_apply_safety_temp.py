# scripts/vesc_apply_safety_temp.py - Apply supported Pico-side safety persistence
#
# Applies the repo safety envelope using supported firmware packet paths:
# - COMM_SET_MCCONF_TEMP for current/duty/ERPM/watt runtime limits
# - COMM_SET_BATTERY_CUT for battery cutoff start/end
#
# When STORE_PERSISTENT is enabled, these values are requested to be stored in
# flash by the VESC firmware. This is the safe direct-from-Pico persistence path
# on FW 6.6 for the supported fields above.
#
# Run: mpremote mount . run scripts/vesc_apply_safety_temp.py

import math
import struct

from scripts.lib.vesc_uart_template import VescUartTemplate, crc16
from scripts.lib.vesc_terminal import run_terminal_cmd

try:
    from config.vesc_config import get_battery_cut_limits, get_temp_limits
except ImportError:
    print("FAILED: Could not import config.vesc_config")
    raise SystemExit(1)


COMM_FW_VERSION = 0
COMM_GET_MCCONF = 14
COMM_SET_MCCONF_TEMP = 48
COMM_SET_BATTERY_CUT = 86
COMM_GET_MCCONF_TEMP = 91
COMM_GET_BATTERY_CUT = 115

# When enabled, request firmware to store the applied temporary limits
# persistently. This relies on FW support for the store flag in
# COMM_SET_MCCONF_TEMP.
STORE_PERSISTENT = True

vesc = VescUartTemplate(rxbuf=2048)


def abort(message):
    print("\nFAILED: %s" % message)
    raise SystemExit(1)


def read_u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def read_f32_be(data, offset):
    return struct.unpack_from(">f", data, offset)[0]


def read_f32_auto(data, offset):
    raw = read_u32(data, offset)
    exponent = (raw >> 23) & 0xFF
    fraction = raw & 0x7FFFFF
    negative = raw & (1 << 31)

    value = 0.0
    if exponent != 0 or fraction != 0:
        value = fraction / (8388608.0 * 2.0) + 0.5
        exponent -= 126

    if negative:
        value = -value

    return math.ldexp(value, exponent)


def append_f32_auto(buf, value):
    if abs(value) < 1.5e-38:
        value = 0.0

    frac, exponent = math.frexp(value)
    frac_abs = abs(frac)
    frac_serialized = 0

    if frac_abs >= 0.5:
        frac_serialized = int((frac_abs - 0.5) * 2.0 * 8388608.0)
        exponent += 126

    result = ((exponent & 0xFF) << 23) | (frac_serialized & 0x7FFFFF)
    if frac < 0:
        result |= 1 << 31

    buf.extend(struct.pack(">I", result))


def clamp(value, lower, upper):
    if value < lower:
        return lower
    if value > upper:
        return upper
    return value


def pack_scaled_f32(value, scale=1000):
    return struct.pack(">i", int(round(value * scale)))


def read_fw_version():
    payload = vesc.request(COMM_FW_VERSION, timeout_ms=2000)
    if not payload or payload[0] != COMM_FW_VERSION or len(payload) < 3:
        return None
    return payload


def read_mcconf_blob():
    payload = vesc.request(COMM_GET_MCCONF, timeout_ms=4000)
    if payload and payload[0] == COMM_GET_MCCONF and len(payload) > 60:
        return payload[1:]
    return None


def parse_temp_mcconf(payload):
    if payload is None or payload[0] != COMM_GET_MCCONF_TEMP or len(payload) < 41:
        return None

    data = payload[1:]
    return {
        "motor_min_scale": read_f32_auto(data, 0),
        "motor_max_scale": read_f32_auto(data, 4),
        "min_erpm": read_f32_auto(data, 8),
        "max_erpm": read_f32_auto(data, 12),
        "min_duty": read_f32_auto(data, 16),
        "max_duty": read_f32_auto(data, 20),
        "watt_min": read_f32_auto(data, 24),
        "watt_max": read_f32_auto(data, 28),
        "battery_min_current_a": read_f32_auto(data, 32),
        "battery_max_current_a": read_f32_auto(data, 36),
    }


def parse_battery_cut(payload):
    if payload is None or payload[0] != COMM_GET_BATTERY_CUT or len(payload) < 9:
        return None

    return {
        "start_v": struct.unpack_from(">i", payload, 1)[0] / 1000.0,
        "end_v": struct.unpack_from(">i", payload, 5)[0] / 1000.0,
    }


def plausible_limits(limits):
    return (
        limits["motor_max_current_a"] > 0.0
        and limits["motor_min_current_a"] < 0.0
        and limits["battery_max_current_a"] > 0.0
        and limits["battery_min_current_a"] < 0.0
        and abs(limits["motor_max_current_a"]) < 5000.0
        and abs(limits["motor_min_current_a"]) < 5000.0
        and abs(limits["battery_max_current_a"]) < 5000.0
        and abs(limits["battery_min_current_a"]) < 5000.0
    )


def live_current_limits_from_mcconf(blob):
    candidates = (
        ((8, 12, 16, 20), read_f32_auto, "auto-float@8"),
        ((8, 12, 16, 20), read_f32_be, "ieee-float@8"),
        ((4, 8, 12, 16), read_f32_be, "ieee-float@4"),
    )

    for offsets, reader, label in candidates:
        limits = {
            "motor_max_current_a": reader(blob, offsets[0]),
            "motor_min_current_a": reader(blob, offsets[1]),
            "battery_max_current_a": reader(blob, offsets[2]),
            "battery_min_current_a": reader(blob, offsets[3]),
        }
        if plausible_limits(limits):
            return limits, label

    return None, None


def build_temp_payload(temp_limits, live_limits):
    live_motor_max = live_limits["motor_max_current_a"]
    live_motor_min = live_limits["motor_min_current_a"]

    if live_motor_max <= 0.0 or live_motor_min >= 0.0:
        abort("Live motor limits are not valid for scaling")

    motor_min_scale = clamp(
        abs(temp_limits["motor_min_current_a"]) / abs(live_motor_min), 0.0, 1.0
    )
    motor_max_scale = clamp(
        temp_limits["motor_max_current_a"] / live_motor_max, 0.0, 1.0
    )

    payload = bytearray([
        COMM_SET_MCCONF_TEMP,
        1 if STORE_PERSISTENT else 0,  # store
        0,                              # forward_can
        1,                              # ack
        0,                              # divide_by_controllers
    ])
    append_f32_auto(payload, motor_min_scale)
    append_f32_auto(payload, motor_max_scale)
    append_f32_auto(payload, temp_limits["min_erpm"])
    append_f32_auto(payload, temp_limits["max_erpm"])
    append_f32_auto(payload, temp_limits["min_duty"])
    append_f32_auto(payload, temp_limits["max_duty"])
    append_f32_auto(payload, temp_limits["watt_min"])
    append_f32_auto(payload, temp_limits["watt_max"])
    append_f32_auto(payload, temp_limits["battery_min_current_a"])
    append_f32_auto(payload, temp_limits["battery_max_current_a"])

    return payload, {"motor_min_scale": motor_min_scale, "motor_max_scale": motor_max_scale}


def verify_temp(actual, expected, derived):
    checks = (
        ("motor_min_scale", derived["motor_min_scale"]),
        ("motor_max_scale", derived["motor_max_scale"]),
        ("min_erpm", expected["min_erpm"]),
        ("max_erpm", expected["max_erpm"]),
        ("min_duty", expected["min_duty"]),
        ("max_duty", expected["max_duty"]),
        ("watt_min", expected["watt_min"]),
        ("watt_max", expected["watt_max"]),
        ("battery_min_current_a", expected["battery_min_current_a"]),
        ("battery_max_current_a", expected["battery_max_current_a"]),
    )

    all_ok = True
    for name, expected_value in checks:
        got = actual.get(name)
        ok = abs(got - expected_value) < 0.02
        if not ok:
            all_ok = False
        print("    %-24s %s" % (name, "OK" if ok else "FAILED (got %.3f expected %.3f)" % (got, expected_value)))
    return all_ok


def apply_battery_cut(cut_limits):
    payload = bytearray([COMM_SET_BATTERY_CUT])
    payload.extend(pack_scaled_f32(cut_limits["start_v"]))
    payload.extend(pack_scaled_f32(cut_limits["end_v"]))
    payload.append(1 if STORE_PERSISTENT else 0)
    payload.append(0)

    ack = vesc.send_command(bytes(payload), expected_cmd=COMM_SET_BATTERY_CUT, timeout_ms=3000)
    if ack is None:
        abort("COMM_SET_BATTERY_CUT did not ACK")

    readback = parse_battery_cut(vesc.request(COMM_GET_BATTERY_CUT, timeout_ms=2000))
    if readback is None:
        abort("Could not read COMM_GET_BATTERY_CUT")

    start_ok = abs(readback["start_v"] - cut_limits["start_v"]) < 0.05
    end_ok = abs(readback["end_v"] - cut_limits["end_v"]) < 0.05
    print("\nBattery-cut verify:")
    print("    %-24s %s" % (
        "start_v",
        "OK" if start_ok else "FAILED (got %.3f expected %.3f)" % (readback["start_v"], cut_limits["start_v"]),
    ))
    print("    %-24s %s" % (
        "end_v",
        "OK" if end_ok else "FAILED (got %.3f expected %.3f)" % (readback["end_v"], cut_limits["end_v"]),
    ))
    if not (start_ok and end_ok):
        abort("Battery cutoff verification failed")

    return readback


def diff_offsets(before_blob, after_blob):
    limit = min(len(before_blob), len(after_blob))
    out = []
    for idx in range(limit):
        if before_blob[idx] != after_blob[idx]:
            out.append(idx)
    return out


print()
print("=" * 50)
print("  VESC Apply Safety Envelope")
print("=" * 50)
print("Store mode: %s" % ("PERSISTENT" if STORE_PERSISTENT else "TEMP-ONLY"))

fw = read_fw_version()
if fw is None:
    abort("Could not read firmware version")

fw_major = fw[1]
fw_minor = fw[2]
print("FW: %d.%d" % (fw_major, fw_minor))
if (fw_major, fw_minor) < (3, 42):
    abort("Firmware does not support COMM_SET_MCCONF_TEMP")

before_blob = read_mcconf_blob()
if before_blob is None:
    abort("Could not read MCCONF before apply")

before_crc = crc16(before_blob)
print("MCCONF before: len=%d crc=%04X" % (len(before_blob), before_crc))
print("Header enums before: pwm=%d comm=%d motor_type=%d sensor_mode=%d" % (before_blob[4], before_blob[5], before_blob[6], before_blob[7]))

temp_limits = get_temp_limits()
battery_cut_limits = get_battery_cut_limits()
live_limits, parser = live_current_limits_from_mcconf(before_blob)
if live_limits is None:
    abort("Could not parse live current limits from MCCONF")

print("Current-limit parser: %s" % parser)
print("Live limits: motor %.1f/%.1f A, battery %.1f/%.1f A" % (
    live_limits["motor_max_current_a"],
    live_limits["motor_min_current_a"],
    live_limits["battery_max_current_a"],
    live_limits["battery_min_current_a"],
))
print("Target battery cut: %.1f -> %.1f V" % (
    battery_cut_limits["start_v"],
    battery_cut_limits["end_v"],
))

payload, derived = build_temp_payload(temp_limits, live_limits)

ack = vesc.send_command(bytes(payload), expected_cmd=COMM_SET_MCCONF_TEMP, timeout_ms=3000)
if ack is None:
    abort("COMM_SET_MCCONF_TEMP did not ACK")

temp_rb_raw = vesc.request(COMM_GET_MCCONF_TEMP, timeout_ms=3000)
temp_rb = parse_temp_mcconf(temp_rb_raw)
if temp_rb is None:
    abort("Could not read COMM_GET_MCCONF_TEMP")

print("\nTemp-limit verify:")
if not verify_temp(temp_rb, temp_limits, derived):
    abort("Temporary limits verification failed")

apply_battery_cut(battery_cut_limits)

after_blob = read_mcconf_blob()
if after_blob is None:
    abort("Could not read MCCONF after apply")

after_crc = crc16(after_blob)
print("\nMCCONF after:  len=%d crc=%04X" % (len(after_blob), after_crc))
print("Header enums after:  pwm=%d comm=%d motor_type=%d sensor_mode=%d" % (after_blob[4], after_blob[5], after_blob[6], after_blob[7]))

same_len = len(before_blob) == len(after_blob)
same_crc = before_crc == after_crc
same_blob = before_blob == after_blob

# Observed on FW 5.2 and FW 6.6 with COMM_SET_MCCONF_TEMP plus
# COMM_SET_BATTERY_CUT:
# - Battery max/min current fields can appear altered in COMM_GET_MCCONF readback.
# - A nearby current-control block can drift slightly as the runtime overlay is
#   materialized by the firmware.
# - Battery cutoff start/end bytes can change intentionally when that dedicated
#   persistent packet is used. On the tested 6.6 layout these landed slightly
#   earlier than the old 56..63 expectation.
allowed_runtime_ranges = (
    (16, 23),   # battery max/min current bytes
    (54, 63),   # battery cutoff start/end bytes
    (81, 84),   # FW 6.6 nearby runtime control bytes
    (101, 108), # nearby current-control bytes
)

changed = diff_offsets(before_blob, after_blob)
unexpected = []
for idx in changed:
    in_allowed = False
    for start, end in allowed_runtime_ranges:
        if start <= idx <= end:
            in_allowed = True
            break
    if not in_allowed:
        unexpected.append(idx)

print("\nPersistent-config audit:")
print("  Same length: %s" % ("YES" if same_len else "NO"))
print("  Same CRC16:  %s" % ("YES" if same_crc else "NO"))
print("  Byte exact:  %s" % ("YES" if same_blob else "NO"))
print("  Changed bytes count: %d" % len(changed))
if changed:
    print("  Changed byte offsets: %s" % ", ".join(str(i) for i in changed))

# Always treat these header enums as protected sentinels.
if before_blob[4:8] != after_blob[4:8]:
    abort("Protected header enums changed (pwm/comm/motor/sensor)")

print("  Protected header enums unchanged: YES")

if unexpected:
    print("  Unexpected changed offsets: %s" % ", ".join(str(i) for i in unexpected))
    abort("MCCONF changed outside expected runtime-overlay regions")

print("\nPASS: Safety temp limits applied")
if same_blob:
    print("PASS: MCCONF remained byte-exact")
else:
    print("PASS: MCCONF deltas were limited to expected safety-envelope regions")

fault_lines = run_terminal_cmd(vesc, "fault", timeout_ms=1200)
for line in fault_lines:
    if line.strip():
        print("Terminal fault: %s" % line.strip())

print("\nSupported Pico-side persistent fields on this workflow:")
print("  - motor current scale envelope")
print("  - battery current limits in COMM_SET_MCCONF_TEMP")
print("  - ERPM, duty, and watt limits in COMM_SET_MCCONF_TEMP")
print("  - battery cutoff start/end via COMM_SET_BATTERY_CUT")
print("\nRemaining limitation:")
print("  - min/max input voltage (l_min_vin / l_max_vin) do not have a dedicated")
print("    safe UART packet in this workflow and are not changed here")