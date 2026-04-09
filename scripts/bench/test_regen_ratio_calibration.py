# scripts/bench/test_regen_ratio_calibration.py
#
# Measure motor-to-wheel speed ratio from live telemetry while riding with
# steady throttle, then print the measured gear ratio.
#
# Run:
#   mpremote mount . run scripts/bench/test_regen_ratio_calibration.py
#
# Notes:
# - Works best with steady throttle on flat ground.
# - Uses absolute motor RPM, so sign conventions do not matter.

from time import sleep_ms, ticks_ms, ticks_diff

try:
    from machine import WDT
except Exception:
    WDT = None

from core import SharedState
from drivers.throttle import Throttle
from drivers.wheel_speed_hall import WheelSpeedHall
from services.vesc_comm import UARTPort, VESCComm


REQUEST_PERIOD_MS = 50
SAMPLE_PERIOD_MS = 20
RUN_MS = 30_000
PRINT_PERIOD_MS = 250

# Filter out poor-quality samples.
# Keep this low so light throttle in a stand test still captures samples.
MIN_THROTTLE_FRAC = 0.05
MIN_WHEEL_RPM = 5.0  # Low floor for calibration; just reject noise
MIN_MOTOR_RPM = 40.0


state = SharedState()
throttle = Throttle()
wheel = WheelSpeedHall()
uart = UARTPort()
vesc = VESCComm(uart, state)

# If watchdog is enabled by the runtime, keep feeding it during this bench run.
wdt = WDT(timeout=8000) if WDT is not None else None

print()
print("=" * 64)
print("  ReGenX Ratio Calibration (motor_mech_rpm / wheel_rpm)")
print("=" * 64)
print("Collecting for %.1f seconds..." % (RUN_MS / 1000.0))
print("Use steady throttle and hold speed as constant as possible.")

ratios = []
rej_throttle = 0
rej_wheel_valid = 0
rej_wheel_rpm = 0
rej_motor_rpm = 0
last_req = ticks_ms()
last_print = ticks_ms()
start = ticks_ms()

while ticks_diff(ticks_ms(), start) < RUN_MS:
    now = ticks_ms()

    if wdt is not None:
        wdt.feed()

    if ticks_diff(now, last_req) >= REQUEST_PERIOD_MS:
        vesc.request_telemetry()
        last_req = now

    vesc.service_rx()
    throttle.update()
    wheel_rpm, wheel_valid = wheel.update()

    motor_mech_rpm = abs(state.vesc_mech_rpm)

    sample_ok = True
    if not wheel_valid:
        rej_wheel_valid += 1
        sample_ok = False
    if throttle.is_valid and throttle.fraction < MIN_THROTTLE_FRAC:
        rej_throttle += 1
        sample_ok = False
    if wheel_rpm < MIN_WHEEL_RPM:
        rej_wheel_rpm += 1
        sample_ok = False
    if motor_mech_rpm < MIN_MOTOR_RPM:
        rej_motor_rpm += 1
        sample_ok = False

    if sample_ok:
        ratio = motor_mech_rpm / max(wheel_rpm, 1e-6)
        ratios.append(ratio)

        if ticks_diff(now, last_print) >= PRINT_PERIOD_MS:
            print(
                "thr=%4.0f%% wheel=%7.1f rpm motor=%7.1f rpm ratio=%5.2f"
                % (throttle.fraction * 100.0, wheel_rpm, motor_mech_rpm, ratio)
            )
            last_print = now

    sleep_ms(SAMPLE_PERIOD_MS)

print()
print("=" * 64)
print("Summary")
print("=" * 64)

if not ratios:
    print("No valid samples captured.")
    print("Reject counters:")
    print("  wheel_invalid:   %d" % rej_wheel_valid)
    print("  throttle_low:    %d" % rej_throttle)
    print("  wheel_rpm_low:   %d" % rej_wheel_rpm)
    print("  motor_rpm_low:   %d" % rej_motor_rpm)
    print("Check throttle validity, wheel sensor validity, and VESC telemetry.")
    raise SystemExit(1)

ratios.sort()
n = len(ratios)
median = ratios[n // 2] if (n % 2 == 1) else 0.5 * (ratios[n // 2 - 1] + ratios[n // 2])
mean = sum(ratios) / n

# Trim 10% on each side for a robust average.
trim = n // 10
if trim > 0 and (n - 2 * trim) >= 3:
    core = ratios[trim : n - trim]
else:
    core = ratios

robust_mean = sum(core) / len(core)

print("Samples: %d" % n)
print("Ratio min/max: %.2f / %.2f" % (ratios[0], ratios[-1]))
print("Ratio mean:    %.3f" % mean)
print("Ratio median:  %.3f" % median)
print("Ratio robust:  %.3f" % robust_mean)

suggested = round(robust_mean, 2)
print()
print("Measured motor/wheel gear ratio: %.2f" % suggested)
print("Use this to verify VESC_MOTOR_POLE_PAIRS and gear assumptions.")

if suggested < 1.5:
    print("Note: ratio near 1 suggests direct-drive behavior or scaling mismatch.")
elif suggested > 8.0:
    print("Note: unusually high ratio; verify pole-pair and wheel-speed scaling.")

print("Done")
