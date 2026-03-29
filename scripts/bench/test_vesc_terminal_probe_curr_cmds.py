# scripts/bench/test_vesc_terminal_probe_curr_cmds.py
#
# Probe availability/output of terminal commands useful for current diagnostics.

from scripts.lib.vesc_terminal import run_terminal_cmd
from scripts.lib.vesc_uart_template import VescUartTemplate

vesc = VescUartTemplate(rxbuf=4096)


print()
print("=" * 58)
print("  VESC Terminal Current-Command Probe")
print("=" * 58)

for cmd, t in (("help", 3000), ("foc_state", 2000), ("hw_status", 2000), ("fault", 1200)):
    print("\n>>> %s" % cmd)
    out = run_terminal_cmd(vesc, cmd, timeout_ms=t)
    if not out:
        print("(No output)")
        continue
    if cmd == "help":
        for line in out:
            low = line.lower()
            if "curr" in low or "foc" in low or "sample" in low or "plot" in low or "state" in low:
                print(line)
    else:
        for line in out:
            print(line)

print("\nDone")
