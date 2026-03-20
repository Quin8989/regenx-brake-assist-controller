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
| Wheel speed hall sensor | Fork-mounted, 6 spoke magnets, digital pulse input on GP5 |
| 16×2 I2C LCD | Status display via PCF8574 backpack on I2C1 |

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
READY (no active motor state).

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
| 6 | PrechargeManager | 10 ms | Control precharge relay + boost, run watchdog |
| 7 | StateMachine | 10 ms | Gate mode transitions with safety checks |
| 8 | ControlLoop | 10 ms | Compute assist/regen current commands (slew + PI) |
| 9 | CommandManager | 20 ms | Transmit current commands to VESC over UART |
| 10 | DisplayManager | 200 ms | Update 16×2 LCD with status/fault info |
| 11 | Logger | 500 ms | Debug output to serial console |

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
| READY | Electrically ready, no rider request active | Inhibited |
| ASSIST | Rider requesting forward power | Active (assist) |
| REGEN | Rider braking, carrier locked | Active (regen) |
| FAULT | One or more faults latched | Inhibited |

Transitions:

```
OFF → PRECHARGE → READY ⇄ ASSIST
                  READY ⇄ REGEN
                  ASSIST ⇄ REGEN   (direct transitions allowed)
              Any state → FAULT    (when faults present)
                  FAULT → READY    (when all faults clear)
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
| UNDERVOLTAGE | Cap voltage < VCAP_MIN_OPERATING (15.0 V) while in READY+ | No |
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
| 4 | GP2 | I2C1 SDA (LCD) |
| 5 | GP3 | I2C1 SCL (LCD) |
| 6 | GP4 | (available) |
| 7 | GP5 | Wheel speed hall sensor (digital input) |
| 20 | GP15 | Precharge enable (active-high) |
| 21 | GP16 | Boost enable (active-high) |
| 31 | GP26 | ADC0 — Hall throttle |

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
| GPIO | GP5 (digital input with internal pull-up) |
| Magnets | 6 per revolution |
| Timeout | 1200 ms (wheel stopped if no edge) |
| Min edge spacing | 1500 µs (debounce) |

### 8.4 LCD

HD44780-compatible 16×2 character display via PCF8574 I2C backpack.

| Parameter | Value |
|---|---|
| I2C bus | I2C1 |
| Address | 0x27 |
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
4. Pressing the **soft reset button** (GP4) automatically dumps the entire
   buffer as CSV to the serial console, then clears it.
5. The `dump()` method can also be called manually from the REPL.

**Logged fields:**

| # | Field | Source |
|---|---|---|
| 0 | `tick_ms` | `ticks_ms()` at capture time |
| 1 | `system_state` | OFF / PRECHARGE / READY / ASSIST / REGEN / FAULT |
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
  uart_port.py       # Raw UART read/write wrapper
  precharge_io.py    # GPIO pin control for precharge + boost relays
  lcd_driver.py      # HD44780 via PCF8574 I2C backpack
  reset_button.py    # Soft reset button edge-detect driver (GP4)

services/
  input_manager.py      # Reads throttle + wheel, decides ASSIST/REGEN/NEUTRAL
  control_loop.py       # Assist current mapping + regen PI slip controller
  vesc_protocol.py      # VESC UART packet framing, CRC, command builders, parsing
  vesc_comm.py          # VESCComm (telemetry service) + CommandManager (TX gate)
  safety_supervisor.py  # Overvoltage, undervoltage, timeout, throttle checks
  precharge_manager.py  # Precharge ON/OFF + watchdog (grace, progress, timeout)
  energy_estimator.py   # ½CV² energy + percentage calculation
  display_manager.py    # LCD page rendering (run/precharge/fault pages)
  bench_logger.py       # RAM ring-buffer data logger for bench debugging

app/
  controller.py      # Orchestrator — sequences services via PeriodicTimers
  state_machine.py   # State transitions: OFF→PRECHARGE→READY→ASSIST/REGEN, FAULT

tests/               # pytest suite (263 tests)
```

---

## 12. Setup and Deployment

1. Flash MicroPython UF2 onto the Pico.
2. Copy this project onto the Pico filesystem.
3. Confirm hardware pin mapping in `config/settings.py`.
4. Set motor pole pairs in VESC Tool and verify with FOC detection.
5. Set current limits in both VESC Tool and `config/settings.py`.
6. Power cycle or reset the board; `boot.py` then `main.py` run automatically.

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
