# config/vesc_config.py — VESC-specific communication settings
#
# All values here must be verified against the actual installed VESC firmware
# version and configuration export before implementation.

# --- UART settings ---
VESC_BAUD_RATE = 115200        # TODO: confirm against VESC app config

# --- Telemetry mode ---
# True = Pico polls for telemetry; False = VESC sends unsolicited (if supported)
TELEMETRY_POLLED = True

# --- Required telemetry fields ---
REQUIRED_TELEMETRY = [
    "bus_voltage",
    "motor_current",
    "input_current",
    "rpm",
    "duty_cycle",
    "fault_code",
]

# --- Command mode ---
# "current"       — motor current in amps (positive = assist)
# "brake_current" — brake current in amps (positive = regen braking)
# "duty"          — duty cycle fraction
ASSIST_COMMAND_MODE = "current"        # TODO: confirm best mode for this VESC FW
REGEN_COMMAND_MODE = "brake_current"   # TODO: confirm best mode for this VESC FW

# --- Command heartbeat ---
COMMAND_HEARTBEAT_MS = 200     # Re-send active command to maintain control authority

# --- Firmware compatibility ---
VESC_FIRMWARE_VERSION = None   # TODO: record once known (e.g. "6.02")
VESC_HARDWARE_VERSION = None   # TODO: record once known

# TODO: Record actual VESC firmware version once known
# TODO: Record actual enabled app configuration if relevant
# TODO: Confirm assist mode (motor current, duty, or other)
# TODO: Confirm regen mode (brake current, negative current, or other)
# TODO: Confirm which telemetry fields are available in chosen request packet
