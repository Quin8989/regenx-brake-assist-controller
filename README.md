# ReGenX Brake-Assist Controller

MicroPython firmware for a Raspberry Pi Pico supervising a VESC-based
assist/regen ebike drivetrain with supercapacitor energy storage.

---

## 1. System Overview

The controller manages a geared hub motor ebike drivetrain where the Pico acts
as the outer application controller and a Flipsky Mini FSESC4.20 (VESC 4.12
hardware) acts as the inner current/power-electronics controller.  Energy is
stored in a supercapacitor bank rather than a battery.

| Component | Role |
|---|---|
| Raspberry Pi Pico (RP2040) | Application controller — reads sensors, runs state machine, computes current commands |
| FSESC4.20 (VESC 4.12) | Inner loop — FOC motor control, current regulation, telemetry reporting |
| Puyan H01 geared hub motor | 250–350 W class, 15 pole pairs, planetary gearbox with one-way freewheel clutch |
| Supercapacitor bank (20 F) | Energy storage — charges via regen braking, discharges during assist |
| Hall throttle | 3-wire analog (0.8–4.2 V typical), read on Pico ADC0 |
| Wheel speed hall sensor | Fork-mounted, 6 spoke magnets, digital pulse input on GP9 |
| RG1602A 16×2 LCD | Status display via 4-bit parallel GPIO (RS, E, D4–D7) |

Communication between the Pico and VESC is over UART at 115200 baud.  Both
devices use 3.3 V logic, so no level-shifter is needed.

---

## 2. Riding Modes — Assist, Coast, and Regen

The drivetrain has three distinct riding states, determined entirely from
sensor data with no brake-lever switch required.

### 2.1 ASSIST (throttle applied)

When the rider applies the throttle, the firmware maps the throttle position
(0–100%) to a motor current command (0–40 A) and sends it to the VESC.  A
slew-rate limiter prevents sudden torque jumps.  The VESC's inner FOC loop
handles actual torque control.

### 2.2 COAST / NEUTRAL (throttle off, no brake)

When the rider releases the throttle and is not braking, the one-way freewheel
clutch on the planetary carrier disengages.  The wheel spins freely and the
motor sits nearly still.  No current is commanded — true zero-drag coast.

The system enters the NEUTRAL command mode, and the state machine returns to
COAST (no active motor state).

### 2.3 REGEN (throttle off, mechanical brake applied)

When the rider squeezes the mechanical brake, the planetary carrier locks.
This forces the wheel to drive the motor through the gear train.  The motor
now spins at approximately `wheel_rpm × 5.0` (the gear ratio).

The firmware detects this carrier lock by comparing motor RPM (reported by the
VESC from back-EMF sensing, even with zero commanded current) to wheel RPM
(from the hall sensor):

- **Carrier locked (braking):** `motor_rpm ≈ wheel_rpm × 5.0`
- **Carrier free (coasting):** `motor_rpm ≈ 0`

Once in REGEN, a PI slip controller commands brake current to supplement the
rider's mechanical braking and recover energy into the supercapacitor.  The PI
loop holds a fixed carrier-slip target of 2%, meaning the electrical braking
torque tracks the mechanical braking effort closely.

Regen is disabled entirely when the cap voltage reaches the soft cutoff
(40.0 V) to prevent overcharging.

### 2.4 Carrier-Lock Detection with Hysteresis

The transition between COAST and REGEN uses hysteresis to prevent chatter at
the engagement boundary:

| Transition | Condition |
|---|---|
| Enter REGEN | Carrier slip < 30% (motor ≥ 70% of locked speed) |
| Exit REGEN | Carrier slip > 50% (motor < 50% of locked speed) |

Carrier slip is defined as:

```
locked_motor_rpm = wheel_rpm × REGEN_LOCKED_RATIO      (e.g. 100 RPM × 5 = 500 RPM)
lock_frac        = actual_motor_rpm / locked_motor_rpm  (0 = free, 1 = locked)
carrier_slip     = 1 - lock_frac                        (0 = locked, 1 = free)
```

This means that once the system enters REGEN (tight 30% threshold), it will
stay in REGEN even if slip drifts up to 49%, only exiting when the rider
clearly releases the brake (slip > 50%).  Applying the throttle at any time
overrides to ASSIST and clears the hysteresis flag.

