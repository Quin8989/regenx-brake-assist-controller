# drivers/lcd_driver.py — Raw LCD communication layer
#
# Low-level display methods without knowledge of application states.

from time import sleep_ms, sleep_us

from machine import I2C, Pin

from config.settings import (
    LCD_COLS,
    LCD_I2C_ADDR,
    LCD_I2C_ID,
    LCD_ROWS,
    LCD_SCL_PIN,
    LCD_SDA_PIN,
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

# PCF8574 bit map (common backpack wiring)
_PIN_RS = 0x01
_PIN_RW = 0x02
_PIN_E = 0x04
_PIN_BL = 0x08


class LCDDriver:
    def __init__(self):
        self._i2c = I2C(
            LCD_I2C_ID,
            sda=Pin(LCD_SDA_PIN),
            scl=Pin(LCD_SCL_PIN),
            freq=400_000,
        )
        self._addr = LCD_I2C_ADDR
        self._cols = LCD_COLS
        self._rows = LCD_ROWS
        self._backlight = _PIN_BL
        self._init_display()

    def _init_display(self):
        # HD44780 4-bit init sequence via PCF8574.
        sleep_ms(50)
        self._write4bits(0x03 << 4)
        sleep_ms(5)
        self._write4bits(0x03 << 4)
        sleep_us(200)
        self._write4bits(0x03 << 4)
        sleep_us(200)
        self._write4bits(0x02 << 4)

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
            self._write(ord(ch), _PIN_RS)

    def _command(self, value):
        self._write(value, 0)

    def _write(self, value, mode):
        high = mode | (value & 0xF0)
        low = mode | ((value << 4) & 0xF0)
        self._write4bits(high)
        self._write4bits(low)

    def _write4bits(self, value):
        self._expander_write(value)
        self._pulse_enable(value)

    def _expander_write(self, value):
        self._i2c.writeto(self._addr, bytes([value | self._backlight]))

    def _pulse_enable(self, value):
        self._expander_write(value | _PIN_E)
        sleep_us(1)
        self._expander_write(value & ~_PIN_E)
        sleep_us(50)
