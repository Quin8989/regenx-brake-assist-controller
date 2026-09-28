# ReGenX v2 — recovered plan and gap review

Recovered 2026-09-28. The v2 plan lived in earlier Claude sessions and was never
committed. This file puts it in the repo so it doesn't get lost again. It then
checks the current code against it.

## Where the plan came from

| Date | Source | What it holds |
|---|---|---|
| 2026-08-01 | Artifact **"ReGenX v2 — Phase 1 Research"** (claude.ai/artifact/7Gzbj8PB6ina6a8HCTu45g) | Architecture options, supercap sizing, parts shortlist, firmware rewrite plan, 5 open questions |
| 2026-08-01 | Artifact **"Freegen Planetary Simulator"** (claude.ai/artifact/5Cdr6Ydr8EFz4YXNC8W46z) | Mechanism study: sun = rotor, ring = shell, carrier = braked member, one-way clutch on carrier |
| 2026-08-02 | Artifact **"ReGenX v2 — Schematic Set"**, drawing RGX-2-100 Rev A (claude.ai/artifact/LL4u8k2G5xwEaXAXknfnVV) | 3 sheets: block diagram, power distribution, MCU and interfaces, plus parts lists and net schedule |
| 2026-08-17 to 08-20 | Google Drive: Bafang G020 model zip, `cad_step/` (ring gear, sun gear, carrier ratchet, ratchet race, axle), `planet_36t_reference.step`, `Assem1.SLDASM` | Mechanical CAD of the G020 carrier brake. This looks like the last v2 work in progress. |

The transcripts of the local sessions ("RegenX v2 hardware design review" ×2 on
Aug 2, "RegenX hub motor model" and "G020 planetary gear model sourcing" on
Aug 17) are stored on the PC they ran on. A cloud session can't read them.
They would hold the reasons behind the Rev A changes below.

**Code status.** The latest commit on `main` is `fde9745` (2026-04-23), which is
v1. No v2 firmware has been pushed to GitHub, and none is in Google Drive. The
user says a lot of v2 work is on their local drive. The gap review in §2 is
against GitHub only. Once the local work is pushed (for example to a `v2-wip`
branch), redo §2 against it.

---

## 1. The plan

### 1.1 Concept (unchanged from v1)

This is the carrier-brake regen concept. The geared hub's planetary carrier sits
on a one-way clutch:

- **Assist:** the carrier is grounded by the clutch.
- **Coasting:** the carrier freewheels, so there is zero cogging drag.
- **Braking:** the rider clamps a friction band on the carrier, and motor torque
  on the sun makes the reaction.

The split between electrical energy and band heat depends only on carrier slip:
`P_heat / P_total = s`. Keeping a true freewheel is the whole point. Grin's GMAC
gives it up, which is why the plan chose to build this concept instead of buying
GMAC.

### 1.2 Decisions: Phase 1 (Aug 1) vs Schematic Rev A (Aug 2)

Rev A is later, so it wins where the two disagree. Rows marked ⚠ are still open.

| Topic | Phase 1 research | Schematic Rev A | Notes |
|---|---|---|---|
| Wheel | Front (braking physics, kit-ability) | **Rear, Bafang G020** | Rev A chose rear. Needs torque arm(s) rated for reversed torque. |
| Motor | Front geared hub, or reuse Puyan H01 | **Bafang G020**, single-stage planetary ~5:1, 3 halls, shell speed sensor 6 pulses/rev | Freegen sim used 21/39/99 (k = 4.71). CAD has a 36T planet reference. The exact G020 `k` is not pinned down. |
| Controller | VESC 6-class | **Flipsky Mini FSESC4.20** (kept from v1), sensored FOC | Halls go through the W1 adapter to J2 |
| MCU | Pico 2 (RP2350) | **Pico (RP2040)** | Both are dual-core, so the core split below still applies |
| MCU↔VESC link | RS-422 first, then CAN | **3.3 V UART**, 100 Ω series (R3/R4), twisted pair, **GP0/GP1** | RS-422/CAN is deferred |
| Energy store | 16 × 350 F cells = 20 F @ 43 V, per-cell balancing | **3 × 16.2 V 20 F modules in series = 6.67 F** (48.6 V rated) | About ⅓ of the Phase 1 energy (see §3) |
| Precharge | Resistor + contactor, driven by the MCU | **Passive keep-alive:** 4S Li-ion BT1 → F3 → R1 4.7 Ω → D1 → bank. U1 (LM2596HV CC/CV) charges BT1 back from SW+ through D2 | No MCU pin. The bank never goes fully flat while BT1 is connected. |
| Wheel speed | 32–64 pole magnetic ring counted by PIO. Called "precision-critical". | **G020 shell sensor, 6 pulses/rev, GP13**, internal pull-up | ⚠ Conflicts with Phase 1's own requirement (see §3) |
| Brake lever sensor | Hydraulic pressure sensor or lever hall. "The missing input." | **Not on the schematic** | ⚠ Open. Depends on hydraulic vs mechanical brakes. |
| Display | SPI IPS ST7789 on core 1 | **SSD1306 128×64 OLED, I²C0 GP4/GP5, addr 0x3C, powered from +3V3** | |
| Logging | microSD over SPI on core 1 | Not on the schematic | ⚠ Open |
| Fail-safe | Normally-closed contactor across the motor phases (dead-short braking when power is lost) | Not on the schematic | ⚠ Open. Phase 1 flagged it as needing analysis. |
| Throttle | Hall throttle | J3 powered from +3V3, R2 100 kΩ pulldown, GP26 | Matches v1's calibration (1070–3240 counts) |