### 2.5 Why No Brake Switch?

The VESC continuously tracks rotor position from back-EMF, even when no
current is commanded.  It reports electrical RPM in every telemetry packet
(~20 Hz).  Dividing by the 15 pole pairs gives mechanical RPM.  This
passive sensing is sufficient to distinguish a locked carrier (motor spinning)
from a free carrier (motor still), eliminating the need for a dedicated brake
input signal.

---

## 3. Control Architecture

The firmware runs a cooperative scheduler on the Pico's single core.  Each
service is called once per main-loop cycle at its configured period.

### 3.1 Execution Order

| # | Service | Period | Purpose |
|---|---|---|---|
| 1 | InputManager | 10 ms | Read throttle + wheel hall, decide ASSIST/REGEN/NEUTRAL |
| 2 | VESCComm (RX) | continuous | Parse incoming VESC telemetry packets |
| 3 | VESCComm (TX) | 50 ms | Request telemetry from VESC |
| 4 | EnergyEstimator | 10 ms | Compute ½CV² stored energy and percentage |
| 5 | SafetySupervisor | 10 ms | Check voltage limits, telemetry freshness, throttle validity |
| 6 | StateMachine | 10 ms | Gate mode transitions with safety checks |
| 7 | ControlLoop | 10 ms | Compute assist/regen current commands (slew + PI) |
| 8 | CommandManager | 20 ms | Transmit current commands to VESC over UART |
| 9 | DisplayManager | 200 ms | Update 16×2 LCD with status/fault info |
| 10 | Logger | 500 ms | Debug output to serial console |

### 3.2 Data Flow

```
Throttle ADC ──┐
               ├──► InputManager ──► requested_mode + requested_level
Wheel Hall ────┘                              │
                                              ▼
VESC Telemetry ──► VESCComm ──► SharedState ◄── SafetySupervisor
                                    │                   │
                                    ▼                   ▼
                              StateMachine ──► system_state + inhibit flags
                                    │
                                    ▼
                              ControlLoop ──► assist_command / regen_command
                                    │
                                    ▼
                              CommandManager ──► VESC UART TX
```

All services communicate through a single `SharedState` object — no queues,
no callbacks, no interrupts (except the wheel hall edge ISR for timing).

---

## 4. System States

| State | Description | Motor Commands |
|---|---|---|
| OFF | Initial state after boot | Inhibited |
| PRECHARGE | Precharge relay active, waiting for cap ≥ 15.0 V | Inhibited |
| COAST | Electrically ready, no rider request active | Inhibited |
| ASSIST | Rider requesting forward power | Active (assist) |
| REGEN | Rider braking, carrier locked | Active (regen) |
| FAULT | One or more faults latched | Inhibited |

Transitions:

```
OFF → PRECHARGE → COAST ⇄ ASSIST
                  COAST ⇄ REGEN
                  ASSIST ⇄ REGEN   (direct transitions allowed)
              Any state → FAULT    (when faults present)
                  FAULT → COAST    (when all faults clear)
```

---

## 5. Regen PI Slip Controller

When in REGEN state, the ControlLoop runs a PI controller that regulates
braking current by controlling carrier slip:

1. **Estimate carrier RPM** from wheel speed and motor speed:
   `carrier_rpm = wheel_rpm × (1 - motor_rpm / (wheel_rpm × 5.0))`

2. **Compute target carrier RPM** from the fixed slip target:
   `target_rpm = wheel_rpm × 0.02`

3. **PI controller** drives the error (carrier_rpm − target_rpm) toward zero:
   - P gain: 0.35 A/RPM
   - I gain: 0.12 A/RPM·s
   - Anti-windup clamp: ±25 A

4. **Scale** by rider authority (always 1.0 in REGEN) and **slew-limit**
   the output at 20 A/s to prevent torque steps.

5. **Hard clamp** at REGEN_CURRENT_LIMIT_A (40 A).

Regen preconditions (any failure → command goes to zero):
- Cap voltage < 40.0 V (soft cutoff)
- Wheel speed valid and ≥ 20 RPM
- System not inhibited

---

## 6. Safety and Fault Handling

