# main.py — boot, wiring, core 1 launch, the 100 Hz loop. Target-only.
#
# Boot philosophy (RGX-2-003 §3): if anything here fails, the safe state is
# "no commands" — A1's 200 ms app timeout releases the motor without our
# help. The top-level guard therefore never tries to be clever.

import gc
import time
import _thread

import machine
from machine import UART, Pin, WDT

import config
import control
import sensors
import strategy
import ui
import vesc


def build():
    uart = UART(config.UART_ID, baudrate=config.UART_BAUD,
                tx=Pin(config.PIN_UART_TX), rx=Pin(config.PIN_UART_RX),
                rxbuf=config.UART_RXBUF, txbuf=config.UART_TXBUF,
                timeout=0)
    link = vesc.VescLink(uart, config)
    bank = sensors.SensorBank()
    loop = control.ControlLoop(link, bank, strategy.Placeholder())
    return loop


def run():
    loop = build()
    core1 = ui.Core1(loop.snapshot)
    _thread.start_new_thread(core1.run, ())

    # INIT: fw handshake (state machine exits INIT on first fw + telemetry)
    loop.link.request_fw()

    wdt = WDT(timeout=config.WDT_MS) if config.WDT_ENABLE else None
    gc.collect()
    gc.disable()                      # collections are scheduled, not random

    tick = 0
    next_ms = time.ticks_add(time.ticks_ms(), config.TICK_MS)
    while True:
        if wdt:
            wdt.feed()
        t0 = time.ticks_us()
        now = time.ticks_ms()

        loop.tick(now)

        tick += 1
        if tick % config.GC_DIV == 0:
            g0 = time.ticks_us()
            gc.collect()
            g_ms = time.ticks_diff(time.ticks_us(), g0) / 1000.0
            if g_ms > loop.gc_max_ms:
                loop.gc_max_ms = g_ms

        # fixed-rate alignment: a late tick is counted, never stretched
        if time.ticks_diff(time.ticks_us(), t0) > (config.TICK_MS - 1) * 1000:
            loop.deadline_miss += 1
        while time.ticks_diff(next_ms, time.ticks_ms()) > 0:
            time.sleep_ms(1)
        next_ms = time.ticks_add(next_ms, config.TICK_MS)
        # if we fell behind, realign instead of bursting
        if time.ticks_diff(time.ticks_ms(), next_ms) > 0:
            next_ms = time.ticks_add(time.ticks_ms(), config.TICK_MS)


try:
    run()
except Exception as e:  # last resort: leave the motor to A1's timeout
    try:
        import sys
        sys.print_exception(e)
    except Exception:
        pass
    machine.reset() if config.WDT_ENABLE else None
