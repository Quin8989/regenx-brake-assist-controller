"""Shared VESC UART read/write template for MicroPython scripts.

This module centralizes VESC packet framing, CRC, parsing, and command
request/reply helpers so all scripts use the same validated transport path.
"""

import struct
from time import sleep_ms, ticks_ms, ticks_diff
from machine import UART, Pin


def crc16(data):
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def wrap_frame(payload):
    length = len(payload)
    if length <= 255:
        frame = bytes([0x02, length]) + payload
    else:
        frame = bytes([0x03, length >> 8, length & 0xFF]) + payload
    frame += struct.pack(">H", crc16(payload))
    frame += bytes([0x03])
    return frame


def extract_frame(buf):
    if len(buf) < 6:
        return None, 0

    idx = 0
    while idx < len(buf):
        if buf[idx] == 0x02 and idx + 4 < len(buf):
            length = buf[idx + 1]
            frame_size = length + 5
            if idx + frame_size > len(buf):
                return None, idx

            payload = bytes(buf[idx + 2:idx + 2 + length])
            crc_recv = (buf[idx + 2 + length] << 8) | buf[idx + 3 + length]
            if crc16(payload) == crc_recv:
                return payload, idx + frame_size
            idx += 1
            continue

        if buf[idx] == 0x03 and idx + 5 < len(buf):
            length = (buf[idx + 1] << 8) | buf[idx + 2]
            if length <= 0 or length >= 10000:
                idx += 1
                continue

            frame_size = length + 6
            if idx + frame_size > len(buf):
                return None, idx

            payload = bytes(buf[idx + 3:idx + 3 + length])
            crc_recv = (buf[idx + 3 + length] << 8) | buf[idx + 4 + length]
            if crc16(payload) == crc_recv:
                return payload, idx + frame_size
            idx += 1
            continue

        idx += 1

    return None, len(buf)


class VescUartTemplate:
    """Reusable UART transport for VESC scripts.

    Default pins and baud match this repository wiring:
      GP4 TX -> VESC RX
      GP5 RX -> VESC TX
      115200 baud
    """

    def __init__(self, uart_id=1, baudrate=115200, tx_pin=4, rx_pin=5, rxbuf=1024):
        self.uart = UART(uart_id, baudrate=baudrate, tx=Pin(tx_pin), rx=Pin(rx_pin), rxbuf=rxbuf)

    def flush_rx(self, settle_ms=20):
        self.uart.read()
        if settle_ms > 0:
            sleep_ms(settle_ms)

    def write_payload(self, payload, flush_first=True):
        if flush_first:
            self.flush_rx()
        self.uart.write(wrap_frame(payload))

    def wait_for_command(self, expected_cmd, timeout_ms=1500):
        buf = bytearray()
        start = ticks_ms()

        while ticks_diff(ticks_ms(), start) < timeout_ms:
            data = self.uart.read()
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
                if payload and payload[0] == expected_cmd:
                    return payload

            sleep_ms(5)

        return None

    def send_command(self, payload, expected_cmd=None, timeout_ms=1500, rx_settle_ms=20):
        self.flush_rx(settle_ms=rx_settle_ms)
        self.uart.write(wrap_frame(payload))
        if expected_cmd is None:
            return None
        return self.wait_for_command(expected_cmd, timeout_ms=timeout_ms)

    def request(self, command_id, timeout_ms=1500):
        return self.send_command(bytes([command_id]), expected_cmd=command_id, timeout_ms=timeout_ms)

    def request_blob(self, command_id, min_len=30, timeout_ms=2500):
        payload = self.request(command_id, timeout_ms=timeout_ms)
        if payload and payload[0] == command_id and len(payload) >= min_len:
            return payload[1:]
        return None
