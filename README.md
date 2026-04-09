# ReGenX Brake-Assist Controller

MicroPython firmware for a Raspberry Pi Pico supervising a VESC-based
assist/regen ebike drivetrain with supercapacitor energy storage.

---

## 1. System Overview

The controller manages a geared hub motor ebike drivetrain where the Pico acts
as the outer application controller and a Flipsky Mini FSESC4.20 (VESC 4.12
hardware design, running FW 6.6) acts as the inner current/power-electronics
controller.  Energy is stored in a supercapacitor bank rather than a battery.

| Component | Role |
|---|---|
| Raspberry Pi Pico (RP2040) | Application controller — reads sensors, runs state machine, computes current commands |
| FSESC4.20 (VESC 4.12 hw, FW 6.6) | Inner loop — FOC motor control, current regulation, telemetry reporting |
| Puyan H01 geared hub motor | 250–350 W class, 11 pole pairs, planetary gearbox with one-way freewheel clutch |
| Supercapacitor bank (20 F) | Energy storage — charges via regen braking, discharges during assist |
| Hall throttle | 3-wire analog (0.8–4.2 V typical), read on Pico ADC0 |
| Wheel speed hall sensor | Fork-mounted, 3 spoke magnets, digital pulse input on GP9 |
| RG1602A 16×2 LCD | Status display via 4-bit parallel GPIO (RS, E, D4–D7) |

Communication between the Pico and VESC is over UART at 115200 baud.  Both
devices use 3.3 V logic, so no level-shifter is needed.

---

## 2. Riding Modes — Assist, Neutral, and Regen

The drivetrain has three distinct riding states, determined entirely from
sensor data with no brake-lever switch required.

### 2.1 ASSIST (throttle applied)

When the rider applies the throttle, the firmware maps the throttle position
(0–100%) to a motor current command (0–40 A) and sends it to the VESC.
The VESC's inner FOC loop handles actual current ramping and torque control.

### 2.2 NEUTRAL (throttle off, no brake)

When the rider releases the throttle and is not braking, the one-way freewheel
clutch on the planetary carrier disengages.  The wheel spins freely and the
motor sits nearly still.  No current is commanded — true zero-drag coast.

The system enters the NEUTRAL command mode.  The state machine remains in
REGEN (the default running state), but the control loop commands zero current.

### 2.3 REGEN (throttle off, mechanical brake applied)

When the rider squeezes the mechanical brake, the planetary carrier locks.
This forces the wheel to drive the motor through the gear train.  The motor
now spins at approximately `wheel_rpm × 5.0` (the gear ratio).

The firmware detects regen conditions using motor RPM alone (reported by the
VESC from back-EMF sensing, even with zero commanded current).  The wheel
speed sensor is **not** used in the regen control path — it is retained only
for the LCD speed display.

- **Carrier locked (braking):** motor RPM above entry threshold (30 RPM)
- **Carrier free (coasting):** motor RPM near zero

Once in REGEN, an integral controller tracks the ratio of actual motor
current to commanded current:

1. On entry, immediately command `REGEN_COMMAND_MAX_A` (30 A).
2. Each cycle, compute the ratio `actual / commanded` and compare it
   against `REGEN_TARGET_RATIO` (0.7).
3. When ratio < target, the carrier is slipping — the integral ramps the
   command down.  When ratio > target, the motor is absorbing well — the
   integral ramps the command back up.
4. This continuously tracks rider brake pressure in both directions.
   Starting at max ensures the carrier never freezes on entry.
5. When the rider releases the brake, motor RPM drops below the exit
   threshold and regen exits naturally.

Regen is disabled entirely when the cap voltage reaches the soft cutoff
(40.0 V) to prevent overcharging.

### 2.4 Motor-RPM Regen Detection with Hysteresis

The transition from NEUTRAL to REGEN uses RPM thresholds with hysteresis
and a holdoff timer to prevent false triggers from motor inertia after
assist:

| Condition | Threshold |
|---|---|
| Enter REGEN | `abs(motor_rpm)` ≥ 30 RPM AND throttle-off holdoff (300 ms) expired |
| Stay in REGEN | `abs(motor_rpm)` ≥ 15 RPM (exit threshold) |
| Exit REGEN | `abs(motor_rpm)` < 15 RPM |

