# ReGenX Brake-Assist Controller

Raspberry Pi Pico (RP2040, MicroPython) firmware that uses a small FOC motor
controller (FSESC4.20 / VESC 6.6) and a supercapacitor bank to add friction-free
brake-assist regen to a bicycle.  The main wheel brake is a standard band brake
clamping a planetary-gear carrier; when the brake is applied the motor spins up
through the planetary and we harvest energy into the caps.

Production regen is an **AIMD-FF adaptive controller** (additive-increase /
multiplicative-decrease on a feedforward gain), tuned offline with the `sim/`
package against a physics simulator.

---

## 0. Quick Start

For someone who just cloned the repo and wants the firmware on a Pico talking
to a VESC:

```bash
# 1. PC toolchain
python3 -m venv .venv
source .venv/bin/activate            # Linux/macOS
# .venv\Scripts\Activate.ps1        # Windows PowerShell
pip install pytest mpremote numpy scipy
python -m pytest -q                  # 311 tests should pass

# 2. Flash MicroPython on the Pico
#    Hold BOOTSEL, plug in, drag an RPI_PICO .uf2 onto the mounted drive.
#    Get it from https://micropython.org/download/RPI_PICO/

# 3. One-time VESC setup in VESC Tool — see §11.4:
#    Motor Settings → FOC → Detect and Calculate
#    App Settings  → App to Use: UART, 115200 baud

# 4. Deploy firmware + provision VESC limits + install the LispBM push script
./scripts/deploy_to_flash.sh         # or: bash scripts/deploy_to_flash.sh

# 5. Power cycle the Pico.  boot.py → main.py run autonomously.
```

Pick which regen strategy to run with before deploying:

```bash
python scripts/set_regen_strategy.py --show          # show active
python scripts/set_regen_strategy.py aimd_ff         # default
python scripts/set_regen_strategy.py pi_controller   # reference
```

From here the numbered sections below describe each subsystem in detail.

---

## 1. Status

| Aspect | State |
|---|---|
| Firmware | Feature-complete for bench bring-up. |
| Test suite | 311 passing (`pytest -q`). |
| Sim / tune | `sim/run_tune.py` + `sim/run_gallery.py` working; tuned params synced. |
| Hardware | Not yet run on full stack (LCD, VESC, caps, motor) — this is the next step. |

---

## 2. Hardware

| Part | Detail |
|---|---|
| MCU | Raspberry Pi Pico (RP2040), MicroPython |
| Motor ctrl | FSESC4.20 (VESC HW, FW 6.6) |
| Motor | Puyan H01 geared hub, 11 pole pairs, λ = 0.0111 Wb, R = 0.082 Ω |
| Gearing | Planetary, N = 4.8, η = 0.95 (Puyan H01 spec) |
| Storage | 20 F supercap bank, ~10–43 V operating |
| Display | HD44780 16×2, 4-bit parallel GPIO (not I²C) |
| Brake | External band brake on planetary carrier |
| UART | Pico GP4/GP5 ↔ VESC UART1, 115200 baud |
| Throttle | 3-wire hall, ADC0 (GP26) |

### 2.1 Pin Map

| Pico pin | GPIO | Function |
|---|---|---|
| 6  | GP4  | UART1 TX → VESC RX |
| 7  | GP5  | UART1 RX ← VESC TX |
| 11 | GP8  | Soft reset button (active-low, internal pull-up) |
| 22 | GP17 | LCD RS |
| 24 | GP18 | LCD E |
| 25 | GP19 | LCD D4 |
| 26 | GP20 | LCD D5 |
| 27 | GP21 | LCD D6 |
| 29 | GP22 | LCD D7 |
| 31 | GP26 | ADC0 — hall throttle |
| 34 | GP28 | LCD backlight enable |
| 36 | 3V3  | LCD VDD |
| 38 | GND  | Common |
| 39 | VSYS | 5 V input |

All logic is 3.3 V.  The FSESC STM32F4 UART is also 3.3 V, so no level-shifter.

