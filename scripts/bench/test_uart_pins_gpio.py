# scripts/bench/test_uart_pins_gpio.py — GPIO sanity test for Pico UART header pins
#
# SAFETY:
#   Disconnect the VESC UART wires from GP4 and GP5 before running this test.
#   GP5 is normally connected to VESC TX. Driving it as a GPIO output while the
#   VESC is attached can electrically fight the VESC output driver.
#
# Purpose:
#   1. Verify GP4 (pin 6) and GP5 (pin 7) are physically reachable on the Pico.
#   2. Let the user probe each pin with a multimeter/LED.
#
# Behavior:
#   - Holds GP4 HIGH for 3 seconds while GP5 stays LOW.
#   - Holds GP5 HIGH for 3 seconds while GP4 stays LOW.
#   - Repeats 5 cycles.

from machine import Pin
from time import sleep_ms


def set_pins(gp4_value, gp5_value):
    gp4.value(gp4_value)
    gp5.value(gp5_value)
    print("GP4={} GP5={}".format(gp4_value, gp5_value))


gp4 = Pin(4, Pin.OUT, value=0)
gp5 = Pin(5, Pin.OUT, value=0)

print("UART GPIO sanity test starting")
print("Disconnect VESC wires from GP4/GP5 before using this test")

for cycle in range(5):
    print("Cycle {}: GP4 HIGH, GP5 LOW".format(cycle + 1))
    set_pins(1, 0)
    sleep_ms(3000)

    print("Cycle {}: GP4 LOW, GP5 HIGH".format(cycle + 1))
    set_pins(0, 1)
    sleep_ms(3000)

set_pins(0, 0)
print("Done")