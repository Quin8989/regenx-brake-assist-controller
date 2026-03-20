# services/display_manager.py — Convert system state into LCD content
#
# Reads shared_state and chooses what to show on the 16×2 LCD.
# Updates at a limited rate to avoid flicker.
#
# Page layouts (16 columns × 2 rows):
#
#   RUN (ASSIST):    "ASSIST  25.2V 68%"
#                    "  12.3A   124RPM"
#
#   RUN (REGEN):     "REGEN   25.2V 68%"
#                    "  12.3A   124RPM"
#
#   RUN (READY):     "COAST   25.2V 68%"
#                    "   0.0A     0RPM"
#
#   PRECHARGE:       "PRECHARGE...    "
#                    "Vcap: 12.4V  31%"
#
#   FAULT:           "!! FAULT !!     "   (alternates with fault name)
#                    "Overvoltage     "
#
#   OFF:             "ReGenX  v1.0    "
#                    "    Standby     "

from time import ticks_ms, ticks_diff

from core import FAULT_LABELS, CommandMode, SystemState

_FAULT_FLASH_MS = 800  # Toggle period for fault page header


class DisplayManager:
    def __init__(self, lcd_driver, shared_state):
        self._lcd = lcd_driver
        self._state = shared_state
        self._fault_flash = False
        self._fault_flash_ms = 0
        self._fault_index = 0

    def update(self):
        """Refresh LCD based on current system state."""
        if self._lcd is None:
            return
        try:
            self._update_page()
        except OSError:
            pass

    def _update_page(self):
        s = self._state

        if s.system_state == SystemState.FAULT:
            self._show_fault_page()
            return

        if s.system_state == SystemState.PRECHARGE:
            self._show_precharge_page()
            return

        if s.system_state == SystemState.OFF:
            self._show_off_page()
            return

        self._show_run_page()

    # ----- RUN page (READY / ASSIST / REGEN) -----

    def _show_run_page(self):
        s = self._state

        if s.system_state == SystemState.ASSIST:
            mode = "ASSIST"
        elif s.system_state == SystemState.REGEN:
            mode = "REGEN"
        else:
            mode = "COAST"

        # Line 0: "ASSIST  25.2V 68%"   (mode 6, gap, voltage 5, space, pct 3)
        volts = f"{s.cap_voltage_v:.1f}V"
        pct = f"{s.cap_energy_percent:.0f}%"
        pad0 = 16 - len(mode) - len(volts) - len(pct)
        line0 = mode + " " * max(pad0 - 1, 1) + volts + " " + pct

        # Line 1: "  12.3A   124RPM"   (current right-aligned 6, gap, RPM right-aligned)
        amps = f"{abs(s.vesc_motor_current_a):.1f}A"
        rpm = f"{int(s.wheel_speed_rpm)}RPM" if s.wheel_speed_valid else f"{int(abs(s.vesc_mech_rpm))}RPM"
        pad1 = 16 - len(amps) - len(rpm)
        line1 = " " * max(pad1 // 2, 1) + amps + " " * max(pad1 - pad1 // 2, 1) + rpm

        self._lcd.write_line(0, line0[:16])
        self._lcd.write_line(1, line1[:16])

    # ----- PRECHARGE page -----

    def _show_precharge_page(self):
        s = self._state
        line0 = "PRECHARGE..."
        pct = f"{s.cap_energy_percent:.0f}%"
        line1 = f"Vcap:{s.cap_voltage_v:>5.1f}V {pct:>4s}"
        self._lcd.write_line(0, line0)
        self._lcd.write_line(1, line1[:16])

    # ----- FAULT page (cycles through active faults) -----

    def _show_fault_page(self):
        s = self._state
        now = ticks_ms()

        # Cycle to next fault label periodically
        if ticks_diff(now, self._fault_flash_ms) >= _FAULT_FLASH_MS:
            self._fault_flash_ms = now
            if len(s.fault_flags) > 1:
                self._fault_index = (self._fault_index + 1) % len(s.fault_flags)

        faults = list(s.fault_flags)
        if faults:
            idx = self._fault_index % len(faults)
            label = FAULT_LABELS.get(faults[idx], str(faults[idx]))
        else:
            label = "Unknown"

        self._lcd.write_line(0, "!! FAULT !!")
        self._lcd.write_line(1, label[:16])

    # ----- OFF page -----

    def _show_off_page(self):
        self._lcd.write_line(0, "ReGenX  v1.0")
        self._lcd.write_line(1, "    Standby")
