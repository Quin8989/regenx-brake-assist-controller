# scripts/vesc_characterize_motor.py - Apply temporary limits then run VESC FOC characterization
#
# This script avoids full MCCONF writes. On FW 5.2 the VESC supports
# COMM_SET_MCCONF_TEMP, which adjusts the active current, duty, ERPM, and watt
# envelope without serializing or storing the whole motor configuration blob.
#
# It then runs the VESC's built-in FOC commissioning routine over UART.
#
# It does not write the full serialized MCCONF blob. After characterization,
# scripts/vesc_apply_safety_temp.py can store the supported safety-envelope
# fields directly from the Pico on FW 6.6.
#
# Run: mpremote mount . run scripts/vesc_characterize_motor.py

import math
import struct
from time import sleep_ms, ticks_ms, ticks_diff

from scripts.lib.vesc_uart_template import VescUartTemplate, extract_frame
from scripts.lib.vesc_terminal import help_has_command, run_terminal_cmd

try:
    from config.vesc_config import VESC_CHARACTERIZATION, get_temp_limits
except ImportError:
    print("FAILED: Could not import config.vesc_config")
    print("Run with: mpremote mount . run scripts/vesc_characterize_motor.py")
    raise SystemExit(1)

vesc = VescUartTemplate(rxbuf=2048)
uart = vesc.uart


def flush_rx():
    vesc.flush_rx(settle_ms=20)


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
                    buf = buf[consumed:]
                break

            buf = buf[consumed:]
            if payload and payload[0] == expected_cmd:
                return payload
        sleep_ms(5)

    return None


def send_command(payload, expected_cmd=None, timeout_ms=5000):
    return vesc.send_command(payload, expected_cmd=expected_cmd, timeout_ms=timeout_ms)


def read_fw_version(timeout_ms=2000):
    return vesc.request(COMM_FW_VERSION, timeout_ms=timeout_ms)


def read_mcconf(command_id=None, timeout_ms=5000):
    if command_id is None:
        command_id = COMM_GET_MCCONF
    return vesc.request(command_id, timeout_ms=timeout_ms)


def wait_for_mcconf_ready(command_id=None, total_timeout_ms=15000, retry_delay_ms=250):
    start = ticks_ms()
    while ticks_diff(ticks_ms(), start) < total_timeout_ms:
        payload = read_mcconf(command_id=command_id, timeout_ms=2000)
        if payload is not None:
            return payload
        sleep_ms(retry_delay_ms)
    return None


def abort(message):
    print("\nFAILED: %s" % message)
    raise SystemExit(1)


def pack_scaled_f32(value, scale=1000):
    return struct.pack(">i", int(round(value * scale)))


def pack_u32(value):
    return struct.pack(">I", value)


def append_float32_auto(buf, value):
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

    buf.extend(pack_u32(result))


def read_u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def read_float32_auto(data, offset):
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


def read_float32_be(data, offset):
    return struct.unpack_from(">f", data, offset)[0]


def clamp(value, lower, upper):
    if value < lower:
        return lower
    if value > upper:
        return upper
    return value


def _plausible_current_limits(limits):
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


def _read_limits_at(config_blob, offsets, read_float):
    return {
        "motor_max_current_a": read_float(config_blob, offsets[0]),
        "motor_min_current_a": read_float(config_blob, offsets[1]),
        "battery_max_current_a": read_float(config_blob, offsets[2]),
        "battery_min_current_a": read_float(config_blob, offsets[3]),
    }


def read_live_current_limits(config_blob):
    if len(config_blob) < 24:
        abort("Live MCCONF blob is too short to parse current limits")

    # FW/layout compatibility:
    # - Some builds encode these fields with VESC auto-float packing.
    # - Others store standard IEEE float32.
    # - Offsets can differ by one enum/flags block.
    candidates = (
        ((8, 12, 16, 20), read_float32_auto, "auto-float@8"),
        ((8, 12, 16, 20), read_float32_be, "ieee-float@8"),
        ((4, 8, 12, 16), read_float32_be, "ieee-float@4"),
    )

    for offsets, reader, label in candidates:
        limits = _read_limits_at(config_blob, offsets, reader)
        if _plausible_current_limits(limits):
            print("  Parsed live limits using %s" % label)
            return limits

    abort("Could not parse live current limits from MCCONF blob")


