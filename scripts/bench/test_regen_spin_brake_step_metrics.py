# scripts/bench/test_regen_spin_brake_step_metrics.py
#
# Bench regen step test with tighter metrics:
# - Spin up wheel with assist.
# - Wait until speed is in a target band.
# - Apply fixed regen command.
# - Report early-window iq and peak/min iq, not only long-window average.
#
# Run:
#   mpremote mount . run scripts/bench/test_regen_spin_brake_step_metrics.py

from time import sleep_ms, ticks_ms, ticks_diff
import math
import struct

from core import SharedState
from services.vesc_comm import UARTPort, VESCComm

try:
    from machine import WDT
except Exception:
    WDT = None

from scripts.lib.vesc_uart_template import VescUartTemplate

SAMPLE_PERIOD_MS = 20
TELEMETRY_PERIOD_MS = 50

COMM_GET_MCCONF = 14

SPIN_ASSIST_A = 4.0
SPIN_TIMEOUT_MS = 4000
TARGET_RPM_MIN = 140.0
TARGET_RPM_MAX = 220.0
IN_BAND_CONFIRM_SAMPLES = 4

EARLY_WINDOW_MS = 250
MEASURE_WINDOW_MS = 900

BRAKE_LEVELS_A = (5.0, 10.0, 15.0, 20.0, 30.0)

LIMIT_MARGIN_A = 0.5
LIMIT_MARGIN_V = 0.3

state = SharedState()
uart = UARTPort()
vesc = VESCComm(uart, state)
wdt = WDT(timeout=8000) if WDT is not None else None


def _read_u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def _read_float32_auto(data, offset):
    raw = _read_u32(data, offset)
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


def _read_u16_scaled_10(data, offset):
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


def _read_live_limits():
    raw = VescUartTemplate(rxbuf=3072).request(COMM_GET_MCCONF, timeout_ms=4000)
    if not raw or raw[0] != COMM_GET_MCCONF or len(raw) < 64:
        return None, None

    blob = raw[1:]

    current = {
        "motor_max_current_a": _read_float32_auto(blob, 8),
        "motor_min_current_a": _read_float32_auto(blob, 12),
        "battery_max_current_a": _read_float32_auto(blob, 16),
        "battery_min_current_a": _read_float32_auto(blob, 20),
    }
    if not _plausible_current_limits(current):
        current = None

    voltage = {
        "min_input_voltage_v": _read_u16_scaled_10(blob, 50),
        "max_input_voltage_v": _read_u16_scaled_10(blob, 52),
        "battery_cut_start_v": _read_u16_scaled_10(blob, 54),
        "battery_cut_end_v": _read_u16_scaled_10(blob, 56),
    }
    if not _plausible_voltage_limits(voltage):
        voltage = None

    return current, voltage


def _service_once(cmd_mode, amps):
    if cmd_mode == "assist":
        vesc.send_assist(amps)
    elif cmd_mode == "regen":
        vesc.send_regen(amps)
    else:
        vesc.send_neutral()


def _spin_to_band():
    start = ticks_ms()
    last_req = start
    in_band_count = 0
    confirmed_rpm = 0.0

    while ticks_diff(ticks_ms(), start) < SPIN_TIMEOUT_MS:
        now = ticks_ms()

        if wdt is not None:
            wdt.feed()

        if ticks_diff(now, last_req) >= TELEMETRY_PERIOD_MS:
            vesc.request_telemetry()
            last_req = now

        vesc.service_rx()
        _service_once("assist", SPIN_ASSIST_A)

        rpm = abs(state.vesc_mech_rpm)
        if TARGET_RPM_MIN <= rpm <= TARGET_RPM_MAX:
            in_band_count += 1
            confirmed_rpm = rpm
            if in_band_count >= IN_BAND_CONFIRM_SAMPLES:
                return True, confirmed_rpm
        else:
            in_band_count = 0

        sleep_ms(SAMPLE_PERIOD_MS)

    return False, confirmed_rpm


