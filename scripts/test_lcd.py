# scripts/test_lcd.py — LCD hardware test
#
# Run on the Pico via mpremote:
#   mpremote connect /dev/ttyACM0 run scripts/test_lcd.py
#
# Expected: backlight turns on, line 1 shows "Hello ReGenX!",
#           line 2 shows "LCD test OK".

from drivers.lcd_driver import LCDDriver

lcd = LCDDriver()
lcd.write_line(0, "Hello ReGenX!")
lcd.write_line(1, "LCD test OK")
print("LCD test complete")