def build_temp_config_payload(temp_limits, live_limits):
    live_motor_max = live_limits["motor_max_current_a"]
    live_motor_min = live_limits["motor_min_current_a"]

    if live_motor_max <= 0.0:
        abort("Live motor max current is invalid for temp scaling")

    if live_motor_min >= 0.0:
        abort("Live motor min current is invalid for temp scaling")

    motor_min_scale = clamp(
        abs(temp_limits["motor_min_current_a"]) / abs(live_motor_min), 0.0, 1.0
    )
    motor_max_scale = clamp(
        temp_limits["motor_max_current_a"] / live_motor_max, 0.0, 1.0
    )

    payload = bytearray([COMM_SET_MCCONF_TEMP, 0, 0, 1, 0])
    append_float32_auto(payload, motor_min_scale)
    append_float32_auto(payload, motor_max_scale)
    append_float32_auto(payload, temp_limits["min_erpm"])
    append_float32_auto(payload, temp_limits["max_erpm"])
    append_float32_auto(payload, temp_limits["min_duty"])
    append_float32_auto(payload, temp_limits["max_duty"])
    append_float32_auto(payload, temp_limits["watt_min"])
    append_float32_auto(payload, temp_limits["watt_max"])
    append_float32_auto(payload, temp_limits["battery_min_current_a"])
    append_float32_auto(payload, temp_limits["battery_max_current_a"])

    derived = {
        "motor_min_scale": motor_min_scale,
        "motor_max_scale": motor_max_scale,
    }
    return payload, derived


def read_temp_mcconf(timeout_ms=5000):
    return vesc.request(COMM_GET_MCCONF_TEMP, timeout_ms=timeout_ms)


def parse_temp_mcconf(payload):
    if payload is None or len(payload) < 41:
        return None

    data = payload[1:]
    return {
        "motor_min_scale": read_float32_auto(data, 0),
        "motor_max_scale": read_float32_auto(data, 4),
        "min_erpm": read_float32_auto(data, 8),
        "max_erpm": read_float32_auto(data, 12),
        "min_duty": read_float32_auto(data, 16),
        "max_duty": read_float32_auto(data, 20),
        "watt_min": read_float32_auto(data, 24),
        "watt_max": read_float32_auto(data, 28),
        "battery_min_current_a": read_float32_auto(data, 32),
        "battery_max_current_a": read_float32_auto(data, 36),
    }


def verify_temp_config(actual, expected, derived):
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
        actual_value = actual.get(name)
        ok = abs(actual_value - expected_value) < 0.02
        if not ok:
            all_ok = False
        print(
            "    %-24s %s"
            % (
                name,
                "OK" if ok else "FAILED (got %.3f expected %.3f)" % (actual_value, expected_value),
            )
        )
    return all_ok


COMM_GET_MCCONF = 14
COMM_GET_MCCONF_DEFAULT = 15
COMM_FW_VERSION = 0
COMM_SET_MCCONF_TEMP = 48
COMM_DETECT_APPLY_ALL_FOC = 58
COMM_GET_MCCONF_TEMP = 91

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

temp_limits = get_temp_limits()
detect_can = bool(VESC_CHARACTERIZATION.get("detect_can", False))
max_power_loss_w = float(VESC_CHARACTERIZATION.get("max_power_loss_w", 30.0))
min_input_current_a = float(VESC_CHARACTERIZATION.get("min_input_current_a", 0.0))
max_input_current_a = float(VESC_CHARACTERIZATION.get("max_input_current_a", 0.0))
openloop_erpm = float(VESC_CHARACTERIZATION.get("openloop_erpm", 0.0))
sensorless_erpm = float(VESC_CHARACTERIZATION.get("sensorless_erpm", 0.0))

if not temp_limits:
    abort("Shared config does not define temp characterization limits")

print("\nShared settings source: config.vesc_config")
print("Temporary limits and characterization settings loaded from the repo-mounted Python module")

print("\n[1/7] Checking VESC UART communication...")
fw_payload = read_fw_version()
if fw_payload is None or len(fw_payload) < 3:
    abort("Could not read firmware version from the VESC")

fw_major = fw_payload[1]
fw_minor = fw_payload[2]
print("  OK - FW Version: %d.%d" % (fw_major, fw_minor))
if (fw_major, fw_minor) < (3, 42):
    abort("FW %d.%d does not support COMM_SET_MCCONF_TEMP" % (fw_major, fw_minor))

if len(fw_payload) > 3:
    hw_name = fw_payload[3:].split(b"\x00")[0]
    if hw_name:
        try:
            print("  Hardware: %s" % hw_name.decode())
        except UnicodeError:
            print("  Hardware bytes: %s" % hw_name.hex())

print("\n[2/7] Reading live MCCONF for temp-limit derivation...")
live_payload = wait_for_mcconf_ready(COMM_GET_MCCONF)
if live_payload is None:
    abort("Could not read live MCCONF from the VESC")
print("  OK - %d byte payload from live VESC" % len(live_payload))

default_payload = read_mcconf(COMM_GET_MCCONF_DEFAULT, timeout_ms=2000)
if default_payload is not None:
    print("  Firmware default MCCONF is also readable (%d bytes)" % len(default_payload))
else:
    print("  Firmware default MCCONF not available; proceeding from live config")

live_limits = read_live_current_limits(live_payload[1:])
temp_payload, derived = build_temp_config_payload(temp_limits, live_limits)

