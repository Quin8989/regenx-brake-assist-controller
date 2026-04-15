# scripts/bench/test_vesc_read_config.py — Read VESC safety-relevant config via UART
#
# This script intentionally avoids decoding the full serialized MCCONF layout,
# which changes across firmware versions and was producing incorrect values on
# FW 6.6. Instead, it extracts the safety-relevant fields using the same
# tolerant parsing strategy used by scripts/vesc_characterize_motor.py.

import math
import struct

from scripts.lib.vesc_terminal import run_terminal_cmd
from scripts.lib.vesc_uart_template import VescUartTemplate

try:
    from config.settings import (
        MOTOR_CURRENT_MAX_A,
        VCAP_ABSOLUTE_MAX,
        VCAP_MIN_OPERATING,
        VESC_BAUD_RATE,
    )
except ImportError:
    MOTOR_CURRENT_MAX_A = 50.0
    VCAP_MIN_OPERATING = 10.0
    VCAP_ABSOLUTE_MAX = 42.0
    VESC_BAUD_RATE = 115200


COMM_FW_VERSION = 0
COMM_GET_VALUES = 4
COMM_GET_MCCONF = 14
COMM_GET_MCCONF_TEMP = 91

TELEMETRY_FMT = ">hhiiiihihiiiiiiB"
TELEMETRY_SIZE = 53

vesc = VescUartTemplate(rxbuf=2048)


def abort(message):
    print("\nFAILED: %s" % message)
    raise SystemExit(1)


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


def read_u16_scaled_10(data, offset):
    return struct.unpack_from(">H", data, offset)[0] / 10.0


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


def _plausible_voltage_limits(limits):
    return (
        0.0 <= limits["min_input_voltage_v"] <= 100.0
        and 0.0 <= limits["max_input_voltage_v"] <= 100.0
        and 0.0 <= limits["battery_cut_start_v"] <= 100.0
        and 0.0 <= limits["battery_cut_end_v"] <= 100.0
        and limits["max_input_voltage_v"] >= limits["min_input_voltage_v"]
        and limits["battery_cut_start_v"] >= limits["battery_cut_end_v"]
    )


def _read_limits_at(config_blob, offsets, read_float):
    return {
        "motor_max_current_a": read_float(config_blob, offsets[0]),
        "motor_min_current_a": read_float(config_blob, offsets[1]),
        "battery_max_current_a": read_float(config_blob, offsets[2]),
        "battery_min_current_a": read_float(config_blob, offsets[3]),
    }


def _read_voltage_limits_at(config_blob, offsets, read_float):
    return {
        "min_input_voltage_v": read_float(config_blob, offsets[0]),
        "max_input_voltage_v": read_float(config_blob, offsets[1]),
        "battery_cut_start_v": read_float(config_blob, offsets[2]),
        "battery_cut_end_v": read_float(config_blob, offsets[3]),
    }


def read_live_current_limits(config_blob):
    if len(config_blob) < 24:
        abort("Live MCCONF blob is too short to parse current limits")

    candidates = (
        ((8, 12, 16, 20), read_float32_auto, "auto-float@8"),
        ((8, 12, 16, 20), read_float32_be, "ieee-float@8"),
        ((4, 8, 12, 16), read_float32_be, "ieee-float@4"),
    )

    for offsets, reader, label in candidates:
        limits = _read_limits_at(config_blob, offsets, reader)
        if _plausible_current_limits(limits):
            return limits, label

    abort("Could not parse live current limits from MCCONF blob")


def read_live_voltage_limits(config_blob):
    if len(config_blob) < 64:
        abort("Live MCCONF blob is too short to parse voltage limits")

    candidates = (
        ((50, 52, 54, 56), read_u16_scaled_10, "u16x10@50"),
        ((48, 52, 56, 60), read_float32_be, "ieee-float@48"),
        ((48, 52, 56, 60), read_float32_auto, "auto-float@48"),
    )

    for offsets, reader, label in candidates:
        limits = _read_voltage_limits_at(config_blob, offsets, reader)
        if _plausible_voltage_limits(limits):
            return limits, label

    return None, None


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


