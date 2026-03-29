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

import time

from scripts.lib.vesc_uart_template import VescUartTemplate

COMM_FW_VERSION = 0x00

vesc = VescUartTemplate(rxbuf=1024)

payload = vesc.request(COMM_FW_VERSION, timeout_ms=1500)
if not payload:
    print("FAIL: no response — check wiring and VESC power")
else:
    if len(payload) >= 3 and payload[0] == COMM_FW_VERSION:
        print("FW Version: {}.{}".format(payload[1], payload[2]))
        if len(payload) > 3:
            hw = payload[3:].split(b"\x00")[0]
            if hw:
                print("Hardware:", hw.decode())
        print("PASS")
    else:
        print("FAIL: unexpected payload:", payload.hex())
