# Shared VESC configuration for commissioning and overlay flashing.
#
# Current, voltage, and watt limits are imported from config.settings so
# every module in the project shares a single source of truth.

from config.settings import (
    MOTOR_CURRENT_MAX_A,
    VCAP_ABSOLUTE_MAX,
    VCAP_MIN_OPERATING,
    VESC_ABS_CURRENT_MAX_A,
    VESC_WATT_MAX,
)

VESC_CHARACTERIZATION = {
    "detect_can": False,
    "max_power_loss_w": 30.0,
    "min_input_current_a": 0.0,
    "max_input_current_a": 0.0,
    "openloop_erpm": 0.0,
    "sensorless_erpm": 0.0,
}

VESC_TEMP_LIMITS = {
    "motor_max_current_a": MOTOR_CURRENT_MAX_A,
    "motor_min_current_a": -MOTOR_CURRENT_MAX_A,
    "battery_max_current_a": MOTOR_CURRENT_MAX_A,
    "battery_min_current_a": -MOTOR_CURRENT_MAX_A,
    "min_erpm": -200000.0,
    "max_erpm": 200000.0,
    "min_duty": 0.0,
    "max_duty": 0.95,
    "watt_min": -VESC_WATT_MAX,
    "watt_max": VESC_WATT_MAX,
}

VESC_BATTERY_CUT_LIMITS = {
    "start_v": VCAP_MIN_OPERATING,
    "end_v": VCAP_MIN_OPERATING - 1.0,
}

VESC_FLASH_LIMITS = {
    "motor_max_current_a": MOTOR_CURRENT_MAX_A,
    "motor_min_current_a": -MOTOR_CURRENT_MAX_A,
    "abs_current_max_a": VESC_ABS_CURRENT_MAX_A,
    "battery_max_current_a": MOTOR_CURRENT_MAX_A,
    "battery_min_current_a": -MOTOR_CURRENT_MAX_A,
    "min_input_voltage_v": VCAP_MIN_OPERATING,
    "max_input_voltage_v": VCAP_ABSOLUTE_MAX,
    "battery_cut_start_v": VCAP_MIN_OPERATING,
    "battery_cut_end_v": VCAP_MIN_OPERATING - 1.0,
    "watt_max": VESC_WATT_MAX,
    "watt_min": -VESC_WATT_MAX,
}

VESC_OVERLAY_PATCHES = (
    {"offset": 6, "type": "u8", "value": 2, "name": "Motor type (FOC)"},
    {"offset": 8, "type": "f32", "value": MOTOR_CURRENT_MAX_A, "name": "Motor max current"},
    {"offset": 12, "type": "f32", "value": -MOTOR_CURRENT_MAX_A, "name": "Motor min current"},
    {"offset": 16, "type": "f32", "value": MOTOR_CURRENT_MAX_A, "name": "Battery max current"},
    {"offset": 20, "type": "f32", "value": -MOTOR_CURRENT_MAX_A, "name": "Battery min current"},
    {"offset": 48, "type": "f32", "value": VCAP_MIN_OPERATING, "name": "Min input voltage"},
    {"offset": 52, "type": "f32", "value": VCAP_ABSOLUTE_MAX, "name": "Max input voltage"},
    {"offset": 56, "type": "f32", "value": VCAP_MIN_OPERATING, "name": "Battery cutoff start"},
    {"offset": 60, "type": "f32", "value": VCAP_MIN_OPERATING - 1.0, "name": "Battery cutoff end"},
    {"offset": 93, "type": "f32", "value": VESC_WATT_MAX, "name": "Max watts"},
    {"offset": 97, "type": "f32", "value": -VESC_WATT_MAX, "name": "Min watts (regen)"},
)


def get_overlay_patches():
    return [
        (patch["offset"], patch["type"], patch["value"], patch["name"])
        for patch in VESC_OVERLAY_PATCHES
    ]


def get_temp_limits():
    return dict(VESC_TEMP_LIMITS)


def get_battery_cut_limits():
    return dict(VESC_BATTERY_CUT_LIMITS)


def get_flash_limits():
    return dict(VESC_FLASH_LIMITS)
