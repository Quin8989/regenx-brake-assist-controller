"""Target only motor brake current minimum via Lisp config API.

Run:
  mpremote mount . run scripts/vesc_target_motor_brake_min.py
"""

from time import sleep_ms

from scripts.lib.vesc_uart_template import VescUartTemplate

try:
    from config.vesc_config import get_flash_limits
except ImportError:
    print("FAILED: Could not import config.vesc_config")
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


vesc = VescUartTemplate(rxbuf=1024)

print("\n=== Target Motor Brake Min ===")
fw_major, fw_minor, hw_name = read_fw_info(vesc)
if fw_major is None:
    abort("Could not read firmware version")

print("FW: %d.%d" % (fw_major, fw_minor))
print("HW: %s" % hw_name)
if (fw_major, fw_minor) != EXPECTED_FW:
    abort("This writer is guarded for FW %d.%d only" % EXPECTED_FW)
if hw_name != EXPECTED_HW:
    abort("This writer is guarded for HW %s only" % EXPECTED_HW)

target_min = get_flash_limits()["motor_min_current_a"]

cmds = [
    "(conf-set 'l-current-min %.3f)" % target_min,
    "(conf-set 'l-current-min-scale 1.000)",
    "(conf-store)",
]

for cmd in cmds:
    print("SEND: %s" % cmd)
    send_lisp_expr(vesc, cmd)
    sleep_ms(700)

print("Done. Re-run audit script to verify.")
