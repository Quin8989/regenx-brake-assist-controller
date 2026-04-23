# Bench Test Notes

Staged bring-up checklist for running the ReGenX firmware against real
hardware for the first time.  Record observations inline — this file is the
primary paper trail.

Assume the PC-side pytest suite is already green (311 passing).

---

## Stage 0 — PC preflight

- [ ] `python -m pytest -q` → 311 passed.
- [ ] `python scripts/sync_tune_params.py sim/output/tune/<latest>/results.json`
      exits 0 (no drift) **or** any drift has been hand-copied into
      `firmware/config/settings.py`.
- [ ] `firmware/config/vesc_snapshot_meta.txt` matches the VESC you intend to
      use (serial / hw name / fw version).

---

## Stage 1 — Pico alone

- [ ] Pico powered from USB only.  No LCD, no VESC, no caps.
- [ ] `mpremote` reaches the REPL; `import main` raises no ImportError.
- [ ] Soft reset button (GP8) triggers bench-log CSV dump even with an
      empty buffer.

---

## Stage 2 — VESC UART reachability

- [ ] Pico GP4 (TX) → VESC RX, GP5 (RX) ← VESC TX, common GND.
- [ ] `mpremote run scripts/bench/test_vesc_uart_healthcheck.py` prints
      `PASS` and a firmware/hw string.

---

## Stage 3 — LCD on Pico (no VESC, no caps)

Wiring per §2.1 of `README.md`.  4-bit parallel — **no I²C bus scan**,
there is no I²C involved.

- [ ] Backlight (GP28) lights up.
- [ ] Initial splash text appears on both rows.
- [ ] **Re-init watchdog check:** with the firmware running, briefly
      short GP17 (RS) to GND for ~50 ms to corrupt the controller state.
      The display should garble and then self-heal within
      `LCD_REINIT_INTERVAL_MS` without a reset and without entering FAULT.

---

## Stage 4 — VESC link (motor disconnected, bench supply on VESC)

- [ ] UART wiring: Pico GP4 → VESC RX, Pico GP5 → VESC TX, common GND.
- [ ] `mpremote mount . run scripts/vesc_provision.py` reports the expected
      firmware version and writes limits (watt / motor / battery) matching
      `settings.py`.
- [ ] **Opcode-whitelist check:** with the firmware running, connect VESC
      Tool over USB in parallel.  While VESC Tool is talking to the board,
      `SharedState.cap_voltage_v` and friends must stay numerically stable
      (no NaN, no impossible jumps).  The whitelist must silently drop
      frames with opcodes outside the table in §5.1 of the README.

---

## Stage 5 — Telemetry at rest (motor still disconnected)

- [ ] Telemetry page on LCD updates at ~10 Hz.
- [ ] `vesc_motor_current_a` offset < 0.5 A.
- [ ] `cap_voltage_v` matches the bench-supply reading within 0.2 V.
- [ ] `fault_code == 0`.
- [ ] Disconnect VESC TX and confirm FAULT `VESC_TIMEOUT` within ~500 ms.
      Reconnect — fault auto-clears.

---

## Stage 6 — Motor on stand, assist only, low-power cap

- [ ] Motor on stand, wheel free.  Rim brake **not** engaged.
- [ ] Temporarily cap `MOTOR_COMMAND_LIMIT_A` to 5 A in `settings.py`.
- [ ] Throttle at 25 % → wheel spins in the expected direction.
- [ ] Release throttle → current drops to zero within one tick.
- [ ] `THROTTLE_RANGE` fault fires when the hall signal is unplugged mid-run.

---

## Stage 7 — Regen on stand, caps partially charged

- [ ] Precharge caps to ≥ 10 V externally.  Confirm firmware leaves
      `PRECHARGE`.
- [ ] Hold rim brake, bring wheel up to speed with assist, release
      throttle — supervisor enters `REGEN` once motor RPM ≥
      `REGEN_ENTRY_RPM` (25).