def _measure_regen_step(cmd_a, current_limits, voltage_limits):
    start = ticks_ms()
    last_req = start

    count = 0
    iq_sum = 0.0
    motor_sum = 0.0
    input_sum = 0.0
    rpm_sum = 0.0

    early_count = 0
    early_iq_sum = 0.0

    iq_min = 1e9
    iq_max = -1e9
    fault_nonzero_samples = 0
    vin_min = 1e9
    vin_max = -1e9
    motor_min = 1e9
    input_min = 1e9
    near_motor_regen_limit_samples = 0
    near_battery_regen_limit_samples = 0
    near_max_vin_samples = 0
    near_min_vin_samples = 0
    fault_counts = {}

    while ticks_diff(ticks_ms(), start) < MEASURE_WINDOW_MS:
        now = ticks_ms()

        if wdt is not None:
            wdt.feed()

        if ticks_diff(now, last_req) >= TELEMETRY_PERIOD_MS:
            vesc.request_telemetry()
            last_req = now

        vesc.service_rx()
        _service_once("regen", cmd_a)

        iq = state.vesc_iq_current_a
        motor = state.vesc_motor_current_a
        inp = state.vesc_input_current_a
        rpm = abs(state.vesc_mech_rpm)
        vin = state.vesc_bus_voltage_v
        fault = int(state.vesc_fault_code)

        iq_sum += iq
        motor_sum += motor
        input_sum += inp
        rpm_sum += rpm
        count += 1

        if ticks_diff(now, start) <= EARLY_WINDOW_MS:
            early_iq_sum += iq
            early_count += 1

        if iq < iq_min:
            iq_min = iq
        if iq > iq_max:
            iq_max = iq

        if fault != 0:
            fault_nonzero_samples += 1
            fault_counts[fault] = fault_counts.get(fault, 0) + 1

        if vin < vin_min:
            vin_min = vin
        if vin > vin_max:
            vin_max = vin

        if motor < motor_min:
            motor_min = motor
        if inp < input_min:
            input_min = inp

        if current_limits is not None:
            if motor <= (current_limits["motor_min_current_a"] + LIMIT_MARGIN_A):
                near_motor_regen_limit_samples += 1
            if inp <= (current_limits["battery_min_current_a"] + LIMIT_MARGIN_A):
                near_battery_regen_limit_samples += 1

        if voltage_limits is not None:
            if vin >= (voltage_limits["max_input_voltage_v"] - LIMIT_MARGIN_V):
                near_max_vin_samples += 1
            if vin <= (voltage_limits["min_input_voltage_v"] + LIMIT_MARGIN_V):
                near_min_vin_samples += 1

        sleep_ms(SAMPLE_PERIOD_MS)

    if count == 0:
        return None

    return {
        "iq_avg": iq_sum / count,
        "iq_early_avg": (early_iq_sum / early_count) if early_count else 0.0,
        "iq_min": iq_min,
        "iq_max": iq_max,
        "motor_avg": motor_sum / count,
        "input_avg": input_sum / count,
        "rpm_avg": rpm_sum / count,
        "fault_nonzero_samples": fault_nonzero_samples,
        "vin_min": vin_min,
        "vin_max": vin_max,
        "motor_min": motor_min,
        "input_min": input_min,
        "near_motor_regen_limit_samples": near_motor_regen_limit_samples,
        "near_battery_regen_limit_samples": near_battery_regen_limit_samples,
        "near_max_vin_samples": near_max_vin_samples,
        "near_min_vin_samples": near_min_vin_samples,
        "fault_counts": fault_counts,
    }


print()
print("=" * 76)
print("  Regen Step Metrics (Speed-Banded)")
print("=" * 76)
print("Target spin band: %.0f..%.0f mech RPM" % (TARGET_RPM_MIN, TARGET_RPM_MAX))
print("Early window: %d ms, measure window: %d ms" % (EARLY_WINDOW_MS, MEASURE_WINDOW_MS))

live_current_limits, live_voltage_limits = _read_live_limits()
if live_current_limits is None:
    print("Live current limits: unavailable")
else:
    print(
        "Live current limits: motor=[%.1f, %.1f]A battery=[%.1f, %.1f]A"
        % (
            live_current_limits["motor_min_current_a"],
            live_current_limits["motor_max_current_a"],
            live_current_limits["battery_min_current_a"],
            live_current_limits["battery_max_current_a"],
        )
    )

if live_voltage_limits is None:
    print("Live voltage limits: unavailable")
else:
    print(
        "Live voltage limits: min=%.1fV max=%.1fV cut_start=%.1fV cut_end=%.1fV"
        % (
            live_voltage_limits["min_input_voltage_v"],
            live_voltage_limits["max_input_voltage_v"],
            live_voltage_limits["battery_cut_start_v"],
            live_voltage_limits["battery_cut_end_v"],
        )
    )

for cmd in BRAKE_LEVELS_A:
    ok, pre_rpm = _spin_to_band()
    if not ok:
        print("cmd=%5.1fA -> spin band not reached (check wheel free-spinning)" % cmd)
        continue

    m = _measure_regen_step(cmd, live_current_limits, live_voltage_limits)
    if m is None:
        print("cmd=%5.1fA -> no samples" % cmd)
        continue

    print(
        "cmd=%5.1fA  pre_rpm=%7.1f  iq_early=%+7.2fA  iq_avg=%+7.2fA  iq_min=%+7.2fA  "
        "motor_avg=%+7.2fA  in_avg=%+7.2fA  rpm_avg=%7.1f"
        % (
            cmd,
            pre_rpm,
            m["iq_early_avg"],
            m["iq_avg"],
            m["iq_min"],
            m["motor_avg"],
            m["input_avg"],
            m["rpm_avg"],
        )
    )
    print(
        "           fault_nonzero=%d  vin=[%.2f, %.2f]V  motor_min=%+.2fA  input_min=%+.2fA"
        % (
            m["fault_nonzero_samples"],
            m["vin_min"],
            m["vin_max"],
            m["motor_min"],
            m["input_min"],
        )
    )
    if live_current_limits is not None or live_voltage_limits is not None:
        print(
            "           near_limits: motor_regen=%d  batt_regen=%d  vin_max=%d  vin_min=%d"
            % (
                m["near_motor_regen_limit_samples"],
                m["near_battery_regen_limit_samples"],
                m["near_max_vin_samples"],
                m["near_min_vin_samples"],
            )
        )
    if m["fault_counts"]:
        codes = sorted(m["fault_counts"].keys())
        print(
            "           fault_codes: %s"
            % (", ".join("%d(x%d)" % (c, m["fault_counts"][c]) for c in codes))
        )

# Neutral at end
for _ in range(12):
    _service_once("neutral", 0.0)
    sleep_ms(20)

print("\nDone")
