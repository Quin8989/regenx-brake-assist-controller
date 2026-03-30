# scripts/bench/test_regen_comprehensive_sweep.py
#
# Comprehensive forced-current regen characterization.
#
# Protocol per brake level:
#   1. Spin up with assist until wheel is confirmed in-band (TARGET_RPM_MIN..MAX).
#   2. Apply fixed brake command for MEASURE_MS, sampling at SAMPLE_PERIOD_MS.
#   3. Log per-sample: t_ms, iq, motor_current, input_current, duty, erpm,
#      mech_rpm, vin, fet_temp, motor_temp, fault_code.
#   4. Compute per-step summary:
#      - iq: early avg (first 250 ms), full avg, min, max
#      - motor / battery current: avg, min
#      - deceleration rate: (pre_rpm - avg_rpm) / window_s  [RPM/s]
#      - VIN: min, max (monitors charge path)
#      - FET/motor temp: max observed
#      - Fault histogram: code -> count
#      - Near-limit indicators vs live MCCONF limits
#
# Run:
#   mpremote mount . run scripts/bench/test_regen_comprehensive_sweep.py

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

# ── Timing ────────────────────────────────────────────────────────────────────
SAMPLE_PERIOD_MS   = 20     # per-sample telemetry/command rate
TELEMETRY_PERIOD_MS = 40    # how often to fire COMM_GET_VALUES request
SPIN_TIMEOUT_MS    = 6000   # max time to reach target RPM band
IN_BAND_CONFIRM_N  = 4      # consecutive in-band samples before brake applied
EARLY_WINDOW_MS    = 300    # "early response" averaging window
MEASURE_MS         = 1500   # braking measurement window per level
SETTLE_MS          = 300    # neutral coast between levels

# ── Sweep configuration ───────────────────────────────────────────────────────
SPIN_ASSIST_A   = 4.0
TARGET_RPM_MIN  = 140.0
TARGET_RPM_MAX  = 220.0
BRAKE_LEVELS_A  = (0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0, 15.0, 20.0)

# ── VESC protocol ─────────────────────────────────────────────────────────────
COMM_GET_MCCONF = 14
LIMIT_MARGIN_A  = 0.5
LIMIT_MARGIN_V  = 0.3

FAULT_NAMES = {
    0: "NONE",
    1: "OVER_VOLTAGE",
    2: "UNDER_VOLTAGE",
    3: "DRV",
    4: "ABS_OVER_CURRENT",
    5: "OVER_TEMP_FET",
    6: "OVER_TEMP_MOTOR",
    7: "GD_OVER_VOLT",
    8: "GD_UNDER_VOLT",
    9: "MCU_UNDER_VOLT",
    10: "WDT_RESET",
    15: "HI_OFFSET_CS1",
    16: "HI_OFFSET_CS2",
    17: "HI_OFFSET_CS3",
    18: "UNBAL_CURRENTS",
}

# ── Setup ─────────────────────────────────────────────────────────────────────
state = SharedState()
uart  = UARTPort()
vesc  = VESCComm(uart, state)
wdt   = WDT(timeout=8000) if WDT is not None else None


# ── MCCONF limit helpers ──────────────────────────────────────────────────────
def _rf32(data, off):
    raw = struct.unpack_from(">I", data, off)[0]
    exp = (raw >> 23) & 0xFF
    frac = raw & 0x7FFFFF
    neg = raw & (1 << 31)
    v = 0.0
    if exp or frac:
        v = frac / (8388608.0 * 2.0) + 0.5
        exp -= 126
    if neg:
        v = -v
    return math.ldexp(v, exp)


def _ru16x10(data, off):
    return struct.unpack_from(">H", data, off)[0] / 10.0


