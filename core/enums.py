# core/enums.py — Central symbolic definitions used throughout the application
#
# Prevents hard-coded strings and magic numbers from scattering through the project.


class SystemState:
    OFF = "OFF"
    PRECHARGE = "PRECHARGE"
    READY = "READY"
    ASSIST = "ASSIST"
    REGEN = "REGEN"
    FAULT = "FAULT"


class PrechargeState:
    IDLE = "IDLE"
    START = "START"
    WAIT_FOR_VOLTAGE = "WAIT_FOR_VOLTAGE"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class FaultCode:
    VESC_TIMEOUT = "VESC_TIMEOUT"
    VESC_PACKET = "VESC_PACKET"
    OVERVOLTAGE = "OVERVOLTAGE"
    UNDERVOLTAGE = "UNDERVOLTAGE"
    PRECHARGE_TIMEOUT = "PRECHARGE_TIMEOUT"
    PRECHARGE_INVALID = "PRECHARGE_INVALID"
    THROTTLE_RANGE = "THROTTLE_RANGE"
    ADC_INVALID = "ADC_INVALID"
    INTERNAL = "INTERNAL"
    MOTOR_INHIBITED = "MOTOR_INHIBITED"
    SENSOR_STALE = "SENSOR_STALE"


class CommandMode:
    NEUTRAL = "NEUTRAL"
    ASSIST = "ASSIST"
    REGEN = "REGEN"


class DisplayPage:
    STATUS = "STATUS"
    TELEMETRY = "TELEMETRY"
    ENERGY = "ENERGY"
    FAULT = "FAULT"
    PRECHARGE = "PRECHARGE"


# TODO: Finalize state list
# TODO: Finalize fault code list
# TODO: Finalize display page IDs and user-facing labels
