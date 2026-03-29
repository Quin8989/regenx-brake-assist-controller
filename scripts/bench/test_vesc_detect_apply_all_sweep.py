# scripts/bench/test_vesc_detect_apply_all_sweep.py
#
# Sweep foc_detect_apply_all with decreasing power loss values.
#
# Run:
#   mpremote mount . run scripts/bench/test_vesc_detect_apply_all_sweep.py

from time import sleep_ms

from scripts.lib.vesc_terminal import run_terminal_cmd
from scripts.lib.vesc_uart_template import VescUartTemplate

POWER_LOSS_VALUES = (3.0, 2.0, 1.0)

vesc = VescUartTemplate(rxbuf=4096)


print()
print("=" * 60)
print("  VESC FOC Detect Apply All Sweep")
print("=" * 60)
print("NOTE: Wheel must be unloaded and free to spin.")

for value in POWER_LOSS_VALUES:
    cmd = "foc_detect_apply_all %.1f" % value
    print("\n>>> %s" % cmd)
    lines = run_terminal_cmd(vesc, cmd, timeout_ms=30000)
    if not lines:
        print("(No COMM_PRINT response)")
    else:
        for line in lines:
            print(line)

    # Short pause between attempts.
    sleep_ms(500)

print("\nDone")