- [ ] Cap voltage rises visibly.
- [ ] **Commanded-power limiter sweep:** at a fixed RPM, step cap voltage
      (external supply) across 15 V / 25 V / 35 V.  Commanded current
      should never exceed `VESC_WATT_MAX / V_bus` (so ≤ 100 A / 60 A / 43 A
      respectively, before the hard `REGEN_CURRENT_MAX_A = 40 A` floor
      also applies).
- [ ] Voltage taper: raise cap supply past 40 V — regen command
      linearly tapers, reaching zero at 42 V.
- [ ] Overvoltage: raise past 43 V — FAULT `OVERVOLTAGE` latches.

---

## Stage 8 — Carrier-lock check (band brake on stand)

- [ ] Apply increasing band-brake force.  Log `vesc_mech_rpm` and regen
      current.
- [ ] Verify the AIMD-FF controller backs off (multiplicative decrease)
      when `−drpm_peak_neg > unlock_thresh` (see
      `REGEN_STRATEGY_PARAMS["aimd_ff"]` in `firmware/config/settings.py`).
      Re-engagement should be smooth, no grab-release judder.
- [ ] Save a 30-second bench-log CSV via the soft-reset button.

---

## Stage 9 — Rotor spin-down (verifies `T_DRAG_COEFF`)

**Why.**  `T_DRAG_COEFF = 0.0012 Nm·s/rad` in `sim/physics.py` was
derived from a bench observation (2026-04-22): with the front wheel
in the air on the bench, LCD showing ~35 km/h (w_rotor ≈ 141 rad/s
through the 4.8:1 planetary), release throttle + band brake — the
motor rotor visibly comes to rest in ~1.5 s while the wheel
freewheels on for tens of seconds.  This stage confirms the decay
constant with timed data.

- [ ] Warm the motor under assist for ~30 s so bearings are at
      temperature.
- [ ] Bring up to ~35 km/h (wheel-in-air), then release throttle *and*
      the band brake simultaneously.  Start logging `vesc_mech_rpm`
      via the bench logger (2 Hz) or slow-mo video of a reference
      mark on the rotor if telemetry is too slow.
- [ ] Fit `ω(t) = ω₀·exp(-t/τ)` to the decay.  Record `τ`.
- [ ] Expected: `τ ≈ 0.35 – 0.40 s` (visible-rest time ≈ 4τ ≈ 1.4–1.6 s).
      If τ differs, update `T_DRAG_COEFF` via `b = J_rotor / τ` with
      `J_rotor ≈ 4.4e-4 kg·m²` from rotor dimensions/mass.
- [ ] The wheel will keep spinning for tens of seconds on its bearing
      drag alone — that is expected (freewheel disengaged, wheel
      decoupled from rotor).

---

## Stage 10 — `identify.py` residual (carrier locked)

- [ ] Capture a fresh trace on the stand where the band brake is held
      at a **constant** force and the wheel is driven down from ~20 km/h.
      Keeping the carrier locked keeps the kinematic-sun approximation
      in `sim/physics.py` valid.  Save as `data/phase3_trace.csv`.
- [ ] Run `python -m sim.identify data/phase3_trace.csv`.
- [ ] Target: `iq_rms < 5 A` with the updated `J_CARRIER = 0.015` and
      `T_DRAG_COEFF = 0.0012`.
- [ ] Existing traces (`drill`, `ride`, `phase2`) give very large
      residuals (76 / 128 / 521 A RMS) because the brake force was not
      held constant and/or the freewheel was disengaged — the current
      sim does not model those regimes.  Do not use them to refit.

---

## Stage 11 — Push-IQ lisp aggregation (see README §5.2)

- [ ] Flash `scripts/vesc_lisp_push_iq.lisp` via VESC Tool →
      LispBM → Upload & Run.
- [ ] Spin the wheel by hand.  Confirm `vesc_mech_rpm_fast` on the
      Pico updates at ~100 Hz (watch bench log at 2 Hz — should see
      fresh values every sample).