def _read_live_limits():
    raw = VescUartTemplate(rxbuf=3072).request(COMM_GET_MCCONF, timeout_ms=4000)
    if not raw or raw[0] != COMM_GET_MCCONF or len(raw) < 64:
        return None, None
    b = raw[1:]
    cur = {
        "motor_max":   _rf32(b, 8),
        "motor_min":   _rf32(b, 12),
        "battery_max": _rf32(b, 16),
        "battery_min": _rf32(b, 20),
    }
    if not (cur["motor_max"] > 0 and cur["motor_min"] < 0
            and abs(cur["motor_max"]) < 5000 and abs(cur["motor_min"]) < 5000):
        cur = None
    volt = {
        "vin_min":       _ru16x10(b, 50),
        "vin_max":       _ru16x10(b, 52),
        "cut_start":     _ru16x10(b, 54),
        "cut_end":       _ru16x10(b, 56),
    }
    if not (0 <= volt["vin_min"] <= 100 and 0 <= volt["vin_max"] <= 100
            and volt["vin_max"] >= volt["vin_min"]):
        volt = None
    return cur, volt


# ── Service helpers ───────────────────────────────────────────────────────────
def _tick(last_req_ms):
    now = ticks_ms()
    if wdt is not None:
        wdt.feed()
    if ticks_diff(now, last_req_ms) >= TELEMETRY_PERIOD_MS:
        vesc.request_telemetry()
        last_req_ms = now
    vesc.service_rx()
    return last_req_ms


def _spin_to_band():
    start = ticks_ms()
    last_req = start
    in_band = 0
    confirmed_rpm = 0.0
    while ticks_diff(ticks_ms(), start) < SPIN_TIMEOUT_MS:
        last_req = _tick(last_req)
        vesc.send_assist(SPIN_ASSIST_A)
        rpm = abs(state.vesc_mech_rpm)
        if TARGET_RPM_MIN <= rpm <= TARGET_RPM_MAX:
            in_band += 1
            confirmed_rpm = rpm
            if in_band >= IN_BAND_CONFIRM_N:
                return True, confirmed_rpm
        else:
            in_band = 0
        sleep_ms(SAMPLE_PERIOD_MS)
    return False, confirmed_rpm


def _coast(duration_ms):
    start = ticks_ms()
    last_req = start
    while ticks_diff(ticks_ms(), start) < duration_ms:
        last_req = _tick(last_req)
        vesc.send_neutral()
        sleep_ms(SAMPLE_PERIOD_MS)


