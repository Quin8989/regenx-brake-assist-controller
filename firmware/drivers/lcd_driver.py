# drivers/lcd_driver.py — Raw LCD communication layer (4-bit parallel GPIO)
#
# Drives an RG1602A (ST7066U / HD44780-compatible) 16×2 character LCD
# directly over 6 GPIO pins in 4-bit mode.  No I2C backpack required.
#
# Wiring:  RS, E, D4, D5, D6, D7 → Pico GPIO.  RW → GND (write-only).

from time import sleep_ms, sleep_us

from machine import Pin

from config.settings import (
    LCD_BL_PIN,
    LCD_COLS,
    LCD_ROWS,
    LCD_RS_PIN,
    LCD_E_PIN,
    LCD_D4_PIN,
    LCD_D5_PIN,
    LCD_D6_PIN,
    LCD_D7_PIN,
)

_LCD_CLEARDISPLAY = 0x01
_LCD_RETURNHOME = 0x02
_LCD_ENTRYMODESET = 0x04
_LCD_DISPLAYCONTROL = 0x08
_LCD_FUNCTIONSET = 0x20
_LCD_SETDDRAMADDR = 0x80

_LCD_ENTRYLEFT = 0x02
_LCD_ENTRYSHIFTDECREMENT = 0x00

_LCD_DISPLAYON = 0x04
_LCD_CURSOROFF = 0x00
_LCD_BLINKOFF = 0x00

_LCD_4BITMODE = 0x00
_LCD_2LINE = 0x08
_LCD_1LINE = 0x00
_LCD_5x8DOTS = 0x00


class LCDDriver:
    def __init__(self):
        self._rs = Pin(LCD_RS_PIN, Pin.OUT, value=0)
        self._e = Pin(LCD_E_PIN, Pin.OUT, value=0)
        self._data = [
            Pin(LCD_D4_PIN, Pin.OUT, value=0),
            Pin(LCD_D5_PIN, Pin.OUT, value=0),
            Pin(LCD_D6_PIN, Pin.OUT, value=0),
            Pin(LCD_D7_PIN, Pin.OUT, value=0),
        ]
        self._bl = Pin(LCD_BL_PIN, Pin.OUT, value=1)
        self._cols = LCD_COLS
        self._rows = LCD_ROWS
        self._init_display()

    def _init_display(self):
        # HD44780 4-bit init sequence (direct GPIO).
        sleep_ms(50)
        self._write4bits(0x03)
        sleep_ms(5)
        self._write4bits(0x03)
        sleep_us(200)
        self._write4bits(0x03)
        sleep_us(200)
        self._write4bits(0x02)  # Switch to 4-bit mode

        line_mode = _LCD_2LINE if self._rows > 1 else _LCD_1LINE
        self._command(_LCD_FUNCTIONSET | _LCD_4BITMODE | line_mode | _LCD_5x8DOTS)
        self._command(
            _LCD_DISPLAYCONTROL | _LCD_DISPLAYON | _LCD_CURSOROFF | _LCD_BLINKOFF
        )
        self._command(_LCD_CLEARDISPLAY)
        sleep_ms(2)
        self._command(_LCD_ENTRYMODESET | _LCD_ENTRYLEFT | _LCD_ENTRYSHIFTDECREMENT)
        self._command(_LCD_RETURNHOME)
        sleep_ms(2)

    def write_line(self, row, text):
        """Write a string to a specific row (0-indexed), truncated to screen width."""
        if row < 0 or row >= self._rows:
            return
        text = text[:self._cols]
        if len(text) < self._cols:
            text = text + (" " * (self._cols - len(text)))
        row_offsets = (0x00, 0x40, 0x14, 0x54)
        self._command(_LCD_SETDDRAMADDR | row_offsets[row])
        for ch in text:
            self._write(ord(ch), rs=1)

    def _command(self, value):
        self._write(value, rs=0)

    def _write(self, value, rs):
        self._rs.value(rs)
        self._write4bits(value >> 4)
        self._write4bits(value & 0x0F)

    def _write4bits(self, nibble):
        for i, pin in enumerate(self._data):
            pin.value((nibble >> i) & 1)
        self._pulse_enable()

    def _pulse_enable(self):
        self._e.value(0)
        sleep_us(1)
        self._e.value(1)
        sleep_us(1)
        self._e.value(0)
        sleep_us(50)
