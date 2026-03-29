# scripts/bench/test_vesc_terminal_faults.py
#
# Query VESC terminal fault diagnostics over UART.
#
# Run:
#   mpremote mount . run scripts/bench/test_vesc_terminal_faults.py

from scripts.lib.vesc_terminal import run_terminal_cmd
from scripts.lib.vesc_uart_template import VescUartTemplate

vesc = VescUartTemplate(rxbuf=2048)


print()
print("=" * 50)
print("  VESC Terminal Fault Diagnostics")
print("=" * 50)

for cmd in ("fault", "faults"):
    print("\n>>> %s" % cmd)
    lines = run_terminal_cmd(vesc, cmd, timeout_ms=1500)
    if not lines:
        print("(No COMM_PRINT response)")
        continue
    for line in lines:
        print(line)

print("\nDone")