def print_check(name, actual, expected, tolerance=0.25, comparator=None):
    if comparator is None:
        ok = abs(actual - expected) <= tolerance
    else:
        ok = comparator(actual, expected)
    print(
        "  %-28s %8.2f  expected %-8.2f %s"
        % (name, actual, expected, "OK" if ok else "MISMATCH")
    )
    return ok


print()
print("=" * 58)
print("  VESC UART Safety Audit")
print("=" * 58)

fw_payload = vesc.request(COMM_FW_VERSION, timeout_ms=1500)
if fw_payload is None or fw_payload[0] != COMM_FW_VERSION or len(fw_payload) < 3:
    abort("Could not read firmware version")

fw_major = fw_payload[1]
fw_minor = fw_payload[2]
hw_name = ""
if len(fw_payload) > 3:
    hw_name = fw_payload[3:].split(b"\x00")[0].decode("utf-8", "replace")

print("Firmware: %d.%d" % (fw_major, fw_minor))
print("Hardware: %s" % hw_name)
print("Expected UART baud from project: %d" % VESC_BAUD_RATE)

print("\n--- Live Telemetry ---")
telemetry = vesc.request(COMM_GET_VALUES, timeout_ms=1500)
if not telemetry or telemetry[0] != COMM_GET_VALUES or len(telemetry) < 1 + TELEMETRY_SIZE:
    abort("Could not read live telemetry")

vals = struct.unpack_from(TELEMETRY_FMT, telemetry, 1)
print("  FET temp:       %.1f C" % (vals[0] / 10.0))
print("  Motor temp:     %.1f C" % (vals[1] / 10.0))
print("  Motor current:  %.2f A" % (vals[2] / 100.0))
print("  Input current:  %.2f A" % (vals[3] / 100.0))
print("  Duty cycle:     %.1f %%" % (vals[6] / 10.0))
print("  ERPM:           %d" % vals[7])
print("  Bus voltage:    %.1f V" % (vals[8] / 10.0))
print("  Fault code:     %d" % vals[15])

print("\n--- Active Fault / Offsets ---")
fault_lines = run_terminal_cmd(vesc, "fault", timeout_ms=1200)
for line in fault_lines:
    if line.strip():
        print("  " + line.strip())

status_lines = run_terminal_cmd(vesc, "hw_status", timeout_ms=2200)
for line in status_lines:
    if "FOC Current Offsets:" in line or "Voltage Measurement Range:" in line or "Current Measurement Range:" in line:
        print("  " + line.strip())

print("\n--- Live MCCONF Safety Limits ---")
mcconf = vesc.request(COMM_GET_MCCONF, timeout_ms=4000)
if mcconf is None or mcconf[0] != COMM_GET_MCCONF:
    abort("Could not read live MCCONF")

config_blob = mcconf[1:]
current_limits, current_label = read_live_current_limits(config_blob)
print("  Current limits parser: %s" % current_label)
print("  Motor max current:      %.2f A" % current_limits["motor_max_current_a"])
print("  Motor min current:      %.2f A" % current_limits["motor_min_current_a"])
print("  Battery max current:    %.2f A" % current_limits["battery_max_current_a"])
print("  Battery min current:    %.2f A" % current_limits["battery_min_current_a"])

voltage_limits, voltage_label = read_live_voltage_limits(config_blob)
if voltage_limits is not None:
    print("  Voltage limits parser:  %s" % voltage_label)
    print("  Min input voltage:      %.2f V" % voltage_limits["min_input_voltage_v"])
    print("  Max input voltage:      %.2f V" % voltage_limits["max_input_voltage_v"])
    print("  Battery cutoff start:   %.2f V" % voltage_limits["battery_cut_start_v"])
    print("  Battery cutoff end:     %.2f V" % voltage_limits["battery_cut_end_v"])
else:
    print("  Voltage limits parser:  unavailable on this firmware layout")

