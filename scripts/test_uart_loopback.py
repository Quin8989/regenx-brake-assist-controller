# scripts/test_uart_loopback.py — UART loopback test (no VESC needed)
#
# Wiring: connect GP0 (TX, pin 1) directly to GP1 (RX, pin 2) with a jumper.
#
# Run on the Pico via mpremote:
#   mpremote connect /dev/ttyACM0 run scripts/test_uart_loopback.py
#
# Expected output: "PASS: received b'hello'" (Pico echoes back to itself).

from machine import UART, Pin
import time

uart = UART(0, baudrate=115200, tx=Pin(0), rx=Pin(1))
uart.read()  # flush

uart.write(b"hello")
time.sleep_ms(50)
data = uart.read()

if data == b"hello":
    print("PASS: received", data)
else:
    print("FAIL: expected b'hello', got", data)