# ── Per-step measurement ──────────────────────────────────────────────────────
def _measure_step(cmd_a, cur_limits, volt_limits):
    start = ticks_ms()
    last_req = start

    # accumulators
    n = 0
    iq_sum = iq_early_sum = 0.0
    motor_sum = input_sum = duty_sum = rpm_sum = 0.0
    vin_sum = 0.0
    n_early = 0

    iq_min = iq_peak_neg = 1e9
    iq_max = -1e9
    motor_min = input_min = 1e9
    vin_min = 1e9
    vin_max = -1e9
    fet_max = motor_temp_max = -1e9

    fault_counts = {}
    near_motor_lim = near_batt_lim = near_vin_max = near_vin_min = 0

    rpm_first = None   # first measured RPM (for decel calc)

    while ticks_diff(ticks_ms(), start) < MEASURE_MS:
        now = ticks_ms()
        last_req = _tick(last_req)
        vesc.send_regen(cmd_a)

        t_rel = ticks_diff(now, start)
        iq    = state.vesc_iq_current_a
        motor = state.vesc_motor_current_a
        inp   = state.vesc_input_current_a
        duty  = state.vesc_duty_cycle
        rpm   = state.vesc_mech_rpm
        vin   = state.vesc_bus_voltage_v
        fet   = state.vesc_temp_fet_c
        mtemp = state.vesc_temp_motor_c
        fault = int(state.vesc_fault_code)

        if rpm_first is None:
            rpm_first = abs(rpm)

        n += 1
        iq_sum    += iq
        motor_sum += motor
        input_sum += inp
        duty_sum  += duty
        rpm_sum   += abs(rpm)
        vin_sum   += vin

        if t_rel <= EARLY_WINDOW_MS:
            iq_early_sum += iq
            n_early += 1

        if iq < iq_min:    iq_min = iq
        if iq > iq_max:    iq_max = iq
        if iq < iq_peak_neg: iq_peak_neg = iq
        if motor < motor_min: motor_min = motor
        if inp < input_min:   input_min = inp
        if vin < vin_min:  vin_min = vin
        if vin > vin_max:  vin_max = vin
        if fet > fet_max:       fet_max = fet
        if mtemp > motor_temp_max: motor_temp_max = mtemp

        fault_counts[fault] = fault_counts.get(fault, 0) + 1

        if cur_limits:
            if motor <= cur_limits["motor_min"] + LIMIT_MARGIN_A:
                near_motor_lim += 1
            if inp <= cur_limits["battery_min"] + LIMIT_MARGIN_A:
                near_batt_lim += 1
        if volt_limits:
            if vin >= volt_limits["vin_max"] - LIMIT_MARGIN_V:
                near_vin_max += 1
            if vin <= volt_limits["vin_min"] + LIMIT_MARGIN_V:
                near_vin_min += 1

        sleep_ms(SAMPLE_PERIOD_MS)

    if n == 0:
        return None

    duration_s = MEASURE_MS / 1000.0
    rpm_avg = rpm_sum / n
    decel_rpm_s = (rpm_first - rpm_avg) / duration_s if rpm_first is not None else 0.0

    return {
        "n": n,
        "iq_avg":       iq_sum / n,
        "iq_early_avg": iq_early_sum / n_early if n_early else 0.0,
        "iq_min":       iq_min,
        "iq_max":       iq_max,
        "iq_peak_neg":  iq_peak_neg,
        "motor_avg":    motor_sum / n,
        "motor_min":    motor_min,
        "input_avg":    input_sum / n,
        "input_min":    input_min,
        "duty_avg":     duty_sum / n,
        "rpm_avg":      rpm_avg,
        "rpm_first":    rpm_first if rpm_first is not None else 0.0,
        "decel_rpm_s":  decel_rpm_s,
        "vin_avg":      vin_sum / n,
        "vin_min":      vin_min,
        "vin_max":      vin_max,
        "fet_max":      fet_max,
        "motor_temp_max": motor_temp_max,
        "fault_counts": fault_counts,
        "near_motor_lim": near_motor_lim,
        "near_batt_lim":  near_batt_lim,
        "near_vin_max":   near_vin_max,
        "near_vin_min":   near_vin_min,
    }


# ── Main ──────────────────────────────────────────────────────────────────────
print()
print("=" * 80)
print("  Comprehensive Forced Regen Characterization")
print("=" * 80)
print("Spin band: %.0f..%.0f mech RPM  |  Levels: %s A"
      % (TARGET_RPM_MIN, TARGET_RPM_MAX,
         ", ".join("%.1f" % x for x in BRAKE_LEVELS_A)))

cur_lim, volt_lim = _read_live_limits()

if cur_lim:
    print("MCCONF current limits: motor=[%.1f, %.1f]A  battery=[%.1f, %.1f]A"
          % (cur_lim["motor_min"], cur_lim["motor_max"],
             cur_lim["battery_min"], cur_lim["battery_max"]))
else:
    print("MCCONF current limits: unavailable")

if volt_lim:
    print("MCCONF voltage limits: vin_min=%.1fV  vin_max=%.1fV  cut_start=%.1fV  cut_end=%.1fV"
          % (volt_lim["vin_min"], volt_lim["vin_max"],
             volt_lim["cut_start"], volt_lim["cut_end"]))
else:
    print("MCCONF voltage limits: unavailable")

print()

results = []