print("\n--- Active Temporary Limits ---")
temp_mcconf = parse_temp_mcconf(vesc.request(COMM_GET_MCCONF_TEMP, timeout_ms=2000))
if temp_mcconf is None:
    print("  No temporary MCCONF limits active or readable")
else:
    print("  Motor max scale:        %.3f" % temp_mcconf["motor_max_scale"])
    print("  Motor min scale:        %.3f" % temp_mcconf["motor_min_scale"])
    print("  Battery max current:    %.2f A" % temp_mcconf["battery_max_current_a"])
    print("  Battery min current:    %.2f A" % temp_mcconf["battery_min_current_a"])
    print("  Max watts:              %.1f W" % temp_mcconf["watt_max"])
    print("  Min watts:              %.1f W" % temp_mcconf["watt_min"])

print("\n--- Comparison Against Safety Envelope ---")
within_safety = True
within_safety &= print_check(
    "Motor current max",
    current_limits["motor_max_current_a"],
    MOTOR_CURRENT_MAX_A,
    comparator=lambda actual, expected: actual <= expected + 0.25,
)
within_safety &= print_check(
    "Motor current brake",
    abs(current_limits["motor_min_current_a"]),
    MOTOR_CURRENT_MAX_A,
    comparator=lambda actual, expected: actual <= expected + 0.25,
)
within_safety &= print_check(
    "Battery current max",
    current_limits["battery_max_current_a"],
    MOTOR_CURRENT_MAX_A,
    comparator=lambda actual, expected: actual <= expected + 0.25,
)
within_safety &= print_check(
    "Battery current regen",
    abs(current_limits["battery_min_current_a"]),
    MOTOR_CURRENT_MAX_A,
    comparator=lambda actual, expected: actual <= expected + 0.25,
)

if voltage_limits is not None:
    within_safety &= print_check(
        "Min input voltage",
        voltage_limits["min_input_voltage_v"],
        VCAP_MIN_OPERATING,
        tolerance=1.25,
        comparator=lambda actual, expected: actual >= (expected - 1.25),
    )
    within_safety &= print_check(
        "Max input voltage",
        voltage_limits["max_input_voltage_v"],
        VCAP_ABSOLUTE_MAX,
        tolerance=0.25,
        comparator=lambda actual, expected: actual <= expected + 0.25,
    )

print("\n--- Comparison Against Desired Stored Targets ---")
stored_match = True
stored_match &= print_check(
    "Motor current max",
    current_limits["motor_max_current_a"],
    MOTOR_CURRENT_MAX_A,
    tolerance=0.25,
)
stored_match &= print_check(
    "Motor current brake",
    abs(current_limits["motor_min_current_a"]),
    MOTOR_CURRENT_MAX_A,
    tolerance=0.25,
)
stored_match &= print_check(
    "Battery current max",
    current_limits["battery_max_current_a"],
    MOTOR_CURRENT_MAX_A,
    tolerance=0.25,
)
stored_match &= print_check(
    "Battery current regen",
    abs(current_limits["battery_min_current_a"]),
    MOTOR_CURRENT_MAX_A,
    tolerance=0.25,
)

if voltage_limits is not None:
    stored_match &= print_check(
        "Min input voltage",
        voltage_limits["min_input_voltage_v"],
        VCAP_MIN_OPERATING,
        tolerance=0.25,
    )
    stored_match &= print_check(
        "Max input voltage",
        voltage_limits["max_input_voltage_v"],
        VCAP_ABSOLUTE_MAX,
        tolerance=0.25,
    )

print("\n--- Verdict ---")
if stored_match:
    print("  PASS: Live VESC limits match the configured project targets.")
elif within_safety:
    print("  PASS: Live VESC limits are within the project safety envelope.")
    print("  NOTE: Stored limits do not match the desired project targets yet.")
else:
    print("  ATTENTION: One or more live VESC limits exceed the project safety envelope.")
    print("  Use VESC Tool to lower persistent motor/app settings, then rerun this script.")

print("\n" + "=" * 58)
print("  Done")
print("=" * 58)
