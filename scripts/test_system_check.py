# scripts/test_system_check.py — System integration test
#
# Verifies:
#   1. VESC UART communication (telemetry RX)
#   2. Throttle ADC readability
#   3. LCD display functionality
#
# Display shows:
#   Line 1: "VESC: OK/WAIT/FAIL"
#   Line 2: "THR: XX% CAP: YYV"
#
# Run with: mpremote run scripts/test_system_check.py

import time
from machine import WDT

from core import SharedState
from drivers.lcd_driver import LCDDriver
from drivers.throttle import Throttle
from services.vesc_comm import VESCComm

# RP2040 watchdog max is 8388 ms.
wdt = WDT(timeout=8000)

# Initialize hardware
print("[INIT] LCD...")
lcd = LCDDriver()
lcd.write_line(0, "SYSTEM TEST")
lcd.write_line(1, "Initializing...")
time.sleep(0.5)

print("[INIT] Throttle...")
throttle = Throttle()

print("[INIT] VESC UART...")

print("[INIT] Shared state...")
state = SharedState()

print("[INIT] VESCComm...")
vesc_comm = VESCComm(state)

# Ensure motor is always inhibited during test
state.inhibit_motor_commands = True
print("[SAFETY] Motor inhibited for test\n")

# Test loop
print("=== SYSTEM TEST RUNNING ===\n")
vesc_ok = False
vesc_timeout = 0
throttle_ok = False

start_time = time.ticks_ms()
test_duration_ms = 30_000  # 30 seconds

while time.ticks_ms() - start_time < test_duration_ms:
    wdt.feed()

    # Keep motor inhibited throughout test
    state.inhibit_motor_commands = True

    # 1. Request VESC telemetry periodically
    if time.ticks_ms() % 100 == 0:
        vesc_comm.request_telemetry()

    # 2. Service VESC RX
    vesc_comm.service_rx()

    # 3. Check if VESC has responded
    if state.last_vesc_rx_ms > 0:
        age_ms = time.ticks_ms() - state.last_vesc_rx_ms
        if age_ms < 500:
            vesc_ok = True
            vesc_timeout = 0
        else:
            vesc_timeout += 1

    # 4. Read throttle
    throttle.update()
    throttle_ok = throttle.is_valid
    throttle_pct = int(throttle.fraction * 100 + 0.5)

    # 5. Print debug info every 1 second
    now_ms = time.ticks_ms()
    if now_ms % 1000 < 100:
        status_line = f"VESC: {'OK' if vesc_ok else 'WAIT'} | THR: {throttle_pct:2d}% | CAP: {state.cap_voltage_v:.1f}V"
        print(status_line)

    # 6. Update LCD
    vesc_status = "OK" if vesc_ok else "CONNECTING..."
    thr_status = f"THR: {throttle_pct:2d}%"
    cap_status = f"CAP: {state.cap_voltage_v:.1f}V"

    # Line 0: VESC status
    lcd.write_line(0, f"VESC: {vesc_status:11s}")
    # Line 1: Throttle and cap
    lcd.write_line(1, f"{thr_status:8s} {cap_status:7s}")

    time.sleep(0.01)

# End of test
print("\n=== TEST COMPLETE ===\n")
print(f"VESC Communication: {'PASS' if vesc_ok else 'FAIL (no telemetry received)'}")
print(f"Throttle Valid: {'PASS' if throttle_ok else 'FAIL (invalid range)'}")
print(f"Final Cap Voltage: {state.cap_voltage_v:.1f} V")
print(f"Final Throttle: {throttle_pct}%")

# Display final result
if vesc_ok and throttle_ok:
    lcd.write_line(0, "TEST: OK")
    lcd.write_line(1, f"CAP:{state.cap_voltage_v:.0f}V THR:{throttle_pct:2d}%")
else:
    lcd.write_line(0, "TEST: FAIL")
    if not vesc_ok:
        lcd.write_line(1, "VESC no telemetry")
    elif not throttle_ok:
        lcd.write_line(1, "Throttle invalid")

print("\nHold in this state for inspection. Press reset to clear.")
