# scripts/bench/monitor_throttle_noise.py
#
# Monitor throttle ADC + VESC telemetry at 100 Hz for ~60 seconds.
# Prints EVERY sample where throttle fraction > 0 or assist/regen
# command is non-zero.  Prints a heartbeat line every 2s so you
# know it's alive.  Also tracks ADC min/max/peak stats.
#
# Run:  mpremote mount . run scripts/bench/monitor_throttle_noise.py
# Stop: Ctrl-C

from time import sleep_ms, ticks_ms, ticks_diff
from machine import ADC, Pin

try:
    from machine import WDT
except Exception:
    WDT = None

from core import SharedState
from drivers.throttle import Throttle
from services.vesc_comm import UARTPort, VESCComm
from config.settings import (
    THROTTLE_RAW_MIN,
    THROTTLE_DEADBAND,
)

SAMPLE_MS = 10         # 100 Hz
TELEM_MS = 25          # 40 Hz telemetry
RUN_S = 65             # run for ~65 seconds
HEARTBEAT_MS = 2000    # status line every 2s

state = SharedState()
uart = UARTPort()
vesc = VESCComm(uart, state)
throttle = Throttle()
wdt = WDT(timeout=8000) if WDT is not None else None

# Deadband edge in raw counts
deadband_raw = THROTTLE_RAW_MIN + int(THROTTLE_DEADBAND * (3240 - THROTTLE_RAW_MIN))

print()
print("=" * 72)
print("  THROTTLE NOISE MONITOR — hands off throttle!")
print("=" * 72)
print("  Deadband edge (raw): %d   Idle cal: %d" % (deadband_raw, THROTTLE_RAW_MIN))
print("  Any line starting with >>> means throttle broke deadband")
print("  Running for %d seconds..." % RUN_S)
print()
print("%-8s  %-6s  %-8s  %-6s  %-8s  %-8s  %-8s  %-8s" % (
    "t_ms", "raw", "frac", "mode", "assist", "regen", "mot_rpm", "iq"))
print("-" * 72)

start = ticks_ms()
last_telem = start
last_hb = start
adc_min = 9999
adc_max = 0
spike_count = 0
spike_max_raw = 0
samples = 0

try:
    while ticks_diff(ticks_ms(), start) < RUN_S * 1000:
        if wdt is not None:
            wdt.feed()

        now = ticks_ms()

        # Service VESC
        if ticks_diff(now, last_telem) >= TELEM_MS:
            vesc.request_telemetry()
            last_telem = now
        else:
            vesc.service_rx()

        # Sample throttle
        throttle.update()
        raw = throttle.raw
        frac = throttle.fraction
        samples += 1

        # Track stats
        if raw < adc_min:
            adc_min = raw
        if raw > adc_max:
            adc_max = raw

        elapsed = ticks_diff(now, start)

        # Print if throttle broke deadband or any command is non-zero
        if frac > 0.0 or state.assist_command_request > 0.0 or state.regen_command_request > 0.0:
            spike_count += 1
            if raw > spike_max_raw:
                spike_max_raw = raw
            print(">>> %5d  %5d  %7.4f  %-6s  %7.2f  %7.2f  %7.1f  %+6.2f" % (
                elapsed, raw, frac,
                str(state.requested_mode),
                state.assist_command_request,
                state.regen_command_request,
                state.vesc_mech_rpm,
                state.vesc_iq_current_a,
            ))

        # Heartbeat
        if ticks_diff(now, last_hb) >= HEARTBEAT_MS:
            last_hb = now
            print("    %5d  %5d  %7.4f  %-6s  %7.2f  %7.2f  %7.1f  %+6.2f  [adc %d-%d]" % (
                elapsed, raw, frac,
                str(state.requested_mode),
                state.assist_command_request,
                state.regen_command_request,
                state.vesc_mech_rpm,
                state.vesc_iq_current_a,
                adc_min, adc_max,
            ))

        sleep_ms(SAMPLE_MS)

except KeyboardInterrupt:
    print("\n  [Stopped by user]")

elapsed_s = ticks_diff(ticks_ms(), start) / 1000.0
print()
print("=" * 72)
print("  SUMMARY after %.1f seconds (%d samples)" % (elapsed_s, samples))
print("=" * 72)
print("  ADC min: %d   max: %d   range: %d counts" % (adc_min, adc_max, adc_max - adc_min))
print("  Deadband edge: %d" % deadband_raw)
print("  Margin (idle max to deadband): %d counts" % (deadband_raw - adc_max))
print("  Spikes above deadband: %d" % spike_count)
if spike_count > 0:
    print("  Worst spike raw: %d (%.1f counts over deadband)" % (
        spike_max_raw, spike_max_raw - deadband_raw))
print()
if adc_max >= deadband_raw:
    print("  *** ADC NOISE EXCEEDS DEADBAND — this is your spurious motor spin ***")
    print("  *** Recommended: increase THROTTLE_DEADBAND to at least %.3f ***" % (
        (adc_max - THROTTLE_RAW_MIN + 15) / (3240 - THROTTLE_RAW_MIN)))
else:
    print("  ADC noise stays within deadband (margin: %d counts)" % (deadband_raw - adc_max))
    print("  Spurious spin may be from VESC telemetry RPM noise or EMI on UART")

# Send zero current to be safe
for _ in range(5):
    vesc.send_current(0.0)
    sleep_ms(20)
