# scripts/vesc_save_snapshot.py - Save the final live VESC config snapshot
#
# This script captures the live VESC state after preparation and flashing:
# - Firmware version / hardware string
# - MCCONF raw blob
# - APPCONF raw blob (if supported)
# - Metadata report with payload sizes and CRC16 checksums
#
# It saves those files on the Pico and prints the exact mpremote commands
# needed to copy them into the repo.
#
# Run: mpremote run scripts/vesc_save_snapshot.py

import struct

from scripts.lib.vesc_terminal import run_terminal_cmd
from scripts.lib.vesc_uart_template import VescUartTemplate, crc16

COMM_FW_VERSION = 0
COMM_GET_MCCONF = 14
APPCONF_CANDIDATES = [17, 16, 18]
vesc = VescUartTemplate(rxbuf=1024)

MCCONF_FILE = "vesc_snapshot_mcconf.bin"
APPCONF_FILE = "vesc_snapshot_appconf.bin"
META_FILE = "vesc_snapshot_meta.txt"


def get_fw_info():
    payload = vesc.request(COMM_FW_VERSION, timeout_ms=1000)
    if not payload or payload[0] != COMM_FW_VERSION or len(payload) < 3:
        return None, None, ""

    major = payload[1]
    minor = payload[2]
    hw_name = ""
    if len(payload) > 3:
        rest = payload[3:]
        nul = rest.find(b"\x00")
        if nul >= 0:
            rest = rest[:nul]
        try:
            hw_name = rest.decode("utf-8", "replace")
        except Exception:
            hw_name = ""
    return major, minor, hw_name


def get_conf_blob(cmd_id):
    return vesc.request_blob(cmd_id, min_len=30, timeout_ms=2500)


print()
print("=" * 50)
print("  VESC Save Snapshot")
print("  ReGenX Brake-Assist Controller")
print("=" * 50)

fw_major, fw_minor, hw_name = get_fw_info()
if fw_major is None:
    print("FAILED: Could not read firmware version")
    raise SystemExit(1)

print("Firmware: %d.%d" % (fw_major, fw_minor))
print("Hardware: %s" % hw_name)

fault_text = ""
for line in run_terminal_cmd(vesc, "fault", timeout_ms=1200):
    stripped = line.strip()
    if stripped and not stripped.startswith("->"):
        fault_text = stripped
        break

offset_line = ""
for line in run_terminal_cmd(vesc, "hw_status", timeout_ms=2200):
    if "FOC Current Offsets:" in line:
        offset_line = line.strip()
        break

if fault_text:
    print("Active fault: %s" % fault_text)
if offset_line:
    print(offset_line)

mcconf = get_conf_blob(COMM_GET_MCCONF)
if mcconf is None:
    print("FAILED: Could not read MCCONF")
    raise SystemExit(1)

with open(MCCONF_FILE, "wb") as handle:
    handle.write(mcconf)

mc_crc = crc16(mcconf)
print("Saved %s (%d bytes, crc=%04X)" % (MCCONF_FILE, len(mcconf), mc_crc))

appconf = None
app_cmd_used = None
for cmd in APPCONF_CANDIDATES:
    blob = get_conf_blob(cmd)
    if blob is not None:
        appconf = blob
        app_cmd_used = cmd
        break

app_len = 0
app_crc = 0
if appconf is not None:
    with open(APPCONF_FILE, "wb") as handle:
        handle.write(appconf)
    app_len = len(appconf)
    app_crc = crc16(appconf)
    print("Saved %s (%d bytes, crc=%04X, cmd=%d)" % (APPCONF_FILE, app_len, app_crc, app_cmd_used))
else:
    print("WARNING: APPCONF read not supported by tested command IDs")

with open(META_FILE, "w") as handle:
    handle.write("vesc_snapshot_version=1\n")
    handle.write("fw_major=%d\n" % fw_major)
    handle.write("fw_minor=%d\n" % fw_minor)
    handle.write("hw_name=%s\n" % hw_name)
    handle.write("active_fault=%s\n" % fault_text)
    handle.write("foc_current_offsets=%s\n" % offset_line)
    handle.write("mcconf_file=%s\n" % MCCONF_FILE)
    handle.write("mcconf_len=%d\n" % len(mcconf))
    handle.write("mcconf_crc16=%04X\n" % mc_crc)
    if appconf is not None:
        handle.write("appconf_file=%s\n" % APPCONF_FILE)
        handle.write("appconf_cmd_id=%d\n" % app_cmd_used)
        handle.write("appconf_len=%d\n" % app_len)
        handle.write("appconf_crc16=%04X\n" % app_crc)
    else:
        handle.write("appconf_file=\n")
        handle.write("appconf_cmd_id=\n")
        handle.write("appconf_len=0\n")
        handle.write("appconf_crc16=\n")

print("Saved %s" % META_FILE)
print("\nCopy into the repo with:")
print("  mpremote fs cp :%s config/%s" % (MCCONF_FILE, MCCONF_FILE))
print("  mpremote fs cp :%s config/%s" % (META_FILE, META_FILE))
if appconf is not None:
    print("  mpremote fs cp :%s config/%s" % (APPCONF_FILE, APPCONF_FILE))

print("\nDone")