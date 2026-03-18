# drivers/lcd_driver.py — Raw LCD communication layer
#
# Low-level display methods without knowledge of application states.
# TODO: Choose actual LCD type and bus (I2C character LCD or SPI TFT)

from machine import I2C, Pin
from config.pins import LCD_I2C_ID, LCD_SDA_PIN, LCD_SCL_PIN, LCD_I2C_ADDR


class LCDDriver:
    def __init__(self):
        self._i2c = I2C(
            LCD_I2C_ID,
            sda=Pin(LCD_SDA_PIN),
            scl=Pin(LCD_SCL_PIN),
            freq=400_000,
        )
        self._addr = LCD_I2C_ADDR
        self._cols = 16   # TODO: set based on chosen display
        self._rows = 2    # TODO: set based on chosen display
        self._init_display()

    def _init_display(self):
        """Send initialization commands to the LCD."""
        # TODO: Implement init sequence for the chosen LCD module
        pass

    def clear(self):
        """Clear the entire display."""
        # TODO: Implement
        pass

    def write_line(self, row, text):
        """Write a string to a specific row (0-indexed), truncated to screen width."""
        text = text[:self._cols]
        # TODO: Implement cursor positioning and write
        pass

    def write_at(self, row, col, text):
        """Write a string at a specific row and column position."""
        # TODO: Implement
        pass

    # TODO: Choose the actual LCD type and bus
    # TODO: Decide screen dimensions and line length
    # TODO: Decide whether display writes should be rate-limited or diff-based