print("\n[3/7] Planned temporary limits:")
print("  %-28s %12s -> %s" % ("Field", "Live", "Temporary"))
print("  " + "-" * 58)
print("  %-28s %12.1f -> %.1f" % ("Motor max current (A)", live_limits["motor_max_current_a"], temp_limits["motor_max_current_a"]))
print("  %-28s %12.1f -> %.1f" % ("Motor min current (A)", live_limits["motor_min_current_a"], temp_limits["motor_min_current_a"]))
print("  %-28s %12.1f -> %.1f" % ("Battery max current (A)", live_limits["battery_max_current_a"], temp_limits["battery_max_current_a"]))
print("  %-28s %12.1f -> %.1f" % ("Battery min current (A)", live_limits["battery_min_current_a"], temp_limits["battery_min_current_a"]))
print("  %-28s %12s -> %.3f" % ("Motor max scale", "live", derived["motor_max_scale"]))
print("  %-28s %12s -> %.3f" % ("Motor min scale", "live", derived["motor_min_scale"]))
print("  %-28s %12s -> %.1f" % ("Min ERPM", "live", temp_limits["min_erpm"]))
print("  %-28s %12s -> %.1f" % ("Max ERPM", "live", temp_limits["max_erpm"]))
print("  %-28s %12s -> %.3f" % ("Min duty", "live", temp_limits["min_duty"]))
print("  %-28s %12s -> %.3f" % ("Max duty", "live", temp_limits["max_duty"]))
print("  %-28s %12s -> %.1f" % ("Min watts", "live", temp_limits["watt_min"]))
print("  %-28s %12s -> %.1f" % ("Max watts", "live", temp_limits["watt_max"]))

print("\n[4/7] Applying temporary characterization limits...")
temp_ack = send_command(bytes(temp_payload), expected_cmd=COMM_SET_MCCONF_TEMP, timeout_ms=3000)
if temp_ack is None:
    abort("COMM_SET_MCCONF_TEMP did not return an ACK")

temp_readback = parse_temp_mcconf(read_temp_mcconf(timeout_ms=3000))
if temp_readback is None:
    abort("Could not read active temporary MCCONF")

print("  Verification against COMM_GET_MCCONF_TEMP:")
if not verify_temp_config(temp_readback, temp_limits, derived):
    abort("Temporary characterization limits did not verify")

print("\nShared settings source: config.vesc_config.VESC_CHARACTERIZATION")
print("Characterization settings loaded from the repo-mounted Python module")

print("\n[5/7] Checking MCCONF connectivity after temp-limit apply...")
before = wait_for_mcconf_ready()
if before is None:
    abort("Could not read MCCONF from the VESC")
print("  OK - %d byte payload" % len(before))

dc_cal_available = help_has_command(vesc, "foc_dc_cal")
if dc_cal_available:
    print("\n[6/8] Running foc_dc_cal via terminal before characterization...")
    dc_cal_lines = run_terminal_cmd(vesc, "foc_dc_cal", timeout_ms=5000)
    for line in dc_cal_lines:
        if line.strip():
            print("  " + line.strip())
else:
    print("\n[6/8] foc_dc_cal terminal command not available on this firmware")

print("\n[7/8] Running COMM_DETECT_APPLY_ALL_FOC...")
payload = bytearray([COMM_DETECT_APPLY_ALL_FOC, 1 if detect_can else 0])
payload.extend(pack_scaled_f32(max_power_loss_w))
payload.extend(pack_scaled_f32(min_input_current_a))
payload.extend(pack_scaled_f32(max_input_current_a))
payload.extend(pack_scaled_f32(openloop_erpm))
payload.extend(pack_scaled_f32(sensorless_erpm))

send_command(bytes(payload))
result_payload = wait_for_command(COMM_DETECT_APPLY_ALL_FOC, timeout_ms=90000)
if result_payload is None or len(result_payload) < 3:
    abort("No characterization result returned from the VESC")

result = struct.unpack_from(">h", result_payload, 1)[0]
print("  Result: %d (%s)" % (result, RESULTS.get(result, "Unknown result")))
if result < 0:
    abort("Motor characterization failed")

print("\n[8/8] Re-reading MCCONF after characterization...")
sleep_ms(1000)
after = wait_for_mcconf_ready(total_timeout_ms=20000)
if after is None:
    abort("Could not read MCCONF after characterization")

print("  OK - characterization completed and the VESC still responds")
print("  Captured MCCONF bytes: %d" % len(after[1:]))
print("  Result code: %d" % result)
fault_lines = run_terminal_cmd(vesc, "fault", timeout_ms=1200)
for line in fault_lines:
    if line.strip():
        print("  Terminal fault: %s" % line.strip())
print("\nNext step:")
print("  Run mpremote mount . run scripts/vesc_apply_safety_temp.py to store the supported safety envelope from the Pico.")
print("  If l_min_vin / l_max_vin still need correction, use VESC Tool once, then run mpremote run scripts/vesc_save_snapshot.py")

print("\n" + "=" * 50)
print("  Done")
print("=" * 50)