# main.py — wiring, core 1 launch, the fixed-rate 100 Hz loop. Target only.
#
# Failure policy is the hardware's: if this script dies before the WDT is
# armed the Pico sits at the REPL and A1's 200 ms UART timeout releases the
# motor; after that the 2 s WDT reboots it. Either way the motor gets 0 A,
# so no exception handling is needed here.

import os

if "nomain" in os.listdir("/"):     # bench escape for tools/deploy.sh
    raise SystemExit

import _thread
import gc
import time

from machine import UART, WDT, Pin

import config as C
import control
import sensors
import strategy
import ui
import vesc

uart = UART(C.UART_ID, baudrate=C.UART_BAUD, tx=Pin(C.PIN_UART_TX),
            rx=Pin(C.PIN_UART_RX), rxbuf=C.UART_RXBUF, timeout=0)
loop = control.Control(vesc.Link(uart), sensors.Sensors(), strategy.Placeholder())
sn = loop.sn
_thread.start_new_thread(ui.Core1(sn).run, ())
wdt = WDT(timeout=C.WDT_MS)

n = 0
due = time.ticks_ms()
while True:
    wdt.feed()
    t0 = time.ticks_us()
    loop.tick()
    n += 1
    if n % C.GC_DIV == 0:
        gc.collect()                # scheduled, so pauses land in known slots
    t = time.ticks_diff(time.ticks_us(), t0)
    if t > sn[control.SN_TMAX]:
        sn[control.SN_TMAX] = t     # worst tick incl. GC, us (gate FW-2)
    due = time.ticks_add(due, C.TICK_MS)
    wait = time.ticks_diff(due, time.ticks_ms())
    if wait > 0:
        time.sleep_ms(wait)
    else:                           # late: count it and realign, never burst
        sn[control.SN_MISS] += 1
        due = time.ticks_ms()
