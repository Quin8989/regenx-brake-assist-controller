# services/display_manager.py — Convert system state into LCD and LED content
#
# Reads shared_state and chooses what to show.
# Updates the screen at a limited rate to avoid flicker.

from core import FAULT_LABELS, SystemState


class DisplayManager:
    def __init__(self, lcd_driver, shared_state):
        self._lcd = lcd_driver
        self._state = shared_state

    def update(self):
        """Refresh LCD based on current system state."""
        if self._lcd is None:
            return
        try:
            self._update_page()
        except OSError:
            # LCD disconnected or I2C NAK — silently skip this cycle.
            pass

    def _update_page(self):
        s = self._state

        if s.system_state == SystemState.FAULT:
            self._show_fault_page()
            return

        if s.system_state == SystemState.PRECHARGE:
            self._show_precharge_page()
            return

        self._show_run_page()

    def _show_run_page(self):
        s = self._state
        line0 = f"{s.system_state:<8s} {s.cap_voltage_v:>5.1f}V"
        line1 = f"E:{s.cap_energy_percent:>3.0f}% {s.vesc_motor_current_a:>5.1f}A"
        self._lcd.write_line(0, line0)
        self._lcd.write_line(1, line1)

    def _show_precharge_page(self):
        s = self._state
        line0 = "PRECHARGE"
        line1 = f"Vcap: {s.cap_voltage_v:>5.1f}V"
        self._lcd.write_line(0, line0)
        self._lcd.write_line(1, line1)

    def _show_fault_page(self):
        s = self._state
        line0 = "** FAULT **"
        # Show first active fault
        if s.fault_flags:
            code = next(iter(s.fault_flags))
            line1 = FAULT_LABELS.get(code, str(code))[:16]
        else:
            line1 = "Unknown"
        self._lcd.write_line(0, line0)
        self._lcd.write_line(1, line1)
