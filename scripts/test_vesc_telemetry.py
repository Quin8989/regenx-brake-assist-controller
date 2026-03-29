# scripts/test_vesc_telemetry.py — Read VESC telemetry (COMM_GET_VALUES)
#
# Wiring: Pico GP4 (TX) -> VESC RX, Pico GP5 (RX) -> VESC TX, GND -> GND.
# Motor may be disconnected.  VESC must be powered.
#
# Run on the Pico via mpremote:
#   mpremote connect /dev/ttyACM0 run scripts/test_vesc_telemetry.py
#
# Expected output: parsed telemetry values including bus voltage, temps,
# and fault code.  Motor current may show small offset noise with no
# motor connected.  Motor temp will read ~-72 C if no sensor is wired.

import struct

from scripts.lib.vesc_uart_template import VescUartTemplate

COMM_GET_VALUES = 0x04
TELEMETRY_FMT = ">hhiiiihihiiiiiiB"
TELEMETRY_SIZE = 53  # bytes after opcode

vesc = VescUartTemplate(rxbuf=1024)

payload = vesc.request(COMM_GET_VALUES, timeout_ms=1500)
if not payload:
    print("FAIL: no response — check wiring and VESC power")
else:
    if payload[0] == COMM_GET_VALUES and len(payload) >= 1 + TELEMETRY_SIZE:
        vals = struct.unpack_from(TELEMETRY_FMT, payload, 1)
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
        print("FAIL: unexpected payload (cmd={}, len={})".format(payload[0], len(payload)))
