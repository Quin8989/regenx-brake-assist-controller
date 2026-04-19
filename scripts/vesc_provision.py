"""One-shot VESC provisioning: apply all repo limits, verify, and save snapshot.

Steps performed:
  1. Read and verify firmware version + hardware name
    2. Apply all limits via Lisp conf-set (from firmware/config/vesc_config.py)
  3. Persist to flash via conf-store
  4. Read back MCCONF blob and verify key fields match
  5. Save MCCONF/APPCONF snapshots + metadata to Pico filesystem
  6. Print mpremote commands to copy snapshots into the repo

Run:
  mpremote mount . run scripts/vesc_provision.py
"""

from time import sleep_ms
import struct

from machine import WDT

from scripts.lib.vesc_uart_template import VescUartTemplate, crc16
from scripts.lib.vesc_terminal import run_terminal_cmd
from scripts.lib.path_setup import ensure_firmware_path

ensure_firmware_path()

try:
    from config.vesc_config import get_flash_limits
except ImportError:
    print("FAILED: Could not import firmware/config/vesc_config.py")
    print("Run with: mpremote mount . run scripts/vesc_provision.py")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMM_FW_VERSION = 0
COMM_GET_MCCONF = 14
COMM_LISP_READ_CODE = 130
COMM_LISP_WRITE_CODE = 131
COMM_LISP_ERASE_CODE = 132
COMM_LISP_SET_RUNNING = 133
COMM_LISP_REPL_CMD = 138
APPCONF_CANDIDATES = [17, 16, 18]

EXPECTED_FW = (6, 6)
EXPECTED_HW = "410"

MCCONF_FILE = "vesc_snapshot_mcconf.bin"
APPCONF_FILE = "vesc_snapshot_appconf.bin"
META_FILE = "vesc_snapshot_meta.txt"
LISP_PUSH_FILE = "scripts/vesc_lisp_push_iq.lisp"
LISP_WRITE_CHUNK = 384


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def abort(message):
    print("\nFAILED: %s" % message)
    raise SystemExit(1)


def read_fw_info(vesc):
    payload = vesc.request(COMM_FW_VERSION, timeout_ms=2000)
    if not payload or payload[0] != COMM_FW_VERSION or len(payload) < 3:
        return None, None, ""
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
    return payload[1], payload[2], hw_name


def send_lisp_expr(vesc, expr):
    payload = bytes([COMM_LISP_REPL_CMD]) + expr.encode("utf-8") + b"\x00"
    vesc.send_command(payload, expected_cmd=None, timeout_ms=0)


def _load_lisp_source(path):
    """Load LispBM source and strip comments/blank lines for robust upload."""
    try:
        with open(path, "r") as f:
            lines = f.read().splitlines()
    except OSError:
        return None

    code_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(";"):
            continue
        code_lines.append(line)

    if not code_lines:
        return None

    return ("\n".join(code_lines) + "\n").encode("utf-8")


def _build_lisp_flash_blob(source_bytes):
    """Pack source using VESC Tool-compatible Lisp flash format."""
    vb = struct.pack(">H", 0) + source_bytes
    if not vb.endswith(b"\x00"):
        vb += b"\x00"
    vb += struct.pack(">H", 0)  # num_imports = 0

    size_field = len(vb) - 2
    return struct.pack(">I", size_field) + struct.pack(">H", crc16(vb)) + vb


def _lisp_erase(vesc, blob_size):
    payload = bytes([COMM_LISP_ERASE_CODE]) + struct.pack(">i", blob_size)
    reply = vesc.send_command(payload, expected_cmd=COMM_LISP_ERASE_CODE, timeout_ms=3000)
    return bool(reply and len(reply) >= 2 and reply[1] == 1)


def _lisp_write_blob(vesc, blob):
    offset = 0
    while offset < len(blob):
        chunk = blob[offset:offset + LISP_WRITE_CHUNK]
        payload = bytes([COMM_LISP_WRITE_CODE]) + struct.pack(">I", offset) + chunk
        reply = vesc.send_command(payload, expected_cmd=COMM_LISP_WRITE_CODE, timeout_ms=2500)
        if not reply or len(reply) < 6:
            return False
        if reply[1] != 1:
            return False
        echoed_offset = struct.unpack_from(">I", reply, 2)[0]
        if echoed_offset != offset:
            return False
        offset += len(chunk)
        sleep_ms(10)
    return True


