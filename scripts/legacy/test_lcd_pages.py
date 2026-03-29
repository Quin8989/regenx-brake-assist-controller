# scripts/legacy/test_lcd_pages.py — Cycle through all LCD display pages on real hardware
#
# Reproduces the exact formatting from services/display_manager.py
# Uses only lcd_driver (already on Pico) + config/settings.py.
# Upload: mpremote cp config/settings.py :config/settings.py
#         mpremote cp drivers/lcd_driver.py :drivers/lcd_driver.py
#    Run: mpremote run scripts/legacy/test_lcd_pages.py

from time import sleep

from drivers.lcd_driver import LCDDriver

lcd = LCDDriver()
PAUSE = 3  # seconds between pages


def show(label, line0, line1):
    print(f"--- {label} ---")
    print(f"  [{line0:<16s}]")
    print(f"  [{line1:<16s}]")
    lcd.write_line(0, line0[:16])
    lcd.write_line(1, line1[:16])
    sleep(PAUSE)


# ---------- helpers matching display_manager.py formatting ----------

def run_page(mode, volts_v, pct, amps, rpm):
    volts = f"{volts_v:.1f}V"
    pct_s = f"{pct:.0f}%"
    pad0 = 16 - len(mode) - len(volts) - len(pct_s)
    line0 = mode + " " * max(pad0 - 1, 1) + volts + " " + pct_s

    amps_s = f"{abs(amps):.1f}A"
    rpm_s = f"{int(abs(rpm))}RPM"
    pad1 = 16 - len(amps_s) - len(rpm_s)
    line1 = " " * max(pad1 // 2, 1) + amps_s + " " * max(pad1 - pad1 // 2, 1) + rpm_s
    return line0[:16], line1[:16]


def precharge_page(volts_v, pct):
    pct_s = f"{pct:.0f}%"
    line1 = f"Vcap:{volts_v:>5.1f}V {pct_s:>4s}"
    return "PRECHARGE...", line1[:16]


# ---------- scenarios ----------

print("\n=== ReGenX LCD Page Test ===\n")
print(f"Each page displays for {PAUSE} seconds.\n")

# 1. OFF
show("OFF (boot)", "ReGenX  v1.0", "    Standby")

# 2. PRECHARGE early
l0, l1 = precharge_page(8.5, 0)
show("PRECHARGE (8.5V)", l0, l1)

# 3. PRECHARGE mid
l0, l1 = precharge_page(22.0, 35)
show("PRECHARGE (22V)", l0, l1)

# 4. COAST idle
l0, l1 = run_page("COAST", 25.2, 68, 0.0, 0)
show("COAST (idle 25.2V)", l0, l1)

# 5. COAST rolling
l0, l1 = run_page("COAST", 15.0, 0, 0.0, 85)
show("COAST (rolling 15V)", l0, l1)

# 6. COAST low battery
l0, l1 = run_page("COAST", 15.3, 1, 0.0, 0)
show("COAST (low batt 15.3V)", l0, l1)

# 7. COAST full charge
l0, l1 = run_page("COAST", 40.0, 100, 0.0, 0)
show("COAST (full 40V)", l0, l1)

# 8. ASSIST light
l0, l1 = run_page("ASSIST", 30.0, 78, 5.2, 210)
show("ASSIST (light 30V)", l0, l1)

# 9. ASSIST full
l0, l1 = run_page("ASSIST", 22.0, 35, 38.7, 580)
show("ASSIST (full 22V)", l0, l1)

# 10. ASSIST high RPM
l0, l1 = run_page("ASSIST", 35.0, 88, 20.0, 1200)
show("ASSIST (high RPM)", l0, l1)

# 11. REGEN braking
l0, l1 = run_page("REGEN", 28.0, 62, -12.3, 420)
show("REGEN (braking 28V)", l0, l1)

# 12. REGEN hard brake
l0, l1 = run_page("REGEN", 38.0, 95, -35.0, 750)
show("REGEN (hard brake 38V)", l0, l1)

# 13-17. FAULT pages
faults = [
    ("Overvoltage", "FAULT: Overvoltage"),
    ("VESC Timeout", "FAULT: VESC Timeout"),
    ("Throttle Range", "FAULT: Throttle Range"),
    ("Precharge Stall", "FAULT: Precharge Stall"),
    ("Internal Error", "FAULT: Internal Error"),
]
for label_text, desc in faults:
    show(desc, "!! FAULT !!", label_text)

# 18. Return to standby
show("OFF (end)", "ReGenX  v1.0", "  Test Complete")

print("\n=== LCD Page Test Complete ===")
