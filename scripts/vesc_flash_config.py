# scripts/vesc_flash_config.py - Guarded live-blob MCCONF writer for FW 6.6
#
# This script patches the live serialized MCCONF blob read from the connected
# VESC and writes it back with COMM_SET_MCCONF. It is intentionally guarded:
# - validated only for FW 6.6 / HW 410 in this project
# - only writes fields we can first parse from the live blob
# - verifies exact live read-back after the write
#
# Run: mpremote mount . run scripts/vesc_flash_config.py

import math
import struct
from time import sleep_ms, ticks_ms, ticks_diff

from scripts.lib.vesc_uart_template import VescUartTemplate, crc16

try:
    from config.vesc_config import get_flash_limits
except ImportError:
    print("FAILED: Could not import config.vesc_config")
    print("Run with: mpremote mount . run scripts/vesc_flash_config.py")
    raise SystemExit(1)


COMM_FW_VERSION = 0
COMM_SET_MCCONF = 13
COMM_GET_MCCONF = 14

EXPECTED_FW = (6, 6)
EXPECTED_HW = "410"
ALLOW_EXPERIMENTAL_WRITE = False

vesc = VescUartTemplate(rxbuf=4096)


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


def read_u16_scaled_10(data, offset):
    return struct.unpack_from(">H", data, offset)[0] / 10.0


def write_f32_be(data, offset, value):
    struct.pack_into(">f", data, offset, value)


def write_f32_auto(data, offset, value):
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

    struct.pack_into(">I", data, offset, result)


def write_u16_scaled_10(data, offset, value):
    scaled = int(round(value * 10.0))
    if scaled < 0 or scaled > 0xFFFF:
        abort("Scaled uint16 value out of range at offset %d" % offset)
    struct.pack_into(">H", data, offset, scaled)


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


def _read_limits_at(config_blob, offsets, reader):
    return {
        "motor_max_current_a": reader(config_blob, offsets[0]),
        "motor_min_current_a": reader(config_blob, offsets[1]),
        "battery_max_current_a": reader(config_blob, offsets[2]),
        "battery_min_current_a": reader(config_blob, offsets[3]),
    }


def _read_voltage_at(config_blob, offsets, reader):
    return {
        "min_input_voltage_v": reader(config_blob, offsets[0]),
        "max_input_voltage_v": reader(config_blob, offsets[1]),
        "battery_cut_start_v": reader(config_blob, offsets[2]),
        "battery_cut_end_v": reader(config_blob, offsets[3]),
    }


def detect_current_layout(config_blob):
    candidates = (
        ((8, 12, 16, 20), read_f32_auto, write_f32_auto, "auto-float@8"),
        ((8, 12, 16, 20), read_f32_be, write_f32_be, "ieee-float@8"),
        ((4, 8, 12, 16), read_f32_be, write_f32_be, "ieee-float@4"),
    )

    for offsets, reader, writer, label in candidates:
        limits = _read_limits_at(config_blob, offsets, reader)
        if _plausible_current_limits(limits):
            return {
                "offsets": offsets,
                "reader": reader,
                "writer": writer,
                "label": label,
                "values": limits,
            }

    return None


def detect_voltage_layout(config_blob):
    candidates = (
        (
            (50, 52),
            (54, 56),
            (73, 77),
            read_u16_scaled_10,
            write_u16_scaled_10,
            read_f32_be,
            write_f32_be,
            "u16x10@50 + f32@73",
        ),
        ((48, 52, 56, 60, 93, 97), read_f32_be, write_f32_be, "ieee-float@48"),
        ((48, 52, 56, 60, 93, 97), read_f32_auto, write_f32_auto, "auto-float@48"),
    )

    for candidate in candidates:
        if len(candidate) == 8:
            vin_offsets, cut_offsets, watt_offsets, voltage_reader, voltage_writer, watt_reader, watt_writer, label = candidate
            limits = {
                "min_input_voltage_v": voltage_reader(config_blob, vin_offsets[0]),
                "max_input_voltage_v": voltage_reader(config_blob, vin_offsets[1]),
                "battery_cut_start_v": voltage_reader(config_blob, cut_offsets[0]),
                "battery_cut_end_v": voltage_reader(config_blob, cut_offsets[1]),
                "watt_max": watt_reader(config_blob, watt_offsets[0]),
                "watt_min": watt_reader(config_blob, watt_offsets[1]),
            }
        else:
            offsets, reader, writer, label = candidate
            limits = _read_voltage_at(config_blob, offsets[:4], reader)
            limits["watt_max"] = reader(config_blob, offsets[4])
            limits["watt_min"] = reader(config_blob, offsets[5])
            vin_offsets = offsets[:2]
            cut_offsets = offsets[2:4]
            watt_offsets = offsets[4:6]
            voltage_reader = reader
            voltage_writer = writer
            watt_reader = reader
            watt_writer = writer

        if _plausible_voltage_limits(limits):
            return {
                "vin_offsets": vin_offsets,
                "cut_offsets": cut_offsets,
                "watt_offsets": watt_offsets,
                "voltage_reader": voltage_reader,
                "voltage_writer": voltage_writer,
                "watt_reader": watt_reader,
                "watt_writer": watt_writer,
                "label": label,
                "values": limits,
            }

    return None