---

## 3. Runtime Architecture

Single-thread cooperative scheduler.  No queues, no interrupts, no callbacks.
`main.py` wires components and runs periodic tasks via `utils.PeriodicTimer`.

```
main.py
  └── app/controller.py                # orchestrates services at fixed rates
        ├── drivers/throttle.py        # ADC → normalized fraction
        ├── drivers/gpio_io.py         # soft-reset button edge detection
        ├── drivers/lcd_driver.py      # HD44780 4-bit parallel
        └── services/
              ├── input_manager.py     # decides ASSIST vs REGEN from throttle + RPM
              ├── control_loop.py      # calls active regen strategy, applies limits
              ├── regen/strategies.py  # PI-slip or AIMD-FF
              ├── vesc_protocol.py     # packet framing + COMM_* opcodes
              ├── vesc_comm.py         # UART + telemetry + whitelist RX
              ├── system_supervisor.py # state machine + fault handling
              ├── display_manager.py   # LCD pages (+ periodic re-init watchdog)
              └── bench_logger.py      # RAM ring buffer for offline CSV dump
```

### 3.1 System States

| State | Meaning |
|---|---|
| `PRECHARGE` | Cap bus below `VCAP_MIN_OPERATING` (10.0 V). Motor inhibited. |
| `ASSIST`    | Throttle active, feeding motor current. |
| `REGEN`     | Throttle released + brake held, strategy is producing regen current. |
| `FAULT`     | Overvoltage / VESC timeout / VESC fault / throttle open / internal error. |

There is no `OFF` state — power applied means the supervisor is running.
Latching faults (`OVERVOLTAGE`, `INTERNAL`) require a soft reset.

### 3.2 Command Modes

`CommandMode.ASSIST` and `CommandMode.REGEN`.  The input manager sets
`requested_mode` based on throttle position + motor RPM hysteresis
(`REGEN_ENTRY_RPM = 25`, `REGEN_EXIT_RPM = 18`).

---

## 4. Regen Control — AIMD-FF

The active production strategy is `AimdFfRegenStrategy`
(`firmware/regen/strategies.py`).  Each tick:

1. Read `rpm` and `vcap` from telemetry; apply a linear voltage taper
   (`VCAP_REGEN_TAPER_START_V = 40 V` → `VCAP_REGEN_TAPER_END_V = 42 V`).
2. Read `drpm_peak_neg` — the most-negative per-sample Δrpm/dt over the
   last 10 ms window, computed on the VESC at 1 kHz and pushed at 100 Hz
   via the LispBM script in §5.3.  This replaces the firmware-side EMA
   that the earlier version used.
3. If `-drpm_peak_neg > unlock_thresh` (a real carrier-slip spike
   occurred somewhere inside the 10 ms), apply **multiplicative
   decrease**: `k ← k · (1 − β·(0.35 + 0.65·level))`.
4. Otherwise apply **additive increase**: `k ← min(1, k + k_ai)`.
5. Output feedforward current `I = k · λ·ωe / R_phase`, clipped to
   `REGEN_CURRENT_MAX_A` and scaled by the voltage taper.

Parameters live in `firmware/config/settings.py → REGEN_STRATEGY_PARAMS["aimd_ff"]`.
The four-param set (`k, beta_md, unlock_thresh, k_ai`) is tuned by
differential evolution in `sim/run_tune.py`; drift is detected with
`scripts/sync_tune_params.py` (see §7).

### 4.0 Drivetrain and why the band brake exists

The Puyan-H01-class geared hub motor has a **one-way freewheel** between
the ring gear (wheel output) and the wheel itself, so the rider does not
have to back-drive the motor while coasting.  A side-effect of that
freewheel is that torque cannot travel from the wheel back into the
motor — i.e. the motor cannot regen-brake through its normal drivetrain.

