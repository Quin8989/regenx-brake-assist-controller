# scripts/test_vesc_telemetry.py — Read VESC telemetry (COMM_GET_VALUES)
#
# Wiring: Pico GP0 (TX) -> VESC RX, Pico GP1 (RX) -> VESC TX, GND -> GND.
# Motor may be disconnected.  VESC must be powered.
#
# Run on the Pico via mpremote:
#   mpremote connect /dev/ttyACM0 run scripts/test_vesc_telemetry.py
#
# Expected output: parsed telemetry values including bus voltage, temps,
# and fault code.  Motor current may show small offset noise with no
# motor connected.  Motor temp will read ~-72 C if no sensor is wired.

from machine import UART, Pin
import struct
import time

COMM_GET_VALUES = 0x04
TELEMETRY_FMT = ">hhiiiihihiiiiiiB"
TELEMETRY_SIZE = 53  # bytes after opcode

uart = UART(0, baudrate=115200, tx=Pin(0), rx=Pin(1))
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


payload = bytes([COMM_GET_VALUES])
frame = bytes([0x02, len(payload)]) + payload + struct.pack(">H", crc16(payload)) + bytes([0x03])

uart.write(frame)
time.sleep_ms(200)

resp = uart.read()
if not resp:
    print("FAIL: no response — check wiring and VESC power")
elif resp[0] == 0x02 and len(resp) > 4:
    plen = resp[1]
    p = resp[2 : 2 + plen]
    if p[0] == COMM_GET_VALUES and len(p) >= 1 + TELEMETRY_SIZE:
        vals = struct.unpack_from(TELEMETRY_FMT, p, 1)
        print("--- VESC Telemetry ---")
        print("FET temp:       {:.1f} C".format(vals[0] / 10.0))
        print("Motor temp:     {:.1f} C".format(vals[1] / 10.0))
        print("Motor current:  {:.2f} A".format(vals[2] / 100.0))
        print("Input current:  {:.2f} A".format(vals[3] / 100.0))
        print("Duty cycle:     {:.1f} %".format(vals[6] / 10.0))
        print("ERPM:           {}".format(vals[7]))
        print("Bus voltage:    {:.1f} V".format(vals[8] / 10.0))
        print("Ah drawn:       {:.4f}".format(vals[9] / 10000.0))
        print("Ah charged:     {:.4f}".format(vals[10] / 10000.0))
        print("Wh drawn:       {:.4f}".format(vals[11] / 10000.0))
        print("Wh charged:     {:.4f}".format(vals[12] / 10000.0))
        print("Tachometer:     {}".format(vals[13]))
        print("Tach (abs):     {}".format(vals[14]))
        print("Fault code:     {}".format(vals[15]))
        print("PASS")
    else:
        print("FAIL: unexpected payload (cmd={}, len={})".format(p[0], len(p)))
else:
    print("FAIL: malformed response:", resp.hex())
