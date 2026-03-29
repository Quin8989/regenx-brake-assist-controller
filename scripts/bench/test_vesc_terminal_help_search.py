# scripts/bench/test_vesc_terminal_help_search.py
#
# Search VESC terminal help output for keywords from the Pico side.

from scripts.lib.vesc_terminal import run_terminal_cmd
from scripts.lib.vesc_uart_template import VescUartTemplate

KEYWORDS = (
    "set",
    "conf",
    "write",
    "store",
    "app",
    "uart",
    "can",
)

vesc = VescUartTemplate(rxbuf=4096)


print()
print("=" * 62)
print("  VESC Terminal Help Search")
print("=" * 62)
print("Keywords:", ", ".join(KEYWORDS))

lines = run_terminal_cmd(vesc, "help", timeout_ms=4000)
matches = []
for line in lines:
    low = line.lower()
    for keyword in KEYWORDS:
        if keyword in low:
            matches.append(line.rstrip())
            break

print()
if not matches:
    print("No matches found.")
else:
    for line in matches:
        print(line)

print("\nDone")