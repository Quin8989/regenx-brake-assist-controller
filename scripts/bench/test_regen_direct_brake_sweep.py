# scripts/bench/test_regen_direct_brake_sweep.py
#
# Directly command fixed VESC brake currents (COMM_SET_BRAKE_CURRENT) and
# measure resulting iq from telemetry. This bypasses InputManager/StateMachine/
# PI logic and isolates VESC-side limits/response.
#
# Run:
#   mpremote mount . run scripts/bench/test_regen_direct_brake_sweep.py
#
# Safety:
# - Wheel must be off the ground.
# - Keep drivetrain clear.

from time import sleep_ms, ticks_ms, ticks_diff

from core import SharedState
from services.vesc_comm import UARTPort, VESCComm

try:
    from machine import WDT
except Exception:
    WDT = None

SAMPLE_PERIOD_MS = 20
TELEMETRY_PERIOD_MS = 50
SETTLE_MS = 500
MEASURE_MS = 1500

BRAKE_LEVELS_A = (5.0, 10.0, 15.0, 20.0, 30.0)

state = SharedState()
uart = UARTPort()
vesc = VESCComm(uart, state)
wdt = WDT(timeout=8000) if WDT is not None else None


def service_until(duration_ms, brake_cmd_a):
    start = ticks_ms()
    last_req = start

    count = 0
    iq_sum = 0.0
    rpm_sum = 0.0
    duty_sum = 0.0

    while ticks_diff(ticks_ms(), start) < duration_ms:
        now = ticks_ms()

        if wdt is not None:
            wdt.feed()

        if ticks_diff(now, last_req) >= TELEMETRY_PERIOD_MS:
            vesc.request_telemetry()
            last_req = now

        vesc.service_rx()
        vesc.send_current(-brake_cmd_a)

        iq_sum += state.vesc_iq_current_a
        rpm_sum += state.vesc_mech_rpm
        duty_sum += state.vesc_duty_cycle
        count += 1

        sleep_ms(SAMPLE_PERIOD_MS)

    if count <= 0:
        return 0.0, 0.0, 0.0

    return iq_sum / count, rpm_sum / count, duty_sum / count


print()
print("=" * 68)
print("  Direct Regen Brake Sweep (Bypass PI/State Machine)")
print("=" * 68)
print("Levels: %s A" % (", ".join("%.0f" % x for x in BRAKE_LEVELS_A)))

for level in BRAKE_LEVELS_A:
    # Allow command/telemetry to settle.
    service_until(SETTLE_MS, level)
    iq_avg, rpm_avg, duty_avg = service_until(MEASURE_MS, level)
    print(
        "cmd=%5.1fA -> avg_iq=%+7.2fA  avg_mech_rpm=%7.1f  avg_duty=%6.3f"
        % (level, iq_avg, rpm_avg, duty_avg)
    )

# Always return to neutral at end.
for _ in range(10):
    vesc.send_current(0.0)
    sleep_ms(20)

print("\nDone")