To restore a regen torque path on demand, an external **band brake
clamps the planetary carrier**.  Normally the carrier is free and the
planetary has no output; clamping the carrier locks it to the housing,
which re-establishes the gear ratio from wheel → ring → planets → sun →
motor rotor.  The rider's brake lever pulls the band; harder pull =
higher carrier lock torque = more regen throughput, up to the point
where the motor's reaction torque exceeds the band friction and the
carrier starts to slip (AIMD-FF backs off here).

When the band brake is released, the rotor sees no load (neither the
wheel nor the band can apply torque through the planetary) and spins
down on its own magnetic + bearing drag in about half a second.  This
spin-down is used to pin `T_DRAG_COEFF` in `sim/physics.py` — see
`tests/bench_test_notes.md` Stage 9.

### 4.1 Command Power Limit

After the strategy returns a current, `services/control_loop.py` clamps it by
both `REGEN_CURRENT_MAX_A` **and** `VESC_WATT_MAX / max(V_bus, 1.0)` using the
commanded (not measured) current-voltage product.  This prevents a single
high-current tick from being sent while the VESC is still enforcing its own
watt ceiling asynchronously.

### 4.2 A/B With PI-Slip

`PiSlipRegenStrategy` is kept in tree as an alternative.  Select at runtime
with:

```
python scripts/set_regen_strategy.py pi_controller
python scripts/set_regen_strategy.py aimd_ff
```

Both are exercised by every sim run.

---

## 5. VESC Protocol

`services/vesc_protocol.py` implements minimal VESC UART packet framing (start
byte + length + payload + CRC16 + end byte).  `services/vesc_comm.py` owns the
UART, decodes telemetry, and forwards commands.

### 5.1 Opcode Whitelist

The RX path silently drops frames whose opcode is not on an explicit whitelist.
This keeps spurious bus traffic (or a stray VESC Tool reconnect) from writing
into `SharedState`.

Whitelisted opcodes (see `vesc_protocol.py`):

| Opcode | Name | Direction |
|---|---|---|
| 0   | `FW_VERSION`             | RX (response) |
| 4   | `GET_VALUES`             | RX (full telemetry) |
| 6   | `SET_CURRENT`            | TX |
| 7   | `SET_CURRENT_BRAKE`      | TX |
| 30  | `ALIVE`                  | TX (heartbeat) |
| 36  | `CUSTOM_APP_DATA`        | RX (LBM push, optional) |
| 48  | `SET_MCCONF_TEMP`        | TX (provisioning only) |
| 50  | `GET_VALUES_SELECTIVE`   | RX (selective telemetry) |
| 63  | `APP_DISABLE_OUTPUT`     | TX |
| 86  | `SET_BATTERY_CUT`        | TX |
| 159 | `MOTOR_ESTOP`            | TX |

Regression test: `tests/test_vesc_comm.py::test_unknown_opcode_does_not_corrupt_state`.

### 5.2 Push-IQ Packet (on-VESC aggregation)

`scripts/vesc_lisp_push_iq.lisp` runs a 1 kHz LispBM loop on the VESC's
STM32 that computes derived telemetry the Pico can't sample fast enough,
then pushes it at 100 Hz over the UART comm-header using
`COMM_CUSTOM_APP_DATA` (opcode 36):

| Bytes | Field | Units | Semantics |
|---|---|---|---|
| 0..3   | `rpm_now`         | electrical rpm | rpm at send instant (less filtered) |
| 4..7   | `drpm_mean`       | rpm/s          | mean d(rpm)/dt over 10 ms window |
| 8..11  | `drpm_peak_neg`   | rpm/s          | most-negative per-sample Δrpm/dt (peak-held) |
| 12..15 | `iq_mean`         | A              | mean q-axis current over window |

All fields are big-endian float32.  The Pico parses the 16-byte payload
in `services/vesc_protocol.py::_parse_push_iq`, converts electrical→mech
rpm in `services/vesc_comm.py::_handle_push_iq`, and exposes the values
to strategies via `SharedState` and `StrategyContext`.

Why aggregate on the VESC:
- Sampling `d(rpm)/dt` at the Pico's 100 Hz rate aliases real
  unlock transients (~2–5 ms events).
