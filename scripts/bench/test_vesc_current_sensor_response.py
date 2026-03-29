# scripts/bench/test_vesc_current_sensor_response.py
#
# Read-only current sensor response check using COMM_GET_VALUES.
# No motor commands are sent.
#
# Protocol:
# - Phase 1: 10s idle baseline
# - Phase 2: 20s manual stimulation window (spin wheel by hand)
#
# Run:
#   mpremote mount . run scripts/bench/test_vesc_current_sensor_response.py

from time import sleep_ms, ticks_ms, ticks_diff
import struct

from scripts.lib.vesc_uart_template import VescUartTemplate

COMM_GET_VALUES = 4

SAMPLE_PERIOD_MS = 100
IDLE_MS = 10_000
ACTIVE_MS = 20_000

TELEMETRY_FMT = ">hhiiiihihiiiiiiB"
TELEMETRY_MIN_LEN = 1 + 53

vesc = VescUartTemplate(rxbuf=2048)


def read_values():
    payload = vesc.request(COMM_GET_VALUES, timeout_ms=1200)
    if not payload or payload[0] != COMM_GET_VALUES or len(payload) < TELEMETRY_MIN_LEN:
        return None

    vals = struct.unpack_from(TELEMETRY_FMT, payload, 1)
    return {
        "motor_current_a": vals[2] / 100.0,
        "input_current_a": vals[3] / 100.0,
        "duty_pct": vals[6] / 10.0,
        "erpm": vals[7],
        "vin_v": vals[8] / 10.0,
        "fault_code": vals[15],
    }


def phase_collect(label, duration_ms, prompt=None):
    print("\n--- %s (%0.1fs) ---" % (label, duration_ms / 1000.0))
    if prompt:
        print(prompt)

    start = ticks_ms()
    samples = 0
    read_fail = 0

    i_m_min = None
    i_m_max = None
    i_in_min = None
    i_in_max = None
    erpm_min = None
    erpm_max = None
    duty_min = None
    duty_max = None
    fault_nonzero = 0

    while ticks_diff(ticks_ms(), start) < duration_ms:
        data = read_values()
        if data is None:
            read_fail += 1
            sleep_ms(SAMPLE_PERIOD_MS)
            continue

        samples += 1

        i_m = data["motor_current_a"]
        i_in = data["input_current_a"]
        erpm = data["erpm"]
        duty = data["duty_pct"]

        if i_m_min is None or i_m < i_m_min:
            i_m_min = i_m
        if i_m_max is None or i_m > i_m_max:
            i_m_max = i_m

        if i_in_min is None or i_in < i_in_min:
            i_in_min = i_in
        if i_in_max is None or i_in > i_in_max:
            i_in_max = i_in

        if erpm_min is None or erpm < erpm_min:
            erpm_min = erpm
        if erpm_max is None or erpm > erpm_max:
            erpm_max = erpm

        if duty_min is None or duty < duty_min:
            duty_min = duty
        if duty_max is None or duty > duty_max:
            duty_max = duty

        if int(data["fault_code"]) != 0:
            fault_nonzero += 1

        sleep_ms(SAMPLE_PERIOD_MS)

    if samples == 0:
        return {
            "label": label,
            "samples": 0,
            "read_fail": read_fail,
        }

    return {
        "label": label,
        "samples": samples,
        "read_fail": read_fail,
        "motor_min": i_m_min,
        "motor_max": i_m_max,
        "motor_pp": i_m_max - i_m_min,
        "input_min": i_in_min,
        "input_max": i_in_max,
        "input_pp": i_in_max - i_in_min,
        "erpm_min": erpm_min,
        "erpm_max": erpm_max,
        "duty_min": duty_min,
        "duty_max": duty_max,
        "fault_nonzero": fault_nonzero,
    }


print()
print("=" * 62)
print("  VESC Current Sensor Response Check (Read-Only)")
print("=" * 62)
print("No control commands are sent to motor.")

idle = phase_collect("Phase 1: Idle Baseline", IDLE_MS)
active = phase_collect(
    "Phase 2: Manual Stimulus",
    ACTIVE_MS,
    prompt="Spin the wheel by hand (forward/back) several times now.",
)

print("\n" + "=" * 62)
print("Summary")
print("=" * 62)


def show(res):
    if res.get("samples", 0) == 0:
        print("%s: no samples (read_fail=%d)" % (res["label"], res.get("read_fail", 0)))
        return

    print("%s:" % res["label"])
    print("  samples=%d read_fail=%d" % (res["samples"], res["read_fail"]))
    print(
        "  motor_current: min=%+.2fA max=%+.2fA pp=%.2fA"
        % (res["motor_min"], res["motor_max"], res["motor_pp"])
    )
    print(
        "  input_current: min=%+.2fA max=%+.2fA pp=%.2fA"
        % (res["input_min"], res["input_max"], res["input_pp"])
    )
    print("  erpm range: %d .. %d" % (res["erpm_min"], res["erpm_max"]))
    print("  duty range: %.1f%% .. %.1f%%" % (res["duty_min"], res["duty_max"]))
    print("  nonzero fault samples: %d" % res["fault_nonzero"])


show(idle)
show(active)

if idle.get("samples", 0) > 0 and active.get("samples", 0) > 0:
    ppm = active["motor_pp"]
    ppi = active["input_pp"]
    erpm_span = active["erpm_max"] - active["erpm_min"]

    print("\nInterpretation:")
    if erpm_span > 50 and (ppm > 0.5 or ppi > 0.2):
        print("  PASS-LIKE: telemetry currents responded to wheel movement.")
    elif erpm_span > 50 and (ppm <= 0.5 and ppi <= 0.2):
        print("  INCONCLUSIVE: wheel speed changed but current channels barely moved.")
    else:
        print("  INCONCLUSIVE: no clear wheel movement was observed in telemetry.")

print("\nDone")
