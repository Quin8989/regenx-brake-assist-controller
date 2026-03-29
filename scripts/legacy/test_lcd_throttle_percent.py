# scripts/legacy/test_lcd_throttle_percent.py
#
# Live throttle-to-percent display on LCD for bench testing.
# Uses the same mapping behavior as firmware:
#   1) map raw range to 0..1
#   2) apply dead-zone near zero
#   3) re-scale linearly above dead-zone
#
# Run:
#   mpremote run scripts/legacy/test_lcd_throttle_percent.py

from machine import ADC, Pin
from time import sleep_ms, ticks_ms, ticks_diff

from config.settings import (
    THROTTLE_ADC_PIN,
    THROTTLE_RAW_MIN,
    THROTTLE_RAW_MAX,
    THROTTLE_DEADBAND,
)
from drivers.lcd_driver import LCDDriver


def clamp(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


lcd = LCDDriver()
adc = ADC(Pin(THROTTLE_ADC_PIN))

DURATION_MS = 30000
PERIOD_MS = 100

print("\n=== LCD Throttle Percent Test ===")
print("Duration: %d ms" % DURATION_MS)
print("Move the throttle while watching LCD")

lcd.backlight_on()
lcd.write_line(0, "Throttle Test")
lcd.write_line(1, "Starting...")
sleep_ms(800)

start = ticks_ms()
while ticks_diff(ticks_ms(), start) < DURATION_MS:
    raw = adc.read_u16() >> 4  # 0..4095
    volts = (raw / 4095.0) * 3.3

    # Map to 0..1 using calibrated range
    frac = (raw - THROTTLE_RAW_MIN) / float(THROTTLE_RAW_MAX - THROTTLE_RAW_MIN)
    frac = clamp(frac, 0.0, 1.0)

    # Dead-zone + linear remap (matches drivers/throttle.py)
    if frac < THROTTLE_DEADBAND:
        frac = 0.0
    elif THROTTLE_DEADBAND < 1.0:
        frac = (frac - THROTTLE_DEADBAND) / (1.0 - THROTTLE_DEADBAND)
        frac = clamp(frac, 0.0, 1.0)

    pct = int(frac * 100 + 0.5)

    # 16x2 LCD formatting
    line0 = "THR:%3d%%" % pct
    line1 = "R%4d %1.2fV" % (raw, volts)

    lcd.write_line(0, line0)
    lcd.write_line(1, line1)

    print("raw=%4d  V=%1.3f  pct=%3d" % (raw, volts, pct))
    sleep_ms(PERIOD_MS)

lcd.write_line(0, "Throttle Test")
lcd.write_line(1, "Done")
print("Done")