- Peak-hold on a 1 kHz inner loop catches those spikes cleanly.
- Strategies simplify: `AimdFfRegenStrategy` tests
  `drpm_peak_neg` directly (no EMA state);
  `PiSlipRegenStrategy` uses `drpm_mean` as a cleaner decel proxy
  than a Pico-side numerical derivative.

The aggregation math is unit-tested against the standalone lispBM
evaluator — see `scripts/bench/test_vesc_lisp_aggregation.lisp`.
Sim physics (`sim/physics.py::_run_physics_batch`) mirrors the same
window aggregation so tuning and hardware share a single signal model,
including the injected noise floor.

If the lisp script is not running, `_build_strategy_context` treats the
data as stale after 30 ms and zeroes `drpm_mean`/`drpm_peak_neg`; the
additive-increase branch of each strategy keeps the system behaving,
just without slip protection.

---

## 6. Safety

### 6.1 Fault Table

| Fault | Trigger | Latching |
|---|---|---|
| `OVERVOLTAGE`    | `cap_voltage_v ≥ VCAP_ABSOLUTE_MAX (43.0 V)` | Yes |
| `VESC_TIMEOUT`   | No valid telemetry for 500 ms | No |
| `VESC_FAULT`     | VESC reports non-zero fault code | No |
| `THROTTLE_RANGE` | ADC `< 100` or `> 4000` (open / short) | No |
| `INTERNAL`       | Uncaught exception in main loop | Yes |

Any active fault forces `FAULT` state and `inhibit_motor_commands = True`,
which zeros all commands and resets the strategy state.

### 6.2 RP2040 Hardware Watchdog

- 8 s timeout.
- `boot.py` re-arms on boot (survives soft reset where `mpremote` interrupts
  main loop but WDT is still armed).
- `main()` feeds during init (after drivers, after services) and at the top of
  every scheduler tick.

### 6.3 LCD Re-init Watchdog

`display_manager.py` re-issues the HD44780 init sequence every
`LCD_REINIT_INTERVAL_MS`.  EMI on the E / RS lines can leave the controller
in 8-bit mode or an offset DDRAM address; the periodic re-init is a silent,
idempotent self-heal with no interaction with the fault manager.

---

## 7. Sim and Tuning

```
sim/
  physics.py         # rigid-body + planetary + FOC copper-loss model;
                     # mirrors the VESC's 1 kHz window aggregation and
                     # injects telemetry noise (see §5.2) so tuning
                     # matches hardware signal statistics.
  scoring.py         # composite: energy + symmetric tracking + smoothness
  strategies.py      # PI-slip + AIMD-FF (both share the on-VESC
                     # aggregated signals — no Pico-side EMA).
  strategy_context.py  # 10-field slot set: rpm/iq/vcap + rpm_fast,
                       # iq_mean, drpm_mean, drpm_peak_neg, dt_ctrl
  run_tune.py        # DE pipeline (screen → refine → robust)
  run_gallery.py     # sweep + HTML efficiency gallery
  identify.py        # closed-loop residual check vs bench trace
```

Typical cycle:

```
# 1. Tune
python -m sim.run_tune --strategies aimd_ff --maxiter 20 --popsize 12

# 2. Check whether the new tuned params differ from firmware config
python scripts/sync_tune_params.py sim/output/tune/<run_id>/results.json

# 3. Copy the drifted params by hand into firmware/config/settings.py
#    (the git diff keeps provenance).

# 4. Regenerate the efficiency gallery
python -m sim.run_gallery
```

Tune artifacts land in `sim/output/tune/<timestamp>/` (small, kept in tree).
The ~18 MB `sim/output/eff_gallery.html` is git-ignored and VS Code
file-watcher-excluded (see `.vscode/settings.json`).

---

## 8. Key Constants

All live in `firmware/config/settings.py`.

### 8.1 Voltage / Current

