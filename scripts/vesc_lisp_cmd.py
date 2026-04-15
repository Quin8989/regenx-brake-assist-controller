"""Send one Lisp REPL command to VESC over UART.

Usage:
  mpremote mount . run scripts/vesc_lisp_cmd.py "(conf-store)"
"""

import sys

from scripts.lib.vesc_uart_template import VescUartTemplate


COMM_LISP_REPL_CMD = 138


def main():
  expr = "(conf-store)"
  if len(sys.argv) >= 2:
    expr = sys.argv[1]

  vesc = VescUartTemplate(rxbuf=1024)
  payload = bytes([COMM_LISP_REPL_CMD]) + expr.encode("utf-8") + b"\x00"
  vesc.send_command(payload, expected_cmd=None, timeout_ms=0)
  print("Sent Lisp command: %s" % expr)


main()
