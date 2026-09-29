# ReGenX v2: design-side gap sweep

**Basis.** This covers the design side of the review of main @ `1a410c4`: `v2/design/`, `v2/research/`, `v2/reviews/` and the v1 hand-over notes. It was produced without reading `v2/firmware/` or `v2/tests/`, so §1 is framed as the checklist the code is checked against. The code was reviewed afterwards: see [README.md](README.md) and [findings-detail.md](findings-detail.md). Measurement status comes from SPEC §11 ("No entry below has been measured") and the BOM Inspection sheet, where every Result/Date cell is blank. Numbers come from 8 extractor agents (732 items) and adversarial re-checks of 73 earlier review findings. Of 85 candidate cross-document contradictions, 14 survived refutation.

**Source keys**

| Key | Path |
|---|---|
| SPEC | v2/design/RGX-2-001-SPEC.md (Rev C) |
| SCH | v2/design/RGX-2-100-schematic.html (Rev D) |
| BOM | v2/design/RGX-2-002-BOM.xlsx (Rev A; sheets BOM / Compatibility / Inspection / Queued changes / Sources) |
| FW | v2/design/RGX-2-003-FW.md (Rev B) |
| DEC | v2/research/decisions.md (entry title given) |
| CBM | v2/research/carrier-brake-mechanism.md |
| V2P | docs/V2_PLAN.md: a stale plan recovered from old chat artifacts in this review session. It described the Aug 1–2 Rev A design and has since been deleted. |
| R1 / R2 | v2/reviews/round-1/REVIEW-FINDINGS.md / v2/reviews/round-2/RGX-2-001-REVIEW.md |
| v1 | v1-legacy/ |

---

## 1. Firmware requirements checklist

