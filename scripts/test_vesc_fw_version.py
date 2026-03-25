# scripts/test_vesc_fw_version.py — Read VESC firmware version over UART
#
# Wiring: Pico GP4 (TX) -> VESC RX, Pico GP5 (RX) -> VESC TX, GND -> GND.
# Motor may be disconnected.  VESC must be powered.
#
# Run on the Pico via mpremote:
#   mpremote connect /dev/ttyACM0 run scripts/test_vesc_fw_version.py
#
# Expected output: firmware version and hardware name, e.g.
#   FW Version: 5.2
#   Hardware: 410

from machine import UART, Pin
import struct
import time

COMM_FW_VERSION = 0x00

uart = UART(1, baudrate=115200, tx=Pin(4), rx=Pin(5))
uart.read()  # flush


def crc16(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


payload = bytes([COMM_FW_VERSION])
frame = bytes([0x02, len(payload)]) + payload + struct.pack(">H", crc16(payload)) + bytes([0x03])

uart.write(frame)
time.sleep_ms(200)

resp = uart.read()
if not resp:
    print("FAIL: no response — check wiring and VESC power")
elif resp[0] == 0x02 and len(resp) > 4:
    plen = resp[1]
    pdata = resp[2 : 2 + plen]
    if len(pdata) >= 3 and pdata[0] == COMM_FW_VERSION:
        print("FW Version: {}.{}".format(pdata[1], pdata[2]))
        if len(pdata) > 3:
            hw = pdata[3:].split(b"\x00")[0]
            if hw:
                print("Hardware:", hw.decode())
        print("PASS")
    else:
        print("FAIL: unexpected payload:", resp.hex())
else:
    print("FAIL: malformed response:", resp.hex())