| Name | Value | Description |
|---|---|---|
| VCAP_MIN_OPERATING        | 10.0 V | Precharge gate. |
| VCAP_REGEN_TAPER_START_V  | 40.0 V | Regen starts linear taper. |
| VCAP_REGEN_TAPER_END_V    | 42.0 V | Regen reaches zero. |
| VCAP_ABSOLUTE_MAX         | 43.0 V | Hard overvoltage fault. |
| MOTOR_CURRENT_MAX_A       | 50.0 A | VESC-side ceiling. |
| MOTOR_COMMAND_LIMIT_A     | 45.0 A | Firmware command ceiling (5 A headroom). |
| VESC_WATT_MAX             | 1500 W | Drive + regen watt ceiling. |
| REGEN_CURRENT_MAX_A       | 40.0 A | Hard regen current ceiling. |

### 8.2 Regen

| Name | Value |
|---|---|
| `REGEN_ENTRY_RPM` | 25.0 |
| `REGEN_EXIT_RPM`  | 18.0 |
| `REGEN_HOLDOFF_MS` | 300 |
| `FLUX_LINKAGE_WB` | 0.0111 |
| `MOTOR_PHASE_RESISTANCE_OHM` | 0.082 |

### 8.3 AIMD-FF Params

Set in `REGEN_STRATEGY_PARAMS["aimd_ff"]`.  Synced from
`sim/output/tune/20260421_054733/` (scipy DE, `maxiter=200 popsize=36
polish-maxiter=80 seeds=[7, 42, 123]`, robust CVaR-20 objective — the
first tune under the static-friction scoring baseline).  Seed=42 winner:
composite **78.30** (energy 67.8, tracking 83.7, smoothness 86.3,
robust P5=72.7).

```
k=0.08326140868410246
beta_md=0.05467909462877643
unlock_thresh=842.0
k_ai=0.12576438848635152
```

`unlock_thresh` is on the scale of `drpm_peak_neg` — the most-negative
per-sample Δrpm/dt in the 10 ms window, computed at 1 kHz on the VESC.
A real carrier-unlock spike is a ~2–5 ms event that reaches several
hundred rpm/s on the peak-hold line, so an 842 rpm/s threshold
discriminates real slip from measurement jitter.

---

## 9. Bench Logger

RAM ring buffer (`services/bench_logger.py`), sampled at
`BENCH_LOG_PERIOD_MS` (500 ms, ~2 Hz).  Capacity is 2000 records ≈ 16 minutes.
The soft-reset button dumps the entire buffer as CSV over USB serial and
clears it.

Fields per record:

```
tick_ms, system_state, cap_voltage_v, vesc_mech_rpm,
vesc_motor_current_a, requested_mode, requested_level,
assist_command_request, regen_command_request
```

Host capture:

```
python -m serial.tools.miniterm COM3 115200 --raw > bench_log.csv
```

---

## 10. Project Layout

```
firmware/
  boot.py                  # WDT arm on boot
  main.py                  # entrypoint + scheduler
  core.py                  # SystemState, FaultCode, CommandMode, FaultManager, SharedState
  utils.py                 # clamp, linear_map, PeriodicTimer, Logger
  config/settings.py       # all constants + REGEN_STRATEGY_PARAMS
  drivers/                 # throttle, gpio_io, lcd_driver
  regen/                   # strategies + strategy_context (firmware copy)
  services/                # input, control_loop, vesc_*, supervisor, display, bench_logger
  app/controller.py        # orchestrator

sim/                       # offline physics + tuning (see §7)
scripts/
  vesc_provision.py        # apply limits + verify + save snapshot
  vesc_characterize_motor.py  # dc-cal + FOC detect
  set_regen_strategy.py    # switch active strategy key in settings.py
  sync_tune_params.py      # diff tuned params vs settings
  analyze_strategies.py    # per-scenario breakdown of tuned strategies
  debug_strategy_traces.py # per-tick traces for strategy diagnostics
  deploy_to_flash.sh       # mpremote-based upload
  vesc_lisp_push_iq.lisp   # optional LBM snippet
  bench/                   # low-level single-shot diagnostics
  lib/                     # path_setup, vesc_terminal, vesc_uart_template

tests/                     # 311 pytest tests (mock hardware)
data/                      # captured bench traces (CSV)
```

