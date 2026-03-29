# scripts/bench/vesc_backup_save_to_pico.py
#
# Save full VESC MCCONF to Pico filesystem so it can be restored later.
# This is a full binary backup (not just selected fields).
#
# Run: mpremote run scripts/bench/vesc_backup_save_to_pico.py

import struct

from scripts.lib.vesc_uart_template import VescUartTemplate, crc16

BACKUP_PATH = "vesc_mcconf_backup.bin"
MAGIC = b"VMCF"  # VESC Motor ConFig
VERSION = 1

COMM_GET_MCCONF = 14

vesc = VescUartTemplate(rxbuf=1024)


def read_mcconf_payload():
    payload = vesc.request(COMM_GET_MCCONF, timeout_ms=5000)
    if payload and payload[0] == COMM_GET_MCCONF and len(payload) > 50:
        return payload
    return None


print()
print("=" * 50)
print("  VESC Backup Save -> Pico")
print("=" * 50)

payload = read_mcconf_payload()
if payload is None:
    print("FAILED: Could not read MCCONF from VESC")
    raise SystemExit

mcconf_data = payload[1:]  # drop command byte
mc_crc = crc16(mcconf_data)
header = MAGIC + bytes([VERSION]) + struct.pack(">H", len(mcconf_data)) + struct.pack(">H", mc_crc)
blob = header + mcconf_data

with open(BACKUP_PATH, "wb") as f:
    f.write(blob)

print("Saved backup to /%s" % BACKUP_PATH)
print("Config bytes: %d" % len(mcconf_data))
print("Data CRC16: %04X" % mc_crc)
print("Total file bytes: %d" % len(blob))
print("\nDone")
