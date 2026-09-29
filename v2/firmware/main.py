# main.py — wiring, core 1 launch, the fixed-rate 100 Hz loop. Target only.
#
# Failure policy is the hardware's: if this script dies before the WDT is
# armed the Pico sits at the REPL and A1's 200 ms UART timeout releases the
# motor; after that the 2 s WDT reboots it. Either way the motor gets 0 A,
# so no exception handling is needed here.

import _thread
import gc
import os
import time

from machine import UART, WDT, Pin

import config as C
import control
import sensors
import ui
import vesc


def run():
    uart = UART(C.UART_ID, baudrate=C.UART_BAUD, tx=Pin(C.PIN_UART_TX),
                rx=Pin(C.PIN_UART_RX), rxbuf=C.UART_RXBUF, timeout=0)
    loop = control.Control(vesc.Link(uart), sensors.Sensors())
    sn = loop.sn
    _thread.start_new_thread(ui.Core1(sn).run, ())
    wdt = WDT(timeout=C.WDT_MS)
    n = 0
    due = time.ticks_ms()
    while True:
        wdt.feed()
        loop.tick()
        n += 1
        if n % C.GC_DIV == 0:
            gc.collect()            # scheduled, so pauses land in known slots
        due = time.ticks_add(due, C.TICK_MS)
        wait = time.ticks_diff(due, time.ticks_ms())
        if wait > 0:
            time.sleep_ms(wait)
        else:                       # late: count it (the display shows it)
            sn[control.SN_LATE] += 1    # and realign, never burst
            due = time.ticks_ms()


# tools/deploy.sh drops /nomain so a redeploy is not racing the WDT. Returning
# (not SystemExit, which soft-reboots into main.py again) leaves the REPL.
if "nomain" not in os.listdir("/"):
    run()
