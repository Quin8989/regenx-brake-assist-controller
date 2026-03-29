"""Apply preferred VESC limits using Lisp conf-set/conf-store.

This path asks firmware to update fields internally instead of patching raw
MCCONF bytes. It is safer on FW 6.6 / HW 410 where raw full MCCONF writes have
been unstable.

Run:
  mpremote mount . run scripts/vesc_apply_preferred_via_lisp.py
"""

from time import sleep_ms

from scripts.lib.vesc_uart_template import VescUartTemplate

try:
    from config.vesc_config import get_flash_limits
except ImportError:
    print("FAILED: Could not import config.vesc_config")
    print("Run with: mpremote mount . run scripts/vesc_apply_preferred_via_lisp.py")
    raise SystemExit(1)


COMM_FW_VERSION = 0
COMM_LISP_REPL_CMD = 138

EXPECTED_FW = (6, 6)
EXPECTED_HW = "410"


def abort(message):
    print("\nFAILED: %s" % message)
    raise SystemExit(1)


def read_fw_info(vesc):
    payload = vesc.request(COMM_FW_VERSION, timeout_ms=2000)
    if not payload or payload[0] != COMM_FW_VERSION or len(payload) < 3:
        return None, None, ""

    hw_name = ""
    if len(payload) > 3:
        hw_name = payload[3:].split(b"\x00")[0].decode("utf-8", "replace")
    return payload[1], payload[2], hw_name


def send_lisp_expr(vesc, expr):
    payload = bytes([COMM_LISP_REPL_CMD]) + expr.encode("utf-8") + b"\x00"
    vesc.send_command(payload, expected_cmd=None, timeout_ms=0)


vesc = VescUartTemplate(rxbuf=2048)

print()
print("=" * 54)
print("  VESC Preferred Limits via Lisp API")
print("=" * 54)

fw_major, fw_minor, hw_name = read_fw_info(vesc)
if fw_major is None:
    abort("Could not read firmware version")

print("FW: %d.%d" % (fw_major, fw_minor))
print("HW: %s" % hw_name)
if (fw_major, fw_minor) != EXPECTED_FW:
    abort("This writer is guarded for FW %d.%d only" % EXPECTED_FW)
if hw_name != EXPECTED_HW:
    abort("This writer is guarded for HW %s only" % EXPECTED_HW)

targets = get_flash_limits()

commands = [
    "(conf-set 'l-current-max %.3f)" % targets["motor_max_current_a"],
    "(conf-set 'l-current-min %.3f)" % targets["motor_min_current_a"],
    "(conf-set 'l-in-current-max %.3f)" % targets["battery_max_current_a"],
    "(conf-set 'l-in-current-min %.3f)" % targets["battery_min_current_a"],
    "(conf-set 'l-min-vin %.3f)" % targets["min_input_voltage_v"],
    "(conf-set 'l-max-vin %.3f)" % targets["max_input_voltage_v"],
    "(conf-set 'l-battery-cut-start %.3f)" % targets["battery_cut_start_v"],
    "(conf-set 'l-battery-cut-end %.3f)" % targets["battery_cut_end_v"],
    "(conf-set 'l-watt-max %.3f)" % targets["watt_max"],
    "(conf-set 'l-watt-min %.3f)" % targets["watt_min"],
    "(conf-store)",
]

print("Applying commands:")
for cmd in commands:
    print("  " + cmd)
    send_lisp_expr(vesc, cmd)
    # REPL command handling has a 0.5 s guard in firmware.
    sleep_ms(650)

print("\nDone sending Lisp config/store commands.")
print("Now run config audit to confirm persisted values:")
print("  mpremote mount . run scripts/bench/test_vesc_read_config.py")