---

## 11. Setup and Usage

Go through the subsections in order on a new machine.  The Quick Start
above is this section compressed into ~15 commands.

### 11.1 Prerequisites

| Requirement | Version | Purpose |
|---|---|---|
| Python | 3.10+ | Run the pytest test suite and the sim/tune tools |
| Git | any | Version control |
| mpremote | pip install | Upload files and run scripts on the Pico |
| VESC Tool | 3.01+ | One-time FOC detection and App-Settings setup |

**Linux serial permissions** — the Pico appears as `/dev/ttyACM0`.
Your user must be in the `dialout` group or `mpremote` will fail with
"permission denied":

```bash
sudo usermod -aG dialout $USER
# log out and back in (or reboot) for the group change to take effect
```

On Windows the Pico shows up as a COM port; no permission setup needed.

### 11.2 PC toolchain

```bash
cd regenx-brake-assist-controller
python3 -m venv .venv
source .venv/bin/activate            # Linux/macOS
# .venv\Scripts\Activate.ps1        # Windows PowerShell
pip install pytest mpremote numpy scipy

python -m pytest -q                  # 311 passing, mock hardware
```

The firmware test suite uses mock hardware fixtures in `tests/conftest.py`
and does not require a connected Pico.  `numpy`/`scipy` are only needed
for `sim/` — strip them if you only care about the firmware.

### 11.3 Flash MicroPython on the Pico

1. Unplug the Pico from USB.
2. Hold the **BOOTSEL** button on the Pico.
3. While holding BOOTSEL, plug the Pico into USB.
4. Release BOOTSEL — the Pico mounts as a USB drive called **RPI-RP2**.
5. Download the MicroPython UF2 firmware from
   <https://micropython.org/download/RPI_PICO/> (pick the latest stable `.uf2`).
6. Copy the `.uf2` file onto the RPI-RP2 drive.  The Pico reboots
   automatically and reappears as `/dev/ttyACM0` (Linux/macOS) or a COM
   port (Windows).

Confirm it's alive:

```bash
mpremote ls          # should list boot.py (empty MicroPython default)
```

### 11.4 One-time VESC Tool configuration

