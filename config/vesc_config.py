# Shared VESC configuration for commissioning and overlay flashing.

VESC_CHARACTERIZATION = {
    "detect_can": False,
    "max_power_loss_w": 30.0,
    "min_input_current_a": 0.0,
    "max_input_current_a": 0.0,
    "openloop_erpm": 0.0,
    "sensorless_erpm": 0.0,
}

VESC_TEMP_LIMITS = {
    "motor_max_current_a": 40.0,
    "motor_min_current_a": -40.0,
    "battery_max_current_a": 40.0,
    "battery_min_current_a": -40.0,
    "min_erpm": -200000.0,
    "max_erpm": 200000.0,
    "min_duty": 0.0,
    "max_duty": 0.95,
    "watt_min": -500.0,
    "watt_max": 500.0,
}

VESC_BATTERY_CUT_LIMITS = {
    "start_v": 15.0,
    "end_v": 14.0,
}

VESC_FLASH_LIMITS = {
    "motor_max_current_a": 40.0,
    "motor_min_current_a": -40.0,
    "battery_max_current_a": 40.0,
    "battery_min_current_a": -40.0,
    "min_input_voltage_v": 15.0,
    "max_input_voltage_v": 42.0,
    "battery_cut_start_v": 15.0,
    "battery_cut_end_v": 14.0,
    "watt_max": 500.0,
    "watt_min": -500.0,
}

VESC_BASELINE_PATCHES = (
    {"offset": 6, "type": "u8", "value": 2, "name": "Motor type (FOC)"},
    {"offset": 7, "type": "u8", "value": 0, "name": "Sensor mode (sensorless baseline)"},
    {"offset": 8, "type": "f32", "value": 20.0, "name": "Motor max current"},
    {"offset": 12, "type": "f32", "value": -8.0, "name": "Motor min current"},
    {"offset": 16, "type": "f32", "value": 20.0, "name": "Battery max current"},
    {"offset": 20, "type": "f32", "value": -8.0, "name": "Battery min current"},
    {"offset": 48, "type": "f32", "value": 15.0, "name": "Min input voltage"},
    {"offset": 52, "type": "f32", "value": 42.0, "name": "Max input voltage"},
    {"offset": 56, "type": "f32", "value": 15.0, "name": "Battery cutoff start"},
    {"offset": 60, "type": "f32", "value": 14.0, "name": "Battery cutoff end"},
    {"offset": 93, "type": "f32", "value": 500.0, "name": "Max watts"},
    {"offset": 97, "type": "f32", "value": -500.0, "name": "Min watts (regen)"},
)

VESC_OVERLAY_PATCHES = (
    {"offset": 6, "type": "u8", "value": 2, "name": "Motor type (FOC)"},
    {"offset": 8, "type": "f32", "value": 40.0, "name": "Motor max current"},
    {"offset": 12, "type": "f32", "value": -40.0, "name": "Motor min current"},
    {"offset": 16, "type": "f32", "value": 40.0, "name": "Battery max current"},
    {"offset": 20, "type": "f32", "value": -40.0, "name": "Battery min current"},
    {"offset": 48, "type": "f32", "value": 15.0, "name": "Min input voltage"},
    {"offset": 52, "type": "f32", "value": 42.0, "name": "Max input voltage"},
    {"offset": 56, "type": "f32", "value": 15.0, "name": "Battery cutoff start"},
    {"offset": 60, "type": "f32", "value": 14.0, "name": "Battery cutoff end"},
    {"offset": 93, "type": "f32", "value": 500.0, "name": "Max watts"},
    {"offset": 97, "type": "f32", "value": -500.0, "name": "Min watts (regen)"},
)


def get_baseline_patches():
    return [
        (patch["offset"], patch["type"], patch["value"], patch["name"])
        for patch in VESC_BASELINE_PATCHES
    ]


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
