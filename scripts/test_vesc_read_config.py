# scripts/test_vesc_read_config.py — Read VESC motor configuration via UART
#
# Sends COMM_GET_MCCONF (14) and decodes key motor/battery parameters.
# Also pulls live telemetry for current state reference.
#
# Run: mpremote run scripts/test_vesc_read_config.py

import struct
from time import sleep_ms, ticks_ms, ticks_diff
from machine import UART, Pin

# ---- UART setup ----
uart = UART(1, baudrate=115200, tx=Pin(4), rx=Pin(5), rxbuf=1024)

# ---- VESC protocol helpers (inline to keep script self-contained) ----

def crc16(data):
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


def wrap_frame(payload):
    length = len(payload)
    if length <= 255:
        frame = bytes([0x02, length]) + payload
    else:
        frame = bytes([0x03, length >> 8, length & 0xFF]) + payload
    c = crc16(payload)
    frame += struct.pack(">H", c)
    frame += bytes([0x03])
    return frame


def send_and_receive(cmd_payload, timeout_ms=500):
    """Send a command and read the response frame."""
    # Flush RX
    uart.read()
    sleep_ms(10)

    uart.write(wrap_frame(cmd_payload))
    sleep_ms(50)

    buf = bytearray()
    start = ticks_ms()
    while ticks_diff(ticks_ms(), start) < timeout_ms:
        data = uart.read()
        if data:
            buf.extend(data)
            # Check if we have a complete frame
            payload = try_extract(buf)
            if payload is not None:
                return payload
        sleep_ms(10)

    # Try extracting from what we have
    payload = try_extract(buf)
    if payload is not None:
        return payload
    print("  [Received %d bytes but no valid frame]" % len(buf))
    return None


def try_extract(buf):
    """Try to extract payload from a VESC frame (short or long)."""
    if len(buf) < 6:
        return None

    idx = 0
    while idx < len(buf):
        if buf[idx] == 0x02 and idx + 4 < len(buf):
            # Short frame: 0x02 LEN PAYLOAD CRC16 0x03
            length = buf[idx + 1]
            frame_size = length + 5
            if idx + frame_size <= len(buf):
                payload = bytes(buf[idx + 2:idx + 2 + length])
                crc_recv = (buf[idx + 2 + length] << 8) | buf[idx + 3 + length]
                if crc16(payload) == crc_recv:
                    return payload
            idx += 1
        elif buf[idx] == 0x03 and idx + 5 < len(buf):
            # Long frame: 0x03 LEN_HI LEN_LO PAYLOAD CRC16 0x03
            length = (buf[idx + 1] << 8) | buf[idx + 2]
            if length > 0 and length < 10000:
                frame_size = length + 6  # start(1) + len(2) + payload(N) + crc(2) + end(1)
                if idx + frame_size <= len(buf):
                    payload = bytes(buf[idx + 3:idx + 3 + length])
                    crc_recv = (buf[idx + 3 + length] << 8) | buf[idx + 4 + length]
                    if crc16(payload) == crc_recv:
                        return payload
            idx += 1
        else:
            idx += 1
    return None


# ---- Sequential config reader ----

