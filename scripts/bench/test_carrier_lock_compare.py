# scripts/bench/test_carrier_lock_compare.py
#
# Compare motor RPM with and without the brake held.
#   Cycle 1: coast (DO NOT hold brake) — expect motor RPM ≈ 0
#   Cycle 2: brake held — expect motor RPM ≈ wheel_rpm × ratio
#
# Run:  mpremote mount . run scripts/bench/test_carrier_lock_compare.py

from time import sleep_ms, ticks_ms, ticks_diff

from core import SharedState
from drivers.wheel_speed_hall import WheelSpeedHall
from services.vesc_comm import UARTPort, VESCComm
from config.settings import REGEN_LOCKED_RATIO

try:
    from machine import WDT
except Exception:
    WDT = None

SPIN_A = 4.0
SPIN_MS = 2500
OBSERVE_MS = 6000
SAMPLE_MS = 20
TELEM_MS = 50

state = SharedState()
uart = UARTPort()
vesc = VESCComm(uart, state)
wheel = WheelSpeedHall()
wdt = WDT(timeout=8000) if WDT is not None else None


def feed():
    if wdt:
        wdt.feed()


def service(last_req):
    now = ticks_ms()
    if ticks_diff(now, last_req) >= TELEM_MS:
        vesc.request_telemetry()
        return now
    vesc.service_rx()
    return last_req


def run_cycle(label, instructions):
    print()
    print("=" * 60)
    print("  %s" % label)
    print("=" * 60)
    print(instructions)
    print()
    sleep_ms(2000)

    # Spin up
    print("[Spinning up...]")
    start = ticks_ms()
    last_req = start
    while ticks_diff(ticks_ms(), start) < SPIN_MS:
        feed()
        last_req = service(last_req)
        vesc.send_assist(SPIN_A)
        sleep_ms(SAMPLE_MS)

    pre_rpm = abs(state.vesc_mech_rpm)
    print("[Assist off — observing for %ds at %.0f RPM]" % (OBSERVE_MS // 1000, pre_rpm))
    print()
    print("%-6s  %-8s  %-8s  %-8s  %-8s" % ("time", "whl_rpm", "mot_rpm", "ratio", "lock_fr"))
    print("-" * 45)

    # Observe — send neutral, log motor vs wheel
    start = ticks_ms()
    last_req = start
    last_print = start
    while ticks_diff(ticks_ms(), start) < OBSERVE_MS:
        feed()
        last_req = service(last_req)
        vesc.send_neutral()

        raw_rpm, valid, fresh = wheel.update()
        whl = max(0.0, raw_rpm) if valid else 0.0
        mot = abs(state.vesc_mech_rpm)

        now = ticks_ms()
        if ticks_diff(now, last_print) >= 500:
            last_print = now
            elapsed = ticks_diff(now, start) / 1000.0
            expected = max(1e-6, whl * REGEN_LOCKED_RATIO)
            lock_fr = min(mot / expected, 1.0) if expected > 0 else 0.0
            ratio = mot / whl if whl > 1.0 else 0.0
            print("%5.1fs  %7.0f  %7.0f  %7.2f  %7.3f" % (
                elapsed, whl, mot, ratio, lock_fr))

        sleep_ms(SAMPLE_MS)

    # Neutral cleanup
    for _ in range(10):
        vesc.send_neutral()
        sleep_ms(20)


print()
print("*" * 60)
print("  CARRIER LOCK COMPARISON TEST")
print("*" * 60)
print("  Expected locked ratio: %.1f" % REGEN_LOCKED_RATIO)

run_cycle(
    "CYCLE 1: COAST (no brake)",
    ">>> DO NOT TOUCH THE BRAKE <<<")

run_cycle(
    "CYCLE 2: BRAKE HELD",
    ">>> HOLD THE BRAKE DOWN NOW <<<")

run_cycle(
    "CYCLE 3: COAST AGAIN (no brake)",
    ">>> DO NOT TOUCH THE BRAKE <<<")

print()
print("Done.")