- [ ] Rapid-decel by dragging the brake lever briefly: verify
      `drpm_peak_neg` dips far below `drpm_mean` on that sample
      (captures the ~2–5 ms unlock spike that aliases at 100 Hz).
- [ ] Kill the lisp (VESC Tool → Stop script).  Confirm the Pico
      marks push data stale within 30 ms — AIMD-FF should keep
      increasing `k` (no spurious MD) but never trip
      unlock_thresh from zero signal.
- [ ] Math is unit-checked offline by
      `scripts/bench/test_vesc_lisp_aggregation.lisp` via the
      standalone lbm_eval at
      `C:\VSProjects\lispBM\tests\lbm_eval.exe`.

---

## Known pitfalls

- LCD is **4-bit parallel GPIO**, not I²C.  Earlier drafts of this file
  wrongly said to run an I²C bus scan.
- Do **not** re-enable `os.dup2` redirects in `sim/run_tune.py` — it kills
  VS Code's integrated terminal (ConPTY).  `sys.stdout` redirect only.
- When editing settings from a tune, copy params by hand from the
  `sync_tune_params.py` diff so the change is visible in `git diff`.
- `sim/physics.py` treats the motor sun-gear speed as a kinematic
  constraint of wheel + carrier speed.  It does **not** carry an
  independent rotor-inertia state and does **not** model the one-way
  freewheel between the ring gear and the wheel.  Results are only
  faithful while the band brake is engaged (carrier locked).  Coast
  traces will disagree with the sim — that is expected.
# Bench Test Notes

## Purpose

Capture how the firmware should be brought up safely on the bench.
Record known-good wiring, test supply settings, expected serial output,
and observations during bring-up and troubleshooting.

---

## Staged Test Plan

### Stage 1 — LCD Only
- [ ] Verify I2C bus scan detects LCD
- [ ] Verify LCD initializes and displays text
- [ ] Record working I2C address and pin assignment

### Stage 2 — Throttle Read Only
- [ ] Connect throttle to ADC input
- [ ] Verify idle and full-scale raw ADC values
- [ ] Record actual calibration range
- [ ] Verify deadband suppresses zero-throttle noise

### Stage 3 — ADC Voltage Read Only
- [ ] Connect capacitor voltage sense divider
- [ ] Apply known voltage and verify displayed value
- [ ] Record divider ratio calibration
- [ ] Verify sanity checks for disconnected input

### Stage 4 — UART Telemetry Only
- [ ] Connect Pico TX/RX to VESC UART
- [ ] Verify telemetry request is sent and response is received
- [ ] Record actual packet contents and field mapping
- [ ] Verify timeout detection when VESC is disconnected

### Stage 5 — Zero Command Transmit Only
- [ ] Send zero / neutral current command to VESC
- [ ] Verify VESC acknowledges or remains idle
- [ ] Verify no motor movement

### Stage 6 — Assist Command on Stand
- [ ] Mount motor on test stand — wheel free to spin
- [ ] Send small positive current command
- [ ] Verify motor spins in expected direction
- [ ] Verify throttle → assist current mapping

### Stage 7 — Regen Command on Stand
- [ ] Spin motor by hand or from assist
- [ ] Send brake / regen current command
- [ ] Verify braking torque and voltage rise
- [ ] Verify regen cutoff at upper voltage threshold

### Stage 8 — Precharge Validation
- [ ] Start with discharged capacitors
- [ ] Verify precharge path activates
- [ ] Verify voltage rise on capacitor bank
- [ ] Verify transition to REGEN at threshold
- [ ] Verify timeout fault if precharge fails

---

## Safety Checklist Before Motor Testing

- [ ] Motor is secured on a stand — wheel cannot contact anything
- [ ] Emergency power disconnect is accessible
- [ ] Capacitor bank voltage is known before applying power
- [ ] VESC current limits are set conservatively
- [ ] Firmware motor command limits are set conservatively
- [ ] A second person is present or aware of testing

---

## Observations

_Record observations during bring-up below._