The holdoff timer starts when the throttle is released and prevents regen
from triggering while the motor coasts down from assist.  If the throttle is
reapplied and released again, the holdoff resets.  Applying the throttle at
any time overrides to ASSIST.

### 2.5 Why No Brake Switch?

The VESC continuously tracks rotor position from back-EMF, even when no
current is commanded.  It reports electrical RPM in every telemetry packet
(~40 Hz).  Dividing by the 11 pole pairs gives mechanical RPM.  This
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
| 0 | ResetButton | continuous | Check soft reset button (GP8) |
| 1 | InputManager | 10 ms | Read throttle + wheel hall, decide ASSIST/REGEN/NEUTRAL |
| 2 | VESCComm (RX) | continuous | Parse incoming VESC telemetry packets |
| 3 | VESCComm (TX) | 25 ms | Request telemetry from VESC |
| 4 | SafetySupervisor | 10 ms | Check voltage limits, telemetry freshness, throttle validity |
| 5 | StateMachine | 10 ms | Gate mode transitions with safety checks |
| 6 | ControlLoop | 10 ms | Compute assist/regen current commands (integral controller) |
| 7 | CommandManager | 20 ms | Transmit current commands to VESC over UART |
| 8 | Display + Energy | 200 ms | Compute ½CV² energy, update 16×2 LCD |
| 9 | BenchLogger | 500 ms | RAM ring-buffer data capture |

### 3.2 Data Flow

```
Throttle ADC ──────► InputManager ──► requested_mode + requested_level
                          │
                          ▼
VESC Telemetry ──► VESCComm ──► SharedState ◄── SafetySupervisor
                                    │                   │
Wheel Hall ──► (optional LCD) ──────┘                   │
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
| PRECHARGE | Waiting for cap voltage ≥ 15.0 V | Inhibited |
| REGEN | Default running state — no rider request or regen active | Active (regen when braking, zero otherwise) |
| ASSIST | Rider requesting forward power | Active (assist) |
| FAULT | One or more faults latched | Inhibited |

Transitions:

```
OFF → PRECHARGE → REGEN ⇄ ASSIST
              Any state → FAULT    (when faults present)
                  FAULT → REGEN    (when all faults clear)
