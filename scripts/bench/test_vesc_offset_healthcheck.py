# scripts/bench/test_vesc_offset_healthcheck.py
#
# Run VESC terminal diagnostics for current-sensor offset health:
# 1) fault
# 2) hw_status (before)
# 3) foc_dc_cal
# 4) hw_status (after)
# 5) fault
#
# Produces a PASS/FAIL summary for offset sanity.
#
# Run:
#   mpremote mount . run scripts/bench/test_vesc_offset_healthcheck.py

from scripts.lib.vesc_terminal import help_has_command, run_terminal_cmd
from scripts.lib.vesc_uart_template import VescUartTemplate

MIDDLE_ADC = 2048
MAX_DELTA = 620

vesc = VescUartTemplate(rxbuf=3072)


def parse_offsets(lines):
    # Expected line includes text similar to:
    # "FOC Current Offsets: 2048 2051 2039"
    for line in lines:
        if "FOC Current Offsets" in line:
            parts = line.replace(":", " ").split()
            nums = []
            for token in parts:
                try:
                    nums.append(int(round(float(token))))
                except ValueError:
                    continue
            if len(nums) >= 3:
                return nums[-3:]
    return None


def offsets_pass(offsets):
    # Some VESC variants report a placeholder 0.00 for an unused third shunt.
    valid_offsets = [v for v in offsets if v > 0]
    deltas = [abs(v - MIDDLE_ADC) for v in valid_offsets]
    return all(d <= MAX_DELTA for d in deltas), deltas


print()
print("=" * 50)
print("  VESC Offset Health Check")
print("=" * 50)
print("Threshold check: abs(offset - %d) <= %d" % (MIDDLE_ADC, MAX_DELTA))

print("\n[1/5] Active fault before calibration")
fault_before = run_terminal_cmd(vesc, "fault", timeout_ms=1200)
for line in fault_before:
    print("  %s" % line)

print("\n[2/5] hw_status before calibration")
status_before = run_terminal_cmd(vesc, "hw_status", timeout_ms=2200)
for line in status_before:
    print("  %s" % line)
offsets_before = parse_offsets(status_before)

print("\n[3/5] Running foc_dc_cal")
if help_has_command(vesc, "foc_dc_cal"):
    cal_lines = run_terminal_cmd(vesc, "foc_dc_cal", timeout_ms=7000)
    for line in cal_lines:
        print("  %s" % line)
else:
    print("  foc_dc_cal is not available on this firmware terminal.")
    print("  Trying fallback: foc_sensors_detect_apply 5.0")
    print("  NOTE: Ensure wheel is off the ground before running detection commands.")
    cal_lines = run_terminal_cmd(vesc, "foc_sensors_detect_apply 5.0", timeout_ms=15000)
    for line in cal_lines:
        print("  %s" % line)

    help_lines = run_terminal_cmd(vesc, "help", timeout_ms=2500)
    cal_related = []
    for line in help_lines:
        low = line.lower()
        if "cal" in low or "foc" in low or "detect" in low:
            cal_related.append(line)
    if cal_related:
        print("  Available related commands:")
        for line in cal_related:
            print("    %s" % line)

print("\n[4/5] hw_status after calibration")
status_after = run_terminal_cmd(vesc, "hw_status", timeout_ms=2200)
for line in status_after:
    print("  %s" % line)
offsets_after = parse_offsets(status_after)

print("\n[5/5] Active fault after calibration")
fault_after = run_terminal_cmd(vesc, "fault", timeout_ms=1200)
for line in fault_after:
    print("  %s" % line)

print("\n" + "=" * 50)
print("Summary")
print("=" * 50)

if offsets_before is None:
    print("Offsets before: UNAVAILABLE")
else:
    ok_before, deltas_before = offsets_pass(offsets_before)
    print("Offsets before: %s deltas=%s -> %s" % (offsets_before, deltas_before, "PASS" if ok_before else "FAIL"))

if offsets_after is None:
    print("Offsets after:  UNAVAILABLE")
    ok_after = False
else:
    ok_after, deltas_after = offsets_pass(offsets_after)
    print("Offsets after:  %s deltas=%s -> %s" % (offsets_after, deltas_after, "PASS" if ok_after else "FAIL"))

fault_after_text = ""
if fault_after:
    fault_after_text = fault_after[-1].strip()

fault_clear = fault_after_text == "FAULT_CODE_NONE"
print("Fault after:    %s" % (fault_after_text if fault_after_text else "UNAVAILABLE"))

if ok_after and fault_clear:
    print("\nRESULT: PASS (offsets healthy and fault cleared)")
else:
    print("\nRESULT: FAIL (offsets and/or active fault still bad)")

print("\nDone")