def read_fw_info():
    payload = vesc.request(COMM_FW_VERSION, timeout_ms=2000)
    if not payload or payload[0] != COMM_FW_VERSION or len(payload) < 3:
        return None, None, ""

    hw_name = ""
    if len(payload) > 3:
        hw_name = payload[3:].split(b"\x00")[0].decode("utf-8", "replace")
    return payload[1], payload[2], hw_name


def read_mcconf_blob(timeout_ms=5000):
    payload = vesc.request(COMM_GET_MCCONF, timeout_ms=timeout_ms)
    if payload and payload[0] == COMM_GET_MCCONF and len(payload) > 60:
        return payload[1:]
    return None


def wait_for_mcconf_blob(total_timeout_ms=12000, retry_delay_ms=250):
    start = ticks_ms()
    while ticks_diff(ticks_ms(), start) < total_timeout_ms:
        blob = read_mcconf_blob(timeout_ms=2500)
        if blob is not None:
            return blob
        sleep_ms(retry_delay_ms)
    return None


def apply_targets(config_blob, current_layout, voltage_layout, targets):
    patched = bytearray(config_blob)

    current_offsets = current_layout["offsets"]
    current_writer = current_layout["writer"]
    current_writer(patched, current_offsets[0], targets["motor_max_current_a"])
    current_writer(patched, current_offsets[1], targets["motor_min_current_a"])
    current_writer(patched, current_offsets[2], targets["battery_max_current_a"])
    current_writer(patched, current_offsets[3], targets["battery_min_current_a"])

    voltage_writer = voltage_layout["voltage_writer"]
    watt_writer = voltage_layout["watt_writer"]
    vin_offsets = voltage_layout["vin_offsets"]
    cut_offsets = voltage_layout["cut_offsets"]
    watt_offsets = voltage_layout["watt_offsets"]
    voltage_writer(patched, vin_offsets[0], targets["min_input_voltage_v"])
    voltage_writer(patched, vin_offsets[1], targets["max_input_voltage_v"])
    voltage_writer(patched, cut_offsets[0], targets["battery_cut_start_v"])
    voltage_writer(patched, cut_offsets[1], targets["battery_cut_end_v"])
    watt_writer(patched, watt_offsets[0], targets["watt_max"])
    watt_writer(patched, watt_offsets[1], targets["watt_min"])

    return patched


def print_before_after(current_layout, voltage_layout, targets):
    current = current_layout["values"]
    voltage = voltage_layout["values"]

    print("\nPlanned persistent changes:")
    print("  %-24s %12s -> %s" % ("Field", "Current", "Target"))
    print("  " + "-" * 56)
    print("  %-24s %12.2f -> %.2f" % ("Motor max current", current["motor_max_current_a"], targets["motor_max_current_a"]))
    print("  %-24s %12.2f -> %.2f" % ("Motor min current", current["motor_min_current_a"], targets["motor_min_current_a"]))
    print("  %-24s %12.2f -> %.2f" % ("Battery max current", current["battery_max_current_a"], targets["battery_max_current_a"]))
    print("  %-24s %12.2f -> %.2f" % ("Battery min current", current["battery_min_current_a"], targets["battery_min_current_a"]))
    print("  %-24s %12.2f -> %.2f" % ("Min input voltage", voltage["min_input_voltage_v"], targets["min_input_voltage_v"]))
    print("  %-24s %12.2f -> %.2f" % ("Max input voltage", voltage["max_input_voltage_v"], targets["max_input_voltage_v"]))
    print("  %-24s %12.2f -> %.2f" % ("Battery cut start", voltage["battery_cut_start_v"], targets["battery_cut_start_v"]))
    print("  %-24s %12.2f -> %.2f" % ("Battery cut end", voltage["battery_cut_end_v"], targets["battery_cut_end_v"]))
    print("  %-24s %12.2f -> %.2f" % ("Max watts", voltage["watt_max"], targets["watt_max"]))
    print("  %-24s %12.2f -> %.2f" % ("Min watts", voltage["watt_min"], targets["watt_min"]))