### 1.3 Rev A pin map (RP2040)

| Net | Pin | v1 today |
|---|---|---|
| UART_TX → A1 RX | GP0 (UART0) | GP4 (UART1) |
| UART_RX ← A1 TX | GP1 (UART0) | GP5 (UART1) |
| SDA / SCL → SSD1306 | GP4 / GP5 (I²C0) | HD44780 parallel on GP17–22, backlight GP28 (or PCF8574 on GP16/17) |
| SPD (shell speed, 6 ppr) | GP13, pull-up on | — |
| THR | GP26 / ADC0 | GP26 (same) |
| Reset button | not on Rev A | GP8 |
| Power | VSYS ← A1 5 V BEC through L1 / C4 100 µF / C5 100 nF | — |

### 1.4 Firmware rewrite plan (Phase 1)

The goal is about 800 lines in 6 modules, down from about 3,150 lines in 21.
Most of the cut comes from deleting code, not compressing it.

| Module | Job | Est. |
|---|---|---|
| `main.py` | Scheduler and core split. **Core 0 runs control at 500 Hz and nothing else.** | ~80 |
| `link.py` | VESC transport: zero-copy ring buffer, table CRC, live error counters, push telemetry instead of polling | ~180 |
| `sense.py` | Throttle, brake lever, wheel speed via PIO. Debounced validity on all three. | ~140 |
| `control.py` | Slip estimate, slip-speed PI, assist mapping, friction handover when caps are full | ~150 |
| `power.py` | Precharge / keep-alive sequencing, voltage taper, overvoltage, fault debouncing | ~120 |
| `ui.py` | Display and logging. **Core 1 only**, so it can never stall control. | ~130 |

Core relation: `ω_carrier = (ω_motor + k·ω_wheel) / (1 + k)`, with `k = 4.8` in
the Phase 1 doc (v1 motor). The wheel term carries about 83 % of the weight, so
wheel-speed quality decides how good the slip estimate can be.

**To delete:**
- The 1 kHz LispBM peak-hold script (`scripts/vesc_lisp_push_iq.lisp`), including its timebase bug
- The `drpm_mean` / `drpm_peak_neg` plumbing
- AIMD and its 4 tuned parameters, replaced by a 2-gain slip PI
- The polled telemetry path
- `REGEN_HOLDOFF_MS` and the RPM entry/exit thresholds, replaced by the lever sensor plus slip
- The strategy-selection indirection
- The second LCD driver
- `vesc_config.py`
- The string enums

**Tests:** keep about 60–80, covering frame parsing and resync under injected
garbage, the slip algebra, fault debouncing, and precharge / keep-alive
sequencing.

### 1.5 Open questions from Phase 1 (still unanswered)

1. ~~Front or rear~~: Rev A says rear G020.
2. QR only, or thru-axle too? Geared cores use 12 mm threaded axles.
3. Open the motor (now the G020) and check carrier, clutch and axial room. The Aug 17–20 CAD work suggests this was underway.
4. Hydraulic or mechanical brakes? This decides the lever sensor.
5. Budget ceiling. Phase 1 estimated $700–1,600 CAD.

---

## 2. Gap review: what exists vs. what v2 needs

This covers what is in the GitHub repo, which is v1. Local v2 work hasn't been
reviewed yet.

Legend: ✅ done / reusable · 🟡 partial, needs rework · ❌ missing (from GitHub)

### 2.1 Per module

