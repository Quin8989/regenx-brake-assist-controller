# scripts/bench/test_vesc_uart_healthcheck.py
#
# Query a few VESC terminal diagnostics over UART from the Pico side:
# - fault
# - hw_status
# - foc_state

from scripts.lib.vesc_terminal import run_terminal_cmd
from scripts.lib.vesc_uart_template import VescUartTemplate

vesc = VescUartTemplate(rxbuf=4096)


print()
print("=" * 58)
print("  VESC UART Healthcheck")
print("=" * 58)

for cmd in ("fault", "hw_status", "foc_state"):
    print("\n>>> %s" % cmd)
    lines = run_terminal_cmd(vesc, cmd, timeout_ms=2200)
    if not lines:
        print("(No COMM_PRINT response)")
        continue
    for line in lines:
        if line.strip():
            print(line.rstrip())

print("\nDone")