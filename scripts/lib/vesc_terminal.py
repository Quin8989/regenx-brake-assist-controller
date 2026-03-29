from time import sleep_ms, ticks_ms, ticks_diff

from scripts.lib.vesc_uart_template import extract_frame, wrap_frame

COMM_TERMINAL_CMD = 20
COMM_PRINT = 21


def read_print_lines(uart, timeout_ms=1500):
    lines = []
    buf = bytearray()
    start = ticks_ms()

    while ticks_diff(ticks_ms(), start) < timeout_ms:
        data = uart.read()
        if data:
            buf.extend(data)
            start = ticks_ms()

        while True:
            payload, consumed = extract_frame(buf)
            if payload is None:
                if consumed > 0:
                    buf = buf[consumed:]
                break

            buf = buf[consumed:]
            if payload and payload[0] == COMM_PRINT:
                try:
                    lines.append(payload[1:].decode("utf-8", "replace"))
                except Exception:
                    lines.append("")

        sleep_ms(10)

    return lines


def run_terminal_cmd(vesc, cmd_text, timeout_ms=1500, settle_ms=20):
    payload = bytes([COMM_TERMINAL_CMD]) + cmd_text.encode("utf-8")
    vesc.flush_rx(settle_ms=settle_ms)
    vesc.uart.write(wrap_frame(payload))
    sleep_ms(80)
    return read_print_lines(vesc.uart, timeout_ms=timeout_ms)


def help_has_command(vesc, command_name, timeout_ms=3500):
    lines = run_terminal_cmd(vesc, "help", timeout_ms=timeout_ms)
    needle = command_name.strip().lower()
    for line in lines:
        text = line.strip().lower()
        if not text:
            continue
        head = text.split()[0]
        if head == needle:
            return True
    return False