| v2 piece | Status | What's there now | What's missing |
|---|---|---|---|
| Core split, 500 Hz control | ❌ | One cooperative loop at 100 Hz (`FAST_LOOP_PERIOD_MS = 10`). The display re-inits the HD44780 every 5 s with **~70 ms of blocking writes on the control path** (`display_manager._maybe_reinit_lcd`). Flash log writes also run in the same loop. | A `_thread` runner on core 1, a lock-free state snapshot between cores, and a 500 Hz loop. |
| `link.py` transport | 🟡 | `vesc_comm.py` + `vesc_protocol.py` (634 lines) work and are tested. The CRC is bit-by-bit (8 iterations per byte). Resync does `buf = buf[1:]`, copying once per garbage byte. Each frame is copied with `bytes()`. Telemetry is polled every 10 ms, plus the Lisp push. | Ring buffer, table CRC, CRC-fail / resync / frame counters, push-only telemetry. **UART must move to UART0 on GP0/GP1.** |
| Throttle | ✅ | `drivers/throttle.py`. Calibration matches the J3 range on a 3.3 V supply. An open circuit reads near 0 through R2 and trips `THROTTLE_FAULT_LOW`. | Debounced validity. One stale comment in `settings.py` still says "5 V supply, 0.8–4.2 V". |
| Wheel speed | ❌ | `SharedState.wheel_speed_rpm/valid` exist, but **nothing ever writes them**. The km/h branch in `display_manager` is dead code. | A GP13 pulse counter (PIO, or IRQ plus a period timer), 6 ppr scaling, timeout to zero, validity flag, tests. |
| Brake lever | ❌ | None. Regen entry is inferred from motor RPM > 116 with throttle off, plus a 300 ms holdoff. | A sensor choice and a pin. Rev A has no spare pin assigned; GP27/ADC1 is free. |
| Slip estimate + slip PI | ❌ | Regen uses `aimd_ff` (motor-RPM feedforward with AIMD on `drpm_peak_neg` from the Lisp script). `pi_controller` is a decel-proxy PI, not a slip PI. | The ω_carrier estimator with G020 `k` and sign conventions, a 2-gain slip PI, and a sim strategy to tune it. |
| Friction handover when caps are full | ❌ | At 42 V the taper sets regen to **0 A**. See §2.2: with this gearbox that means **zero braking from this hub**. | A "sun hold" mode that holds the rotor near 0 rpm so the band works as a plain friction brake, plus the rules for entering and leaving it. |
| Keep-alive power logic | 🟡 | `PRECHARGE` waits for Vcap ≥ 10 V, with no hardware behind it. The VESC battery cut is set to 10 / 9 V at boot. | Thresholds re-based on BT1 (bank floor ≈ BT1 − D1 ≈ 12.6–15.9 V). **An assist floor above the BT1 level**, so assist never drains the keep-alive pack through R1. Re-derive the battery-cut values. A "BT1 missing" check. |
| Fault debouncing | ❌ | Every fault trips on a single sample (throttle out of range, telemetry age, VESC fault code). | Debounce counters or time windows, as planned for `power.py`. |
| Display | ❌ | HD44780 16×2 drivers (parallel + PCF8574), 16-character page layouts. | An SSD1306 driver (MicroPython `ssd1306` + `framebuf`) and 128×64 pages, running on core 1. |
| Energy / SoC readout | 🟡 | `CAPACITANCE_F = 20.0`; energy % runs from 10 V to 40 V. | 6.67 F, and a floor at the BT1 level. |
| Logging | 🟡 | RAM ring buffer plus a flash CSV (`/data/ride_log.csv`), written from the control loop. | Move to core 1. SD only if it comes back into the hardware. |
| Motor / VESC config | ❌ | Puyan H01: 11 pole pairs, λ = 0.0111 Wb, R = 0.082 Ω, 4.8:1, 0.33 m wheel. The VESC snapshots in `firmware/config/` are for the Puyan. The entry/exit RPMs are derived from 4.8:1. | G020 detection (pole pairs, R, λ, hall table), G020 `k`, rear wheel radius. Re-derive every RPM threshold. Rev A is **sensored**, so the "sensorless floor" reasoning behind `REGEN_EXIT_RPM` no longer applies. |
| Sim | 🟡 | Solid 1 kHz physics with band stiffness, telemetry delay and noise models. `GEAR_N = 4.8`, 20 F, Puyan constants, `VCAP_INIT = 25 V`. | G020 `k`, a 6.67 F bank with the BT1/R1/D1 keep-alive and the U1 load, 6 ppr wheel-sensor quantisation and latency, a slip-PI strategy, and a sun-hold handover. |
| Tests | 🟡 | 343 tests pass (20 s run) against the v1 architecture. | The planned 60–80 on the hard parts. None exist yet for the slip algebra, wheel counter, keep-alive thresholds or SSD1306. |
| Delete list | ❌ | Everything on the §1.4 delete list is still present. | — |

### 2.2 Safety problems in the current fault policy

These are real in v1 today, and v2 must not inherit them. In this gearbox the
torques are locked together: `T_ring = k·T_sun` and
`T_carrier = −(1+k)·T_sun`. **If the motor makes no torque, the wheel gets no
braking torque from this hub, however hard the band is clamped.** Every path
that sets motor current to zero during braking is therefore a lost brake:

