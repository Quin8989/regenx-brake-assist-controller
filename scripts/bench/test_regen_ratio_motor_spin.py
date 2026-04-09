# scripts/bench/test_regen_ratio_motor_spin.py
#
# Automatically spin the motor with a small assist current and estimate:
#   ratio = abs(vesc_mech_rpm) / wheel_rpm
#
# Run:
#   mpremote mount . run scripts/bench/test_regen_ratio_motor_spin.py
#
# SAFETY:
# - Wheel must be off the ground.
# - Keep hands/loose items clear of drivetrain.
# - Script always sends neutral at the end.

from time import sleep_ms, ticks_ms, ticks_diff

from core import SharedState
from drivers.wheel_speed_hall import WheelSpeedHall
from services.vesc_comm import UARTPort, VESCComm

try:
    from machine import WDT
except Exception:
    WDT = None

SPIN_CURRENT_A = 3.0
RAMP_MS = 1500
RUN_MS = 12000
COOLDOWN_MS = 1000

TELEMETRY_PERIOD_MS = 50
LOOP_PERIOD_MS = 20
PRINT_PERIOD_MS = 250

MIN_WHEEL_RPM = 8.0
MIN_MOTOR_RPM = 50.0


state = SharedState()
wheel = WheelSpeedHall()
uart = UARTPort()
vesc = VESCComm(uart, state)
wdt = WDT(timeout=8000) if WDT is not None else None


print()
print("=" * 64)
print("  ReGenX Auto Spin Ratio Calibration")
print("=" * 64)
print("Target assist current: %.1f A" % SPIN_CURRENT_A)
print("Wheel must be floating in air.")

state.inhibit_motor_commands = False

ratios = []
rej_wheel_invalid = 0
rej_wheel_low = 0
rej_motor_low = 0

start = ticks_ms()
last_telemetry = start
last_print = start


def current_target(now_ms):
    t = ticks_diff(now_ms, start)
    if t < RAMP_MS:
        return SPIN_CURRENT_A * (t / float(max(RAMP_MS, 1)))
    if t < (RAMP_MS + RUN_MS):
        return SPIN_CURRENT_A
    return 0.0


try:
    while ticks_diff(ticks_ms(), start) < (RAMP_MS + RUN_MS + COOLDOWN_MS):
        now = ticks_ms()

        if wdt is not None:
            wdt.feed()

        if ticks_diff(now, last_telemetry) >= TELEMETRY_PERIOD_MS:
            vesc.request_telemetry()
            last_telemetry = now

        vesc.service_rx()

        wheel_rpm, wheel_valid = wheel.update()
        motor_rpm = abs(state.vesc_mech_rpm)

        cmd = current_target(now)
        if cmd > 0.0:
            vesc.send_assist(cmd)
        else:
            vesc.send_neutral()

        valid = True
        if not wheel_valid:
            rej_wheel_invalid += 1
            valid = False
        if wheel_rpm < MIN_WHEEL_RPM:
            rej_wheel_low += 1
            valid = False
        if motor_rpm < MIN_MOTOR_RPM:
            rej_motor_low += 1
            valid = False

        if valid:
            ratio = motor_rpm / max(wheel_rpm, 1e-6)
            ratios.append(ratio)

        if ticks_diff(now, last_print) >= PRINT_PERIOD_MS:
            live_ratio = (motor_rpm / wheel_rpm) if (wheel_valid and wheel_rpm > 1e-6) else 0.0
            print(
                "cmd=%4.1fA wheel=%7.1f rpm(%s) motor=%7.1f rpm ratio=%5.2f"
                % (cmd, wheel_rpm, "V" if wheel_valid else "-", motor_rpm, live_ratio)
            )
            last_print = now

        sleep_ms(LOOP_PERIOD_MS)

finally:
    # Ensure motor command is dropped even on exceptions.
    for _ in range(8):
        vesc.send_neutral()
        sleep_ms(20)

print()
print("=" * 64)
print("Summary")
print("=" * 64)

if not ratios:
    print("No valid ratio samples captured.")
    print("Reject counters:")
    print("  wheel_invalid: %d" % rej_wheel_invalid)
    print("  wheel_low:     %d" % rej_wheel_low)
    print("  motor_low:     %d" % rej_motor_low)
    print("Check wheel hall signal and VESC telemetry.")
    raise SystemExit(1)

ratios.sort()
n = len(ratios)
mean = sum(ratios) / n
median = ratios[n // 2] if (n % 2 == 1) else 0.5 * (ratios[n // 2 - 1] + ratios[n // 2])
trim = n // 10
core = ratios[trim : n - trim] if (trim > 0 and (n - 2 * trim) >= 3) else ratios
robust = sum(core) / len(core)

print("Samples: %d" % n)
print("Ratio min/max: %.2f / %.2f" % (ratios[0], ratios[-1]))
print("Ratio mean:    %.3f" % mean)
print("Ratio median:  %.3f" % median)
print("Ratio robust:  %.3f" % robust)

suggested = round(robust, 2)
print()
print("Measured motor/wheel gear ratio: %.2f" % suggested)
print("Done")