The SafetySupervisor runs at 100 Hz and checks:

| Fault | Trigger | Latching? |
|---|---|---|
| OVERVOLTAGE | Cap voltage ≥ VCAP_ABSOLUTE_MAX (42.0 V) | Yes |
| UNDERVOLTAGE | Cap voltage < VCAP_MIN_OPERATING (15.0 V) while in COAST+ | No |
| VESC_TIMEOUT | No valid telemetry for 500 ms | No |
| THROTTLE_RANGE | ADC below 100 or above 4000 (open/short circuit) | No |
| PRECHARGE_STALL | Voltage rise stalls for 3 consecutive 60 s windows | Yes |
| INTERNAL | Uncaught exception in main loop | Yes |

Latching faults require a power cycle to clear.  Non-latching faults
auto-clear when the condition resolves.

When any fault is active, the state machine forces FAULT state and
`inhibit_motor_commands = True`, which zeros all current commands and resets
all dynamic controller state (slew limiters, PI integrator).

---

## 7. Precharge System

The supercapacitor bank must be precharged before the VESC can operate safely.
The precharge circuit uses a 62 Ω resistor from a 20 V source, giving an RC
time constant of ~1240 s (20.7 min).

| Parameter | Value | Purpose |
|---|---|---|
| Target voltage | 15.0 V (VCAP_MIN_OPERATING) | Minimum for VESC + motor operation |
| VESC boot voltage | ~6.0 V | VESC powers on and begins sending telemetry |
| Telemetry grace period | 12 min | No voltage data until VESC boots (~7.4 min worst case from 0 V) |
| Progress window | 60 s | Evaluate voltage rise each window |
| Min progress ratio | 10% | Must see ≥10% of expected ΔV per window |
| Max bad windows | 3 | 3 consecutive stalled windows → PRECHARGE_STALL fault |
| Hard timeout | 35 min | Absolute limit (20% margin over 29 min worst case) |

The boost enable pin (GP16) controls a DC/DC boost converter that is activated
alongside the precharge relay.

---

## 8. Hardware Configuration

### 8.1 Pin Map

| Pico Pin | GPIO | Function |
|---|---|---|
| 1 | GP0 | UART0 TX → VESC RX |
| 2 | GP1 | UART0 RX ← VESC TX |
| 11 | GP8 | Soft reset button (active-low, internal pull-up) |
| 12 | GP9 | Wheel speed hall sensor (digital input) |
| 20 | GP15 | Precharge enable (active-high) |
| 21 | GP16 | Boost enable (active-high) |
| 22 | GP17 | LCD RS (Register Select) |
| 24 | GP18 | LCD E (Enable) |
| 25 | GP19 | LCD D4 |
| 26 | GP20 | LCD D5 |
| 27 | GP21 | LCD D6 |
| 29 | GP22 | LCD D7 |
| 31 | GP26 | ADC0 — Hall throttle |
| 34 | GP28 | LCD backlight enable (active-high) |
| 36 | 3V3(OUT) | LCD VDD (3.3 V power) |
| 38 | GND | Common ground |
| 39 | VSYS | 5 V external power input |

All Pico GPIO are 3.3 V logic.  The FSESC4.20 UART is also 3.3 V (STM32F4),
so no level-shifter is needed.

### 8.2 Throttle

3-wire hall throttle with 5 V supply.  Analog output typically 0.8–4.2 V.

| Parameter | Value |
|---|---|
| ADC valid range | 300–3800 (raw 12-bit counts) |
| Fault low | < 100 (open circuit) |
| Fault high | > 4000 (short circuit) |
| Deadband | 3% of range |

### 8.3 Wheel Speed Sensor

Fork-mounted hall sensor with 6 spoke magnets.

| Parameter | Value |
|---|---|
| GPIO | GP9 (digital input with internal pull-up) |
| Magnets | 6 per revolution |
| Timeout | 1200 ms (wheel stopped if no edge) |
| Min edge spacing | 1500 µs (debounce) |

### 8.4 LCD

RG1602A (ST7066U / HD44780-compatible) 16×2 character display driven
directly via 4-bit parallel GPIO.  No I2C backpack required.  RW pin is tied
to GND (write-only).  V0 (contrast) tied directly to GND (optimal at 3.3 V).
VDD powered from Pico 3V3(OUT).  Backlight driven from GP28 (no resistor).

