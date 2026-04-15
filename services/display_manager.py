# services/display_manager.py — Convert system state into LCD content
#
# Reads shared_state and chooses what to show on the 16×2 LCD.
# Updates at a limited rate to avoid flicker.
#
# Page layouts (16 columns × 2 rows):
#
#   RUN (ASSIST):    "ASSIST  25.2V 68%"
#                    "     +12.3A      "
#
#   RUN (REGEN):     "REGEN   25.2V 68%"
#                    "     -12.3A      "
#
#   RUN (idle):      "REGEN   25.2V 68%"
#                    "      +0.0A      "
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

from config.settings import (
    CAPACITANCE_F,
    VCAP_MIN_OPERATING,
    VCAP_SOFT_REGEN_CUTOFF,
)
from core import FAULT_LABELS, SystemState
from utils import clamp

_FAULT_FLASH_MS = 800  # Toggle period for fault page header
_VESC_FAULT_SHOW_MS = 3000  # Duration to show VESC fault overlay after detection

# EMA smoothing factor for displayed current (0..1).
# Lower = smoother but laggier.  At 5 Hz display rate, 0.3 gives a
# ~0.5 s settling time — responsive enough to see mode changes,
# smooth enough to stop digit jitter while riding.
_CURRENT_EMA_ALPHA = 0.3

# Energy estimation constants (½CV²)
_HALF_C = 0.5 * CAPACITANCE_F
_E_MIN = _HALF_C * VCAP_MIN_OPERATING * VCAP_MIN_OPERATING
_E_MAX = _HALF_C * VCAP_SOFT_REGEN_CUTOFF * VCAP_SOFT_REGEN_CUTOFF
_E_RANGE = _E_MAX - _E_MIN if _E_MAX > _E_MIN else 0.0

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


class DisplayManager:
    def __init__(self, lcd_driver, shared_state):
        self._lcd = lcd_driver
        self._state = shared_state
        self._fault_flash_ms = 0
        self._fault_index = 0
        self._vesc_fault_until_ms = 0
        self._last_vesc_fault_code = 0
        self._smooth_current_a = 0.0

    def update(self):
        """Recompute energy estimate then refresh LCD."""
        self._update_energy()
        if self._lcd is None:
            return
        try:
            self._update_page()
        except OSError:
            pass

    def _update_energy(self):
        v = self._state.cap_voltage_v
        energy_j = _HALF_C * v * v
        if _E_RANGE > 0.0:
            pct = (energy_j - _E_MIN) / _E_RANGE * 100.0
            self._state.cap_energy_percent = clamp(pct, 0.0, 100.0)
        else:
            self._state.cap_energy_percent = 0.0

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

        if s.system_state == SystemState.OFF:
            self._show_off_page()
            return

        self._show_run_page()

    # ----- VESC hardware fault overlay -----

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

        # Line 1: smoothed bus (input) current — this is what actually
        # charges/discharges the caps, not the motor phase current.
        raw = s.vesc_input_current_a
        self._smooth_current_a += _CURRENT_EMA_ALPHA * (raw - self._smooth_current_a)
        amps = f"{self._smooth_current_a:+.1f}A"
        pad1 = 16 - len(amps)
        line1 = " " * (pad1 // 2) + amps

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

    # ----- OFF page -----

    def _show_off_page(self):
        self._lcd.write_line(0, "ReGenX  v1.0")
        self._lcd.write_line(1, "    Standby")
