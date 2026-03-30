# scripts/bench/test_regen_spin_brake_sweep.py
#
# Spin the wheel with assist current, then command regen brake current while
# wheel is still spinning. This isolates VESC regen response under back-EMF.
#
# Run:
#   mpremote mount . run scripts/bench/test_regen_spin_brake_sweep.py

from time import sleep_ms, ticks_ms, ticks_diff

from core import SharedState
from services.vesc_comm import UARTPort, VESCComm

try:
    from machine import WDT
except Exception:
    WDT = None

SAMPLE_PERIOD_MS = 20
TELEMETRY_PERIOD_MS = 50

SPIN_ASSIST_A = 4.0
SPINUP_MS = 1800
SETTLE_MS = 200
MEASURE_MS = 1200

BRAKE_LEVELS_A = (5.0, 10.0, 15.0, 20.0, 30.0)

state = SharedState()
uart = UARTPort()
vesc = VESCComm(uart, state)
wdt = WDT(timeout=8000) if WDT is not None else None


def run_window(duration_ms, mode, amps=0.0):
    start = ticks_ms()
    last_req = start

    iq_sum = 0.0
    rpm_sum = 0.0
    duty_sum = 0.0
    count = 0

    while ticks_diff(ticks_ms(), start) < duration_ms:
        now = ticks_ms()

        if wdt is not None:
            wdt.feed()

        if ticks_diff(now, last_req) >= TELEMETRY_PERIOD_MS:
            vesc.request_telemetry()
            last_req = now

        vesc.service_rx()

        if mode == "assist":
            vesc.send_assist(amps)
        elif mode == "regen":
            vesc.send_regen(amps)
        else:
            vesc.send_neutral()

        iq_sum += state.vesc_iq_current_a
        rpm_sum += abs(state.vesc_mech_rpm)
        duty_sum += abs(state.vesc_duty_cycle)
        count += 1

        sleep_ms(SAMPLE_PERIOD_MS)

    if count == 0:
        return 0.0, 0.0, 0.0
    return iq_sum / count, rpm_sum / count, duty_sum / count


print()
print("=" * 72)
print("  Spin-Then-Brake Regen Sweep")
print("=" * 72)
print("Spin assist: %.1f A, levels: %s A" % (SPIN_ASSIST_A, ", ".join("%.0f" % x for x in BRAKE_LEVELS_A)))

for level in BRAKE_LEVELS_A:
    # 1) Spin up
    run_window(SPINUP_MS, "assist", SPIN_ASSIST_A)

    # 2) Short neutral transition
    run_window(SETTLE_MS, "neutral", 0.0)

    # 3) Apply regen and measure response while wheel has speed
    iq_avg, rpm_avg, duty_avg = run_window(MEASURE_MS, "regen", level)

    print(
        "cmd=%5.1fA -> avg_iq=%+7.2fA  avg_mech_rpm=%7.1f  avg_duty=%6.3f"
        % (level, iq_avg, rpm_avg, duty_avg)
    )

# Ensure neutral at end
for _ in range(12):
    vesc.send_neutral()
    sleep_ms(20)

print("\nDone")