```

---

## 5. Regen Integral Controller

When in REGEN state, the ControlLoop uses an integral controller that
tracks the ratio of actual motor current to commanded current.  It relies
only on VESC telemetry (motor current) — no wheel speed is needed:

1. **On REGEN entry**, set the internal target to `REGEN_COMMAND_MAX_A`
   (30 A).  Starting at max ensures the carrier never freezes.

2. **Each cycle**, compute `ratio = actual / commanded` and compare to
   `REGEN_TARGET_RATIO` (0.7).  The error drives an integral:
   `target += REGEN_KI_A_PER_S × error × dt`

3. **If ratio < target** (carrier slipping, excess heat), the integral
   ramps the command down.

4. **If ratio > target** (motor absorbing well), the integral ramps
   the command back up.

5. **Hard clamp** at `REGEN_COMMAND_MAX_A` on top, 0 on bottom.

6. **On REGEN exit** (motor RPM drops below exit threshold), the regen
   target is reset to zero.

Regen preconditions (any failure → command goes to zero):
- Cap voltage < 40.0 V (soft cutoff)
- System not inhibited

---

## 6. Safety and Fault Handling

The SafetySupervisor runs at 100 Hz and checks:

| Fault | Trigger | Latching? |
|---|---|---|
| OVERVOLTAGE | Cap voltage ≥ VCAP_ABSOLUTE_MAX (42.0 V) | Yes |
| VESC_TIMEOUT | No valid telemetry for 500 ms | No |
| VESC_FAULT | VESC reports non-zero fault code | No |
| THROTTLE_RANGE | ADC below 100 or above 4000 (open/short circuit) | No |
| INTERNAL | Uncaught exception in main loop | Yes |

Latching faults (OVERVOLTAGE, INTERNAL) require a soft reset button press
or power cycle to clear.  Non-latching faults auto-clear when the condition
resolves.

When any fault is active, the state machine forces FAULT state and
`inhibit_motor_commands = True`, which zeros all current commands and resets
all dynamic controller state (regen integral target).

---

## 7. Precharge

The supercapacitor bank must reach `VCAP_MIN_OPERATING` (15.0 V) before the
VESC can safely drive the motor.  The firmware handles this with a simple
state gate: the state machine stays in PRECHARGE until telemetry reports
cap voltage ≥ 15.0 V, then transitions to REGEN.

The precharge hardware (how the caps get from 0 V to 15 V) is external to
the firmware — it may be a resistor from a bench supply, a dedicated
precharge relay circuit, or manual charging.  The firmware does not control
any precharge relay or boost converter.

---

## 8. Hardware Configuration

### 8.1 Pin Map

| Pico Pin | GPIO | Function |
|---|---|---|
| 6 | GP4 | UART1 TX → VESC RX |
| 7 | GP5 | UART1 RX ← VESC TX |
| 11 | GP8 | Soft reset button (active-low, internal pull-up) |
| 12 | GP9 | Wheel speed hall sensor (digital input) |
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

Fork-mounted hall sensor with 3 spoke magnets.

| Parameter | Value |
|---|---|
| GPIO | GP9 (digital input with internal pull-up) |
| Magnets | 3 per revolution |
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
| MOTOR_CURRENT_MAX_A | 40.0 A | Shared max motor current for assist and regen |

### Regen Tuning

| Constant | Value | Description |
|---|---|---|
| REGEN_ENTRY_RPM | 30.0 | Motor RPM threshold to enter regen |
| REGEN_EXIT_RPM | 15.0 | Motor RPM threshold to exit regen (hysteresis) |
| REGEN_HOLDOFF_MS | 300 | Delay after throttle release before regen can trigger |
| REGEN_COMMAND_MAX_A | 30.0 | Initial (maximum) regen brake current command |
| REGEN_TARGET_RATIO | 0.7 | Desired actual/commanded current ratio (integral setpoint) |
| REGEN_KI_A_PER_S | 25.0 | Integral gain — amps per second per unit ratio error |

### Motor

| Constant | Value | Description |
|---|---|---|
| VESC_MOTOR_POLE_PAIRS | 11 | Puyan H01 geared hub motor |
| VESC_BAUD_RATE | 115200 | UART communication speed |

---

## 10. Bench Debug Logger

A RAM ring-buffer logger captures key system variables for offline analysis
during bench testing.  No flash writes — avoids wear and keeps the main loop
fast.

**How it works:**

1. `BenchLogger.snapshot()` is called at `BENCH_LOG_PERIOD_MS` (default 500 ms,
   ~2 Hz) from step 9 of the main loop.
2. Each record is a tuple of 9 values (timestamp + 8 state fields) stored in a
   fixed-size circular buffer (`BENCH_LOG_MAX_RECORDS` = 2000, ~144 KB).
3. When the buffer is full, the oldest record is silently overwritten.
4. Pressing the **soft reset button** (GP8) automatically dumps the entire
   buffer as CSV to the serial console, then clears it.
5. The `dump()` method can also be called manually from the REPL.

**Logged fields:**

| # | Field | Source |
|---|---|---|
| 0 | `tick_ms` | `ticks_ms()` at capture time |
| 1 | `system_state` | OFF / PRECHARGE / ASSIST / REGEN / FAULT |
| 2 | `cap_voltage_v` | Supercap bus voltage from VESC telemetry |
| 3 | `vesc_mech_rpm` | VESC back-EMF mechanical RPM |
| 4 | `vesc_motor_current_a` | VESC actual motor current (A) |
| 5 | `requested_mode` | NEUTRAL / ASSIST / REGEN |
| 6 | `requested_level` | 0.0–1.0 throttle fraction or regen authority |
| 7 | `assist_command_request` | Assist current command (A) |
| 8 | `regen_command_request` | Regen brake current command (A) |

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
utils.py             # clamp, linear_map, PeriodicTimer, Logger

config/
  settings.py        # All hardware pins, voltage/current limits, tuning constants

drivers/
  throttle.py        # ADC read → deadband → normalized fraction
  wheel_speed_hall.py  # Hall edge timing → RPM with debounce/timeout
  gpio_io.py         # ResetButton (edge-detect soft reset)
  lcd_driver.py      # RG1602A HD44780 4-bit parallel GPIO driver

services/
  input_manager.py      # Reads throttle + wheel, decides ASSIST/REGEN/NEUTRAL
  control_loop.py       # Assist current mapping + regen integral controller
  vesc_protocol.py      # VESC UART packet framing, CRC, command builders, parsing
  vesc_comm.py          # UARTPort + VESCComm (telemetry) + CommandManager (TX gate)
  safety_supervisor.py  # Overvoltage, timeout, throttle, VESC fault checks
  display_manager.py    # LCD page rendering (run/precharge/fault pages)
  bench_logger.py       # RAM ring-buffer data logger for bench debugging

app/
  controller.py      # Orchestrator — sequences services via PeriodicTimers
  state_machine.py   # State transitions: OFF→PRECHARGE→REGEN/ASSIST, FAULT

tests/               # pytest suite (243 tests)

scripts/
   test_system_check.py         # Main integrated hardware check
   test_throttle_characterize.py # Throttle ADC characterization
   test_vesc_fw_version.py      # Quick VESC UART connectivity check
   test_vesc_telemetry.py       # VESC telemetry snapshot
   vesc_apply_safety_temp.py    # Apply supported Pico-side safety envelope persistence
   vesc_characterize_motor.py   # Terminal-assisted dc-cal + FOC characterization
   vesc_save_snapshot.py        # Save final live VESC snapshot from Pico side
   vesc_flash_config.py         # Guard script: persistent UART MCCONF flashing disabled
   bench/                       # Low-level or infrequent diagnostics
      test_uart_loopback.py
      test_uart_pins_gpio.py
      test_vesc_read_config.py
      vesc_backup_save_to_pico.py
   legacy/                      # Older one-off demos kept for reference
      test_lcd.py
      test_lcd_pages.py
      test_lcd_throttle_percent.py
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

All 243 tests should pass.  The test suite uses mock hardware
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

The supported day-to-day scripts stay at the top level of `scripts/`.
Low-level diagnostics live in `scripts/bench/`, and older one-off demos live
in `scripts/legacy/`.  Upload the required project files first (§12.2).
Each script prints `PASS` or `FAIL` with details.

#### 12.3.1 LCD Test

**Wiring:** LCD connected per §8.4 pin map.

```bash
mpremote connect /dev/ttyACM0 run scripts/legacy/test_lcd.py
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

