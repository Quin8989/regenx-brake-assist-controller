# boot.py — Minimal board startup for ReGenX Pico controller
#
# This file runs before main.py on every boot.
# Keep it minimal: no application logic belongs here.
#
# The RP2040 hardware WDT survives soft resets.  When mpremote connects
# (Ctrl-C + raw REPL) the main loop stops feeding, but the old WDT is
# still armed.  Re-arming here buys a fresh 8 s for imports and init.

try:
    from machine import WDT
    WDT(timeout=8000).feed()
except Exception:
    pass

print("ReGenX Brake-Assist Controller — booting")