| Parameter | Value |
|---|---|
| Interface | 4-bit parallel GPIO (RS, E, D4–D7) |
| GPIO pins | GP17 (RS), GP18 (E), GP19–GP22 (D4–D7), GP28 (backlight) |
| Power | 3.3 V from Pico 3V3(OUT) |
| Contrast (V0) | Tied to GND |
| Backlight | GP28 direct (no resistor) |
| Geometry | 16 columns × 2 rows |

---

## 9. Key Constants

### Voltage and Current

| Constant | Value | Description |
|---|---|---|
| VCAP_MIN_OPERATING | 15.0 V | Minimum cap voltage for motor operation |
| VCAP_SOFT_REGEN_CUTOFF | 40.0 V | Software regen disable threshold |
| VCAP_ABSOLUTE_MAX | 42.0 V | Hard overvoltage limit |
| ASSIST_CURRENT_LIMIT_A | 40.0 A | Maximum assist current |
| REGEN_CURRENT_LIMIT_A | 40.0 A | Maximum regen braking current |

### Regen Tuning

| Constant | Value | Description |
|---|---|---|
| REGEN_LOCKED_RATIO | 5.0 | Motor/wheel RPM ratio when carrier fully locked |
| REGEN_ENGAGE_SLIP_FRAC | 0.30 | Enter REGEN below this carrier slip |
| REGEN_DISENGAGE_SLIP_FRAC | 0.50 | Exit REGEN above this carrier slip |
| REGEN_TARGET_SLIP_FRAC | 0.02 | PI slip target (2%) |
| REGEN_MIN_WHEEL_RPM | 20.0 | Minimum wheel speed for regen |
| REGEN_PI_KP | 0.35 A/RPM | Proportional gain |
| REGEN_PI_KI | 0.12 A/RPM·s | Integral gain |
| REGEN_PI_INTEGRAL_LIMIT | 25.0 A | Anti-windup clamp |

### Motor

| Constant | Value | Description |
|---|---|---|
| VESC_MOTOR_POLE_PAIRS | 15 | Puyan H01 geared hub motor |
| VESC_BAUD_RATE | 115200 | UART communication speed |

---

## 10. Bench Debug Logger

A RAM ring-buffer logger captures key system variables for offline analysis
during bench testing.  No flash writes — avoids wear and keeps the main loop
fast.

**How it works:**

1. `BenchLogger.snapshot()` is called at `BENCH_LOG_PERIOD_MS` (default 500 ms,
   ~2 Hz) from step 12 of the main loop.
2. Each record is a tuple of 11 values (timestamp + 10 state fields) stored in a
   fixed-size circular buffer (`BENCH_LOG_MAX_RECORDS` = 2000, ~160 KB).
3. When the buffer is full, the oldest record is silently overwritten.
4. Pressing the **soft reset button** (GP8) automatically dumps the entire
   buffer as CSV to the serial console, then clears it.
5. The `dump()` method can also be called manually from the REPL.

**Logged fields:**

| # | Field | Source |
|---|---|---|
| 0 | `tick_ms` | `ticks_ms()` at capture time |
| 1 | `system_state` | OFF / PRECHARGE / COAST / ASSIST / REGEN / FAULT |
| 2 | `cap_voltage_v` | Supercap bus voltage from VESC telemetry |
| 3 | `wheel_speed_rpm` | Fork-mounted hall sensor |
| 4 | `vesc_mech_rpm` | VESC back-EMF mechanical RPM |
| 5 | `requested_mode` | NEUTRAL / ASSIST / REGEN |
| 6 | `requested_level` | 0.0–1.0 throttle fraction or regen authority |
| 7 | `assist_command_request` | Assist current command (A) |
| 8 | `regen_command_request` | Regen brake current command (A) |
| 9 | `gear_carrier_speed_rpm` | Estimated planetary carrier RPM |
| 10 | `regen_speed_error_rpm` | PI slip error (target − actual) |

**Capture window:** 2000 records ÷ 2 Hz = ~16.7 minutes of continuous data.

**Serial capture (PC side):**  
Connect via USB serial (115200 baud), press the reset button, and pipe the CSV
output into a file.  Example with PuTTY logging or `miniterm`:

