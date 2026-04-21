# services/display_manager.py — Convert system state into LCD content
#
# Reads shared_state and chooses what to show on the 16×2 LCD.
# Updates at a limited rate to avoid flicker.
#
# Page layouts (16 columns × 2 rows):
#
#   RUN (ASSIST):    "ASSIST  25.2V 68%"
#                    " +12.3A   124RPM"
#
#   RUN (REGEN):     "REGEN   25.2V 68%"
#                    " -12.3A   124RPM"
#
#   RUN (idle):      "REGEN   25.2V 68%"
#                    "  +0.0A     0RPM"
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

import math

from config.settings import (
    VCAP_MIN_OPERATING,
    VCAP_REGEN_TAPER_START_V,
    WHEEL_RADIUS_M,
)
from core import FAULT_LABELS, SystemState

_WHEEL_CIRCUMFERENCE_M = 2.0 * math.pi * WHEEL_RADIUS_M
_CAP_V_MIN_SQ = VCAP_MIN_OPERATING ** 2
_CAP_V_MAX_SQ = VCAP_REGEN_TAPER_START_V ** 2
_CAP_RANGE_SQ = _CAP_V_MAX_SQ - _CAP_V_MIN_SQ

_FAULT_FLASH_MS = 800  # Toggle period for fault page header
_VESC_FAULT_SHOW_MS = 3000  # Duration to show VESC fault overlay after detection

# Periodic LCD re-init — HD44780 in 4-bit mode has no readback (RW grounded),
# so we cannot detect controller-state corruption caused by motor-bus EMI or
# regen-spike brownouts.  A blind re-init every N ms restores framing without
# the rider ever seeing it.  Re-init also runs on every fault-state edge.
_LCD_REINIT_PERIOD_MS = 5000

_VESC_FAULT_NAMES = {
    1:  "OVER VOLTAGE",
    2:  "UNDER VOLTAGE",
    3:  "DRV",
    4:  "ABS OVERCURRENT",
    5:  "OVER TEMP FET",
    6:  "OVER TEMP MOTOR",
    7:  "GD OVER VOLT",
    8:  "GD UNDER VOLT",
    9:  "MCU UNDER VOLT",
    10: "WDT RESET",
    15: "HI OFFSET CS1",
    16: "HI OFFSET CS2",
    17: "HI OFFSET CS3",
    18: "UNBAL CURRENTS",
}


def _rpm_to_kmh(rpm):
    return (rpm * _WHEEL_CIRCUMFERENCE_M * 60.0) / 1000.0


class DisplayManager:
    def __init__(self, lcd_driver, shared_state):
        self._lcd = lcd_driver
        self._state = shared_state
        self._fault_flash_ms = 0
        self._fault_index = 0
        self._vesc_fault_until_ms = 0
        self._last_vesc_fault_code = 0
        self._last_reinit_ms = ticks_ms()
        self._last_sys_state = shared_state.system_state

    def update(self):
        """Refresh LCD based on current system state."""
        self._update_energy_percent()
        if self._lcd is None:
            return
        try:
            self._maybe_reinit_lcd()
            self._update_page()
        except OSError:
            pass

    def _maybe_reinit_lcd(self):
        """Re-init the LCD controller periodically and on fault-state edges.

        Covers the only LCD failure mode we cannot detect in software: the
        HD44780 4-bit nibble counter drifting out of sync after an EMI glitch
        or brownout.  Cheap (~70 ms of blocking GPIO writes) and silent.
        """
        reinit = getattr(self._lcd, "reinit", None)
        if reinit is None:
            return
        now = ticks_ms()
        sys_state = self._state.system_state
        fault_edge = (sys_state == SystemState.FAULT) != (
            self._last_sys_state == SystemState.FAULT
        )
        due = ticks_diff(now, self._last_reinit_ms) >= _LCD_REINIT_PERIOD_MS
        if fault_edge or due:
            try:
                reinit()
            except OSError:
                pass
            self._last_reinit_ms = now
        self._last_sys_state = sys_state

    def _update_energy_percent(self):
        """Compute cap_energy_percent from cap_voltage_v (capacitive: E ∝ V²)."""
        v_sq = self._state.cap_voltage_v ** 2
        pct = (v_sq - _CAP_V_MIN_SQ) / _CAP_RANGE_SQ * 100.0
        if pct < 0.0:
            pct = 0.0
        elif pct > 100.0:
            pct = 100.0
        self._state.cap_energy_percent = pct

    def _update_page(self):
        s = self._state

        # VESC hardware fault overlay takes priority over normal run display.
        # Shows the fault name for _VESC_FAULT_SHOW_MS after last detection.
        if s.vesc_fault_code != 0:
            self._last_vesc_fault_code = s.vesc_fault_code
            self._vesc_fault_until_ms = ticks_ms() + _VESC_FAULT_SHOW_MS

        if self._last_vesc_fault_code != 0 and ticks_diff(
            self._vesc_fault_until_ms, ticks_ms()
        ) > 0:
            self._show_vesc_fault_overlay()
            return
        elif self._last_vesc_fault_code != 0:
            self._last_vesc_fault_code = 0

        if s.system_state == SystemState.FAULT:
            self._show_fault_page()
            return

        if s.system_state == SystemState.PRECHARGE:
            self._show_precharge_page()
            return

        self._show_run_page()

    # ----- VESC fault overlay -----

    def _show_vesc_fault_overlay(self):
        code = self._last_vesc_fault_code
        name = _VESC_FAULT_NAMES.get(code, "FAULT %d" % code)
        self._lcd.write_line(0, ("VESC F%d" % code)[:16])
        self._lcd.write_line(1, name[:16])

    # ----- RUN page (ASSIST / REGEN) -----

    def _show_run_page(self):
        s = self._state

        if s.system_state == SystemState.ASSIST:
            mode = "ASSIST"
        elif s.system_state == SystemState.REGEN:
            mode = "REGEN"
        else:
            mode = s.system_state

        # Line 0: "ASSIST  25.2V 68%"   (mode 6, gap, voltage 5, space, pct 3)
        volts = f"{s.cap_voltage_v:.1f}V"
        pct = f"{s.cap_energy_percent:.0f}%"
        pad0 = 16 - len(mode) - len(volts) - len(pct)
        line0 = mode + " " * max(pad0 - 1, 1) + volts + " " + pct

        # Line 1: " +12.3A  12.4km/h"
        # ASSIST: show iq (motor effort); REGEN: show input_current (energy into caps).
        if s.system_state == SystemState.REGEN:
            amps = f"{s.vesc_input_current_a:+.1f}A"
        else:
            amps = f"{s.vesc_iq_current_a:+.1f}A"
        if s.wheel_speed_valid:
            speed_text = f"{_rpm_to_kmh(s.wheel_speed_rpm):.1f}km/h"
        else:
            speed_text = f"{abs(s.vesc_mech_rpm):.0f}RPM"
        pad1 = 16 - len(amps) - len(speed_text)
        line1 = " " * max(pad1 // 2, 1) + amps + " " * max(pad1 - pad1 // 2, 1) + speed_text

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
            code = faults[idx]
            label = FAULT_LABELS.get(code, str(code))
            # Show exception detail for INTERNAL faults
            if code == "INTERNAL" and getattr(s, "last_exception_str", ""):
                label = s.last_exception_str
        else:
            label = "Unknown"

        self._lcd.write_line(0, "!! FAULT !!")
        self._lcd.write_line(1, label[:16])