Notes (n#) are listed at the end of the section.

### 1.1 Link and parser (vesc.py)

| # | Requirement | Source |
|---|---|---|
| L1 | UART0. TX = GP0 (header pin 1) → R3 1 kΩ → A1 RX. RX = GP1 (pin 2) ← R4 1 kΩ ← A1 TX. 3.3 V, 115200 8N1 on both ends, twisted pair, 24 AWG. (n1) | SPEC §8, §7 R3/R4; SCH Interface schedule; FW D4 |
| L2 | Size the `machine.UART` RX ring explicitly (`rxbuf=1024`), **and** `readinto` a fixed 1 KB buffer allocated at init. (n2) | SPEC §10.9; FW D11; DEC "Firmware architecture defined (Rev A)" |
| L3 | Parser is an explicit state machine HUNT→LEN→PAYLOAD→CRC→END. Resync is O(1): a bad byte returns to HUNT at the next byte and increments a counter. No buffer reslicing. | FW D11 |
| L4 | Short frames only. 0x03 (the long-frame start, which equals END) seen in HUNT is junk and counted. Largest receivable frame is 78 B. (n3) | DEC 2026-08-02 "Firmware implementation started" |
| L5 | CRC-16 (VESC CCITT), table-driven from a 512 B `bytes` table, in `@micropython.viper`. A pure-Python fallback is allowed for host tests only. | FW D1, D11 |
| L6 | Parse + CRC < 200 µs per frame on target (gate FW-1). | FW D11, §5 |
| L7 | No allocation after init anywhere on the RX path (readinto, parser, CRC, decode). (n4) | FW D11 |
| L8 | Telemetry uses COMM_GET_VALUES_SELECTIVE (opcode 50) with mask 0x818C: bit 2 avg motor current, 3 avg input current, 7 ERPM, 8 v_in, 15 fault. That gives a 20 B payload and 25 B frame. Poll every second tick (50 Hz), so telemetry age is ≤ 20 ms. (n3) | FW D5, D7, D9; DEC impl entry |
| L9 | Keep a parser for full COMM_GET_VALUES (opcode 4, ~70 B) as a fallback. | FW D5 |
| L10 | Poll A1 FET temperature separately at 1 Hz. | FW D5 |
| L11 | Send COMM_SET_CURRENT (opcode 6) every 10 ms tick (100 Hz), including 0 A. This stream is the keepalive, a 20× margin against the 200 ms A1 timeout. | FW D7 |
| L12 | Wire budget at 115200 is about 11.5 B/ms. Worst tick is TX 30 B + RX 30 B ≈ 5.2 ms, spread over two ticks. Duty is 24 % (DEC) or 26 % (FW). Change baud only if duty exceeds ~60 %. (n3) | FW D4; DEC impl entry |
| L13 | Transport separable from protocol in vesc.py, so UART→CAN is a transport swap only. | FW D3 |
| L14 | RX opcode whitelist {0 FW_VERSION, 4, 6, 50}. Any other opcode is dropped and counted without corrupting state. This includes opcode 36 CUSTOM_APP_DATA from a v1 LispBM script still resident on A1. | v1/README-v1.md §5.1; FW D5 |
| L15 | Link-health counters (CRC errors, resyncs, junk bytes including 0x03, RX overflow, timeouts/silence, RTT) go into every snapshot and every log record, and onto the LINK page. | FW D3, D12, D14; DEC Rev A entry |
| L16 | At INIT, perform the COMM_FW_VERSION handshake. Write the exact A1 FW tuple to the log header every INIT and show it on the SYSTEM page. (n5) | FW D6, D14, §3 |
| L17 | Opcodes, the SELECTIVE bit table and scales are v1 wire facts. Re-confirm them on this A1 at B-9. | DEC impl entry; FW §6 B-9 |

### 1.2 Loop and timing (main / control)

| # | Requirement | Source |
|---|---|---|
| T1 | Control loop at 100 Hz (10 ms tick) on core 0. | FW D8 |
| T2 | Worst-case tick ≤ 4 ms of 10 ms, leaving ≥ 6 ms slack. The MicroPython runtime decision is overturned if FW-2 measures worst tick > 6 ms or GC pause > 5 ms after optimisation. The escape is to move parser + CRC into a frozen native module. | FW §3, D1, D2 |
| T3 | Hardware WDT at 2 s, fed only by the core-0 loop. | FW §3, D13 |
| T4 | On core 0: no allocation, `sleep`, `print`, file I/O, or `ui` import. (n6) | FW §3 |
| T5 | Scheduled `gc.collect()` on a pre-allocated heap, with pauses in single-digit ms. Max pause is measured, shown on the LINK page and reported at FW-2. Which core collects, and when, is **unspecified**. | FW D1, D14 |
| T6 | `@micropython.native` for control and kinematics math (~50 float ops per tick). | FW D1 |
| T7 | No `uasyncio` for the control loop. | FW D2 |
| T8 | Deadline misses are counted. Misses at standstill during a flush are counted and excused. | FW D13 |
| T9 | `dt` is measured elapsed time. Deadlines use `ticks_add` / `ticks_diff`. Timers accumulate (`last += period`). Enums are small ints. These are research carry-overs; Rev B defers "rules" to a Rev A that was never committed. | research/system-design.md §6.6, §8; FW §3 |
| T10 | MicroPython on a Raspberry Pi Pico (RP2040, non-W). Pin the exact release in the repo, chosen at FW-0 (v1.23 has thread regressions that v1.22.x lacks). (n7) | SPEC §2, §10.10; FW §1, D1; DEC "MCU is the Pico" |

### 1.3 Concurrency and core 1

| # | Requirement | Source |
|---|---|---|
| C1 | `_thread` split. Core 0: sensors, vesc, kinematics, control, strategy. Core 1: ui.py (SSD1306, RAM ring logger, flash flush, USB bench). The rp2 port is GIL-less SMP. | FW D2, §3 |
| C2 | Core 1 creates every object at init and allocates nothing in steady state. | FW D2 |
| C3 | The seqlock snapshot is the only shared structure. Single writer (core 0), single reader (core 1). Two pre-allocated `array('f')` plus a version counter. Writer: bump to odd → write spare → bump to even → flip index. Reader: copy → recheck → retry. ~30 floats plus the health-counter block. Core 0 never blocks. (n4) | FW D12 |
| C4 | Single-core fallback, selected automatically if the FW-0 soak fails on 2 pinned releases. Display at 1 Hz, each frame sent as 8 × 128 B I²C chunks (~3.3 ms each at 400 kHz) inside the ≥ 6 ms slack. Logger unchanged. Bench stream rate-limited. ui.py driven by the core-0 scheduler. (n8) | FW D2 |

### 1.4 Sensors (sensors.py)

| # | Requirement | Source |
|---|---|---|
| S1 | SPD = GP13 (header pin 17), read by PIO period capture, never by a Python IRQ. 1 µs resolution. The 32-bit count wraps at 71.6 min, so a stale-period / standstill timeout is required well before that. | SPEC §10.1; SCH Sheet 3 NOTE 4; DEC impl entry |
| S2 | GP13 internal pull-up ON, in addition to R5 10 kΩ → +3V3. R6 1 kΩ series (limits injection to 1.7 mA at 5 V). C6 1 nF C0G (R6·C6 = 1 µs, band ≤ 30 Hz). Schmitt pad. GPIO is not 5 V tolerant. | SPEC §10.2, §7, §8; SCH Sheet 3 NOTE 2 |
| S3 | 6 PPR on a 2.1 m circumference: v[km/h] = 1.26·f[Hz] = 1260/period[ms]. 10/20/30 km/h = 7.9/15.9/23.8 Hz = 126/63/42 ms. Band 8–24 Hz. SHELL_PPR and circumference are config constants. (n9) | SPEC §4.9, §8 |
| S4 | Zero and low speed are **unspecified** (5 km/h = 252 ms period, 1 km/h ≈ 1.26 s). Wheel = 0 must come from an SPD no-edge timeout, never from ERPM, because the rotor is stationary while coasting. See §4 F12. | SPEC §4.9/§8 (gap); BOM Compatibility "Back-EMF, coasting" |
| S5 | THR = GP26/ADC0 (header pin 31), 12-bit, referenced to AVDD. Hall throttle is ratiometric, supplied from +3V3. Chain: J3 SIG → D3 TVS + R2 100 kΩ pull-down → R7 1 kΩ → C7 100 nF at the pin. The ADC reads 0.990 × throttle (100/101). R7·C7 = 100 µs; R2·C7 = 10 ms. | SPEC §7, §8; SCH Sheet 3 |
| S6 | Throttle window: reject outside 0.20–0.85 × V_REF = 0.660–2.805 V = counts 819–3481 (12-bit) = 13107–55705 (`read_u16`). On violation, **zero assist only**. (n10) | SPEC §10.3; SCH Sheet 3 NOTE 6 |
| S7 | An open throttle reads 0 V through R2. τ ≈ 10.1 ms through R7+R2; falling from 0.85 to 0.20 takes ≈ 14.6 ms and passes through the valid window on the way. Idle and open both read < 0.20. | SPEC §9 "Throttle open circuit"; SCH-08 |
| S8 | Map assist from measured endpoints, not the window edges. The v1 unit (WUXING) measured 1073/3238 counts at 3.3 V, i.e. 0.262/0.791, or ≈ 0.259/0.783 after R7/R2. Deadband 0.05, oversample 4. Re-measure on the v2 harness. Do **not** carry v1 fault thresholds 100/4000. | v1/firmware/config/settings.py L91–99; SCH NOTE 5; BOM row J3 L12 |
| S9 | Debounce throttle validity over N samples. N is not given anywhere in design/. | research/hardware-design.md §9b; research/system-design.md §7 |
| S10 | VSYS on ADC3 (internal GP29 = VSYS/3): V = 3 × 3.3 × raw/65535. 5.0 V reads 1.667 V ≈ 2068 counts (12-bit), ≈ 2.42 mV of VSYS per LSB. Plain Pico only; on the Pico W GP29 is shared with the CYW43. | SPEC §10.6, §7 U2; SCH Sheet 3 NOTE 7 |
| S11 | Bank voltage and ERPM come only from A1 telemetry (`v_in`, ERPM); there is no local divider. `v_in` is the A1 terminal voltage: OCV − I·R_total while boosting, OCV + I·R_total while regenerating, with R_total = R_esr 180–360 mΩ + R_ext 6.9 mΩ = 0.187–0.367 Ω. | SPEC §8, §10.7, §4.1 |
| S12 | Nine U2 pins only. GP14/GP27/GP28 unallocated. No bank/pack divider, mux, hall tap, SD or status LED. Any added 5 V-domain input needs ≥ 1 kΩ series and a pull-up to 3V3. | SPEC §8; SCH Sheet 3 NOTE 2 |
| S13 | Brake-lever input is first-class but optional, present or absent at runtime. Pin and type are undecided (§3). Absence must be a config flag, because a normally-open switch with pull-up reads "released" when not fitted. | FW D9, D10; FWDOC-143 |

### 1.5 Kinematics (kinematics.py)

| # | Requirement | Source |
|---|---|---|
| K1 | ω_motor = ERPM / POLE_PAIRS × DIR_SIGN (mechanical, forward +). ω_carrier = (ω_motor + k·ω_wheel)/(1+k). ω_free = k·ω_wheel/(1+k). s = clamp(ω_carrier/ω_free, 0, 1), where 1 = freewheel and 0 = held. (n11) | FW D10; SPEC §1 |
| K2 | Torque ratio T_sun : T_ring : T_carrier = 1 : k : −(1+k). T_sun = 0 means all torques are 0, so zero motor current gives zero rear carrier braking. Carrier locked ⇒ ω_sun = −k·ω_ring. P_heat/P_total = s. | SPEC §1 |
| K3 | Provisional constants: POLE_PAIRS = 10, K_NOMINAL = 5.0, so wheel rpm = ERPM/50 **only while the carrier is held (s = 0)**; otherwise wheel speed comes from SPD. DIR_SIGN is set on the bench at B-2. (n12) | BOM Compatibility rows 2, 8; Queued changes #4; SPEC §11 items 2, 9; FW D10 |
| K4 | A wrong DIR_SIGN makes the computed carrier speed 2kω_w/(1+k), so s = 2 and clamps to 1 during braking. | FW D10 |
| K5 | Assist-time cross-check: with the carrier grounded by the clutch, abs(ω_motor) = k·abs(ω_wheel). Calibrate k live on every assist episode. Drift beyond ±5 % sets the SENSOR flag on the LINK page. | FW D10 |
| K6 | s = 0 both in assist and in held braking. Slip is only meaningful together with the mode (throttle/brake); the envelope decides the mode. | FW D10 |
| K7 | CPython-clean, host-tested behind hal.py. | FW §4 |

### 1.6 Control, envelope and states (control.py)

| # | Requirement | Source |
|---|---|---|
| E1 | States INIT → RUN → LIMP. INIT runs a self-check (contents **unspecified**) and the FW handshake. RUN is the only state that commands nonzero current; INIT and LIMP command 0 A. Behaviour on an unexpected FW tuple is unspecified. | FW §3, D6 |
| E2 | No PRECHARGE, FAULT-contactor or sleep state. OFF is S1 only; switch on means running. | SPEC §2, §6; FW §3 |
| E3 | LIMP is entered on telemetry silence > 250 ms, strategy exception, VESC fault code, or throttle-window fault (n10). In LIMP, command 0 A and keep parsing. Link-silence LIMP auto-recovers after 10 clean frames (≥ ~200 ms at 50 Hz). Other causes "auto-recover where the cause clears, latch where it doesn't". The per-cause latch/recover matrix, debounce, dwell and the meaning of "clean frame" are **undefined**. | FW §3, D7 |
| E4 | A strategy exception gives 0 A + LIMP, never a crash. | FW §3 |
| E5 | The envelope lives in control.py and is applied **after** the strategy, before TX. | FW §3 |
| E6 | Regen taper: full at v_bank ≤ 38 V, linear to 0 at 40 V. Whether it acts on terminal `v_in` or OCV is **unspecified**. (n13) | FW §3; SPEC §6 BANK FULL, §10.8 |
| E7 | Crossover guard: regen = 0 when v_bank > 39 V and v > CROSSOVER_KMH. CROSSOVER_KMH = 28 km/h until kV is measured. Estimated slope 0.70 km/h/V: 8.9 km/h at 12.7 V, 28.0 at 40 V, 31.8 at 45.4 V. | FW §3; BOM Compatibility rows 11–13; Queued #4 |
| E8 | Battery current ≤ 40 A, mirroring the A1 config. | FW §3; SPEC §10.4 |
| E9 | Slew limit on commanded current at 10 ms granularity. **No value given.** | FW §3, D8 |
| E10 | Throttle and brake together: regen wins. | FW §3 |
| E11 | BANK FULL is taper only: no dump, no phase short, no handbrake or short-circuit braking mode. | SPEC §6 |
| E12 | On boot and on MCU reset (VSYS < 1.8 V), commands are 0 A. C4 220 µF gives 6.8 ms hold-up (5.5 ms at −20 %) at 0.35 W. That is decoupling only, not ride-through. | SPEC §4.8, §6, §9 |
| E13 | Exactly one writer of the current setpoint: the Pico over UART. The ADC/UART hybrid is rejected. | FW D15 |
| E14 | The §2 invariant (≥ 2 boosts 10→20 km/h at 100 kg, ΔKE 1 157 J, η_conv 0.70, from a full 40 V bank, ESR-loaded) holds only for I_boost ≤ 38.7 / 26.2 / 19.7 A at 180/270/360 mΩ. Including 85 J of drag it is ≤ 22.2 / 16.8 A at 270/360 mΩ. At 40 A it gives 1.97/1.52/1.13 boosts. **The envelope has no cap for this** (§3, §4 F28). | SPEC §2, §4.2 |
| E15 | Implied assist terminal floor: A1 v_in under load ≥ V_startup + V_margin = 8 + 1.0 = 9.0 V. So V_lo = 9.0 V + I_boost·R_total: at 20 A, 12.74/14.54/16.34 V; at 40 A, 16.48/20.08/23.68 V. **Not in §10 and not in the envelope** (§4 F4). | SPEC §3, §4.2 |

### 1.7 Strategy contract (strategy.py)

| # | Requirement | Source |
|---|---|---|
| P1 | `class Strategy: def update(self, s, omega_wheel, v_bank, throttle, brake, i_motor, dt) -> float`. Returns commanded motor current in A (+ assist, − regen). Pure, no allocation (n4). The Rev A signature without `brake` is superseded. | FW D10; DEC "rescrutinized (Rev B)" |
| P2 | `brake` may be absent at runtime; the strategy must work without it. | FW D9 |
| P3 | The control law is deferred to the scoring work. FW-5 runs a placeholder strategy inside the envelope. | SPEC §2, §12; FW §1, §5 |
| P4 | Context for whoever writes the law: 6 PPR staleness forces a slip setpoint ≥ 10–15 % (5 % gives ~70 % error; research used 0.12). More regen current means more slip. Pad heat = s·P_braking, about 350–525 J per stop at 10–15 %. | research/motor-selection.md §4; research/system-design.md §6.1; CBM §4 |

### 1.8 UI and logging (ui.py)

| # | Requirement | Source |
|---|---|---|
| U1 | DS1: SSD1306 128×64 on I²C0 at 400 kHz, address 0x3C, SDA GP4 (header pin 6), SCL GP5 (pin 7), +3V3 ~20 mA. Pull-ups on the module only, none on U2. framebuf, refreshed at 5 Hz, on core 1. | SPEC §7, §8; SCH Sheet 3 NOTE 3; FW D14 |
| U2 | Pages. RIDE: speed, bank V, power, slip bar. LINK: health counters, RTT, deadline misses, GC max pause, SENSOR flag. SYSTEM: VSYS, temps, state (INIT/RUN/LIMP), last fault, A1 FW tuple. | FW D14, D10 |
| U3 | I²C errors are counted. Re-init is lazy, never scheduled, and never runs on core 0. | FW D14 |
| U4 | Every budget number has a LINK counter: tick ≤ 4 ms, GC pause ≤ 5 ms, frame < 200 µs, silence 250 ms, RTT, deadline misses, wire duty, telemetry age ≤ 20 ms. FW-7 exits when the LINK page is green. | FW §4, §5 |
| U5 | Only FET temperature is valid. A1 `temp_motor` is meaningless (sensing disabled, J2 pin 6 unconnected) and must never be displayed or used. | SCH Sheet 3 NOTE 1; FW D5 |
| U6 | Logger: binary RAM ring of 24 B records via `struct.pack_into`, 96 KB = 4096 records = 6.8 min at 10 Hz while riding, 1 Hz at standstill. **The record layout is not defined in design/.** | FW D13; DEC impl entry |
| U7 | Flush to flash only at verified standstill: wheel = 0 and throttle idle > 3 s (the idle threshold is undefined). An XIP erase stalls both cores, typically ~45 ms per 4 KB sector. Target: littlefs region ≥ 1 MB, wear-levelled; ≤ 96 KB per flush. | FW D13; SPEC §10.9 |
| U8 | No synchronous flash writes while moving. "Moving" is undefined in SPEC. | SPEC §10.9 |
| U9 | USB bench mode streams data and never touches flash; rate-limited in the fallback. | FW D13 |
| U10 | VSYS is logged. Rate and capture method are unspecified, and the 5.5–6.8 ms hold-up is shorter than one tick. (n14) | SPEC §10.6; SCH Sheet 3 NOTE 7 |
| U11 | Power budget: +5 V BEC 1 500 mA with 70 mA load (U2+DS1, 0.35 W); +3V3 ≥ 300 mA with 30 mA load. | SPEC §4.8 |

### 1.9 Safety, protection and A1 configuration

A1 configuration is a commissioning checklist (FW D6). SPEC does not require U2 to read it back or verify it.

| # | Requirement | Source |
|---|---|---|
| A1 | Battery current limit 40 A. | SPEC §10.4, §7 A1; SCH Sheet 2 NOTE 2; FW D6 |
| A2 | Max input voltage 40 V. This is the only bank-overvoltage element at regen currents. VESC 5.x raises FAULT_CODE_OVER_VOLTAGE once the integrated excess over l_max_vin exceeds 0.05·l_max_vin = 2.0; 5.x has no soft regen-voltage limit. (n13) | SPEC §10.5a, §9; SCH Sheet 2 NOTE 2; FW D6 |
| A3 | Motor-temperature sensing disabled; J2 pin 6 unconnected. | SPEC §10.5; SCH Sheet 3 NOTE 1; FW D6 |
| A4 | App = UART at 115200. C-0 instead uses "ADC and UART" with the throttle on A1's ADC; switch back to the UART app for FW-5 onward. v1 found "App to Use" to be the most often missed step. | FW D6, D15; v1/README-v1.md §11.4 |
| A5 | App timeout 200 ms and timeout brake current 0. A dead Pico, wedged loop or cut harness then releases current within ≤ 0.2 s. | FW D6, D7 |
| A6 | Sensored FOC on M1 halls H1–H3 (J1 pins 3–5 → J2 pins 3–5, 5 V A1 domain). | SPEC §7, §8 |
| A7 | Stay on the installed major FW line; update only to Flipsky-blessed builds, and only for cause. (n5) | FW D6 |
| A8 | **Not specified in design** (§4 F6): regen battery limit (`l_in_current_min`), motor current max/min including brake, `l_abs_current_max`, `l_min_vin`, battery cut start/end, FOC "Sample in V0 and V7" / lower observer KI. | FWDOC-127; RES-09/10/11 |
| X1 | Above the back-EMF crossover the body diodes rectify into the bank regardless of the command, including after an A1 OV fault. Commanding 0 A is not protection. | SPEC §9 "Bank overvoltage" |
| X2 | Zero motor current means zero rear braking. This happens in LIMP, under the taper, under the crossover guard, and on A1 timeout or fault. The front brake is the fail-safe. | SPEC §1; DEC 2026-08-01 "Rear wheel" |
| X3 | Accepted without mitigation: no bank temperature sensing, no motor thermal protection, no annunciation of F1/F2/F3/S2/BMS operation. | SPEC §9, §12 |
| X4 | No sleep. With S1 closed and stationary, BT1 supplies 3.50 W and lasts 4.0 h (14.25 Wh usable). | SPEC §2, §4.5 |

### 1.10 Testing

| # | Requirement | Source |
|---|---|---|
| Q1 | Host-first. kinematics, strategy, the parser state machine and the envelope must be CPython-clean behind a ~30-line hal.py; rp2/PIO/`_thread` stay behind hal. | FW §4 |
| Q2 | Parser property tests: random corruption, truncation and reordering must give no false accepts and no stuck states. (n15) | FW §4, §5 FW-1 |
| Q3 | Replay regression inputs: v1 ride logs plus C-0 observer corpora. | FW §4 |
| Q4 | v2 unit tests run in CI beside v1 (`.github/workflows/tests.yml`: Python 3.13, unpinned `pytest numpy scipy`, triggers only on push/PR to main). | FW §4 |
| Q5 | Nothing in v2 imports from v1-legacy; v1 tools are ported, not imported. | README.md Ground rules; v1/README.md |
| Q6 | Each implementation gate lands together with its test. | DEC Rev A entry |

### 1.11 Tooling and deploy

| # | Requirement | Source |
|---|---|---|
| D1 | Pinned MicroPython release in the repo. None was found outside `v2/firmware/`, which was not inspected. | FW D1, §5 FW-0 |
| D2 | Board: plain RPI_PICO (RP2040, non-W). | SPEC §7 U2; DEC "MCU is the Pico" |
| D3 | A1 provisioning through a v2 tool or a written VESC Tool procedure that applies A1–A8. No LispBM. UART0 on GP0/GP1. | FW D5, D6 |
| D4 | No design text exists for: a deploy script for the 8 flat modules, a WDT safe-boot / bench escape, a host decoder for the 24 B records, bench-stream capture, or mpy-cross / unix-port CI. See §4 F23/F36, §6. | V1-35..38, V1-41; FWDOC-136 |

**Notes**

- n1. V2P §1.2 "R3/R4 100 Ω" is a Rev A value and superseded. v1 tools default to UART1 on GP4/GP5, which are the DS1 I²C lines in v2 (§6).
- n2. FW Rev B states only the 1 KB readinto buffer. The driver `rxbuf=1024` appears only in DEC Rev A (FWDOC-140).
- n3. FW Rev B was not updated: it says "~30 B" and 26 %, and has no long-frame rule (FWDOC-139). The row uses the DEC implementation-entry values (21/21 pre-code checks).
- n4. On rp2, floats are heap objects (MICROPY_OBJ_REPR_A), so float decode and float math allocate. See §4 F14.
- n5. D6 assumes a "5.2-class" tuple. The owned A1 is recorded at FW 6.6 / HW 410 (§5 C10).
- n6. Conflicts with float boxing (§4 F14) and with the D2 fallback, which runs ui on core 0 (§5b).
- n7. The BOM does not lock the board variant (Pico vs Pico W), §4 F32.
- n8. Contradicts T4 ("no `ui` import on core 0") and D14 ("never on core 0"). The rp2 I²C default timeout is 50 ms, longer than the slack (§4 F24).
- n9. research/system-design.md §9 `WHEEL_RADIUS_M = 0.33` (2.073 m, −1.3 %) is superseded by the 2.1 m circumference.
- n10. FW §3 sends a throttle-window fault to LIMP, which also zeroes regen. SPEC §10.3 and SCH NOTE 6 (assist only) are authoritative (§4 F1).
- n11. The research magnitude form (k·ω_wheel − ω_motor)/(1+k) (hardware-design.md:197, system-design.md:168, decisions.md:364) is superseded.
- n12. v2/README.md gives k ≈ 4.7 (the sim's gear set 21/39/99), which is 6.4 % off and exceeds the ±5 % SENSOR tolerance. Design wins, so k = 5.0. SPEC §11 lists pole pairs as "unknown" and ratio "~5:1" as unverified.
- n13. The taper endpoint equals the A1 OV trip with zero margin (§5 C03, §4 F2–F3).
- n14. SPEC §12 "Data logging — out of scope" contradicts §10.6, §10.9 and FW D13 (§5b).
- n15. Not achievable with CRC-16 (a random frame is accepted with probability ~2⁻¹⁶, or ~2⁻²⁴ counting the END byte). The implemented scope (DEC) is: single-bit corruption never delivers a wrong payload, and recovery costs ≤ 1 following frame.

---

## 2. Implementation gates and bench/measurement schedule

### 2.1 Firmware gates (FW §5)

| Gate | Content / pass criterion | Needs | Retires | Status |
|---|---|---|---|---|
| FW-0 | Pin the MicroPython release. 24 h dual-core soak (loop skeleton, display churn, ring writes). Failure on 2 releases selects the single-core fallback. | Pico + USB | D2 | Pending ("FW-0..FW-2 pending a Pico", v2/README.md) |
| FW-1 | Viper CRC + parser + host property tests + on-target timing < 200 µs/frame. | Pico | D11; "postmortems #2/#3" (dangling ref) | DEC reports 43 host tests passing, but **v2 tests have never run in CI** (run 36467082911 on 1a410c4 failed at the v1 step, so the v2 step was skipped). On-target part pending. |
| FW-2 | Timed 100 Hz loop + counters + WDT. Report worst tick and GC pause. Fail at > 6 ms or > 5 ms; budget is ≤ 4 ms. | Pico | D1, D8 | Pending |
| FW-3 | Bench link: SELECTIVE at 50 Hz, RTT measured, FW tuple logged. Prerequisites: A1 set to A1–A5, v1 LispBM erased, ≤ 0.2 s release on keepalive loss verified. | A1 | D5, D6, D7 | Pending |
| C-0 | Observer rides on A1's "ADC and UART" app. Pico is read-only and collects telemetry + wheel corpora. **No rear regen braking** in this mode. | Motor, teardown, B-10 | D15 | Pending |
| FW-4 | PIO wheel capture + throttle + brake (if fitted), verified by drill-spin. Prerequisite: SPEC §11 item 8. | Motor | "§7" (dangling; intended as SPEC §10.1–3), D9 | Pending |
| FW-5 | Kinematics + envelope + placeholder strategy, wheel off the ground, k cross-check live. Prerequisites: B-2, POLE_PAIRS, k. | Motor + carrier brake | D10 | Pending |
| FW-6 | Core-1 display + logger + XIP-aware standstill flush. | Pico (+A1) | D13, D14 | Pending |
| FW-7 | First UART-controlled ride with the LINK page green. | All | v1 failure mode | Pending |
| (missing) | No gate covers: fixed-current open-loop regen into the capacitor bank (research bring-up step 5); closed-loop slip Kp/Ki tuning on a stand (step 6); A1 release ≤ 0.2 s on keepalive loss; maximum XIP stall; A1 achievable regen current versus ERPM and v_bank; VESC ERPM filter lag. | — | — | Not scheduled |

### 2.2 Bench items (FW §6)

| Item | Content | Feeds | Status |
|---|---|---|---|
| B-2 | DIR_SIGN from carrier-braked counter-rotation | D10 | Pending |
| B-9 | Confirm A1 FW tuple; confirm the SELECTIVE 0x818C response (25 B) parses | D5, D6 | Pending. v1 records **6.6 / HW 410**, not 5.2 |
| B-10 | "ADC and UART" app works for C-0 observer mode | D15 | Pending |
| B-11 | Probe the Mini FSESC4.20 for a CAN transceiver or pads | D3; SPEC §12 | Pending |
| — | B-1 and B-3..B-8 are not defined anywhere. B-2/9/10/11 reuse the numbers of SPEC §11 items 2/9/10/11, which have different content. | — | Doc defect |

### 2.3 SPEC §11 measurement schedule (items 1–2 run first)

| # | Item | Assumed | Firmware consumer | Inspection line | Status |
|---|---|---|---|---|---|
| 1 | G020 teardown for carrier access. Failure obsoletes the design. | Accessible, Case B clutch | Whole design; the k cross-check assumes Case B | BOM Insp. #9 | Pending |
| 2 | M1 kV, phase R, pole pairs | kV_wheel ≈ 5.56 rpm/V (est.); pp 10 (BOM) / unknown (SPEC) | CROSSOVER_KMH, POLE_PAIRS, assist availability, E(ω)/(3R) ceiling | BOM Insp. #8 (kV only) | Pending |
| 3 | A1 start-up voltage, rising and falling | 8 V | V_lo, 9.0 V assist floor, battery cut, keep-alive (R1_max 8.00 Ω, collapse 10.76 V) | none | Pending |
| 4 | C1–C3 capacitance (match ≤ 10 %), leakage at 13 V / 24 h, balance type | ≤ 10 % spread | 40 V ceiling. A worse spread drops it to 38 V, which moves the 38/39/40 V constants and the A1 40 V setting. | BOM Insp. #2 | Pending |
| 5 | Bank ESR by step load, pass/fail | 180–360 mΩ | Assist cap (≤ 355 mΩ at 20 A, ≤ 270 at 26.2 A, ≤ 174 at 40 A), V_lo, ESR-compensated taper | none | Pending |
| 6 | R_ext four-wire at 20 A | 6.9 mΩ | R_total | BOM Insp. #5 (S1 contacts only) | Pending |
| 7 | Bank self-discharge over 72 h | 0.37 mA at 14 V | — | none | Pending |
| 8 | Shell-sensor output structure by scope, unpowered and powered | Open collector | Gates GP13 connection | BOM Insp. #1 (unpowered only) | Pending |
| 9 | Gear ratio by hand rotation count (DEC says tooth count) | ~5:1 | k | none | Pending |
| 10 | A1 BEC 5 V / 1.5 A at the connector, under load | Yes | U2 supply | BOM row A1, col L10 | Pending |
| 11 | Telemetry round-trip at 115200 | "tolerable" | D9 ≤ 20 ms age; slip lag | FW-3 | Pending |
| — | Unscheduled §11 parameters: thermistor on the speed wire (a 10 k NTC would give ≈ 1.75–1.8 V high level, below V_IH 2.0 V); C_rr and C_d·A (~85 J per boost); Bafang hall mapping (BOM "Bench verify"); J3 at 3.3 V (SCH NOTE 5, BOM L12); SSD1306 daylight readability | — | GP13, N_boosts, commutation, throttle, UI | none | Pending |

### 2.4 BOM Inspection sheet (all Result/Date cells blank)

| # | Check | Criterion | FW-relevant |
|---|---|---|---|
| 1 | Higo/Z910 pinout map | Unpowered: continuity, phase symmetry, hall supply polarity, white wire vs rotation on scope | Yes (SPD source, 6 PPR) |
| 2 | C1–C3 | Each capacitance within ≤ 10 %; leakage at 13 V / 24 h; balance type | Yes (V_hi) |
| 3 | BMS configuration | UV 3.00 / OV 4.25 V per cell, verified on a bench supply before connecting cells | No |
| 4 | U1 | 17.2 V / 0.2 A into a dummy load; conducts only above 19.2 V input; no reverse leakage | No |
| 5 | S1 contacts | Four-wire at 20 A (feeds R_ext) | Indirect |
| 6 | S2 | Closed when cold; **trip 105–115 °C** (spec says 100–110 °C, §5 C70); manual reset latches | No |
| 7 | D3 | V_C ≤ 4.5 V at 1 A from the batch datasheet; no I_R check | Indirect (THR) |
| 8 | M1 kV | Drill-spin, open-circuit phase voltage vs rpm; replaces 5.56 rpm/V and the crossover table | Yes |
| 9 | Teardown | Carrier access | Yes |
| — | Teardown questions q1–q9 (CBM §7). FW-relevant: q1 Case A/B; q8 clutch direction vs cassette (DIR_SIGN). **Missing from the list:** sun/ring/planet tooth counts, rotor magnet count, shell magnet count and sensor type (DEC 2026-08-01 "Motor" asks for tooth count). | — | Yes |
| — | Not on the sheet: DS1 3.3 V logic, address 0x3C and pull-up value (BOM L31); A1 J2 JST-PH pin order; A1 configuration readback. | — | Yes |

---

## 3. Open decisions and queued hardware changes

### 3.1 BOM "Queued changes"

| # | Target | Change | Applied? | Firmware impact |
|---|---|---|---|---|
| 1 | Drawing Rev E | W1 becomes a 9-pin Higo Z910 splitter; J1 redrawn as the motor's single cable (3 phase + 5 V/GND/H1–H3 + white speed wire). Described as "electrically identical". | No; SCH is still Rev D | SPD = white wire, now inside the phase-lead jacket, which conflicts with SCH Sheet 1 NOTE 3. Needs PIO glitch rejection (§4 F20). |
| 2 | SPEC §7 S2 | ≥ 50 → ≥ 24 V DC, justified as "≤ 23 V open contact" | No; the drawing is not queued | None. The actual open-contact voltage is V_bank − V_pack: 23.2 V (16.8 V pack), 28 V (12.0 V pack), 33.4 V (45.4 V bank). ≥ 24 V is too low. The KSD301 (48 V) would pass a ≥ 45.4 V requirement. |
| 3 | SPEC §4.5 | Standby 93 → 82–91 days with a Bluetooth BMS | No | None |
| 4 | Control law | Full-bank regen tapered to 0 above ~28 km/h; wheel rpm = ERPM/50 | Partly, via the FW §3 crossover guard | CROSSOVER_KMH; ERPM/50 is valid only with the carrier held |

### 3.2 Hardware changes claimed or needed but not queued

| Item | State | Firmware impact | Source |
|---|---|---|---|
| Brake-lever sensor for drawing Rev E | FW D9 and DEC say "queued into Rev E", but it is absent from the Queued sheet, SPEC Rev C and SCH Rev D. Options: digital hall/reed/cutoff switch on GP14 with pull-up (D9); GP27/ADC1 (V2P); analog pressure or travel sensor (research). Which lever it senses (carrier brake or rear disc) and whether brakes are hydraulic or mechanical (V2P §1.5 Q4) are unanswered. | 10th pin; meaning of `brake`; lever-to-torque latency ~100–160 ms → ~35 ms | FW D9; DEC Rev B; CBM §6 |
| Part-number back-annotation | SPEC §12 and SCH say "P/N TBD" for S1, S2, the F1 block and J4. BOM selects WATERWICH 8–60 V DC 275 A; KSD301 110 °C manual reset; Blue Sea 5502100; SkyRC iMAX B6 + XH-4S. | None | SPEC §12; BOM rows 19, 22, 24, 25 |
| BT1 capacity basis | SPEC uses ~15 Wh / ~1.01 Ah; BOM buys 4S 18650 cells at ≥ 1 Ah (in practice 2.5–3.5 Ah ≈ 37–52 Wh). | None (SPEC figures are conservative) | SPEC §4.5, §4.7; BOM row BT1 |
| CAN provision | Not on the drawing (SN65HVD230 + 2 GPIO). can2040 needs a custom MicroPython build and a PIO block. | vesc.py transport; FW-0 re-soak | FW D3; SPEC §12 |
| VSYS back-feed diode | No Schottky or ideal diode between BEC and VSYS. With USB connected and S1 open, ~4.7 V back-feeds the A1 BEC and hall 5 V rail. | Refuse RUN if v_in < 8 V; log when USB-powered | SCH Sheet 3 |
| Mechanical RGX-2-2xx | Torque-arm plate (reversing load; net ≤ 45 N·m; spider 54 N·m nominal, 80 N·m design) and 2a disc/spider/ramp are not in the BOM. | None | CBM §6, §8 |

### 3.3 Open decisions

| Decision | Options / current state | Trigger / owner | Firmware impact | Source |
|---|---|---|---|---|
| A1 firmware line | D6 says "stay on shipped 5.x", but the owned unit is recorded at 6.6 / HW 410 | Owner; B-9 will reveal it | INIT tuple check must accept 6.6, or D6 must be reworded. LispBM is available on 6.6. | FW D6; v1/firmware/config/vesc_snapshot_meta.txt |
| Assist current cap vs §2 invariant | Config allows 40 A; invariant needs ≤ 26.2 A at nominal ESR, ≤ 19.7 A at 360 mΩ (22.2 / 16.8 A with drag). Owner accepted "~2 boosts". | Owner; SPEC §11 item 5 | Envelope/strategy cap parameterised on measured ESR | SPEC §4.2; DEC "External design review" |
| Low-voltage assist floor and A1 cut values | Nothing specified. v1 used l_min_vin 10 V (README-v1 says 14 V) and cut 10/9 V. | SPEC §11 item 3 | Envelope floor v_in ≥ 9.0 V; A1 cut-end ≥ 9 V | SPEC §3; FWDOC-127; DEC-RF-01 |
| Regen battery limit | Unspecified; VESC `l_in_current_min` is a separate setting | Commissioning | Set −40 A explicitly | SPEC §10.4 |
| Taper reference and margin | Taper runs on terminal v_in from 38 → 40 V, which equals the A1 OV trip. Options: end the taper ≤ 39 V; taper on OCV = v_in − I_in·R_total; or raise A1 max vin to at most 45.4 V (a SPEC change). | Design decision | control.py taper; slew value | SPEC §6, §10.5a, §10.8; FW §3 |
| CAN escape | Only on link-health evidence (CRC bursts correlated with phase current while buffers are healthy); depends on B-11 | Data | Transport swap + custom build | FW D3 |
| LispBM push telemetry | Upgrade path only if RTT/freshness data shows the request hop matters (~1–2 ms, ~8 B) | Ride data | None by default | FW D5 |
| Baud rate | 115200; revisit if wire duty > ~60 % | Counter | — | FW D4 |
| Runtime escape | Frozen native parser + CRC if FW-2 exceeds 6 ms tick / 5 ms GC | FW-2 | Custom build pipeline | FW D1 |
| Single-core fallback | Automatic if FW-0 fails on 2 releases | FW-0 | C4 path | FW D2 |
| Full-bank braking | Deleted (no dump, no phase short, no sun-hold); revisit only if the fade proves objectionable | Ride data | None | DEC 2026-08-01 "Sensing from the motor"; SPEC §6 |
| Rider warning of regen withdrawal | None specified; §12 accepts no hardware annunciation | — | RIDE-page indicator | §4 F18 |
| Tighter slip sensing | Side-cover magnets or direct carrier sensing if < 10–15 % slip is wanted | Teardown | SHELL_PPR constant | DEC 2026-08-01; research/motor-selection.md §4 |
| Mechanism concept | 2a is default; alternatives 2e / 2b / Path 1. If the 2c pawl latch is chosen, firmware must zero regen ~50 ms for release and the lever sensor becomes mandatory. | Teardown q5 | Conditional | CBM §5 |
| Clutch Case A vs Case B | Case B expected | Teardown q1 | Validity of the k cross-check | CBM §2 |
| Fallback motor SX2 | 4.78:1, 15 pole pairs, 10 k NTC on the speed wire | Teardown failure | k, POLE_PAIRS, GP13 conditioning | DEC 2026-08-01 "Motor"; research/motor-selection.md §2 |
| Control law and sim location | Law deferred. The sim is to be "revamped" inside frozen v1-legacy, or copied to v2/sim with provenance. | Owner; needs a decision entry | strategy.py | SPEC §12; v2/README.md; README.md |
| A1 config readback at INIT | SPEC is silent on whether U2 verifies A1 limits | — | INIT check that refuses RUN on mismatch | SPEC §10 items 4/5/5a; FW D6 |
| Data-logging scope | SPEC §12 says out of scope; §10.6 and FW D13 require logging | — | Logger scope | SPEC §10, §12 |
| Non-firmware | U1 deletion (logged, not relitigated); pack UVLO ~13.2 V (not adopted); F3 3 A time-delay (no answer); TP1 (not adopted); S1 anti-spark; polarised BT1 connector; LIC or 3S2P bank (superseded) | Owner | None | DEC; R1; R2 |

---

## 4. Review findings still open after the adversarial check

Verdict key: U = unresolved, P = partially resolved. Items whose verdict was resolved (RES-16, R2-3d) are excluded.

### 4.1 Braking path and envelope

| # | IDs | V | Finding | Firmware impact |
|---|---|---|---|---|
| F1 | FWDOC-122, RES-17 (V1-25) | U | FW §3 sends a throttle-window fault to LIMP, which commands 0 A including regen. A loose throttle wire therefore removes the rear brake (T_sun = 0), and "latch where it doesn't clear" makes it last the rest of the ride. SPEC §10.3 and SCH NOTE 6 require zeroing assist only. | Stay in RUN with a debounced, non-latching ASSIST_INHIBIT: clamp the command to ≤ 0 A and keep regen available. Reserve LIMP for link silence, VESC fault and exceptions. Host test: a throttle fault during braking still produces a negative command. |
| F2 | FWDOC-123, SCH-81, RES-12 (R2-CF-4) | U/P | The taper ends at 40 V, which is exactly the A1 OV trip. Telemetry can be up to 20 ms old, ADC tolerance is unallowed for, and VESC 5.x has no soft regen-voltage limit. A trip stops switching, hub braking disappears mid-stop, and firmware enters LIMP. | End the taper with a stated margin (for example 37 → 39 V, as in round 1) or taper on OCV. Size the slew limit so ΔI·R_total per 20 ms stays inside the margin. Count FAULT_CODE_OVER_VOLTAGE separately and re-command reduced regen as soon as it clears, rather than using the generic 10-frame LIMP recovery. Warn the rider. |
| F3 | FWDOC-124, DEC-RF-02 | U | The taper input is terminal v_in. Loop gain is 20 A/V × 0.187–0.367 Ω = 3.7–7.3 with 10–30 ms of delay, so it will limit-cycle (felt as judder). At 40 A of regen the taper starts at OCV 30.5 / 26.9 / 23.3 V and the trip is reached at OCV 32.5 / 28.9 / 25.3 V (180/270/360 mΩ). There is no regen battery limit. | Use v_oc = v_in − I_in·R_total (R from SPEC §11 item 5; use the worst case until measured). Cap I_regen ≤ (40 − margin − v_oc)/R_total. Evaluate the 39 V guard on v_oc. Give the slew limit a number. Add a host simulation test with ESR and 20 ms delay near a full bank. |
| F4 | DEC-RF-01 | U | There is no low-voltage assist limit. From the 12.69 V rest point, the A1 terminal drops below 9 V above 19.7 / 13.3 / 10.1 A and below 8 V above 25.1 / 16.9 / 12.8 A. A1 then browns out, the BEC collapses after 5.5–6.8 ms, the Pico resets, and hub braking is lost until INIT → RUN completes. | Envelope: I_assist,max = max(0, (v_oc − 8 − 1.0)/R_total), or taper assist to 0 as v_in approaches 9.0 V. Add A1 battery cut-end ≥ 9 V as a backstop. |
| F5 | SCH-80 | U | Assist is physically unavailable above the back-EMF crossover (≈ 0.70 km/h/V; 20 km/h needs ≈ 28.5 V of EMF). If the kV estimate holds, N_boosts ≈ 1.01 / 0.75 / 0.51 against SPEC's 2.49 / 2.23 / 1.99 (20 A; 180/270/360 mΩ), so the §2 invariant is probably not met. | Gate assist: limit or zero it when v > k_v·(v_in − i_in·R_total) − margin. Run the k cross-check only while the clutch is engaged. Show assist as unavailable. Redo SPEC §4.2 after kV is measured. |
| F6 | FWDOC-127 | U | The D6 A1 checklist is incomplete. It has no `l_in_current_min`, `l_current_max/min`, `l_abs_current_max`, `l_min_vin`, or `l_battery_cut_start/end`. | Extend D6: `l_in_current_max` 40, `l_in_current_min` −40, motor limits matched to the strategy, `l_min_vin` ≤ measured start-up voltage, cut values against V_margin, `l_max_vin` 40. Log a config hash next to the FW tuple at INIT, and consider refusing RUN on mismatch (R2-CF-3). |
| F7 | FWDOC-128 | P | The crossover guard only avoids commanding extra regen; it is not protection. Within the v_bank > 39 V gate, the fixed 28 km/h is about 2.5 % off. The round-1 "UNCONTROLLED" handling was not carried forward and not rejected. | Compute the crossover from kV × v_bank (≈ 0.70 km/h/V until measured). Add an UNCONTROLLED flag, set when ERPM-derived EMF > v_in or when i_in shows charging while the command is ≈ 0. On that flag, freeze integrators, log and display. |
| F8 | RES-14 | U | There is no low-speed regen cutoff. Negative SET_CURRENT near standstill with the carrier held may drive the wheel backwards or stall-heat the motor (inference, not yet bench-verified). v1 used entry 116 / exit 77 rpm (~3 / ~2 km/h). | Zero regen below ~2–3 km/h with hysteresis (≈ 794 / 1190 ERPM at ERPM/50), and only with forward rotation. Treat slip as invalid when ω_free ≈ 0. Once kV and R are known, cap regen at 0.8·E(ω)/(3R) (RES-02). Add a bench test; alternatively consider COMM_SET_CURRENT_BRAKE. |
| F9 | FWDOC-131 | U | C-0 rides have no rear braking. Above ~8.9 km/h at the 12.7 V rest point, body-diode rectification gives uncontrolled, fading braking and an unregulated charge current. | C-0 procedure: front brake only, or configure an ADC brake channel with an explicit regen limit. Observer shows a "C-0: NO REAR REGEN" banner and logs i_in. |
| F10 | FWDOC-132 | U | The rider gets no warning when regen is withdrawn (LIMP, taper, crossover guard, A1 timeout or fault). | RIDE-page "REAR BRAKE LIMITED/OFF" whenever state ≠ RUN, A1 is faulted or timed out, or the ceiling is cut below a threshold. Optional LED. Log the derate reason. |
| F11 | DEC-RF-04 | U | One rear lever cannot drive both the carrier brake and the rear disc. Hydraulic vs cable is still open. | Treat `brake` as advisory and optional. Allow regen only when measured slip s < 1 (carrier actually held). GP14 allocation waits for Rev E. |
| F12 | FWDOC-130 | U | Standstill is undefined. ERPM = 0 while coasting. At 1 km/h there is one edge every 1.26 s. The ring overwrites itself after 6.8 min without a qualifying stop. | wheel = 0 when no SPD edge for ≥ ~1.5–2 s (≤ ~1 km/h), with ERPM = 0 as an extra condition. Use the same timeout to drive ω_wheel → 0. Count ring overwrites; the wear figure is per flush. |
| F13 | V1-13 | U | "v1 logs prove 40 A regen" is not supported. Commanded −40 A, but measured motor current bottomed at −12.03 A (log 182457) and −20.6 A (175003), input current at −5.91 A. There are opposite-sign samples (+26.17 / +29.41 A at a −40 A command). | Treat 40 A regen as unvalidated. At C-0/FW-5 log command vs i_motor/i_in/ERPM/v_in at ≥ 50 Hz. Close the loop on i_motor. Flag sustained abs(cmd − i_motor). Add a FET-temperature derate. Check the sign together with DIR_SIGN at B-2. |
| F14 | DEC-RF-05 | U | At 10–15 % slip, pad heat is 350–525 J per stop, ≈ 178–282 W at 1.6 kW. | Include pad energy as a scoring cost. Integrate s/(1−s)·abs(P_elec) per stop and flag long high-slip descents. |
| F15 | DEC-RF-07 | U | The clamp was sized at 54 N·m instead of the 80 N·m design capacity (4.2 kN normal, 212–423 N of cable). Motor Current Max Brake is not set. | Set the motor brake current so (1+k)·Kt·I_motor stays within the clamp capacity actually achieved. |
| F16 | R2-P2 | P | The ~85 J drag term is not folded in. The invariant limits drop to 22.2 A (270 mΩ) and 16.8 A (360 mΩ). | Any assist target meant to honour §2 must be parameterised on measured ESR and C_rr/C_d·A, not hard-coded at 26.2 A. |
| F17 | R1-F7d | U | Round-trip efficiency (~19–31 %, i.e. one-way 0.44–0.56 squared) is not stated anywhere; the research docs mislabel 45 %. | Strategy and scoring must charge ESR + conversion loss on capture too. Any "boosts left" readout must use the ESR-loaded V_lo, not ½CV². |

### 4.2 Robustness, timing and concurrency

| # | IDs | V | Finding | Firmware impact |
|---|---|---|---|---|
| F18 | FWDOC-126 | U | The slip formula divides by zero at ω_wheel = 0, and only strategy exceptions are fenced. An escaped exception stops the loop: WDT reset at 2 s, A1 releases at 200 ms. | Define s for abs(ω_wheel) < ω_min (fixed value + invalid flag). Handle reverse rolling. Wrap the whole core-0 tick in try/except → 0 A + LIMP + counter. Add host tests. |
| F19 | FWDOC-125 | P | "Allocation-free" core 0 is not achievable: floats are heap objects on rp2 (REPR_A), there is no GIL, and the single GC mutex blocks. This contradicts D12 "no lock" and D10 "no alloc". | Either (a) viper/int fixed-point math on core 0, or (b) bounded allocation: core-0 collects in a scheduled slot with `gc.threshold`, and core 1 is truly allocation-free, including number formatting. FW-0/FW-2 measure GC cadence and lock wait. Correct the D12/D10 claims. |
| F20 | SCH-86 | U | Rev E puts SPD inside the phase-lead jacket, and the only filter is R6·C6 = 1 µs. | PIO rejects periods < ~20–25 ms (30 km/h is 42 ms). Optionally require N consistent periods. Count glitches on LINK. Cross-check against ERPM while the carrier is held. |
| F21 | FWDOC-129 | U | The flush is budgeted at the typical 45 ms per sector. The W25Q16JV maximum tSE is ≈ 400 ms (not re-verified), which exceeds the A1 200 ms timeout and the 250 ms LIMP threshold. | Budget for the maximum. Erase one sector per scheduler slot, with keepalive and telemetry in between. Excuse or tag A1 timeouts and LIMP during a flush. Abort on a throttle change or SPD edge. Put flush duration and max stall on LINK. |
| F22 | RES-26 | U | VSYS sags (5.5–6.8 ms hold-up) are missed by a 10 Hz log and a 5 Hz page, and a brown-out loses the unflushed ring. | Read ADC3 every tick; store per-record min/max and a count of ticks below a threshold (e.g. < 4.5 V). Log `machine.reset_cause()` and a boot counter in the log header at INIT, and show them on SYSTEM. |
| F23 | FWDOC-136 | U | On rp2 the 2 s WDT cannot be stopped and survives soft reset, so mpremote, REPL and bench sessions race it. There is no spare button. | Provide a boot-time escape checked before arming: strap pin, throttle held high at boot, VBUS on GP24, or a `/nowdt` flag file. Arm the WDT after init succeeds. |
| F24 | FWDOC-135 | U | Nothing checks that core 1 is alive. | Core 1 increments a heartbeat word; core 0 checks it at ~1 Hz and sets CORE1_DEAD plus an LED pattern. Wrap the ui thread in try/except with a restart counter. Inhibit flushing while core 1 is unhealthy. |
| F25 | FWDOC-134 | U | In the fallback, rp2 I²C defaults to timeout = 50 000 µs, far above the 6 ms slack. | Construct I2C with timeout ≈ 4–5 ms. Check the remaining budget before each chunk. Drop the rest of the frame on error. Keep re-init off the control path. |
| F26 | RES-23 | U | CAN via can2040 needs a custom MicroPython build. The MCP2515 alternative needs GP18/GP19, costs ~100–200 µs per frame, and needs a drawing change. | If the escape is taken: custom image with can2040, a PIO/IRQ budget alongside the GP13 capture, and re-run FW-0/1/2. |

### 4.3 Constants, calibration and hardware interface

| # | IDs | V | Finding | Firmware impact |
|---|---|---|---|---|
| F27 | FWDOC-133, DEC-RF-03 | P | Nominal k is 5 in the BOM but 4.7 in the README (6.4 % apart, beyond the ±5 % tolerance). The teardown list lacks tooth counts, magnet counts and sensor type. SPEC item 9 changed the method from tooth count to hand rotation. | K_NOMINAL 5.0 and POLE_PAIRS 10 until measured. Apply ±5 % against the configured constant. Log live k and never adopt it silently. Feed the calibrated PP·k product into slip. Tooth counts are still needed for torque scaling. |
| F28 | (see E14) | — | Covered by F16 and §3.3 "Assist current cap". | — |
| F29 | SCH-82 | U | D3 (SMBJ3.3A) leaks 50–200 µA at 3.3 V, varies by maker and rises with temperature. It sits on the hall output ahead of R7, and no I_R limit is specified. | Calibrate endpoints in-circuit with D3 fitted. Log raw counts. Check that the calibrated top stays inside the window across temperature. |
| F30 | SCH-83 | U | USB back-feeds through the Pico's VBUS→VSYS diode into the A1 BEC and hall 5 V rail when S1 is open (FW-3, C-0, bench). | At INIT, refuse RUN if v_in < 8 V. Detect USB via GP24 and log it. Fix in hardware with a diode, or by a bench rule. |
| F31 | SCH-85 | P | Board variant is not locked. On a Pico W, GP29 is shared with the CYW43 and needs GP25. | Pin the RPI_PICO build. Check `os.uname().machine` at INIT. |
| F32 | SCH-84 | U | THR_RTN and the C7/R2/D3 returns should land on AGND (pin 33). | Optional: oversample; drive GP23 high (SMPS PWM mode) to reduce ripple on the ADC. |
| F33 | SCH-88 | U | The A1 JST-PH J2 pin order (+5 V/GND/TEMP) is not verified. | No direct impact. Log hall-related VESC faults via the LIMP path. Add a continuity-mapping step. |
| F34 | R2-D3-1 | P | The pin map exists only in SPEC §8, not on the drawing. | Use SPEC §8 (rows L1, S1, S5, U1). |
| F35 | R2-11c | U | F3 (5 A) cannot clear a shorted D1 (4.94 A = 99 % of rating). The JBD BMS is configured for UV/OV only, with no charge overcurrent limit below 4.94 A. | Optional: flag an unexplained standstill v_in decay of ≈ 0.74 V/s at 40 V (idle decay is 0.011 V/s). |

### 4.4 Configuration, tooling and process

| # | IDs | V | Finding | Firmware impact |
|---|---|---|---|---|
| F36 | V1-08 | U | v1 MCCONF offsets are wrong for FW 6.6. Offsets 48/52/56/60/93/97 read ~0; the real values are f32 @28 = 130, f32 @46 = 1500, int16/10 @50/52/54/56 = 10/42/10/9. The owned A1 last read max vin 42 V and battery ±50 A. | Use a deserializer matched to the FW tuple, or VESC Tool. Reconfigure to 40 V / 40 A with verified readback before the first bank charge. Optionally read back at INIT and enter LIMP on mismatch. |
| F37 | V1-07 | U | v1 VESC tools cannot be reused: `EXPECTED_FW (6,6)`, LispBM `conf-set`, v1 imports, v1 limits (±50 A / 130 A / 1500 W / 10–43 V / cut 10→9 V), UART1 on GP4/GP5 (the DS1 I²C lines). A resident v1 LispBM script pushes opcode 36 with a 16 B payload at 100 Hz ≈ 2 200 B/s ≈ 19 % of RX capacity. | Port the tool. Stop or erase the Lisp script at B-9/FW-3 (COMM_LISP_SET_RUNNING 0). Parser counts opcode-36 frames. Fix the v1 README "directly reusable" line. |
| F38 | FWDOC-137, SPEC-REV-01, RES-01 | U/P | `v2-wip` does not exist (the owner pushed the v2 work to `main` instead, so this part is resolved). CI triggers only on main. The only CI run on 1a410c4 (36467082911, job 109079754363) failed in the v1 step ("README has no table row for VCAP_REGEN_TAPER_START_V"), so the v2 step was skipped. The 6810478 run also failed. | Confirm that 1a410c4 is the intended code, or push `v2-wip`. Add branch triggers. Split v1 and v2 into separate jobs or use `if: always()`. Fix the v1 step. |
| F39 | V1-41 | U | CI has no mpy-cross and no unix-port run, so the viper path is never compiled. Dependencies are unpinned and there is no lint. | Run mpy-cross at the pinned version. Run the CPython-clean tests on the unix port, comparing viper CRC with the pure fallback. Split jobs, pin dependencies, add `v2-wip` / `claude/**` / `workflow_dispatch` triggers. Keep rp2 imports behind hal.py. |

### 4.5 No firmware impact

| IDs | V | Finding |
|---|---|---|
| DEC-RF-06 | U | Torque-arm load is overstated. The net frame torque in regen is T_ring ≤ 45 N·m (reversed sense), not 54 N·m. The spider flat does see 54 N·m (80 N·m at design capacity). |
| SCH-87 | U | S1 make ≥ 300 A is inferred from a 1250 A intermittent rating, not a make rating. L1 (Fair-Rite 2743001112) is ≈ 68–70 Ω at 100 MHz against a ~600 Ω spec. |
| R1-F2b, R1-F6b | U | No TP1 for the "< 2 V" check. No pack UVLO (~13.2 V). |
| R1-A1 | U | P_idle is 3 W vs 3.35 W including the BEC. With 3.35 W: collapse 10.97 V, margin 1.03 V, R1_max 7.16 Ω, life 3.80 h. |
| R1-A6 | P | No single-pulse check of R1 against the HS100 part. |
| R1-Q10b | U | D1's reverse-polarity protection covers the bank side only. The D2/U1 path conducts with BT1 reversed. |
| R2-13d | P | U1 dissipation (0.58 W) is not stated in SPEC §7. |
| R2-D1-2/3/4, R2-D2-1/2/5/6, R2-D3-2/3/5/6/7, R2-DB-1/3 | U | Drawing presentation: SPD run along y = 720; halls stub near SW+; no BANK+/GND labels on Sheet 1; flags without leaders; switch terminals the same size as junction dots; no C1–C3 value enclosure; notes-to-title gap 16 px; +3V3 drawn three ways; R3/R4 label placement; no distinct signal-ground symbol; empty W1 boundary; A1 drawn three times without suffixes; SVG min-width 1260 px; all revisions dated 2026-08-02. |
| R2-DB-2b, R2-DB-4 | P | Drawing parts lists have no quantity column; tolerances missing on C5/C6/C7/L1. |
| R2-SIM-2/3 | U | Sim caption ("split depends only on slip") is circular. Clutch badge reads "Overrunning" at slip 0 in regen. |

---

## 5. Contradictions between documents

### 5a. Survived the refutation pass

| ID | Topic | Conflict | Authoritative value | FW |
|---|---|---|---|---|
| C03 | 40 V used three ways | SPEC §4.1 V_hi working 40 V, §6 BANK FULL "V_bank → 40 V" and FW §3 taper 38→40 V, versus SPEC §10.5a / §9 / SCH Sheet 2 NOTE 2 A1 max input 40 V, which is an OV **fault** that FW §3 maps to LIMP. Both act on the same `v_in`. | Both are 40 V, and no document defines a margin, so a decision is needed (§3.3). The steady-state taper does hold v_in ≤ 40 V (OCV 38 V → v_in 39.76 V at 4.8 A with 0.367 Ω). The exposure is transient (loop gain 3.7–7.3). The fault is not latched: VESC auto-clears and LIMP auto-recovers. | Yes |
| C10 | A1 firmware | FW D5/D6 and DEC Rev B say "stay on shipped 5.x (5.2-class)". The owned A1 reports 6.6 / HW 410 (v1/firmware/config/vesc_snapshot_meta.txt; v1/README-v1.md §2; vesc_provision.py `EXPECTED_FW = (6, 6)`). | D6's operative rule is "keep the installed major; update only for cause; record the tuple". Its "5.x" premise is wrong, and read literally it would force a downgrade. The 0x818C mask uses only base bits (2, 3, 7, 8, 15), so it works on both 5.x and 6.x. | Yes |
| C15 | Worst-ESR invariant | SPEC §4.2 says "not met at any current" and §11 item 5 says "unmet at every current". The SPEC's own equations give N rising as current falls. | The invariant is a **cap on boost current**: ≤ 19.75 A at 360 mΩ (N = 2.00 at 19.7 A, 2.24 at 15 A, 2.50 at 10 A). The ESR pass limit is ≤ 355 / 270 / 174 mΩ at 20 / 26.2 / 40 A. At 40 A it fails even at 180 mΩ (1.97). | Yes |
| C16 | ESR cross-reference | SPEC §4.2 cites "§11 item 3". | It should be §11 item 5 (bank ESR by step load); item 3 is A1 start-up voltage. | No |
| C25 | Dangling references | FW D3 "§6.5"; FW-4 "Retires §7"; FW-1 "postmortems #2/#3"; §3 "unchanged from Rev A"; design/README says RGX-2-003 contains a "v1 postmortem". | Counters are defined in FW D3/D12/D14. FW-4 means SPEC §10.1–3 + D9. #2/#3 are O(n²) reslicing and the pure-Python CRC (DEC Rev A order, which is not numbered). Rev A was never committed. The §3 diagram plus hal.py names 7 of the claimed 8 modules. | Yes |
| C27 | Design package zip | design/README says the zip holds "the files above". The zip lacks RGX-2-003-FW.md and carries a stale README (2593 B vs 2904 B) that lists `RGX-2-002-BOM.md` and "the four files". | The on-disk README is correct; rebuild the zip. The four content files in the zip are byte-identical. | Yes (reviewers get no FW doc) |
| C28 | SPEC §5 diagram | The ASCII diagram draws the BEC/U2 branch upstream of F1/S1 and C1–C3 inline. | The BEC sits on SW+, downstream of F1 and S1; with S1 open, A1 and U2 are unpowered. C1–C3 are a shunt from BANK+ to GND. (research/system-design.md §3 has the same error.) | No |
| C62 | Bank rest voltage | SPEC §4.3 "normal start at ~12.7 V rest" vs §5/§6 OFF ~14 V. | A start begins from OFF at ≈ 13.8 V (14 V; 12.5–15.8 V over the pack range) and settles to 12.69 V with S1 closed and stationary (12.8 V at ~79 s, 12.7 V at ~158 s). | No |
| C67 | R1/F3 duty | "53 W / 22 s" and "3.36 A / 90 s" read as sustained. | Exponential decay, τ = 31.3 s. 53.1 W and 3.36 A are values at t = 0 only; power is > 50 W for 0.95 s. At 22 s: ≈ 13 W and 1.66 A. At 90 s: 0.19 A. 22–32 s to 8 V. E_R1 = 832 J to full (629 J to 8 V). The 100 W / 5 A ratings remain valid. | No |
| C70 | S2 | BOM E22 ≥ 24 V; Inspection trip 105–115 °C; Sources link points to a 130 °C part; research says "self-resets". | SPEC §7 / SCH: normally-closed bimetal, opens 100–110 °C, manual reset, ≥ 5 A, **≥ 50 V DC**, P/N TBD. ≥ 24 V is an unapplied queued change (and too low, see §3.1). | No |
| C74 | +5V_RTN | The schedule lists it on sheets 1 and 3; neither sheet labels it, and Sheet 3 draws it with the GND symbol. | 22 AWG from U2 GND to the A1 V− star, the only U2 ground path; U2 is not on GND. | No |
| C75 | Rail name | R5 and J3 flags read "3V3". | "+3V3" | No |
| C83 | BOM Inspection | Refers to "BOM s5.4" (no such section). Example row gives 6.71/6.68/6.74 F per module. | Each module is 20 F (the bank is 6.667 F); each must match within ≤ 10 %. References should be SPEC §7 BT1 / BOM L18 and SPEC §7 S2, §9 / BOM L22. | No |
| C84 | V2P hardware | V2P uses Rev A values: R3/R4 100 Ω, C4 100 µF, F3 3 A fast, 48.6 V, no S2. It also says 500 Hz loop and push telemetry. | Rev D: R3/R4 1 kΩ; C4 220 µF ±20 %; F3 5 A time-delay (3.36 A = 67 %); R5–R7, C6, C7, D3 added; 45.4 V absolute; 100 Hz loop; SELECTIVE polling. | Yes (V2P FW values) |

### 5b. Other conflicts recorded by readers (not re-refuted separately)

| Topic | Conflict | Authoritative value | FW |
|---|---|---|---|
| Firmware status | FW header "Design only — no code exists yet"; v2/README Status "Firmware starts after…" vs its Layout row "Host-testable core is green"; root README; V2P "No v2 firmware pushed; latest main fde9745" | Code exists on main at 1a410c4; these docs are stale | Yes |
| k and pole pairs | k: README 4.7 vs BOM/SPEC/DEC 5. Pole pairs: 11 / 15 / ? / 10 across research and BOM. | k = 5.0 and 10 pole pairs, provisional (SPEC §11 says "unknown") | Yes |
| Throttle fault action | FW §3 LIMP vs SPEC §10.3 / SCH NOTE 6 | Zero assist only | Yes |
| 40 A config vs invariant | SPEC §10.4 vs §4.2 | 40 A stands as protection; the invariant needs ≤ 26.2 A at nominal ESR | Yes |
| RUN floor | SPEC §6 "Bank 8 V–40 V" vs §3 V_margin | Terminal floor 9.0 V | Yes |
| Logging scope | SPEC §12 "out of scope" vs §10.6 / §10.9 / FW D13 | §10.6 governs | Yes |
| OV element vs taper | §10.5a "only element" vs §10.8 / §6 taper | Taper is control; A1 OV is protection | Yes |
| MCU-reset zeroing | SPEC §6/§9 rely on an A1 timeout that is absent from §10 | FW D6: 200 ms, brake current 0 | Yes |
| SPD source | SPEC §8 "5 V at source" vs §11 open-collector | Unverified; R6 limits injection to 1.7 mA | Yes |
| Implementation vs FW doc | Long frames dropped; 25 B vs ~30 B; 24 % vs 26 %; ≈1 100 vs ≈1 150 lines | DEC implementation values | Yes |
| rxbuf | Rev A 1024; Rev B silent | 1024 | Yes |
| "No false accepts" | FW §4 vs CRC-16 limits | Single-bit guarantee | Yes |
| Fallback UI on core 0 | FW D2 vs §3 / D14 | Unresolved; the fallback needs an explicit exception | Yes |
| Brake sensor | "Queued for Rev E" / "the spec's optional brake sensor" vs Queued sheet and SPEC Rev C (neither has it) | Not queued, not in SPEC | Yes |
| B-numbering | B-1, B-3..B-8 undefined; numbers collide with SPEC §11 | Doc defect | Yes |
| Slip formula | Research magnitude form vs signed Willis form | Signed form (FW D10) | Yes |
| Research pin maps | GP4 SPI MISO, GP5 CS, GP26 mux, GP27 bank divider, RP2350 | SPEC §8 | Yes |
| Deleted architecture unmarked in research | system-overview / system-design: PRECHARGE/READY, contactor, BANK_OV, V_MOD_MAX 15.4 V, mux | SPEC §6 states; no per-module sensing | Yes |
| Hold-up | Research 4 ms at 100 µF vs 6.8 / 5.5 ms at 220 µF | 220 µF | Yes |
| Wheel size | 0.33 m radius vs 2.1 m circumference | 2.1 m | Yes |
| COMM_SET_CURRENT_BRAKE | energy-and-idle §4a and v1 docstring say "dissipative" | Regenerative (controller-candidates §2a) | Yes |
| v1 A1 minimum vin | README-v1 14 V (log shows UV fault at 14.0–14.6 V) vs research "cut 10/9 V" vs snapshot 10/42 V | v2 value undefined; derive from SPEC §11 item 3 | Yes |
| README-v1 drift | REGEN_ENTRY/EXIT 25/18 vs settings 116/77; unlock 842 vs 869; tests 311 vs 343 | v1 source code | Yes |
| V2P firmware | 500 Hz loop; push telemetry; energy computed to 42 V; sun-hold; k 4.8 | FW Rev B / SPEC | Yes |
| Drawing omissions | No baud, no 400 kHz on the SDA/SCL row, no GP13 internal pull-up | SPEC §8, §10.2 | Yes |
| Battery limit direction | SPEC §10.4 gives a single 40 A | Regen limit unspecified | Yes |
| Symbol s | Slip undefined in SPEC §1; s reused for capacitance shortfall in §3 | FW D10 definition | Yes |
| CAN "no architecture change" | FW D3 vs custom build requirement | Custom build required | Yes |
| F1 "least energy" | SPEC §4.7 evaluates at 40 V; at 12.69 V a max-ESR short gives 35.3 A < 50 A (min ESR 70.5 A) | Recompute | No |
| Shorted D1 | R1 at 114.5 W (40 V) / 174 W (45.4 V) > 100 W; S2 acts as backup despite "no backup" | Recompute | No |
| D3 clamp | V_C 4.5 V > 3.8 V absolute max | Relies on R7 + internal clamp | No |
| Numeric nits | 7.51 V figure (applies to 4.7 Ω, not the limit); 93 vs 88.6 days; 673× vs 522×; 2.2 s boost only near 40 A (≈ 3.8–4.0 s at 20 A, drag ~13–14 %); reversed-module "20 V"; S1 0.56 J (0.72 J at 45.4 V; implies ~543–700 µF not listed); leakage test at 13 V vs ~4.2–4.6 V per module; 653 J vs 537/635 J; I²t 14 785 vs 14 815; 83.7 vs 85 J; 37.8 kΩ / 70.1 h | Recompute | No |
| BOM vs SPEC/SCH | BT1 capacity; J4 drawn 2-terminal vs XH-4S balance lead; J3 designator; P/N TBD vs selections | BOM selections pending back-annotation | No |
| Stale research docs | design-parameters (N ~1.6/~2.3, V_lo 18.8 V, t_pre 26–67 s, ΔKE 1 159 J); power-architecture (~120 d, τ 5.4 d, 18.4 V, 66 mWh); energy-and-idle (20 F); hardware-design (bleed, S2 auto-reset, R1 50 W, 10 AWG, V_F 0.8 V); CBM Path 1 wording; round-1 decisions entry misquotes | SPEC Rev C | No |
| Drawing hygiene | +5V_RTN; 3V3; missing note flags; zone overlaps; no round-2 disposition entry | SCH needs Rev E cleanup | No |

---

## 6. v1 carry-forward items

### 6a. Carry or port

| Item | Content | v2 action | Source |
|---|---|---|---|
| Sim and scoring stack | sim/ + scripts/: 1 kHz physics, CVaR-20 robust score, JAX DE tuner, neural teacher, PySR. v1 constants: GEAR_N 4.8, 20 F, VCAP_INIT 25 V, 11 pole pairs, λ 0.0111 Wb, R 0.082 Ω (Puyan H01). | Re-base to k 5.0, 10 pole pairs, 6.667 F + BT1/R1/D1 keep-alive + U1 load, 6 PPR quantisation and latency, D10 contract. Decide where it lives (v1-legacy is frozen). | v1/README.md; v2/README.md Status; V1-02, V1-43 |
| v1 scores | LispBM loop assumed each `(sleep 0.001)` took exactly 1 ms, so drpm windows are mis-scaled. unlock_thresh = 869 rpm/s (settings) / 842 (README). | Retune; carry no LispBM-derived threshold. | v1/scripts/vesc_lisp_push_iq.lisp; V1-03 |
| AIMD-FF | Params k, beta_md, unlock_thresh, k_ai; 10-field context | Re-express as a slip law against D10 | V1-04 |
| Ride logs | 4 files from 2026-04-21. 173048 contains only "MemoryError"; 173803: 219 rows / 111.0 s at ~500 ms; 175003: 154 / 37.6 s at ~100 ms; 182457: 169 / 48.2 s at ~100 ms (≈ 197 s total). No wheel speed; Puyan motor. | Replay only the v_bank, i_motor and envelope paths | v1/logs/*.csv; V1-05 |
| data/ traces | drill_trace 1794, phase2_trace 1841, ride_trace 2667 rows; columns include whl_rpm, carrier_rpm, iq, cap_v | Slip-estimator replay after rescaling k 4.8 → 5.0 | v1/data/*.csv; V1-06 |
| A1 config snapshots | Motor ±50 A, battery +50/−50 A, abs 130 A, 1500 W, vin 10/42 V, cut 10/9 V, R 0.0823 Ω, λ 0.0111 Wb, timeout 1000 ms, brake 0; FW 6.6, HW 410; FOC offsets 2049.48 / 2055.13. Earlier snapshot: motor ±15.593 A, battery +20/−8 A. | Reference only. Reconfigure to 40 A / 40 V / 200 ms; re-detect motor parameters for the G020. | v1/logs/*.bin; v1/firmware/config/vesc_snapshot_meta.txt; V1-09 |
| Wire facts and whitelist | Whitelist {0, 4, 6, 7, 30, 36, 48, 50, 63, 86, 159}; test_unknown_opcode_does_not_corrupt_state | v2 needs {0, 4, 6, 50}; ALIVE is unnecessary; 36 is junk | v1/README-v1.md §5.1; V1-31 |
| Throttle calibration | 1070/3240 (settings), sweep 1073/3238; deadband 0.05; oversample 4 | Keep. Drop fault thresholds 100/4000. | v1/firmware/config/settings.py L91–99; V1-32 |
| Commissioning | "App to Use: UART" (most-missed step); 115200; write, then power-cycle | Add to the C-0/FW-5 checklist, including the switch back and the 200 ms / 0 A brake settings | v1/README-v1.md §11.4; V1-33 |
| Bench fixtures | test_vesc_uart_healthcheck, test_vesc_offset_healthcheck, test_vesc_fault_watch (`mpremote run`, PASS/FAIL); bench_test_notes.md | Port to UART0 GP0/GP1 for FW-3/B-9, with no v1 imports | v1/scripts/bench/; V1-34 |
| Motor characterisation | vesc_characterize_motor.py (COMM_SET_MCCONF_TEMP 48/91, FW ≥ 3.42 guard) | Port with v2 pins and limits | V1-07 |
| FOC generator settings | "Sample in V0 and V7" on; lower observer KI | Add to the A1 checklist | research/controller-candidates.md §3; RES-11 |
| Controller rating | 50 A continuous is optimistic; 20–30 A without airflow; DRV8302 is the known weak point | FET-temperature derate (only 1 Hz polling exists today) | DEC "Controller: keep the Mini FSESC4.20"; RES-78 |
| CAPACITANCE_F | 20.0 is the per-module value (v1/firmware/config/settings.py:278) | Bank = 6.667 F | RES-77 |
| R_phase 0.082 Ω | Puyan value (MOTOR_PHASE_RESISTANCE_OHM), not G020 | Measure (SPEC §11 item 2) | RES-41 |
| REGEN_ENTRY/EXIT 116/77 rpm | v1 sensorless-floor rationale no longer applies | Low-speed cutoff still needed (§4 F8) | RES-14 |

### 6b. v1 failure modes and the v2 requirements that answer them

| # | v1 defect | v2 answer (§1 row) |
|---|---|---|
| 1 | No rxbuf (64 B default ring overflows after ~5.6 ms at 11.5 B/ms) | L2 |
| 2 | O(n²) resync (`buf = buf[1:]`) | L3 |
| 3 | Bit-banged CRC, ~5–7 ms per frame (8 shifts × 70 B) | L5, L6 |
| 4 | 70 ms blocking HD44780 re-init every 5 s inside the 100 Hz loop | C1, U3 |
| 5 | Synchronous append to /data/ride_log.csv every 100 ms (220 000 B cap) | U7, U8 |
| 6 | Zero link diagnostics; EMI blamed without evidence | L15, U4 |
| 7 | No time-domain discipline | T2, T4 |
| 8 | A MemoryError (512 B allocation) killed a ride | C2, L7, T5, U6 |
| 9 | ~3 W idle drain | S1 + BT1 keep-alive; no sleep (E2) |
| 10 | Display glitches (HD44780 parallel on GP17–22, backlight on GP28) | SSD1306 on I²C (U1) |
| 11 | THROTTLE_RANGE fault blocked all commands, including braking | Assist-only inhibit (§4 F1) |
| 12 | OVERVOLTAGE at 43.0 V latched until the GP8 reset button | v2 has no reset button; nothing on the braking path may latch; a per-cause latch list is needed (E3) |
| 13 | Faults tripped on a single sample | Debounce/dwell is specified only for link silence (E3, S9) |
| 14 | Full caps meant no brake | Fade accepted; sun-hold rejected (E11) |
| 15 | Dead electronics meant no brake (a normally-closed phase-short contactor was proposed) | Accepted; A1 releases in ≤ 0.2 s (A5) |
| 16 | LispBM timebase error | No LispBM (L8) |
| 17 | UART1 on GP4/GP5 and a GP8 button | UART0 on GP0/GP1; nine pins (L1, S12) |
| 18 | 8 s WDT re-armed in boot.py; mpremote stops the loop feeding it | v2 uses 2 s and needs a safe-boot path (§4 F23) |
| 19 | CSV logs pulled via `mpremote cp`, GP8 USB dump, or miniterm at 115200 | Binary 24 B records; host decoder needed (D4) |
| 20 | USB adds a ground path (README-v1 §12.2 A/B tests) | Battery-powered laptop or USB isolator; tag USB runs (SPEC §8 "sole U2 ground path") |
| 21 | "Pick the latest stable .uf2" | Pinned release (T10, D1) |
| 22 | deploy_to_flash.sh (pkill, package dirs, auto vesc_provision) | New flat-module deploy; provisioning kept separate (D3, D4) |

### 6c. v1 items that must not appear in v2

1 kHz LispBM peak-hold script; drpm_mean / drpm_peak_neg plumbing; AIMD and its 4 params; REGEN_HOLDOFF_MS 300 ms; the RPM entry/exit thresholds as-is; strategy-selection indirection (set_regen_strategy.py); the second LCD driver; vesc_config.py including VESC_OVERLAY_PATCHES and VERIFY_FIELDS; string enums; THROTTLE_FAULT_LOW/HIGH 100/4000; v1 A1 limits (±50 A, 130 A abs, 1500 W, vin 10–43 V, cut 10/9 V, timeout 1000 ms); UART1 GP4/GP5 defaults; the hard `EXPECTED_FW (6,6)` check. Note that V2P's "delete the polled telemetry path" is itself superseded: polling is now the default (FW D5). Sources: V2P §1.4; V1-30; V1-07.

### 6d. Superseded values that must not reappear in constants, tests or docs

| Superseded | Current | Source |
|---|---|---|
| V_hi absolute 48.6 V | 45.4 V (share 0.35714) | SPEC §4.1 |
| R1 real-roots limit 10.1 Ω; R1 50 W / 650 J | R1_max 8.00 Ω, R1 = 4.7 Ω; 100 W / 832 J | SPEC §4.3, §4.4 |
| C4 100 µF, 4 ms | 220 µF, 6.8 / 5.5 ms | SPEC §4.8 |
| E_R1 = ½·C·V_target² for partial charge | ½·C·V·(2V_final − V) | SPEC §3 |
| Taper 37 → 39 V (round 1) | 38 → 40 V (margin issue, §4 F2) | FW §3 |
| Controller timeout 0.5 s; LINK_LOST 500 ms | 200 ms; 250 ms | FW D6, D7 |
| Loop 200 Hz, supervision 20 Hz, log 20 Hz | 100 Hz, telemetry 50 Hz, log 10 Hz (1 Hz at standstill) | FW D7, D8, D13 |
| 19 200 / 460 800 baud; RS-422; 100 Ω series | 115200; 1 kΩ | FW D4; SPEC §7 |
| PRECHARGE/READY/FAULT + contactor on GP17 | INIT/RUN/LIMP | FW §3; SPEC §6 |
| Per-module sensing, BANK_OV, V_MOD_MAX 15.4 V, 74HC4051, bank divider 100 k/7.87 k on GP27 | Telemetry v_in only | SPEC §8, §9 |
| Throttle on 5 V with 15 k/22 k divider; faults at < 0.5 V / > 4.5 V | +3V3; window 0.20–0.85 × V_REF | SPEC §10.3 |
| Pack floor 13.5 V | BMS UV 12.0 V (3.00 V/cell) | SPEC §7, §9 |
| Six modules, ~800 lines | 8 flat modules, ~1 150 lines | FW §3 |
| Hall tap on GP10 with PIO | ERPM telemetry | SPEC §8 |
| SPI display + SD; "core 1 can't stall control" | SSD1306 on I²C; XIP stalls both cores | FW D13, D14 |
| Rev A contract without `brake`; Rev A "core 1 may block" | With `brake`; standstill-only flush | FW D10, D13 |
| Sleep / dormant mode / wake-on-wheel / latching relay; dump resistor / phase-short ladder | No sleep; taper only | SPEC §2, §6 |
| Pico 2 (RP2350); SX2 as "the candidate"; 1 PPR worry | Pico (RP2040); G020; 6 PPR | DEC |
| WHEEL_RADIUS 0.33 m; 20 F energy math | 2.1 m circumference; 6.667 F | SPEC §4.1, §4.9 |

### 6e. Research and round-1 carry-forwards missing from RGX-2-003 Rev B (no rejection recorded)

| Item | Detail | Source |
|---|---|---|
| Speed-scaled regen ceiling | I_MAX_REGEN(ω) = min(40 A, 0.8·E(ω)/(3R)). P_net = E·I − 1.5·R·I² peaks at E/(3R) and reaches zero at E/(1.5R). SX2 stand-in values at 5 km/h: 30 A peak / 61 A net-zero. | research/system-design.md §6.2; RES-02; R1-SNAP1 |
| Uncontrolled region | If E(ω) > V_bank: hold the integrator, flag UNCONTROLLED, command 0. The 8.9 km/h crossover at the 12.7 V rest point means most braking at ride start is uncontrolled. | system-design.md §6.3; RES-03; R1-SNAP2 |
| Shell-sensor loss | Inhibit regen and flag; "do not guess". Inhibiting means zero rear braking, so this needs an explicit decision. | system-design.md §10; RES-06 |
| Debouncing | Throttle validity over N samples; all faults with a minimum dwell | RES-15, RES-19, V1-28 |
| Wheel-speed estimation | Predict forward between edges from the last deceleration. Timestamp ERPM and SPD at capture. Learn the 6 inter-magnet spacing factors while coasting (±5 % ring irregularity). | RES-04, RES-05; R1-Q9a, R1-Q9d |
| Assist limits | Inhibit assist below V_LO; clamp by available bus voltage | system-design.md §6.5; RES-07 |
| Pack health | V_pack ≈ V_bank(boot) + 0.8 V is invalid for this design (V_F 1.0 V; R1 drop 2.11 V with S1 closed; discard boot readings > ~16 V). Add a pack-low warning. | hardware-design.md §9b; RES-20 |
| Housekeeping | Heartbeat on the onboard LED (GP25); a version stamp for our own firmware; log the minimum VSYS over the ride | RES-21, RES-22, R1-F9c |
| ERPM lag | Measure VESC ERPM filtering. The hall-tap fallback footprint was dropped. | hardware-design.md §4; RES-24 |
| Bring-up steps | Open-loop regen into the capacitive bus; stand-tune Kp/Ki | system-design.md §11 steps 5–6; RES-48, RES-49; R1-SNAP5 |
| U1 balance | f_above ≥ 1.067·f_below is a strategy outcome; there is no S1-left-on reminder | SPEC §4.6; RES-27, RES-34 |