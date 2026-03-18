# drivers/uart_port.py — Low-level UART access for VESC communication
#
# Provides raw serial read/write. Does NOT parse VESC payloads,
# decide motor commands, or hold application state.

from machine import UART, Pin
from config.pins import VESC_UART_ID, VESC_UART_TX, VESC_UART_RX
from config.vesc_config import VESC_BAUD_RATE


class UARTPort:
    def __init__(self):
        self._uart = UART(
            VESC_UART_ID,
            baudrate=VESC_BAUD_RATE,
            tx=Pin(VESC_UART_TX),
            rx=Pin(VESC_UART_RX),
        )

    def write(self, data):
        """Write bytes to the UART. Returns number of bytes written."""
        return self._uart.write(data)

    def read(self, nbytes=None):
        """Non-blocking read. Returns bytes or None."""
        if nbytes is None:
            return self._uart.read()
        return self._uart.read(nbytes)

    def any(self):
        """Return number of bytes waiting in the receive buffer."""
        return self._uart.any()

    # TODO: Decide buffer size
    # TODO: Decide whether to implement a ring buffer or simple polling reads
    # TODO: Decide how to recover from UART framing errors or garbage bytes
