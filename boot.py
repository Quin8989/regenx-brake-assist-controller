# boot.py — Minimal board startup for ReGenX Pico controller
#
# This file runs before main.py on every boot.
# Keep it minimal: no application logic belongs here.

# TODO: Decide whether to enable a watchdog here or later in main.py
# TODO: Decide whether USB serial debug should be conditionally enabled

print("ReGenX Brake-Assist Controller — booting")
