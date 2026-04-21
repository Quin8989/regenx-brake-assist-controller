# services/vesc_protocol.py — VESC UART packet framing, CRC, and parsing
#
# Pure protocol layer — no hardware I/O, no application state.
# Handles frame encoding/decoding, command building, and telemetry parsing
# per the VESC UART packet specification.

import struct

# =============================================================================
# VESC UART frame constants and opcodes
# =============================================================================

FRAME_START_SHORT = 0x02      # Payload <= 256 bytes
FRAME_START_LONG = 0x03       # Payload > 256 bytes
FRAME_END = 0x03

COMM_FW_VERSION = 0           # Request firmware version + HW_NAME
COMM_GET_VALUES = 4           # Request full telemetry
COMM_SET_CURRENT = 6          # Set motor current (amps * 1000)
COMM_SET_CURRENT_BRAKE = 7    # Set brake current ceiling (amps * 1000, positive)
COMM_ALIVE = 30               # Reset VESC timeout watchdog
COMM_CUSTOM_APP_DATA = 36     # Custom data from VESC LispBM (send-data)
COMM_SET_MCCONF_TEMP = 48     # Runtime config: current/watt/ERPM limits (no flash)
COMM_GET_VALUES_SELECTIVE = 50 # Bitmask-filtered telemetry
COMM_APP_DISABLE_OUTPUT = 63  # Disable all app output for N ms
COMM_SET_BATTERY_CUT = 86     # Runtime battery cut voltage adjustment
COMM_MOTOR_ESTOP = 159        # Emergency stop — ignore input for N ms, release motor

# Telemetry struct: everything after the opcode byte
# Includes dq current terms reported by VESC (avg_id, avg_iq).
#
# Bits 0-15 (base, all FW versions):
#   h  temp_fet        (×10)     h  temp_motor      (×10)
#   i  motor_current   (×100)    i  input_current   (×100)
#   i  avg_id          (×100)    i  avg_iq          (×100)
#   h  duty            (×1000)   i  rpm             (×1)
#   h  v_in            (×10)     i  ah              (×10000)
#   i  ah_charged      (×10000)  i  wh              (×10000)
#   i  wh_charged      (×10000)  i  tach            (raw)
#   i  tach_abs        (raw)     B  fault_code
#
# Bits 16-21 (FW 6.x extension):
#   i  pid_pos_now     (×1e6)    B  controller_id
#   h  temp_mos1       (×10)     h  temp_mos2       (×10)
#   h  temp_mos3       (×10)     i  avg_vd          (×1000)
#   i  avg_vq          (×1000)   B  status

_TELEMETRY_FMT = ">hhiiiihihiiiiiiB"
_TELEMETRY_SIZE = 53  # bytes after opcode (bits 0-15)

_TELEMETRY_FMT_EXT = ">iBhhhiiB"
_TELEMETRY_SIZE_FULL = 73  # bytes after opcode (bits 0-21)

# Selective telemetry (COMM_GET_VALUES_SELECTIVE): only fields used by
# control loop, safety, display, and bench logger.
#
# Bit  Field             Fmt  Bytes  Used by
# ---  -----             ---  -----  -------
#  0   temp_fet           h    2     display (VESC fault overlay)
#  1   temp_motor         h    2     display (VESC fault overlay)
#  2   motor_current      i    4     bench logger
#  3   input_current      i    4     control_loop power limiter, display
#  5   avg_iq             i    4     control_loop iq feedback
#  6   duty               h    2     control_loop saturation detection
#  7   rpm                i    4     control_loop feedforward, bench logger
#  8   v_in               h    2     cap voltage — everywhere
# 15   fault_code         B    1     safety supervisor
#
# Skipped: bit 4 (avg_id), 9-14 (Ah/Wh/tach), 16-22 (extension)
SELECTIVE_MASK = 0x000081EF
_SELECTIVE_FMT = ">hhiiihihB"
_SELECTIVE_SIZE = 25  # bytes after header (opcode + 4-byte mask)