def _lisp_set_running(vesc, running=True):
    payload = bytes([COMM_LISP_SET_RUNNING, 1 if running else 0])
    reply = vesc.send_command(payload, expected_cmd=COMM_LISP_SET_RUNNING, timeout_ms=2000)
    return bool(reply and len(reply) >= 2 and reply[1] == 1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

vesc = VescUartTemplate(rxbuf=2048)
wdt = WDT(timeout=8000)

print()
print("=" * 58)
print("  VESC Provision — Apply Limits + Install Lisp Push + Save Snapshot")
print("=" * 58)

# ------------------------------------------------------------------
# Step 1: Firmware / hardware gate
# ------------------------------------------------------------------
print("\n[1/6] Reading firmware version...")
fw_major, fw_minor, hw_name = read_fw_info(vesc)
if fw_major is None:
    abort("Could not read firmware version — check wiring and VESC power")

print("  FW: %d.%d   HW: %s" % (fw_major, fw_minor, hw_name))
if (fw_major, fw_minor) != EXPECTED_FW:
    abort("Script is guarded for FW %d.%d only (got %d.%d)" % (
        EXPECTED_FW[0], EXPECTED_FW[1], fw_major, fw_minor))
if hw_name != EXPECTED_HW:
    abort("Script is guarded for HW '%s' only (got '%s')" % (EXPECTED_HW, hw_name))

# ------------------------------------------------------------------
# Step 2: Apply all limits via Lisp conf-set
# ------------------------------------------------------------------
print("\n[2/6] Applying limits via Lisp conf-set...")
targets = get_flash_limits()

lisp_commands = [
    ("l-current-max", targets["motor_max_current_a"]),
    ("l-current-min", targets["motor_min_current_a"]),
    ("l-abs-current-max", targets["abs_current_max_a"]),
    ("l-in-current-max", targets["battery_max_current_a"]),
    ("l-in-current-min", targets["battery_min_current_a"]),
    ("l-min-vin", targets["min_input_voltage_v"]),
    ("l-max-vin", targets["max_input_voltage_v"]),
    ("l-battery-cut-start", targets["battery_cut_start_v"]),
    ("l-battery-cut-end", targets["battery_cut_end_v"]),
    ("l-watt-max", targets["watt_max"]),
    ("l-watt-min", targets["watt_min"]),
]

for name, value in lisp_commands:
    wdt.feed()
    expr = "(conf-set '%s %.3f)" % (name, value)
    print("  %s" % expr)
    send_lisp_expr(vesc, expr)
    sleep_ms(650)

# ------------------------------------------------------------------
# Step 3: Persist to flash
# ------------------------------------------------------------------
print("\n[3/6] Storing to flash (conf-store)...")
wdt.feed()
send_lisp_expr(vesc, "(conf-store)")
sleep_ms(1500)
print("  conf-store sent")

# ------------------------------------------------------------------
# Step 4: Install + start LispBM push script
# ------------------------------------------------------------------
print("\n[4/6] Installing LispBM push script...")
wdt.feed()

lisp_source = _load_lisp_source(LISP_PUSH_FILE)
if lisp_source is None:
    abort("Could not load %s" % LISP_PUSH_FILE)

lisp_blob = _build_lisp_flash_blob(lisp_source)
print("  Source bytes: %d" % len(lisp_source))
print("  Flash blob:   %d" % len(lisp_blob))

if not _lisp_erase(vesc, len(lisp_blob)):
    abort("Lisp erase failed")

if not _lisp_write_blob(vesc, lisp_blob):
    abort("Lisp write failed")

if not _lisp_set_running(vesc, running=True):
    abort("Lisp start failed")

print("  LispBM push script installed and running")

# ------------------------------------------------------------------
# Step 5: Read back and verify
# ------------------------------------------------------------------
print("\n[5/6] Verifying persisted config...")
wdt.feed()

# Re-read MCCONF to confirm values took hold
mcconf = vesc.request_blob(COMM_GET_MCCONF, min_len=30, timeout_ms=4000)
if mcconf is None:
    abort("Could not read MCCONF after store")

# Verify key fields at known MCCONF offsets (ieee float32 big-endian)

VERIFY_FIELDS = [
    (8, "motor_max_current_a", targets["motor_max_current_a"]),
    (12, "motor_min_current_a", targets["motor_min_current_a"]),
    (16, "battery_max_current_a", targets["battery_max_current_a"]),
    (20, "battery_min_current_a", targets["battery_min_current_a"]),
    (48, "min_input_voltage_v", targets["min_input_voltage_v"]),
    (52, "max_input_voltage_v", targets["max_input_voltage_v"]),
    (56, "battery_cut_start_v", targets["battery_cut_start_v"]),
    (60, "battery_cut_end_v", targets["battery_cut_end_v"]),
    (93, "watt_max", targets["watt_max"]),
    (97, "watt_min", targets["watt_min"]),
]

all_ok = True
for offset, name, expected in VERIFY_FIELDS:
    if offset + 4 > len(mcconf):
        print("  %-28s SKIP (blob too short)" % name)
        continue
    actual = struct.unpack_from(">f", mcconf, offset)[0]
    ok = abs(actual - expected) < 0.5
    status = "OK" if ok else "MISMATCH (got %.2f, expected %.2f)" % (actual, expected)
    print("  %-28s %s" % (name, status))
    if not ok:
        all_ok = False

if not all_ok:
    abort("Config verification failed — some fields did not match")

# ------------------------------------------------------------------
# Step 6: Save snapshot files
# ------------------------------------------------------------------
print("\n[6/6] Saving snapshot...")
wdt.feed()

# Fault and offset info
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

wdt.feed()

if fault_text:
    print("  Active fault: %s" % fault_text)
if offset_line:
    print("  %s" % offset_line)

# MCCONF (already have the blob from step 4, but re-read to include opcode prefix)
# We already have mcconf blob without the opcode byte — use it directly.
with open(MCCONF_FILE, "wb") as f:
    f.write(mcconf)
mc_crc = crc16(mcconf)
print("  Saved %s (%d bytes, crc=%04X)" % (MCCONF_FILE, len(mcconf), mc_crc))

# APPCONF
wdt.feed()
appconf = None
app_cmd_used = None
for cmd in APPCONF_CANDIDATES:
    blob = vesc.request_blob(cmd, min_len=30, timeout_ms=2500)
    if blob is not None:
        appconf = blob
        app_cmd_used = cmd
        break

app_len = 0
app_crc = 0
if appconf is not None:
    with open(APPCONF_FILE, "wb") as f:
        f.write(appconf)
    app_len = len(appconf)
    app_crc = crc16(appconf)
    print("  Saved %s (%d bytes, crc=%04X)" % (APPCONF_FILE, app_len, app_crc))
else:
    print("  WARNING: APPCONF read not supported")

# META
with open(META_FILE, "w") as f:
    f.write("vesc_snapshot_version=2\n")
    f.write("fw_major=%d\n" % fw_major)
    f.write("fw_minor=%d\n" % fw_minor)
    f.write("hw_name=%s\n" % hw_name)
    f.write("active_fault=%s\n" % fault_text)
    f.write("foc_current_offsets=%s\n" % offset_line)
    f.write("mcconf_file=%s\n" % MCCONF_FILE)
    f.write("mcconf_len=%d\n" % len(mcconf))
    f.write("mcconf_crc16=%04X\n" % mc_crc)
    if appconf is not None:
        f.write("appconf_file=%s\n" % APPCONF_FILE)
        f.write("appconf_cmd_id=%d\n" % app_cmd_used)
        f.write("appconf_len=%d\n" % app_len)
        f.write("appconf_crc16=%04X\n" % app_crc)
    else:
        f.write("appconf_file=\n")
        f.write("appconf_cmd_id=\n")
        f.write("appconf_len=0\n")
        f.write("appconf_crc16=\n")
print("  Saved %s" % META_FILE)

# ------------------------------------------------------------------
# Done
# ------------------------------------------------------------------
print()
print("=" * 58)
print("  ALL DONE — Config applied, verified, and snapshot saved")
print("=" * 58)
print()
print("Copy snapshot files into the repo:")
print("  mpremote fs cp :%s config/%s" % (MCCONF_FILE, MCCONF_FILE))
print("  mpremote fs cp :%s config/%s" % (META_FILE, META_FILE))
if appconf is not None:
    print("  mpremote fs cp :%s config/%s" % (APPCONF_FILE, APPCONF_FILE))
print()
