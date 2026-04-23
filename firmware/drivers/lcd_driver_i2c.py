# drivers/lcd_driver_i2c.py — I2C LCD communication layer (PCF8574 backpack)
#
# Drives an HD44780/ST7066U 16×2 character LCD through a PCF8574 I/O
# expander (the common "I2C LCD backpack" module, typically at address
# 0x27 or 0x3F).  Same public surface as drivers/lcd_driver.LCDDriver:
#     - write_line(row, text)
#     - reinit()
#
# PCF8574 → HD44780 pin mapping (standard backpack wiring):
#     P0 = RS
#     P1 = RW   (tied low by driver — write-only)
#     P2 = E
#     P3 = BL   (backlight, active-high)
#     P4 = D4
#     P5 = D5
#     P6 = D6
#     P7 = D7

from time import sleep_ms, sleep_us

from machine import I2C, Pin

from config.settings import (
    LCD_COLS,
    LCD_ROWS,
    LCD_I2C_ADDR,
    LCD_I2C_BUS,
    LCD_I2C_SCL_PIN,
    LCD_I2C_SDA_PIN,
    LCD_I2C_FREQ,
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

# PCF8574 bit masks (see wiring table in module docstring).
_MASK_RS = 0x01
_MASK_E = 0x04
_MASK_BL = 0x08


class LCDDriver:
    """I2C variant of the parallel LCDDriver.

    Provides the same ``write_line(row, text)`` and ``reinit()`` API so
    it is a drop-in replacement at the DisplayManager layer.
    """

    def __init__(self):
        self._i2c = I2C(
            LCD_I2C_BUS,
            scl=Pin(LCD_I2C_SCL_PIN),
            sda=Pin(LCD_I2C_SDA_PIN),
            freq=LCD_I2C_FREQ,
        )
        self._addr = LCD_I2C_ADDR
        self._cols = LCD_COLS
        self._rows = LCD_ROWS
        self._bl = _MASK_BL  # backlight on by default
        self._init_display()

    def reinit(self):
        """Re-run the HD44780 init sequence to recover from EMI corruption.

        Same rationale as the parallel driver — with RW tied low through
        the PCF8574 we cannot poll the busy flag, so DisplayManager calls
        reinit() periodically and on fault edges to resync 4-bit framing.
        """
        self._init_display()

    # -- Public API ---------------------------------------------------------

    def write_line(self, row, text):
        if row < 0 or row >= self._rows:
            return
        text = text[:self._cols]
        if len(text) < self._cols:
            text = text + (" " * (self._cols - len(text)))
        row_offsets = (0x00, 0x40, 0x14, 0x54)
        self._command(_LCD_SETDDRAMADDR | row_offsets[row])
        for ch in text:
            self._write(ord(ch), rs=1)

    # -- HD44780 init -------------------------------------------------------

    def _init_display(self):
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

    # -- Low-level PCF8574 byte framing ------------------------------------

    def _command(self, value):
        self._write(value, rs=0)

    def _write(self, value, rs):
        # High nibble first, then low nibble.
        self._write4bits(value >> 4, rs=rs)
        self._write4bits(value & 0x0F, rs=rs)

    def _write4bits(self, nibble, rs=0):
        # Data bits live on P4..P7 of the expander.
        data = ((nibble & 0x0F) << 4) | self._bl
        if rs:
            data |= _MASK_RS
        self._pulse_enable(data)

    def _pulse_enable(self, data):
        # Latch the nibble by toggling E while other bits are stable.
        self._i2c.writeto(self._addr, bytes([data & ~_MASK_E]))
        sleep_us(1)
        self._i2c.writeto(self._addr, bytes([data | _MASK_E]))
        sleep_us(1)
        self._i2c.writeto(self._addr, bytes([data & ~_MASK_E]))
        sleep_us(50)