# =============================================================================
# CRC and frame wrapping
# =============================================================================


def _crc16(data):
    """CRC-16/CCITT used by VESC framing."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def _wrap_frame(payload):
    """Wrap a payload in VESC UART framing with CRC."""
    length = len(payload)
    if length <= 255:
        frame = bytes([FRAME_START_SHORT, length]) + payload
    else:
        frame = bytes([FRAME_START_LONG, length >> 8, length & 0xFF]) + payload
    crc = _crc16(payload)
    frame += struct.pack(">H", crc)
    frame += bytes([FRAME_END])
    return frame


# =============================================================================
# Command builders
# =============================================================================


def _build_telemetry_request():
    return _wrap_frame(bytes([COMM_GET_VALUES]))


def _build_selective_telemetry_request(mask=SELECTIVE_MASK):
    """Build COMM_GET_VALUES_SELECTIVE — bitmask-filtered telemetry."""
    return _wrap_frame(struct.pack(">BI", COMM_GET_VALUES_SELECTIVE, mask))


def _build_set_current(amps):
    return _wrap_frame(struct.pack(">Bi", COMM_SET_CURRENT, int(amps * 1000)))


def _build_set_brake_current(amps):
    """Build COMM_SET_CURRENT_BRAKE packet.  amps must be >= 0."""
    return _wrap_frame(struct.pack(">Bi", COMM_SET_CURRENT_BRAKE, int(amps * 1000)))


def _build_fw_version_request():
    """Build COMM_FW_VERSION request — returns HW_NAME, FW version, UUID."""
    return _wrap_frame(bytes([COMM_FW_VERSION]))


def _build_alive():
    """Build COMM_ALIVE — resets VESC communication timeout watchdog."""
    return _wrap_frame(bytes([COMM_ALIVE]))


def _build_motor_estop(timeout_ms):
    """Build COMM_MOTOR_ESTOP — ignore input and release motor for timeout_ms."""
    return _wrap_frame(struct.pack(">BH", COMM_MOTOR_ESTOP, timeout_ms))


def _build_app_disable_output(timeout_ms, fwd_can=False):
    """Build COMM_APP_DISABLE_OUTPUT — disable VESC app output for timeout_ms.

    fwd_can: if True, forward to all CAN-connected VESCs.
    """
    flag = 1 if fwd_can else 0
    return _wrap_frame(struct.pack(">BBi", COMM_APP_DISABLE_OUTPUT, flag, timeout_ms))


def _build_set_battery_cut(start_v, end_v, store=False, fwd_can=False):
    """Build COMM_SET_BATTERY_CUT — set battery cutoff voltages at runtime.

    start_v: voltage where current limiting begins (soft cutoff).
    end_v:   voltage where current drops to zero (hard cutoff).
    store:   if True, persist to flash (default: RAM only).
    fwd_can: if True, forward to CAN-connected VESCs.
    """
    start_raw = int(start_v * 1000)
    end_raw = int(end_v * 1000)
    return _wrap_frame(struct.pack(">BiiBB", COMM_SET_BATTERY_CUT,
                                   start_raw, end_raw,
                                   1 if store else 0,
                                   1 if fwd_can else 0))


def _encode_float32_auto(value):
    """Encode a float using VESC's variable-length auto-compression format.

    Matches buffer_append_float32_auto() / buffer_get_float32_auto()
    in the VESC firmware.  Returns 2-5 bytes (type tag + value).
    """
    abs_val = abs(value)
    if abs_val <= 0.127:
        return struct.pack(">Bb", 0, int(value * 1000))
    if abs_val <= 32.767:
        return struct.pack(">Bh", 1, int(value * 1000))
    if abs_val <= 327.67:
        return struct.pack(">Bh", 2, int(value * 100))
    if abs_val <= 2147483.647:
        return struct.pack(">Bi", 3, int(value * 1000))
    return struct.pack(">Bf", 4, value)


def _build_set_mcconf_temp(
    current_min, current_max,
    current_min_scale, current_max_scale,
    in_current_min, in_current_max,
    min_erpm, max_erpm,
    min_duty, max_duty,
    watt_min, watt_max,
    store=False, fwd_can=False, ack=False, divide=False,
):
    """Build COMM_SET_MCCONF_TEMP — runtime motor config without flash write.

    All 12 float fields are required because the VESC applies them as a set.
    Uses VESC auto-compression encoding for each value.
    """
    payload = bytes([
        COMM_SET_MCCONF_TEMP,
        1 if store else 0,
        1 if fwd_can else 0,
        1 if ack else 0,
        1 if divide else 0,
    ])
    for val in (current_min, current_max,
                current_min_scale, current_max_scale,
                in_current_min, in_current_max,
                min_erpm, max_erpm,
                min_duty, max_duty,
                watt_min, watt_max):
        payload += _encode_float32_auto(val)
    return _wrap_frame(payload)


# =============================================================================
# Telemetry parsing
# =============================================================================


def _parse_telemetry(payload):
    """Parse a COMM_GET_VALUES response into a 20-element tuple, or None.

    Always returns 20 values.  Extension fields (bits 16-21) default to
    zero when the payload only contains the base 53 bytes.
    """
    if not payload or payload[0] != COMM_GET_VALUES:
        return None
    data_len = len(payload) - 1
    if data_len < _TELEMETRY_SIZE:
        return None
    try:
        (
            temp_fet, temp_motor,
            motor_current_raw, input_current_raw,
            avg_id_raw, avg_iq_raw,
            duty_raw, rpm, v_in_raw,
            _ah, _ah_charged, _wh, _wh_charged,
            tach, tach_abs,
            fault_code,
        ) = struct.unpack_from(_TELEMETRY_FMT, payload, 1)
    except Exception:
        return None

    # Extension fields (bits 16-21) — present in FW 6.x full responses
    pid_pos_raw = 0
    controller_id = 0
    temp_mos1 = temp_mos2 = temp_mos3 = 0
    avg_vd_raw = avg_vq_raw = 0
    status = 0

    if data_len >= _TELEMETRY_SIZE_FULL:
        try:
            (
                pid_pos_raw, controller_id,
                temp_mos1, temp_mos2, temp_mos3,
                avg_vd_raw, avg_vq_raw,
                status,
            ) = struct.unpack_from(_TELEMETRY_FMT_EXT, payload, 1 + _TELEMETRY_SIZE)
        except Exception:
            pass  # Keep defaults

    return (
        temp_fet / 10.0,
        temp_motor / 10.0,
        motor_current_raw / 100.0,
        input_current_raw / 100.0,
        avg_id_raw / 100.0,
        avg_iq_raw / 100.0,
        duty_raw / 1000.0,
        rpm,
        v_in_raw / 10.0,
        tach,
        tach_abs,
        fault_code,
        pid_pos_raw / 1000000.0,
        controller_id,
        temp_mos1 / 10.0,
        temp_mos2 / 10.0,
        temp_mos3 / 10.0,
        avg_vd_raw / 1000.0,
        avg_vq_raw / 1000.0,
        status,
    )


def _parse_fw_version(payload):
    """Parse a COMM_FW_VERSION response.

    Returns (fw_major, fw_minor, hw_name) or None on failure.
    hw_name is a string like "410" or "Flipsky_412".
    """
    if not payload or payload[0] != COMM_FW_VERSION:
        return None
    if len(payload) < 4:
        return None
    fw_major = payload[1]
    fw_minor = payload[2]
    # HW_NAME is a null-terminated string starting at offset 3
    name_start = 3
    name_end = payload.find(b"\x00", name_start)
    if name_end < 0:
        name_end = len(payload)
    hw_name = payload[name_start:name_end].decode("ascii", "replace")
    return (fw_major, fw_minor, hw_name)


def _parse_selective_telemetry(payload):
    """Parse a COMM_GET_VALUES_SELECTIVE response for our fixed mask.

    Returns a 9-element tuple on success, or None on failure:
        (temp_fet_c, temp_motor_c, motor_current_a, input_current_a,
         iq_current_a, duty_cycle, erpm, v_in_v, fault_code)
    """
    if not payload or payload[0] != COMM_GET_VALUES_SELECTIVE:
        return None
    if len(payload) < 5 + _SELECTIVE_SIZE:
        return None
    # Verify mask matches our expected format
    mask = (payload[1] << 24) | (payload[2] << 16) | (payload[3] << 8) | payload[4]
    if mask != SELECTIVE_MASK:
        return None
    try:
        (
            temp_fet, temp_motor,
            motor_current_raw, input_current_raw,
            avg_iq_raw, duty_raw, rpm, v_in_raw,
            fault_code,
        ) = struct.unpack_from(_SELECTIVE_FMT, payload, 5)
    except Exception:
        return None
    return (
        temp_fet / 10.0,
        temp_motor / 10.0,
        motor_current_raw / 100.0,
        input_current_raw / 100.0,
        avg_iq_raw / 100.0,
        duty_raw / 1000.0,
        rpm,
        v_in_raw / 10.0,
        fault_code,
    )


# =============================================================================
# LISP push telemetry (COMM_CUSTOM_APP_DATA)
# =============================================================================

# Packet layout from vesc_lisp_push_iq.lisp (16 bytes, big-endian float32,
# aggregated over a 10 ms / 1 kHz-sampled window on the VESC):
#   [0]        opcode  (COMM_CUSTOM_APP_DATA = 36)
#   [1..4]     float32 rpm_now         (electrical RPM at send instant)
#   [5..8]     float32 drpm_mean       (mean d(erpm)/dt over window, erpm/s)
#   [9..12]    float32 drpm_peak_neg   (most-negative per-sample d(erpm)/dt, erpm/s)
#   [13..16]   float32 iq_mean         (mean q-axis current over window, A)
_PUSH_IQ_SIZE = 16  # payload bytes after opcode


def _parse_push_iq(payload):
    """Parse a COMM_CUSTOM_APP_DATA aggregated-regen packet from VESC LispBM.

    Returns (erpm_fast, drpm_mean_erps, drpm_peak_neg_erps, iq_mean_a) on
    success, or None.
    """
    if not payload or payload[0] != COMM_CUSTOM_APP_DATA:
        return None
    if len(payload) < 1 + _PUSH_IQ_SIZE:
        return None
    try:
        erpm, drpm_mean, drpm_peak_neg, iq_mean = struct.unpack_from(">ffff", payload, 1)
    except Exception:
        return None
    return (erpm, drpm_mean, drpm_peak_neg, iq_mean)


# =============================================================================
# Frame extraction
# =============================================================================


def _extract_payload(buf):
    """Extract a complete VESC frame (short or long) from a byte buffer.

    Returns (payload_bytes, remaining_buf) on success,
    or (None, buf) when the buffer does not yet contain a complete frame.
    """
    while len(buf) >= 6:
        if buf[0] == FRAME_START_SHORT:
            length = buf[1]
            header_size = 2  # start(1) + len(1)
        elif buf[0] == FRAME_START_LONG:
            if len(buf) < 7:
                break  # Need more data for long-frame header
            length = (buf[1] << 8) | buf[2]
            header_size = 3  # start(1) + len_hi(1) + len_lo(1)
        else:
            buf = buf[1:]
            continue

        frame_size = header_size + length + 3  # payload + crc(2) + end(1)

        if len(buf) < frame_size:
            break

        payload = bytes(buf[header_size:header_size + length])
        crc_offset = header_size + length
        crc_recv = (buf[crc_offset] << 8) | buf[crc_offset + 1]
        end_byte = buf[crc_offset + 2]

        if end_byte != FRAME_END or crc_recv != _crc16(payload):
            buf = buf[1:]
            continue

        return payload, buf[frame_size:]

    return None, buf