```
python -m serial.tools.miniterm COM3 115200 --raw > bench_log.csv
```

---

## 11. Project Layout

```
boot.py              # MicroPython boot stub
main.py              # Entry point — wires components, runs scheduler
core.py              # Enums (SystemState, FaultCode, CommandMode),
                     #   FaultManager, SharedState
utils.py             # clamp, linear_map, SlewLimiter, PeriodicTimer, Logger

config/
  settings.py        # All hardware pins, voltage/current limits, tuning constants

drivers/
  throttle.py        # ADC read → deadband → normalized fraction
  wheel_speed_hall.py  # Hall edge timing → RPM with debounce/timeout
  gpio_io.py         # ResetButton (edge-detect soft reset)
  lcd_driver.py      # RG1602A HD44780 4-bit parallel GPIO driver

services/
  input_manager.py      # Reads throttle + wheel, decides ASSIST/REGEN/NEUTRAL
  control_loop.py       # Assist current mapping + regen PI slip controller
  vesc_protocol.py      # VESC UART packet framing, CRC, command builders, parsing
  vesc_comm.py          # UARTPort + VESCComm (telemetry) + CommandManager (TX gate)
  safety_supervisor.py  # Overvoltage, undervoltage, timeout, throttle checks
  display_manager.py    # LCD page rendering (run/precharge/fault pages)
  bench_logger.py       # RAM ring-buffer data logger for bench debugging

app/
  controller.py      # Orchestrator — sequences services via PeriodicTimers
  state_machine.py   # State transitions: OFF→PRECHARGE→COAST→ASSIST/REGEN, FAULT

tests/               # pytest suite (263 tests)

scripts/
  test_lcd.py              # LCD display test (backlight + text)
  test_uart_loopback.py    # UART self-test (GP0→GP1 jumper)
  test_vesc_fw_version.py  # Read VESC firmware version over UART
  test_vesc_telemetry.py   # Read VESC telemetry (voltage, temp, fault)
```

---

## 12. Setup and Deployment

### 12.0 Prerequisites

| Requirement | Version | Purpose |
|---|---|---|
| Python | 3.10+ | Running the pytest test suite on your PC |
| Git | any | Version control |
| mpremote | any | Uploading files and running scripts on the Pico |
| VESC Tool | 3.01+ | Configuring the FSESC motor controller |

**Linux serial permissions:**
The Pico appears as `/dev/ttyACM0`.  Your user must be in the `dialout`
group or you'll get "permission denied" when connecting with mpremote.

```bash
sudo usermod -aG dialout $USER
```

Log out and back in (or reboot) for the group change to take effect.

### 12.0.1 Development Environment (PC)

Set up a virtual environment for running the test suite locally:

```bash
cd regenx-brake-assist-controller
python3 -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows
pip install pytest mpremote
```

Run the test suite:

```bash
python -m pytest tests/ -q
```

All 263 tests should pass.  The test suite uses mock hardware
(see `tests/conftest.py`) and does not require a connected Pico.

### 12.1 Flash MicroPython

1. Unplug the Pico from USB.
2. Hold the **BOOTSEL** button on the Pico.
3. While holding BOOTSEL, plug the Pico into USB.
4. Release BOOTSEL — the Pico mounts as a USB drive called **RPI-RP2**.
5. Download the MicroPython UF2 firmware from
   https://micropython.org/download/RPI_PICO/ (choose the latest stable `.uf2`).
6. Copy the `.uf2` file onto the RPI-RP2 drive:
   ```bash
   cp RPI_PICO-<version>.uf2 /media/$USER/RPI-RP2/
   ```
7. The Pico reboots automatically and reappears as `/dev/ttyACM0` (Linux)
   or a COM port (Windows).

### 12.2 Upload Project Files

Install `mpremote` (the official MicroPython remote tool):

```bash
pip install mpremote
```

Create directories and copy project files to the Pico:

```bash
mpremote connect /dev/ttyACM0 fs mkdir :config
mpremote connect /dev/ttyACM0 fs mkdir :drivers
mpremote connect /dev/ttyACM0 fs cp config/__init__.py :config/__init__.py
mpremote connect /dev/ttyACM0 fs cp config/settings.py :config/settings.py
mpremote connect /dev/ttyACM0 fs cp drivers/__init__.py :drivers/__init__.py
mpremote connect /dev/ttyACM0 fs cp drivers/lcd_driver.py :drivers/lcd_driver.py
```

Repeat for all remaining project directories and files (`services/`, `app/`,
`core.py`, `utils.py`, `main.py`, `boot.py`).

### 12.3 Hardware Test Scripts

Test scripts live in `scripts/` and are run on the Pico via `mpremote`.
Upload the required project files first (§12.2).  Each script prints
`PASS` or `FAIL` with details.

#### 12.3.1 LCD Test

**Wiring:** LCD connected per §8.4 pin map.

```bash
mpremote connect /dev/ttyACM0 run scripts/test_lcd.py
```

**Expected result:**
- Terminal prints `LCD test complete`
- Backlight turns on (GP28)
- Line 1: `Hello ReGenX!`
- Line 2: `LCD test OK`

**Troubleshooting:**
- Backlight off → check LCD pin 15 wiring to GP28 and pin 16 to GND.
- Backlight on but no text → V0 not connected to GND, or data pins miswired.
- Garbled text → check D4–D7 pin order (GP19=D4, GP20=D5, GP21=D6, GP22=D7).

#### 12.3.2 UART Loopback Test

**Wiring:** Connect GP0 (TX, pin 1) directly to GP1 (RX, pin 2) with a
jumper wire.  No VESC needed.

```bash
mpremote connect /dev/ttyACM0 run scripts/test_uart_loopback.py
```

**Expected result:**
```
PASS: received b'hello'
```

Remove the jumper wire before proceeding to VESC tests.

#### 12.3.3 VESC Firmware Version

**Wiring:** Pico GP0 (TX) → VESC RX, Pico GP1 (RX) → VESC TX,
Pico GND → VESC GND.  VESC must be powered.  Motor may be disconnected.

```bash
mpremote connect /dev/ttyACM0 run scripts/test_vesc_fw_version.py
```

**Expected result (example):**
```
FW Version: 5.2
Hardware: 410
PASS
```

The firmware version and hardware name will vary by VESC board and firmware.
Any response confirms UART communication is working.

#### 12.3.4 VESC Telemetry

**Wiring:** Same as §12.3.3.  Motor may be disconnected.

```bash
mpremote connect /dev/ttyACM0 run scripts/test_vesc_telemetry.py
```

**Expected result (example, motor disconnected):**
```
--- VESC Telemetry ---
FET temp:       25.1 C
Motor temp:     -72.6 C
Motor current:  -0.38 A
Input current:  0.00 A
Duty cycle:     0.0 %
ERPM:           0
Bus voltage:    17.4 V
Ah drawn:       0.0000
Ah charged:     0.0000
Wh drawn:       0.0000
Wh charged:     0.0000
Tachometer:     2
Tach (abs):     2
Fault code:     0
PASS
```

**Notes:**
- Bus voltage reflects the supercapacitor bank / power supply voltage.
- Motor temp reads ~-72°C when no temperature sensor is connected (normal).
- Motor current may show small offset noise (~±0.5 A) with no motor (normal).
- Fault code 0 = no fault.

### 12.4 VESC Tool Configuration

VESC Tool is the official desktop app for configuring the FSESC4.20.
Download it from https://vesc-project.com/vesc_tool (free version works).

Connect the FSESC to your computer via USB (the FSESC has its own USB port,
separate from the Pico).  Open VESC Tool and click **Connect** (top-left).

#### 12.4.1 Motor Detection (do this first)

Go to **Motor Settings → FOC → General**.

1. Click **Detect and Calculate** (the wizard icon or RL button).
2. Enter a small detection current (2–5 A is safe for bench testing).
3. Set the motor pole pair count: the Puyan H01 has **15 pole pairs** (30
   magnets).  If unsure, the wizard will measure this — verify it matches.
4. Click **Apply** and then the **Write Motor Configuration** button (the
   down-arrow icon in the toolbar) to save to the VESC.

This writes the motor resistance, inductance, and flux linkage values.
The VESC needs these for FOC (Field-Oriented Control) to work correctly.