**Wiring:** Connect GP4 (TX, pin 6) directly to GP5 (RX, pin 7) with a
jumper wire.  No VESC needed.

```bash
mpremote connect /dev/ttyACM0 run scripts/bench/test_uart_loopback.py
```

**Expected result:**
```
PASS: received b'hello'
```

Remove the jumper wire before proceeding to VESC tests.

#### 12.3.3 VESC Firmware Version

**Wiring:** Pico GP4 (TX) → VESC RX, Pico GP5 (RX) → VESC TX,
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
3. Set the motor pole pair count: the Puyan H01 has **11 pole pairs** (22
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
| Motor Current Max | 40.0 A | Must match `MOTOR_CURRENT_MAX_A` in settings.py |
| Motor Current Max Brake | 40.0 A | Must match `MOTOR_CURRENT_MAX_A` in settings.py |
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
needing VESC Tool or a USB connection to a PC.  The supported write/store
workflow stays at the top level of `scripts/`.  The deeper inspection script
for reading and decoding the full config now lives in `scripts/bench/`.

#### 12.5.1 Read Current Configuration

```bash
mpremote run scripts/bench/test_vesc_read_config.py
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

#### 12.5.2 Shared VESC Config

The VESC scripts now share a single config file:

- Shared repo module: `config/vesc_config.py`

Run the VESC scripts with a mounted repo so the Pico can import that shared
Python module at runtime.

#### 12.5.3 Prepare And Characterize Motor

```bash
mpremote mount . run scripts/vesc_characterize_motor.py
```

This script:
1. Confirms UART communication by reading the VESC firmware version
2. Reads the live MCCONF and derives a conservative temporary runtime envelope
3. Applies that envelope with `COMM_SET_MCCONF_TEMP`
4. Verifies the active temporary limits with `COMM_GET_MCCONF_TEMP`
5. Runs the VESC built-in FOC commissioning command over UART
6. Confirms the VESC still responds after detection

It does not write any characterization data back to the Pico or into the repo.
The shared module provides both the temporary runtime envelope and the
commissioning input values.

Use it for:
- controllers with unknown prior settings
- new controllers you want to constrain safely before detection
- motor resistance / inductance / flux characterization
- hall or encoder detection
- initial motor setup after rewiring or controller replacement

Run it with the driven wheel off the ground and the motor unloaded.

If characterization fails, the temporary runtime envelope remains active until
the VESC is power-cycled or another tool changes it.

#### 12.5.4 Store Safety Envelope From Pico

```bash
mpremote mount . run scripts/vesc_apply_safety_temp.py
```

This script uses only supported firmware packet paths from the Pico side:
1. Applies the current/duty/ERPM/watt safety envelope with `COMM_SET_MCCONF_TEMP`
2. Sets the battery cutoff start/end with `COMM_SET_BATTERY_CUT`
3. Requests persistent storage for those fields on the VESC
4. Verifies read-back with `COMM_GET_MCCONF_TEMP` and `COMM_GET_BATTERY_CUT`
5. Audits the live MCCONF for unexpected serializer-level changes

On tested FW 6.6 this is the safe direct-from-Pico persistence path for:
- motor current envelope scaling
- battery current limits
- ERPM limits
- duty limits
- watt limits
- battery cutoff start/end

It does not write the full serialized MCCONF blob.

Known limitation:
- `l_min_vin` and `l_max_vin` do not have a dedicated safe UART packet in this workflow
- if those two fields are already correct on the VESC, the rest of the safety envelope can be stored entirely from the Pico
- if those two fields need to change, use VESC Tool once, then continue using the Pico workflow for the supported fields

#### 12.5.5 Flash ReGenX Configuration

```bash
mpremote mount . run scripts/vesc_flash_config.py
```

Persistent UART flashing is intentionally disabled. The previous version of
this script patched guessed byte offsets inside the serialized MCCONF blob and
sent them with `COMM_SET_MCCONF`, which is not safe on all firmware versions.

Use `scripts/vesc_apply_safety_temp.py` for supported Pico-side persistence.
Use VESC Tool only when you need to change unsupported serialized-MCCONF
fields such as `l_min_vin` or `l_max_vin`.

To change the safe Pico-side characterization behavior, edit
`VESC_TEMP_LIMITS` or `VESC_CHARACTERIZATION` in `config/vesc_config.py` and
re-run the characterization script.

Recommended order with the current safe workflow:

1. `mpremote mount . run scripts/vesc_characterize_motor.py`
2. `mpremote mount . run scripts/vesc_apply_safety_temp.py`
3. `mpremote run scripts/bench/test_vesc_read_config.py`
4. `mpremote run scripts/vesc_save_snapshot.py`

#### 12.5.6 Save Final Snapshot

```bash
mpremote run scripts/vesc_save_snapshot.py
```

This script captures the final live VESC state after preparation and flashing.
It saves these files on the Pico:

- `vesc_snapshot_mcconf.bin`
- `vesc_snapshot_meta.txt`
- `vesc_snapshot_appconf.bin` when APPCONF read succeeds

At the end it prints the exact `mpremote fs cp` commands needed to copy those
files into the repo `config/` directory.

#### 12.5.7 Get And Set MCCONF

Use these repo scripts when you want to read or write the full VESC motor
configuration (`MCCONF`) over UART without using VESC Tool.

Get the current live MCCONF from the VESC:

1. Read and save the final live config snapshot:

```bash
mpremote run scripts/vesc_save_snapshot.py
```

2. Copy the saved files from the Pico into the repo:

```bash
mpremote fs cp :vesc_snapshot_mcconf.bin config/vesc_snapshot_mcconf.bin
mpremote fs cp :vesc_snapshot_meta.txt config/vesc_snapshot_meta.txt
```

3. If APPCONF was captured too, copy it as well:

```bash
mpremote fs cp :vesc_snapshot_appconf.bin config/vesc_snapshot_appconf.bin
```

Set MCCONF on the VESC from this repo workflow:

1. Run safe temporary-limit characterization and motor detection:

```bash
mpremote mount . run scripts/vesc_characterize_motor.py
```

2. Store the final persistent configuration with VESC Tool over USB.

   Preferred path on FW 6.6 for supported safety fields:

```bash
mpremote mount . run scripts/vesc_apply_safety_temp.py
```

   Use VESC Tool only when unsupported serialized fields such as
   `l_min_vin` / `l_max_vin` need to change.

3. Verify the final result and save the full live MCCONF back into the repo:

```bash
mpremote run scripts/vesc_save_snapshot.py
```

How the repo uses VESC MCCONF commands:

- `scripts/vesc_characterize_motor.py`
   - reads live config with `COMM_GET_MCCONF`
   - applies temporary limits with `COMM_SET_MCCONF_TEMP`
   - verifies temporary limits with `COMM_GET_MCCONF_TEMP`
   - re-reads live config with `COMM_GET_MCCONF`
- `scripts/vesc_apply_safety_temp.py`
   - applies supported persistent safety fields with `COMM_SET_MCCONF_TEMP`
   - applies battery cutoff start/end with `COMM_SET_BATTERY_CUT`
   - verifies with `COMM_GET_MCCONF_TEMP` and `COMM_GET_BATTERY_CUT`
- `scripts/vesc_flash_config.py`
   - deliberately blocks the old unsafe UART flash path
- `scripts/vesc_save_snapshot.py`
   - reads live config with `COMM_GET_MCCONF`
   - saves the raw MCCONF blob for the repo

Important:

- This repo does not use raw `COMM_SET_MCCONF` writes as part of the supported workflow.
- On the tested firmware path, the direct Pico-side persistent path is limited
   to dedicated supported packets such as `COMM_SET_MCCONF_TEMP` and
   `COMM_SET_BATTERY_CUT`.
- The saved `.bin` files are raw binary config blobs, not human-edited config
   sources.

#### 12.5.8 Protocol Details

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
| `COMM_SET_MCCONF` | 13 | Pico → VESC | Apply motor config and store it |
| `COMM_GET_MCCONF` | 14 | Pico → VESC | Read motor config |
| `COMM_GET_MCCONF_DEFAULT` | 15 | Pico → VESC | Read default motor config |
| `COMM_SET_MCCONF_TEMP` | 48 | Pico → VESC | Apply temporary runtime current/duty/watt limits |
| `COMM_SET_BATTERY_CUT` | 86 | Pico → VESC | Set battery cutoff start/end |
| `COMM_GET_MCCONF_TEMP` | 91 | Pico → VESC | Read active temporary runtime limits |
| `COMM_GET_BATTERY_CUT` | 115 | Pico → VESC | Read battery cutoff start/end |

Serialized MCCONF is firmware-defined and must be handled by the VESC
serializer rather than guessed byte offsets.

#### 12.5.9 Bench-Only Backup Utilities

If you always use the same motor+VESC combination, you can store a full MCCONF
backup file on the Pico for forensic comparison after future reflashes or
retuning.

Save backup to Pico filesystem:

```bash
mpremote run scripts/bench/vesc_backup_save_to_pico.py
```

This creates `/vesc_mcconf_backup.bin` on the Pico and stores:
- Magic (`VMCF`)
- Backup format version
- MCCONF payload length
- CRC16 of MCCONF payload
- Full MCCONF binary payload

Quick check that backup file exists on Pico:

```bash
mpremote fs ls
```

> This repository no longer ships a Pico-side raw MCCONF restore path.
> Restoring or writing persistent VESC config should be done with VESC Tool,
> then verified from the Pico side with `scripts/bench/test_vesc_read_config.py`.

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