Download VESC Tool from <https://vesc-project.com/vesc_tool>.  Connect the
FSESC to your PC via its own USB port (separate from the Pico's USB).

1. **Motor Settings → FOC → Detect and Calculate.**  Use 2–5 A detect
   current on the bench.  Confirm the wizard reports **11 pole pairs**
   for the Puyan H01 (22 magnets).  If detection stutters or fails,
   recheck the three phase-wire connections.  Click **Write Motor
   Configuration** (down-arrow icon) to persist.
2. **App Settings → General → App to Use: UART.**  Without this the VESC
   ignores every command from the Pico.  This is the single most commonly
   missed step.
3. **App Settings → UART → Baud Rate: 115200** (must match
   `VESC_BAUD_RATE` in `firmware/config/settings.py`).  Click **Write App
   Configuration**.
4. Power-cycle the VESC.

Current / voltage / watt limits are **not** set in VESC Tool — the
provisioning script in §11.5 applies them from `firmware/config/settings.py`
so there is only one source of truth.  The values that will be written:

| VESC setting | Firmware constant | Value |
|---|---|---|
| Motor Current Max / Max Brake | `MOTOR_CURRENT_MAX_A` | 50 A |
| Battery Current Max / Max Regen | — | 50 A |
| Watt Max / Watt Max Regen | `VESC_WATT_MAX` | 1500 W |
| Min / Max Input Voltage | — | 14 V / 43 V |
| Absolute Max Current | `VESC_ABS_CURRENT_MAX_A` | 130 A |

The VESC limits are the last hardware safety net below the Pico's software
limits (`MOTOR_COMMAND_LIMIT_A = 45 A`, `VCAP_ABSOLUTE_MAX = 43 V`).

### 11.5 Deploy the firmware and provision the VESC

The top-level deploy script uploads all project files and then runs the
VESC provisioner:

```bash
./scripts/deploy_to_flash.sh         # or: bash scripts/deploy_to_flash.sh
```

What it does, in order:

1. Creates `/app`, `/config`, `/drivers`, `/services`, `/regen`, `/data`
   on the Pico flash filesystem.
2. Copies every `*.py` from `firmware/` into the matching directory on the
   Pico (root files `boot.py`, `main.py`, `core.py`, `utils.py` plus each
   package tree).
3. Runs `scripts/vesc_provision.py` over the Pico's UART:
   * verifies VESC FW ≥ 6.6 and HW name `410`
   * applies every MCCONF limit listed in §11.4 via LispBM `conf-set`
   * persists with `conf-store`, then reads back and verifies
   * installs `scripts/vesc_lisp_push_iq.lisp` (the 1 kHz aggregation
     loop from §5.2) and starts it
   * saves MCCONF/APPCONF binary snapshots to Pico flash for audit

To re-upload firmware without re-provisioning the VESC:

```bash
RUN_VESC_PROVISION=0 ./scripts/deploy_to_flash.sh
```

### 11.6 Smoke-test the hardware before first motion

Bench fixtures live in `scripts/bench/`.  Each prints `PASS` / `FAIL`.
Run them in order after a fresh deploy; any failure means stop and fix
before applying power to the motor.

```bash
# VESC telemetry reachable — motor may be disconnected, VESC powered
mpremote run scripts/bench/test_vesc_uart_healthcheck.py

# Current-sensor offset health (reads VESC terminal diagnostics)
mpremote run scripts/bench/test_vesc_offset_healthcheck.py

# VESC fault watch — leaves the VESC running and prints any fault codes
mpremote run scripts/bench/test_vesc_fault_watch.py
```

The full bench day checklist lives in `tests/bench_test_notes.md`;
at a glance:

1. Pico-only smoke (REPL, soft-reset button, bench-log dump).
2. VESC FW version + telemetry snapshot.
3. LCD on caps-off bench power; verify re-init watchdog self-heal.
4. Opcode-whitelist trace while VESC Tool is also connected.
5. Commanded-power limiter sweep at fixed RPM.
6. `sim/identify.py` residual against a short captured trace.
7. First-motion test with rim brake engaged, low watt cap.

### 11.7 Day-to-day commands

```bash
# See what the Pico is printing live (USB serial REPL)
mpremote repl                                          # Ctrl-] to exit

# Dump the in-RAM bench log as CSV over USB
python -m serial.tools.miniterm COM3 115200 --raw > bench_log.csv
# Then press the soft-reset button on the Pico (GP8) to dump + clear.

# Switch active regen strategy and redeploy
python scripts/set_regen_strategy.py pi_controller
./scripts/deploy_to_flash.sh

# Retune, check for drift vs settings.py, regenerate gallery
python -m sim.run_tune --strategies aimd_ff --maxiter 20 --popsize 12
python scripts/sync_tune_params.py sim/output/tune/<run_id>/results.json
python -m sim.run_gallery                             # writes sim/output/eff_gallery.html

# Full test suite
python -m pytest -q
```

### 11.8 Capture short unplugged sprint tests

The firmware keeps a rolling in-RAM debug log of the most recent ~60 seconds of
runtime (`600` samples at `100 ms` intervals), and also mirrors records to a
persistent CSV on Pico flash (`/data/ride_log.csv`).

This means ride data is still recoverable even if a runtime fault or watchdog
reset happens before you reconnect USB.

Fields captured per sample:

```text
tick_ms,system_state,cap_voltage_v,vesc_mech_rpm,vesc_motor_current_a,
vesc_input_current_a,vesc_duty_cycle,vesc_fault_code,requested_mode,
requested_level,throttle_raw,throttle_valid,inhibit_motor_commands,
assist_command_request,regen_command_request,motor_command_a
```

Recommended workflow:

1. Deploy current firmware.
2. Ride with the Pico fully unplugged from USB.
3. Reconnect USB after the ride.
4. Copy the persistent ride log file from Pico flash.

Linux example:

```bash
/home/q/Desktop/VSCode_Projects/.venv/bin/python -m mpremote connect /dev/ttyACM0 cp :/data/ride_log.csv ride_log.csv
```

Windows example:

```bash
python -m mpremote connect COM3 cp :/data/ride_log.csv ride_log.csv
```

Notes:

- Persistent file path is configured by `BENCH_LOG_PERSIST_PATH`.
- File rollover size is configured by `BENCH_LOG_PERSIST_MAX_BYTES`.
- The persistent file is reset on each firmware boot/session.
- If needed, delete the file manually from host:

```bash
/home/q/Desktop/VSCode_Projects/.venv/bin/python -m mpremote connect /dev/ttyACM0 rm :/data/ride_log.csv
```

- If a host tool such as `mpremote repl` is open during the ride, that no
   longer represents true standalone behaviour.

If a tune produces drifted params, copy them by hand into
`firmware/config/settings.py → REGEN_STRATEGY_PARAMS` (keep the
`AIMD_FF_AUTOGEN_START` / `_END` sentinels intact — future tools may
rewrite between them automatically).  The manual copy keeps provenance
in the git history.

---

## 12. Troubleshooting Notes

### 12.1 LCD blanks or shows corrupted characters

The HD44780-compatible LCD is driven in 4-bit write-only mode with `RW`
grounded.  That means the Pico cannot read the controller state back.  If a
single `E` pulse is missed because of EMI or a local supply dip, the LCD's
internal nibble framing can desynchronise and the next writes look like random
characters or a blank page.

The firmware mitigates this in two ways:

- `DisplayManager` re-runs the LCD init sequence periodically and on display
  mode transitions.
- The display path only rewrites rows whose text actually changed, reducing
  `E`-line traffic during noisy transitions.

If the LCD still corrupts and then later fixes itself, that strongly suggests
an electrical problem rather than bad page-formatting logic.  Check these in
order:

1. Keep LCD wires short, especially `E` and `RS`.
2. Twist or route LCD ground close to the Pico ground return.
3. Verify the LCD 5 V rail does not sag during regen/current transients.
4. Add local decoupling near the LCD module if not already present.
5. Keep LCD wiring physically separated from motor phase, battery, and VESC
   switching-current paths.

### 12.2 Bike feels different with Pico USB plugged in

The firmware does not have a normal "USB-connected mode".  Merely plugging the
Pico into a computer should not change control logic by itself.

There are two real mechanisms that can change behaviour:

1. **Host interaction:** if `mpremote`, a REPL session, or a serial monitor
   opens the Pico, it can interrupt normal runtime.  `mpremote` in particular
   uses raw REPL, which can stop the main loop long enough to affect watchdog
   feeding and control timing.
2. **Electrical changes:** USB adds another ground path and often changes the
   effective noise environment.  That can shift throttle ADC noise, UART signal
   integrity, and LCD robustness.  If the system behaves differently with USB
   attached even when no host tool is connected, treat that as a grounding /
   EMI / supply-reference issue.

Recommended A/B checks:

1. Pico standalone, no USB.
2. Pico plugged into USB, but no serial client open.
3. Pico plugged into USB with `mpremote repl` or another active host session.
4. Laptop on battery vs laptop on charger.

Interpretation:

- `1` vs `2` different: mostly electrical.
- `2` vs `3` different: host/tool interference.
- Battery vs charger different: grounding / common-mode noise issue.

Quick checks:

```bash
ps -ef | grep -i mpremote | grep -v grep
python -m serial.tools.list_ports
```

If you want the bike behaviour during testing to match standalone riding as
closely as possible, power the Pico from its normal bike supply and leave USB
fully disconnected unless you are actively collecting logs.

---

## 13. License

Private project.  No license granted.
