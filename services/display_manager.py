# services/display_manager.py — Convert system state into LCD and LED content
#
# Reads shared_state and chooses what to show.
# Updates the screen at a limited rate to avoid flicker.

from core.enums import SystemState, DisplayPage


class DisplayManager:
    def __init__(self, lcd_driver, status_leds, shared_state):
        self._lcd = lcd_driver
        self._leds = status_leds
        self._state = shared_state

    def update(self):
        """Refresh LCD and LEDs based on current system state."""
        self._update_lcd()
        self._update_leds()

    def _update_lcd(self):
        s = self._state

        # FAULT page overrides all normal pages
        if s.system_state == SystemState.FAULT:
            self._show_fault_page()
            return

        if s.system_state == SystemState.PRECHARGE:
            self._show_precharge_page()
            return

        # Normal run page
        self._show_run_page()

    def _show_run_page(self):
        s = self._state
        line0 = "{:<8s} {:>5.1f}V".format(s.system_state, s.cap_voltage_v)
        line1 = "E:{:>3.0f}% {:>5.1f}A".format(s.cap_energy_percent, s.vesc_motor_current_a)
        self._lcd.write_line(0, line0)
        self._lcd.write_line(1, line1)

    def _show_precharge_page(self):
        s = self._state
        line0 = "PRECHARGE"
        line1 = "Vcap: {:>5.1f}V".format(s.cap_voltage_v)
        self._lcd.write_line(0, line0)
        self._lcd.write_line(1, line1)

    def _show_fault_page(self):
        s = self._state
        from core.faults import FaultManager, FAULT_LABELS
        line0 = "** FAULT **"
        # Show first active fault
        if s.fault_flags:
            code = next(iter(s.fault_flags))
            line1 = FAULT_LABELS.get(code, str(code))[:16]
        else:
            line1 = "Unknown"
        self._lcd.write_line(0, line0)
        self._lcd.write_line(1, line1)

    def _update_leds(self):
        s = self._state
        self._leds.set("ready", s.system_state == SystemState.READY)
        self._leds.set("assist", s.system_state == SystemState.ASSIST)
        self._leds.set("regen", s.system_state == SystemState.REGEN)
        self._leds.set("fault", s.system_state == SystemState.FAULT)
        self._leds.set("low_energy", s.cap_energy_percent < 30.0)

    # TODO: Finalize LCD layout based on chosen display size
    # TODO: Decide which values are always visible vs rotated
    # TODO: Decide whether fault history is shown or only highest-priority active fault
    # TODO: Decide whether LCD replaces or supplements LED indicators