> **Important:** If detection fails or the motor stutters, double-check
> phase wire connections.  All 3 motor phase wires must be connected.

#### 12.4.2 Current Limits

Go to **Motor Settings → General → Current**.

| Setting | Value | Notes |
|---|---|---|
| Motor Current Max | 40.0 A | Must match `ASSIST_CURRENT_LIMIT_A` in settings.py |
| Motor Current Max Brake | 40.0 A | Must match `REGEN_CURRENT_LIMIT_A` in settings.py |
| Absolute Maximum Current | 50.0 A | FSESC4.20 hardware limit — leave as default |
| Battery Current Max | 40.0 A | Max current drawn from the supercap bank |
| Battery Current Max Regen | 40.0 A | Max regen charging current into the supercap bank |

Click **Write Motor Configuration** to save.

> **Safety:** The VESC enforces its own current limits independently of the
> Pico firmware.  Always set VESC limits **equal to or lower than** the
> firmware limits.  The VESC limits are the hardware safety net.

#### 12.4.3 Voltage Limits

Go to **Motor Settings → General → Voltage**.

| Setting | Value | Notes |
|---|---|---|
| Minimum Input Voltage | 14.0 V | VESC shuts down below this (protects supercaps) |
| Maximum Input Voltage | 42.0 V | Must not exceed supercap bank absolute max |

Click **Write Motor Configuration** to save.

> These limits protect the supercapacitor bank.  The Pico firmware has its
> own software limits (`VCAP_MIN_OPERATING` = 15 V, `VCAP_ABSOLUTE_MAX` = 42 V)
> but the VESC hardware limits are the last line of defense.

#### 12.4.4 Enable UART App

Go to **App Settings → General**.

1. Set **App to Use** to **UART**.  This tells the VESC to listen for
   commands on its UART pins instead of PPM/ADC/NRF.
2. Go to **App Settings → UART** and confirm:
   - **Baud Rate:** 115200 (must match `VESC_BAUD_RATE` in settings.py)
3. Click **Write App Configuration** to save.

> **Without this step**, the VESC ignores all UART commands from the Pico.
> This is the most commonly missed setting.

#### 12.4.5 Verify Configuration

After writing both motor and app configs:

1. Power-cycle the VESC (disconnect and reconnect power).
2. Reconnect in VESC Tool and go to **Realtime Data**.
3. You should see live voltage, temperature, and fault status.
4. If the Pico is connected via UART, run the telemetry test script
   (§12.3.4) to confirm the Pico can also read these values.

### 12.5 VESC Configuration via Pico UART

The VESC can also be configured directly from the Pico over UART, without
needing VESC Tool or a USB connection to a PC.  Three scripts in `scripts/`
handle reading, writing, and storing the motor controller configuration.

#### 12.5.1 Read Current Configuration

```bash
mpremote run scripts/test_vesc_read_config.py
```

This reads the full MCCONF binary blob from the VESC and decodes key fields:

```
--- Motor Configuration (MCCONF) ---
  Config signature: 0x83D6207A

  [Motor Config]
  Motor type:     FOC
  Sensor mode:    Sensorless

  [Current Limits]
  Motor max:      40.0 A
  Motor min:      -40.0 A
  Battery max:    40.0 A
  Battery min:    -40.0 A
  Absolute max:   130.0 A

  [Voltage Limits]
  Min input V:    15.0 V
  Max input V:    42.0 V
  Batt cut start: 15.0 V
  Batt cut end:   14.0 V
  ...
```

Use this to verify the current VESC settings at any time.

#### 12.5.2 Write Configuration (RAM Only)

```bash
mpremote run scripts/vesc_write_config.py
```

This script:
1. Reads the current MCCONF from the VESC
2. Shows a diff of current vs. target values (marked with `***`)
3. Patches the binary blob at known field offsets
4. Sends the patched config with `COMM_SET_MCCONF` (command 13)
5. Re-reads the config to verify all changes took effect

**The changes are applied to RAM only** — they take effect immediately but
are lost if the VESC is power-cycled.  This is intentional: you can test
the new settings risk-free, and power-cycle to revert if anything is wrong.

The target values are defined in the `PATCHES` list inside the script:

| Field | Target | Offset | Type |
|---|---|---|---|
| Motor type | FOC (2) | 6 | u8 |
| Motor max current | 40.0 A | 8 | f32 |
| Motor min current | -40.0 A | 12 | f32 |
| Battery max current | 40.0 A | 16 | f32 |
| Battery min current | -40.0 A | 20 | f32 |
| Min input voltage | 15.0 V | 48 | f32 |
| Max input voltage | 42.0 V | 52 | f32 |
| Battery cutoff start | 15.0 V | 56 | f32 |
| Battery cutoff end | 14.0 V | 60 | f32 |
| Max watts | 500 W | 93 | f32 |
| Min watts (regen) | -500 W | 97 | f32 |

To change a target value, edit the `PATCHES` list in the script and re-run.

#### 12.5.3 Store to Flash (Permanent)

```bash
mpremote run scripts/vesc_store_config.py
```

**Only run this after verifying the RAM config is correct** (§12.5.2).

This script:
1. Reads the RAM config and verifies all expected values match
2. Sends `COMM_STORE_MCCONF` (command 15) to write to flash
3. Re-reads to confirm the flash write succeeded

After storing, the config persists across VESC power cycles.

> **Reverting:** If you need to undo a flash write, either run the write
> script with the old values, or use VESC Tool to restore defaults.

#### 12.5.4 Protocol Details

The VESC UART protocol uses framed packets with CRC-16:

| Frame type | Start byte | Length field | Max payload |
|---|---|---|---|
| Short | `0x02` | 1 byte | 255 bytes |
| Long | `0x03` | 2 bytes (big-endian) | 65535 bytes |

MCCONF is ~458 bytes, so it uses long frames.  The MicroPython UART
`rxbuf` must be set to at least 1024 bytes (default 256 is insufficient).

Relevant VESC commands:

| Command | ID | Direction | Description |
|---|---|---|---|
| `COMM_SET_MCCONF` | 13 | Pico → VESC | Apply motor config to RAM |
| `COMM_GET_MCCONF` | 14 | Pico → VESC | Read motor config |
| `COMM_STORE_MCCONF` | 15 | Pico → VESC | Save RAM config to flash |

Config data is big-endian, with `float32` (IEEE 754), `int32`, and `uint8`
fields.  Field offsets depend on firmware version (currently tested with FW 5.2).

#### 12.5.5 Keep a Full Backup on the Pico (Recommended)

If you always use the same motor+VESC combination, store a full MCCONF backup
file on the Pico so you can quickly restore after a VESC reflash.

Save backup to Pico filesystem:

```bash
mpremote run scripts/vesc_backup_save_to_pico.py
```

This creates `/vesc_mcconf_backup.bin` on the Pico and stores:
- Magic (`VMCF`)
- Backup format version
- MCCONF payload length
- CRC16 of MCCONF payload
- Full MCCONF binary payload

Restore from Pico backup to VESC RAM+flash:

```bash
mpremote run scripts/vesc_backup_restore_from_pico.py
```

The restore script validates magic/version/length/CRC before writing, then:
1. Sends `COMM_SET_MCCONF` (apply to RAM)
2. Sends `COMM_STORE_MCCONF` (save to flash)
3. Reads back MCCONF and checks it matches the backup exactly

Quick check that backup file exists on Pico:

```bash
mpremote fs ls
```

> Keep this backup updated whenever you intentionally retune VESC settings.
> Re-run `vesc_backup_save_to_pico.py` after tuning changes are finalized.

### 12.6 Full Deployment

1. Confirm hardware pin mapping in `config/settings.py`.
2. Complete VESC Tool configuration (§12.4).
3. Verify UART communication with test scripts (§12.3).
4. Power cycle or reset the board; `boot.py` then `main.py` run automatically.

---

## 13. Notes

- Capacitor bus voltage is sourced from VESC telemetry (`cap_voltage_v` mirrors
  VESC bus voltage).
- The VESC reports motor ERPM from back-EMF even with zero commanded current,
  which is what enables passive carrier-lock detection.
- If using wheel-speed-based regen, set `WHEEL_HALL_PIN` and verify
  `WHEEL_MAGNET_COUNT` for your hub.
- For practical tuning, start with conservative PI gains and current limits
  before road testing.