class ConfigReader:
    """Reads fields sequentially from a VESC MCCONF binary payload."""
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def remaining(self):
        return len(self.data) - self.pos

    def u8(self):
        v = self.data[self.pos]
        self.pos += 1
        return v

    def i16(self):
        v = struct.unpack_from(">h", self.data, self.pos)[0]
        self.pos += 2
        return v

    def i32(self):
        v = struct.unpack_from(">i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def u32(self):
        v = struct.unpack_from(">I", self.data, self.pos)[0]
        self.pos += 4
        return v

    def f32(self):
        """Read IEEE 754 float32 (big-endian)."""
        v = struct.unpack_from(">f", self.data, self.pos)[0]
        self.pos += 4
        return v

    def b8(self):
        return self.u8() != 0

    def skip(self, n):
        self.pos += n


# ---- Command IDs ----
COMM_FW_VERSION = 0
COMM_GET_VALUES = 4
COMM_GET_MCCONF = 14


# ---- Main ----
print()
print("=" * 50)
print("  VESC Configuration Reader")
print("=" * 50)

# 1. Firmware version
print("\n--- Firmware Version ---")
payload = send_and_receive(bytes([COMM_FW_VERSION]))
if payload and payload[0] == COMM_FW_VERSION:
    fw_major = payload[1]
    fw_minor = payload[2]
    hw_name = ""
    if len(payload) > 3:
        # Hardware name starts at byte 3, null-terminated
        rest = payload[3:]
        nul = rest.find(b"\x00")
        if nul > 0:
            hw_name = rest[:nul].decode("utf-8", "replace")
        elif nul == -1 and len(rest) > 0:
            hw_name = rest[:20].decode("utf-8", "replace")
    print("  Firmware: %d.%d" % (fw_major, fw_minor))
    print("  Hardware: %s" % hw_name)
else:
    print("  Failed to read firmware version")

# 2. Live telemetry
print("\n--- Live Telemetry ---")
payload = send_and_receive(bytes([COMM_GET_VALUES]))
if payload and payload[0] == COMM_GET_VALUES and len(payload) >= 54:
    fmt = ">hhiiiihihiiiiiiB"
    vals = struct.unpack_from(fmt, payload, 1)
    print("  FET temp:       %.1f C" % (vals[0] / 10.0))
    print("  Motor temp:     %.1f C" % (vals[1] / 10.0))
    print("  Motor current:  %.1f A" % (vals[2] / 100.0))
    print("  Input current:  %.1f A" % (vals[3] / 100.0))
    print("  Duty cycle:     %.1f%%" % (vals[6] / 10.0))
    print("  ERPM:           %d" % vals[7])
    print("  Bus voltage:    %.1f V" % (vals[8] / 10.0))
    print("  Fault code:     %d" % vals[15])
else:
    print("  Failed to read telemetry")

# 3. Motor configuration (MCCONF) — larger payload, needs longer read
print("\n--- Motor Configuration (MCCONF) ---")

# Flush
uart.read()
sleep_ms(20)
uart.write(wrap_frame(bytes([COMM_GET_MCCONF])))
sleep_ms(100)

buf = bytearray()
start = ticks_ms()
while ticks_diff(ticks_ms(), start) < 5000:
    data = uart.read()
    if data:
        buf.extend(data)
        start = ticks_ms()  # reset timeout on each chunk
    sleep_ms(5)
    # Check if we have enough for a complete long frame
    if len(buf) >= 6 and buf[0] == 0x03:
        expected = ((buf[1] << 8) | buf[2]) + 6
        if len(buf) >= expected:
            break

print("  Raw bytes received: %d" % len(buf))

if len(buf) > 10:
    # Show first 20 bytes for frame header diagnosis
    hdr = " ".join("%02X" % b for b in buf[:20])
    print("  Header: %s" % hdr)

payload = try_extract(buf)
if payload is None and len(buf) > 0:
    print("  CRC check failed or incomplete frame")
    # Try assuming long frame starting at byte 0
    if buf[0] == 0x03 and len(buf) >= 6:
        length = (buf[1] << 8) | buf[2]
        print("  Long frame declares length: %d" % length)
        print("  Frame needs: %d bytes total" % (length + 6))
        if len(buf) >= length + 6:
            payload = bytes(buf[3:3 + length])
            crc_recv = (buf[3 + length] << 8) | buf[4 + length]
            crc_calc = crc16(payload)
            print("  CRC recv: %04X  calc: %04X" % (crc_recv, crc_calc))
            if crc_recv != crc_calc:
                payload = None

if payload is None or len(payload) < 50:
    print("  Failed to read MCCONF")
    if payload:
        print("  Got %d bytes, cmd=%d" % (len(payload), payload[0]))
else:
    if payload[0] != COMM_GET_MCCONF:
        print("  Unexpected response command: %d (expected %d)" % (payload[0], COMM_GET_MCCONF))
        print("  Payload length: %d bytes" % len(payload))
    else:
        print("  Payload length: %d bytes" % len(payload))
        r = ConfigReader(payload[1:])  # skip command byte

        try:
            # FW 5.x MCCONF starts with a config signature (int32)
            sig = r.i32()
            print("  Config signature: 0x%08X" % (sig & 0xFFFFFFFF))

            # Then packed fields — FW 5.2 uses confgenerator
            # Next is motor_type (uint8 in older, but may be after more fields)
            # Let's check the data: after sig we see 01 00 00 00
            # which could be motor_type=1 (DC) or an int32=1

            # Try reading as the FW 5.2 confgenerator layout:
            # After signature: the fields start

            # Read what appears to be an enum or flags (uint8 or int32)
            val1 = r.u8()
            val2 = r.u8()
            val3 = r.u8()
            val4 = r.u8()

            motor_types = {0: "BLDC", 1: "DC", 2: "FOC", 3: "GPD"}
            sensor_modes = {0: "Sensorless", 1: "Sensored", 2: "Hybrid"}
            pwm_modes = {0: "Nonsynchronous", 1: "Synchronous", 2: "Bipolar"}

            # Check if these look like reasonable enum values
            if val3 <= 3:
                print("\n  [Motor Config]")
                print("  PWM mode:       %s" % pwm_modes.get(val1, "(%d)" % val1))
                print("  Comm mode:      %d" % val2)
                print("  Motor type:     %s" % motor_types.get(val3, "(%d)" % val3))
                print("  Sensor mode:    %s" % sensor_modes.get(val4, "(%d)" % val4))

            # ---- Current limits (IEEE 754 float32) ----
            l_current_max = r.f32()
            l_current_min = r.f32()
            l_in_current_max = r.f32()
            l_in_current_min = r.f32()
            l_abs_current_max = r.f32()

            print("\n  [Current Limits]")
            print("  Motor max:      %.1f A" % l_current_max)
            print("  Motor min:      %.1f A" % l_current_min)
            print("  Battery max:    %.1f A" % l_in_current_max)
            print("  Battery min:    %.1f A" % l_in_current_min)
            print("  Absolute max:   %.1f A" % l_abs_current_max)

            # ---- ERPM limits ----
            l_min_erpm = r.f32()
            l_max_erpm = r.f32()
            l_erpm_start = r.f32()
            l_max_erpm_fbrake = r.f32()
            l_max_erpm_fbrake_cc = r.f32()

            print("\n  [ERPM Limits]")
            print("  Min ERPM:       %.0f" % l_min_erpm)
            print("  Max ERPM:       %.0f" % l_max_erpm)
            print("  ERPM start:     %.1f" % l_erpm_start)

            # ---- Voltage limits ----
            l_min_vin = r.f32()
            l_max_vin = r.f32()
            l_battery_cut_start = r.f32()
            l_battery_cut_end = r.f32()

            print("\n  [Voltage Limits]")
            print("  Min input V:    %.1f V" % l_min_vin)
            print("  Max input V:    %.1f V" % l_max_vin)
            print("  Batt cut start: %.1f V" % l_battery_cut_start)
            print("  Batt cut end:   %.1f V" % l_battery_cut_end)

            # ---- Boolean before temp ----
            l_slow_abs_current = r.b8()

            # ---- Temperature limits ----
            l_temp_fet_start = r.f32()
            l_temp_fet_end = r.f32()
            l_temp_motor_start = r.f32()
            l_temp_motor_end = r.f32()
            l_temp_accel_dec = r.f32()

            print("\n  [Temp Limits]")
            print("  FET start:      %.0f C" % l_temp_fet_start)
            print("  FET end:        %.0f C" % l_temp_fet_end)
            print("  Motor start:    %.0f C" % l_temp_motor_start)
            print("  Motor end:      %.0f C" % l_temp_motor_end)
            print("  Temp accel dec: %.2f" % l_temp_accel_dec)
            print("  Slow abs curr:  %s" % l_slow_abs_current)

            # ---- Duty / power ----
            l_min_duty = r.f32()
            l_max_duty = r.f32()
            l_watt_max = r.f32()
            l_watt_min = r.f32()

            print("\n  [Duty / Power]")
            print("  Min duty:       %.4f" % l_min_duty)
            print("  Max duty:       %.4f" % l_max_duty)
            print("  Max watts:      %.0f W" % l_watt_max)
            print("  Min watts:      %.0f W" % l_watt_min)

            # ---- Current control (cc) ----
            cc_startup_boost_duty = r.f32()
            cc_min_current = r.f32()
            cc_gain = r.f32()
            cc_ramp_step_max = r.f32()

            print("\n  [Current Control]")
            print("  CC startup:     %.4f" % cc_startup_boost_duty)
            print("  CC min current: %.1f A" % cc_min_current)
            print("  CC gain:        %.4f" % cc_gain)
            print("  CC ramp step:   %.4f" % cc_ramp_step_max)

            # ---- Scan remaining data for motor poles + FOC params ----
            print("\n  [Remaining data — offset %d, %d bytes left]" % (r.pos, r.remaining()))

            # Dump ALL remaining bytes as float32 + int32 for analysis
            idx = 0
            while r.remaining() >= 4 and idx < 80:
                pos = r.pos
                fval = r.f32()
                ival = struct.unpack_from(">i", r.data, pos)[0]
                uval = struct.unpack_from(">I", r.data, pos)[0]
                byteval = r.data[pos]
                # Print all non-zero values
                if ival != 0:
                    print("  +%3d: f=%12.4f  i=%d  u=%d  b=%d" % (pos, fval, ival, uval, byteval))
                idx += 1

        except Exception as e:
            print("\n  Parse error at offset %d: %s" % (r.pos, e))
            print("  (Remaining bytes: %d)" % r.remaining())

    # Hex dump of first 100 bytes for reference
    print("\n  [Full hex dump - first 120 bytes]")
    dump = payload[1:121]
    for i in range(0, len(dump), 16):
        row = dump[i:i + 16]
        hex_part = " ".join("%02X" % b for b in row)
        print("  %4d: %s" % (i, hex_part))

print("\n" + "=" * 50)
print("  Done")
print("=" * 50)
