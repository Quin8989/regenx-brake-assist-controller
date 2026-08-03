# Shared VESC configuration for commissioning and overlay flashing.
#
# Current, voltage, and watt limits are imported from config.settings so
# every module in the project shares a single source of truth.
# Script-specific dicts (temp, battery-cut, flash) are derived from a
# single canonical set of limits defined here.

from config.settings import (
    MOTOR_CURRENT_MAX_A,
    VCAP_ABSOLUTE_MAX,
    VCAP_MIN_OPERATING,
    VESC_ABS_CURRENT_MAX_A,
    VESC_WATT_MAX,
)

# =============================================================================
# Canonical limits — single source of truth for all VESC limit dicts
# =============================================================================

_I_MOT = MOTOR_CURRENT_MAX_A
_I_ABS = VESC_ABS_CURRENT_MAX_A
_V_MIN = VCAP_MIN_OPERATING
_V_MAX = VCAP_ABSOLUTE_MAX
_W = VESC_WATT_MAX
_V_CUT_END = _V_MIN - 1.0

# =============================================================================
# Motor characterization parameters (detection script)
# =============================================================================

VESC_CHARACTERIZATION = {
    "detect_can": False,
    "max_power_loss_w": 30.0,
    "min_input_current_a": 0.0,
    "max_input_current_a": 0.0,
    "openloop_erpm": 0.0,
    "sensorless_erpm": 0.0,
}

# =============================================================================
# Script-specific limit dicts — derived from canonical limits above
# =============================================================================

# Temporary runtime limits (COMM_SET_MCCONF_TEMP)
VESC_TEMP_LIMITS = {
    "motor_max_current_a": _I_MOT,
    "motor_min_current_a": -_I_MOT,
    "battery_max_current_a": _I_MOT,
    "battery_min_current_a": -_I_MOT,
    "min_erpm": -200000.0,
    "max_erpm": 200000.0,
    "min_duty": 0.0,
    "max_duty": 0.95,
    "watt_min": -_W,
    "watt_max": _W,
}

# Battery cutoff voltages (COMM_SET_BATTERY_CUT)
VESC_BATTERY_CUT_LIMITS = {
    "start_v": _V_MIN,
    "end_v": _V_CUT_END,
}

# Persistent flash limits (Lisp conf-set) — superset of temp + battery-cut
VESC_FLASH_LIMITS = {
    "motor_max_current_a": _I_MOT,
    "motor_min_current_a": -_I_MOT,
    "abs_current_max_a": _I_ABS,
    "battery_max_current_a": _I_MOT,
    "battery_min_current_a": -_I_MOT,
    "min_input_voltage_v": _V_MIN,
    "max_input_voltage_v": _V_MAX,
    "battery_cut_start_v": _V_MIN,
    "battery_cut_end_v": _V_CUT_END,
    "watt_max": _W,
    "watt_min": -_W,
}

# Binary overlay patches for mcconf (offset, type, value, label)
VESC_OVERLAY_PATCHES = (
    {"offset": 6, "type": "u8", "value": 2, "name": "Motor type (FOC)"},
    {"offset": 8, "type": "f32", "value": _I_MOT, "name": "Motor max current"},
    {"offset": 12, "type": "f32", "value": -_I_MOT, "name": "Motor min current"},
    {"offset": 16, "type": "f32", "value": _I_MOT, "name": "Battery max current"},
    {"offset": 20, "type": "f32", "value": -_I_MOT, "name": "Battery min current"},
    {"offset": 48, "type": "f32", "value": _V_MIN, "name": "Min input voltage"},
    {"offset": 52, "type": "f32", "value": _V_MAX, "name": "Max input voltage"},
    {"offset": 56, "type": "f32", "value": _V_MIN, "name": "Battery cutoff start"},
    {"offset": 60, "type": "f32", "value": _V_CUT_END, "name": "Battery cutoff end"},
    {"offset": 93, "type": "f32", "value": _W, "name": "Max watts"},
    {"offset": 97, "type": "f32", "value": -_W, "name": "Min watts (regen)"},
)


# =============================================================================
# Accessor helpers — return copies so callers can mutate without side-effects
# =============================================================================

def get_overlay_patches():
    return [
        (p["offset"], p["type"], p["value"], p["name"])
        for p in VESC_OVERLAY_PATCHES
    ]


def get_temp_limits():
    return dict(VESC_TEMP_LIMITS)


def get_battery_cut_limits():
    return dict(VESC_BATTERY_CUT_LIMITS)


def get_flash_limits():
    return dict(VESC_FLASH_LIMITS)
