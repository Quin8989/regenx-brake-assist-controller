# scripts/test_throttle_characterize.py
#
# Characterize hall throttle on Pico ADC0 (GP26):
# - No-throttle and full-throttle raw/voltage range
# - Approximate curve via coarse bins
#
# Run:
#   mpremote run scripts/test_throttle_characterize.py

from machine import ADC, Pin
from time import ticks_ms, ticks_diff, sleep_ms

ADC_PIN = 26
RUN_MS = 12000          # total capture time
SAMPLE_MS = 20          # sample period
PRINT_MS = 250          # status print period
BINS = 11               # 0%,10%,...,100%

adc = ADC(Pin(ADC_PIN))

raw_min = 4095
raw_max = 0
hist = [0] * BINS

start = ticks_ms()
last_print = start

print("\n=== Throttle Characterization ===")
print("Pin: GP26 / ADC0")
print("Duration: %d ms" % RUN_MS)
print("Instruction: Sweep throttle slowly from 0%% to 100%% and back a few times.")
print("\nLive samples:")

while ticks_diff(ticks_ms(), start) < RUN_MS:
    raw12 = adc.read_u16() >> 4   # 0..4095
    volts = (raw12 / 4095.0) * 3.3

    if raw12 < raw_min:
        raw_min = raw12
    if raw12 > raw_max:
        raw_max = raw12

    # Put sample in coarse bins based on normalized reading between min/max-agnostic 0..4095.
    idx = int((raw12 * (BINS - 1)) / 4095)
    if idx < 0:
        idx = 0
    if idx >= BINS:
        idx = BINS - 1
    hist[idx] += 1

    now = ticks_ms()
    if ticks_diff(now, last_print) >= PRINT_MS:
        last_print = now
        print("raw=%4d  V=%.3f" % (raw12, volts))

    sleep_ms(SAMPLE_MS)

v_min = (raw_min / 4095.0) * 3.3
v_max = (raw_max / 4095.0) * 3.3
span = raw_max - raw_min

print("\n=== Summary ===")
print("raw_min: %d (%.3f V)" % (raw_min, v_min))
print("raw_max: %d (%.3f V)" % (raw_max, v_max))
print("span:    %d counts" % span)

if span < 200:
    print("WARNING: Very small span. Throttle may not be swept or wiring may be wrong.")

print("\nApprox curve coverage (coarse bins):")
for i in range(BINS):
    pct = int((i * 100) / (BINS - 1))
    print("%3d%%: %d" % (pct, hist[i]))

print("\nDone")
