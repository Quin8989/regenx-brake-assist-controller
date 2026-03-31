"""Set motor pole count on VESC via Lisp conf-set/conf-store.

Writes si-motor-poles (motor poles = pole pairs × 2) and persists with
conf-store so the value survives power cycles.

Run:
  mpremote mount . run scripts/vesc_set_pole_pairs.py
"""

from time import sleep_ms

from scripts.lib.vesc_uart_template import VescUartTemplate

try:
    from config.settings import VESC_MOTOR_POLE_PAIRS
except ImportError:
    print("FAILED: Could not import config.settings")
    print("Run with: mpremote mount . run scripts/vesc_set_pole_pairs.py")
    raise SystemExit(1)


COMM_FW_VERSION = 0
COMM_LISP_REPL_CMD = 138

EXPECTED_FW = (6, 6)
EXPECTED_HW = "410"

MOTOR_POLES = VESC_MOTOR_POLE_PAIRS * 2  # VESC stores motor poles, not pairs


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

print("\n=== Set VESC Motor Pole Count ===")
fw_major, fw_minor, hw_name = read_fw_info(vesc)
if fw_major is None:
    abort("Could not read firmware version")

print("FW: %d.%d" % (fw_major, fw_minor))
print("HW: %s" % hw_name)
if (fw_major, fw_minor) != EXPECTED_FW:
    abort("This writer is guarded for FW %d.%d only" % EXPECTED_FW)
if hw_name != EXPECTED_HW:
    abort("This writer is guarded for HW %s only" % EXPECTED_HW)

print("Pole pairs (settings.py): %d" % VESC_MOTOR_POLE_PAIRS)
print("Motor poles (VESC):       %d" % MOTOR_POLES)

cmds = [
    "(conf-set 'si-motor-poles %d)" % MOTOR_POLES,
    "(conf-store)",
]

for cmd in cmds:
    print("SEND: %s" % cmd)
    send_lisp_expr(vesc, cmd)
    sleep_ms(700)

print("\nDone. Motor poles set to %d (%d pole pairs) and stored." % (MOTOR_POLES, VESC_MOTOR_POLE_PAIRS))
print("Power-cycle the VESC and re-run the audit script to verify:")
print("  mpremote mount . run scripts/bench/test_vesc_read_config.py")
