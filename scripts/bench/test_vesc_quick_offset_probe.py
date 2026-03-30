# scripts/bench/test_vesc_quick_offset_probe.py
#
# Quick offset probe with short terminal command windows to reduce USB drop risk.
#
# Run:
#   mpremote mount . run scripts/bench/test_vesc_quick_offset_probe.py

from scripts.lib.vesc_terminal import help_has_command, run_terminal_cmd
from scripts.lib.vesc_uart_template import VescUartTemplate

vesc = VescUartTemplate(rxbuf=3072)


def print_block(title, lines):
    print("\n--- %s ---" % title)
    if not lines:
        print("(no output)")
        return
    for line in lines:
        s = line.rstrip()
        if s:
            print(s)


print()
print("=" * 58)
print("  VESC Quick Offset Probe")
print("=" * 58)

fault_before = run_terminal_cmd(vesc, "fault", timeout_ms=1200)
status_before = run_terminal_cmd(vesc, "hw_status", timeout_ms=1800)

print_block("fault (before)", fault_before)
print_block("hw_status (before)", status_before)

if help_has_command(vesc, "foc_dc_cal", timeout_ms=2200):
    cal = run_terminal_cmd(vesc, "foc_dc_cal", timeout_ms=2500)
    print_block("foc_dc_cal", cal)
else:
    print("\n--- foc_dc_cal ---")
    print("not available")

fault_after = run_terminal_cmd(vesc, "fault", timeout_ms=1200)
status_after = run_terminal_cmd(vesc, "hw_status", timeout_ms=1800)

print_block("fault (after)", fault_after)
print_block("hw_status (after)", status_after)

print("\nDone")
