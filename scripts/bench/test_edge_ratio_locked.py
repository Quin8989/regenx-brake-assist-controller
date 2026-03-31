# scripts/bench/test_edge_ratio_locked.py
#
# Measure the EXACT motor/wheel ratio at each fresh wheel edge
# with the brake held (carrier locked).
#
# This isolates whether phantom slip comes from sensor timing
# or from real mechanical effects.
#
# Run:  mpremote mount . run scripts/bench/test_edge_ratio_locked.py
# Procedure: HOLD BRAKE before starting. Keep held entire test.

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
OBSERVE_MS = 8000
SAMPLE_MS = 5       # Fast poll to catch edges promptly
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


print()
print("*" * 60)
print("  EDGE-RATIO TEST (brake locked)")
print("*" * 60)
print("  Expected ratio: %.1f" % REGEN_LOCKED_RATIO)
print()
print(">>> HOLD THE BRAKE DOWN NOW <<<")
print("  (keep it held the entire test)")
sleep_ms(3000)

# Spin up with assist (brake held = carrier locked, still drives wheel)
print("[Spinning up with brake held...]")
start = ticks_ms()
last_req = start
while ticks_diff(ticks_ms(), start) < SPIN_MS:
    feed()
    last_req = service(last_req)
    vesc.send_assist(SPIN_A)
    sleep_ms(SAMPLE_MS)

print("[Assist off — logging every wheel edge]")
print()
print("%-6s  %-8s  %-8s  %-8s  %-8s  %-10s" % (
    "edge#", "whl_rpm", "mot_rpm", "ratio", "lock_fr", "edge_dt_ms"))
print("-" * 58)

# Observe — log at every fresh wheel edge
obs_start = ticks_ms()
last_req = obs_start
edge_count = 0
prev_edge_ms = None

while ticks_diff(ticks_ms(), obs_start) < OBSERVE_MS:
    feed()
    last_req = service(last_req)
    vesc.send_neutral()

    raw_rpm, valid, fresh = wheel.update()

    if fresh and valid:
        edge_count += 1
        now = ticks_ms()
        whl = max(0.0, raw_rpm)
        mot = abs(state.vesc_mech_rpm)
        expected = max(1e-6, whl * REGEN_LOCKED_RATIO)
        lock_fr = min(mot / expected, 1.0)
        ratio = mot / whl if whl > 1.0 else 0.0

        edge_dt = 0
        if prev_edge_ms is not None:
            edge_dt = ticks_diff(now, prev_edge_ms)
        prev_edge_ms = now

        print("%5d   %7.1f  %7.0f  %7.2f  %7.3f  %8d" % (
            edge_count, whl, mot, ratio, lock_fr, edge_dt))

    sleep_ms(SAMPLE_MS)

# Cleanup
for _ in range(10):
    vesc.send_neutral()
    sleep_ms(20)

print()
print("Total edges: %d in %d ms" % (edge_count, OBSERVE_MS))
print("Done.")
