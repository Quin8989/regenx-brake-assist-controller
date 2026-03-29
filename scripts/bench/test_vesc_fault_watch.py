# scripts/bench/test_vesc_fault_watch.py
#
# Watch VESC fault behavior over a short window and report whether a fault
# appears transient or persistent.
#
# Run:
#   mpremote mount . run scripts/bench/test_vesc_fault_watch.py

from time import sleep_ms, ticks_ms, ticks_diff
import struct

from scripts.lib.vesc_uart_template import VescUartTemplate

COMM_GET_VALUES = 4

WATCH_SECONDS = 60
SAMPLE_PERIOD_MS = 500

TELEMETRY_FMT = ">hhiiiihihiiiiiiB"
TELEMETRY_MIN_LEN = 1 + 53

FAULT_NAMES = {
    0: "NONE",
    1: "OVER_VOLTAGE",
    2: "UNDER_VOLTAGE",
    3: "DRV",
    4: "ABS_OVER_CURRENT",
    5: "OVER_TEMP_FET",
    6: "OVER_TEMP_MOTOR",
    7: "GATE_DRIVER_OVER_VOLTAGE",
    8: "GATE_DRIVER_UNDER_VOLTAGE",
    9: "MCU_UNDER_VOLTAGE",
    10: "BOOTING_FROM_WATCHDOG_RESET",
    11: "ENCODER_SPI",
    12: "ENCODER_SINCOS_BELOW_MIN_AMPLITUDE",
    13: "ENCODER_SINCOS_ABOVE_MAX_AMPLITUDE",
    14: "FLASH_CORRUPTION",
    15: "HIGH_OFFSET_CURRENT_SENSOR_1",
    16: "HIGH_OFFSET_CURRENT_SENSOR_2",
    17: "HIGH_OFFSET_CURRENT_SENSOR_3",
    18: "UNBALANCED_CURRENTS",
    19: "BRK",
    20: "RESOLVER_LOT",
    21: "RESOLVER_DOS",
    22: "RESOLVER_LOS",
    23: "FLASH_CORRUPTION_APP_CFG",
    24: "FLASH_CORRUPTION_MC_CFG",
    25: "ENCODER_NO_MAGNET",
    26: "ENCODER_MAGNET_TOO_STRONG",
    27: "PHASE_FILTER",
    28: "ENCODER_FAULT",
    29: "LV_OUTPUT_FAULT",
}

vesc = VescUartTemplate(rxbuf=1024)


def read_values():
    payload = vesc.request(COMM_GET_VALUES, timeout_ms=1500)
    if not payload or payload[0] != COMM_GET_VALUES or len(payload) < TELEMETRY_MIN_LEN:
        return None

    vals = struct.unpack_from(TELEMETRY_FMT, payload, 1)
    return {
        "fet_temp_c": vals[0] / 10.0,
        "motor_temp_c": vals[1] / 10.0,
        "motor_current_a": vals[2] / 100.0,
        "input_current_a": vals[3] / 100.0,
        "duty_pct": vals[6] / 10.0,
        "erpm": vals[7],
        "vin_v": vals[8] / 10.0,
        "fault_code": vals[15],
    }


print()
print("=" * 50)
print("  VESC Fault Watch")
print("=" * 50)
print("Window: %d s, period: %d ms" % (WATCH_SECONDS, SAMPLE_PERIOD_MS))

start = ticks_ms()
sample = 0
nonzero_fault_samples = 0
fault_counts = {}
first_nonzero = None
last_nonzero = None

while ticks_diff(ticks_ms(), start) < WATCH_SECONDS * 1000:
    sample += 1
    t_ms = ticks_diff(ticks_ms(), start)
    data = read_values()

    if data is None:
        print("t=%5.1fs  sample=%03d  READ_FAIL" % (t_ms / 1000.0, sample))
        sleep_ms(SAMPLE_PERIOD_MS)
        continue

    fault = int(data["fault_code"])
    fault_counts[fault] = fault_counts.get(fault, 0) + 1

    if fault != 0:
        nonzero_fault_samples += 1
        if first_nonzero is None:
            first_nonzero = t_ms
        last_nonzero = t_ms

    print(
        "t=%5.1fs  f=%2d %-34s  vin=%5.1fV  i_m=%6.2fA  duty=%5.1f%%  erpm=%6d"
        % (
            t_ms / 1000.0,
            fault,
            FAULT_NAMES.get(fault, "UNKNOWN"),
            data["vin_v"],
            data["motor_current_a"],
            data["duty_pct"],
            data["erpm"],
        )
    )
    sleep_ms(SAMPLE_PERIOD_MS)

print("\n" + "=" * 50)
print("Summary")
print("=" * 50)
print("Samples: %d" % sample)
print("Non-zero fault samples: %d" % nonzero_fault_samples)

for code in sorted(fault_counts.keys()):
    print("  %2d %-34s  count=%d" % (code, FAULT_NAMES.get(code, "UNKNOWN"), fault_counts[code]))

if nonzero_fault_samples == 0:
    print("\nClassification: NO ACTIVE FAULT DURING WINDOW")
elif nonzero_fault_samples < max(2, sample // 5):
    print("\nClassification: INTERMITTENT/TRANSIENT FAULT")
else:
    print("\nClassification: PERSISTENT FAULT")

if first_nonzero is not None:
    print("First non-zero at: %.1fs" % (first_nonzero / 1000.0))
    print("Last non-zero at:  %.1fs" % (last_nonzero / 1000.0))

print("\nDone")