1. **A throttle fault kills regen.** `SystemSupervisor._check_throttle_validity`
   raises `THROTTLE_RANGE`. Any fault moves the system to `FAULT`, and
   `_apply_inhibits` blocks *all* motor commands. A loose throttle wire while
   braking removes the brake. A throttle fault should block **assist only**.
2. **Overvoltage latches.** `OVERVOLTAGE` at 43 V is in `LATCHING_FAULTS`, so
   braking stays gone until the rider presses the reset button. v2 should
   switch to sun-hold (friction) mode instead of latching the motor off.
3. **Full caps means no brake.** Between 40 and 42 V the taper ramps regen to
   0 A. This is the "brake disappears" case Phase 1 called out. It is not
   handled yet.
4. **No debouncing.** One bad sample is enough to trigger 1 or 2.
5. **Dead electronics** (VESC timeout, lost power) also means no brake. This
   case needs the hardware fail-safe (the NC phase-short contactor), which is
   not on Rev A.

### 2.3 Known v1 bug the plan mentions

`scripts/vesc_lisp_push_iq.lisp` assumes each loop takes exactly 1 ms
(`(sleep sample-s)`). In reality each loop is 1 ms *plus* the time the loop body
takes. So:

- the "10 ms" window is actually longer, and
- `drpm_peak_neg` (per-sample change divided by a nominal 1 ms) is scaled wrong.

The tuned `unlock_thresh = 869` rpm/s therefore doesn't match what the hardware
measures. v2 deletes the script. If v1 keeps riding in the meantime, time the
loop with `(systime)` instead of assuming 1 ms.

### 2.4 Leftover from v1 (April)

`settings.py` says "firmware ships the PySR distill", but no PySR-derived
strategy exists in `firmware/regen/strategies.py`. The research scripts under
`scripts/pysr*` are unfinished. The v2 plan replaces this with the slip PI, so
this path is probably dead.

---

## 3. Design tensions to settle before writing control code

- **Wheel-speed resolution.** Phase 1 calls wheel speed "precision-critical"
  and says a 3-magnet sensor "is nowhere near adequate". Rev A uses the G020's
  6 pulses/rev. On a 0.33 m wheel at 10 km/h that is about 8 pulses/s, one new
  reading every ~125 ms, which is far too slow to close a 500 Hz slip loop. The
  options:
  - add a 32–64 pole ring, as Phase 1 said;
  - keep motor-side detection (v1 style) and use wheel speed only as a slow
    check;
  - estimate between pulses from motor speed and the last known slip.
- **Energy budget shrank.** Usable bank energy is about ½·6.67·(42² − 15²)
  ≈ 5.1 kJ (1.4 Wh), against 16.6 kJ in the Phase 1 sizing. On the Phase 1
  descent case (100 kg, 5 %, 30 km/h, ≈ 409 W) the bank fills in about **12 s**,
  not ~41 s. The friction handover will run often, not as a rare edge case.
- **No lever sensor.** Without one, "rider intent" still comes from motor RPM
  with the carrier locked, the way v1 does it. Phase 1 wanted to delete that
  path.
- **U1 always draws from the bank.** U1 charges BT1 whenever SW+ is above about
  19 V (up to its CC setting). No MCU enable pin is shown, so firmware can't
  stop it from taking harvested energy.
- **F3 is marginal.** On a cold start with a flat bank and BT1 full, the current
  through R1 is about (16.8 − 0.9) / 4.7 ≈ 3.4 A. That is just over F3's 3 A
  fast-blow rating. It decays with τ = R1·C ≈ 31 s, so it likely holds, but it
  is outside the usual 75 % derating.

---

## 4. Suggested order to resume

1. Answer §1.5 Q2–Q5 and the §3 wheel-speed question. They decide what
   `sense.py` and `control.py` look like.
2. **Fix the fault policy (§2.2 items 1–2) in v1 now** if the bike is still
   being ridden. It is small and it is a safety fix.
3. Bench bring-up on Rev A hardware:
   - move the UART to GP0/GP1;
   - SSD1306 driver on GP4/GP5;
   - GP13 wheel-speed counter;
   - detect the G020 in VESC Tool and snapshot its config.
4. `power.py`: thresholds for the keep-alive bank, the assist floor, debounced
   faults.
5. Update the sim for G020 `k`, 6.67 F + keep-alive, and the 6 ppr sensor.
   Prototype the slip estimator, slip PI and sun-hold handover in the sim first.
6. `control.py` in firmware, then the core split at 500 Hz.
7. `link.py` rewrite. Delete the Lisp script and everything else on the §1.4
   delete list. Trim the tests to the planned 60–80.
