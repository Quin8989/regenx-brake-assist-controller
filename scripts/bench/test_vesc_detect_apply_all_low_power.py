# scripts/bench/test_vesc_detect_apply_all_low_power.py
#
# Run a low-power foc_detect_apply_all terminal command and print all replies.
#
# Run:
#   mpremote mount . run scripts/bench/test_vesc_detect_apply_all_low_power.py

from scripts.lib.vesc_terminal import run_terminal_cmd
from scripts.lib.vesc_uart_template import VescUartTemplate

POWER_LOSS_W = 5.0

vesc = VescUartTemplate(rxbuf=4096)


print()
print("=" * 58)
print("  VESC FOC Detect Apply All (Low Power)")
print("=" * 58)

cmd = "foc_detect_apply_all %.1f" % POWER_LOSS_W
print("Command: %s" % cmd)
print("NOTE: Keep wheel unloaded and free to spin.")

lines = run_terminal_cmd(vesc, cmd, timeout_ms=30000)
if not lines:
    print("(No COMM_PRINT response)")
else:
    for line in lines:
        print(line)

print("\nDone")