for cmd in BRAKE_LEVELS_A:
    ok, pre_rpm = _spin_to_band()
    if not ok:
        print("cmd=%5.1fA  SPIN FAILED (pre_rpm=%.1f) — skipping" % (cmd, pre_rpm))
        results.append((cmd, None, pre_rpm))
        _coast(SETTLE_MS)
        continue

    m = _measure_step(cmd, cur_lim, volt_lim)
    _coast(SETTLE_MS)

    if m is None:
        print("cmd=%5.1fA  NO SAMPLES — skipping" % cmd)
        results.append((cmd, None, pre_rpm))
        continue

    results.append((cmd, m, pre_rpm))

    # ── per-step detail output ────────────────────────────────────────────────
    fault_str = "none"
    nonzero_faults = {k: v for k, v in m["fault_counts"].items() if k != 0}
    if nonzero_faults:
        fault_str = " ".join(
            "%s(x%d)" % (FAULT_NAMES.get(k, "F%d" % k), v)
            for k, v in sorted(nonzero_faults.items())
        )

    print("cmd=%5.1fA  pre_rpm=%6.1f" % (cmd, pre_rpm))
    print("  IQ:      early_avg=%+6.2fA  avg=%+6.2fA  min=%+6.2fA  max=%+6.2fA  peak_neg=%+6.2fA"
          % (m["iq_early_avg"], m["iq_avg"], m["iq_min"], m["iq_max"], m["iq_peak_neg"]))
    print("  Motor:   avg=%+6.2fA  min=%+6.2fA"
          % (m["motor_avg"], m["motor_min"]))
    print("  Battery: avg=%+6.2fA  min=%+6.2fA"
          % (m["input_avg"], m["input_min"]))
    print("  Speed:   start_rpm=%6.1f  avg_rpm=%6.1f  decel=%.1f RPM/s"
          % (m["rpm_first"], m["rpm_avg"], m["decel_rpm_s"]))
    print("  Duty:    avg=%.3f" % m["duty_avg"])
    print("  VIN:     avg=%.2fV  min=%.2fV  max=%.2fV"
          % (m["vin_avg"], m["vin_min"], m["vin_max"]))
    print("  Temps:   FET_max=%.1fC  motor_max=%.1fC"
          % (m["fet_max"], m["motor_temp_max"]))
    print("  Faults:  %s" % fault_str)
    if cur_lim or volt_lim:
        print("  NearLim: motor=%d  batt=%d  vin_hi=%d  vin_lo=%d"
              % (m["near_motor_lim"], m["near_batt_lim"],
                 m["near_vin_max"], m["near_vin_min"]))


# ── Summary table ─────────────────────────────────────────────────────────────
print()
print("=" * 80)
print("  SUMMARY TABLE")
print("=" * 80)
print("%-7s %-8s %-9s %-9s %-9s %-9s %-9s %-8s %s"
      % ("cmd(A)", "pre_rpm", "iq_early", "iq_avg", "iq_min", "decel", "vin_avg", "fet_max", "faults"))
print("-" * 80)

for cmd, m, pre_rpm in results:
    if m is None:
        print("%-7.1f %-8.1f  FAILED" % (cmd, pre_rpm))
        continue
    nonzero_faults = {k: v for k, v in m["fault_counts"].items() if k != 0}
    fault_str = "ok"
    if nonzero_faults:
        fault_str = " ".join(
            "%s(x%d)" % (FAULT_NAMES.get(k, "F%d" % k), v)
            for k, v in sorted(nonzero_faults.items())
        )
    print("%-7.1f %-8.1f %-9.2f %-9.2f %-9.2f %-9.1f %-9.2f %-8.1f %s"
          % (cmd, pre_rpm, m["iq_early_avg"], m["iq_avg"], m["iq_min"],
             m["decel_rpm_s"], m["vin_avg"], m["fet_max"], fault_str))

# Neutral at end
for _ in range(15):
    vesc.send_neutral()
    sleep_ms(20)

print()
print("Done")