def first_different_offsets(before_blob, after_blob, limit=24):
    out = []
    bound = min(len(before_blob), len(after_blob))
    for idx in range(bound):
        if before_blob[idx] != after_blob[idx]:
            out.append(idx)
            if len(out) >= limit:
                break
    return out


print()
print("=" * 54)
print("  Guarded VESC MCCONF Writer")
print("=" * 54)

fw_major, fw_minor, hw_name = read_fw_info()
if fw_major is None:
    abort("Could not read firmware version")

print("FW: %d.%d" % (fw_major, fw_minor))
print("HW: %s" % hw_name)
if (fw_major, fw_minor) != EXPECTED_FW:
    abort("This writer is guarded for FW %d.%d only" % EXPECTED_FW)
if hw_name != EXPECTED_HW:
    abort("This writer is guarded for HW %s only" % EXPECTED_HW)

targets = get_flash_limits()
before_blob = wait_for_mcconf_blob()
if before_blob is None:
    abort("Could not read live MCCONF")

current_layout = detect_current_layout(before_blob)
if current_layout is None:
    abort("Could not detect current-limit layout in live MCCONF")

voltage_layout = detect_voltage_layout(before_blob)
if voltage_layout is None:
    abort("Could not detect voltage-limit layout in live MCCONF")

print("Current layout: %s" % current_layout["label"])
print("Voltage layout: %s" % voltage_layout["label"])
print_before_after(current_layout, voltage_layout, targets)

patched_blob = apply_targets(before_blob, current_layout, voltage_layout, targets)
if patched_blob == before_blob:
    print("\nNo write needed: live MCCONF already matches the configured targets.")
    raise SystemExit(0)

before_crc = crc16(before_blob)
patched_crc = crc16(patched_blob)
print("\nCRC before:  %04X" % before_crc)
print("CRC target:  %04X" % patched_crc)

if not ALLOW_EXPERIMENTAL_WRITE:
    print("\nABORTED: Experimental full COMM_SET_MCCONF write is disabled by default.")
    print("Reason: on tested FW 6.6 / HW 410 hardware, a live full write caused the")
    print("controller to stop responding over UART immediately afterward.")
    print("Use this script only as a dry-run layout/target audit unless you are")
    print("intentionally re-testing the raw writer after manual hardware recovery.")
    raise SystemExit(1)

vesc.send_command(bytes([COMM_SET_MCCONF]) + bytes(patched_blob), expected_cmd=None, timeout_ms=0)
sleep_ms(1200)

after_blob = wait_for_mcconf_blob(total_timeout_ms=15000)
if after_blob is None:
    abort("Could not read MCCONF after COMM_SET_MCCONF")

after_crc = crc16(after_blob)
print("CRC after:   %04X" % after_crc)

if after_blob != bytes(patched_blob):
    changed = first_different_offsets(bytes(patched_blob), after_blob)
    if changed:
        print("First mismatched byte offsets: %s" % ", ".join(str(i) for i in changed))

    after_current = detect_current_layout(after_blob)
    after_voltage = detect_voltage_layout(after_blob)
    if after_current is not None and after_voltage is not None:
        print("\nRead-back values after write:")
        print("  Motor max current:   %.2f A" % after_current["values"]["motor_max_current_a"])
        print("  Motor min current:   %.2f A" % after_current["values"]["motor_min_current_a"])
        print("  Battery max current: %.2f A" % after_current["values"]["battery_max_current_a"])
        print("  Battery min current: %.2f A" % after_current["values"]["battery_min_current_a"])
        print("  Min input voltage:   %.2f V" % after_voltage["values"]["min_input_voltage_v"])
        print("  Max input voltage:   %.2f V" % after_voltage["values"]["max_input_voltage_v"])
        print("  Battery cut start:   %.2f V" % after_voltage["values"]["battery_cut_start_v"])
        print("  Battery cut end:     %.2f V" % after_voltage["values"]["battery_cut_end_v"])
        print("  Max watts:           %.2f W" % after_voltage["values"]["watt_max"])
        print("  Min watts:           %.2f W" % after_voltage["values"]["watt_min"])

    abort("Exact MCCONF read-back did not match the requested target blob")

print("\nPASS: Persistent MCCONF matches the requested target blob exactly.")
print("Saved fields now include current, voltage, battery cut, and watt limits.")
