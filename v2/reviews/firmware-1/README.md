# ReGenX v2 firmware review 1

| | |
|---|---|
| Against | `main` @ `1a410c4`: `v2/firmware/` (9 files, about 1,230 lines) and `v2/tests/` (43 tests) |
| Design baseline | Spec RGX-2-001 Rev C, drawing RGX-2-100 Rev D, BOM RGX-2-002 Rev A, firmware architecture RGX-2-003 Rev B |
| Date | 2026-09-29 |
| Files | This summary · [findings-detail.md](findings-detail.md) (every finding with evidence, fix and verdicts) · [design-gap-sweep.md](design-gap-sweep.md) (requirements checklist, gates, open decisions, open review findings, contradictions between documents) |

## Fix status (2026-09-29)

Addressed on branch `claude/hopeful-gauss-xk356r`. Wherever possible the
approach was to remove the machinery a defect lived in rather than add
another check.
- **Production firmware:** 1,226 → ~835 lines; 16.1 → 10.8 KB `mpy` bytecode.
- **Tests:** 43 → 272, driven through a Willis/clutch plant.
- **Re-review:** an independent re-review of the rewrite found 8 more defects
  (flush churn while parked, a file left open on a write error, a blocking
  UART read, the `nomain` escape soft-rebooting, slewed throttle release,
  the `reset()` hook outside the guard, the throttle never disarming, and
  over-counting). All are fixed.
- **CI:** green on both jobs.
- **Docs:** rationale in `research/decisions.md` (2026-09-29); rules in
  RGX-2-003 Rev C.

| Status | Findings |
|---|---|
| **Fixed** | F01 (sign fixed by A1 provisioning; `DIR_SIGN` deleted), F02, F03, F04, F05, F06, F08 (guard removed), F09, F10, F11, F13, F14, F15, F18, F19, F20, F21, F22, F23, F25, F26, F28, F29, F30, F34, F35, F38 (metric removed), F41, F42, F44 (stream removed), F45, F46, F48 and F55 (k cross-check removed), F50 and C04 (swappable strategy removed: one built-in PI, no NaN path, no reset hook needed), F51, F54, F56, F57, F59, C02 |
| **Partly** | F16 (`tools/A1-SETUP.md` checklist, no automated provisioning), F31 (µs glitches rejected, partial phases can still read up to ~2× for one or two samples), F36 (Wheel, Throttle and Log host-tested; `main.py` and Core1 target-only), F37, F39 (worst tick incl. GC, and misses), F40 (health counters on every page, no separate LINK page), F43 (brake, state, temperature added), F49, F53 (exact reply lengths), F62, F63 |
| **Closed by owner decision** | C01 and F52: no lever sensor. Regen is a slip regulator and the throttle ends regen; with the throttle held, the carrier lever cannot brake (accepted). F17: faults auto-retry, each retry ramping from 0 A (accepted, RGX-2-003 Rev D) |
| **Open: needs the owner or hardware** | F31 residual (C6 value: scope the sensor under PWM first, spec §11 item 8), F27 (slew rate versus the D9 latency budget) |
| **Open: measure on a Pico** | F32, F33, F58 (allocation, GC scan, parser speed: gates FW-1/FW-2), C03 |
| **Open: minor** | C05 (logged `i_in` keeps the VESC sign: + = drawing from the bank) |

**RGX-2-003 Rev D (same day, owner):** the ride log, observer mode (C-0),
the firmware-version request, the supply-voltage reading, the worst-tick
metric and the viper CRC were removed. Findings against them no longer apply:
F11, F13, F14, F23, F25, F42, F43, F44, F45 and C05, the log half of F36,
F39's worst-tick half, and F58's CRC half. F53 is now fully fixed: the parser
accepts only the one reply length.

## Method

**Firmware.** Six independent reviewers each covered one area:
- safety and states;
- VESC link and protocol;
- sensors;
- the MicroPython/RP2040 runtime;
- UI and logging;
- tests and tooling.

A dedup pass merged their findings. Every finding was then checked by agents told to *refute* it: two independent verifiers for high and critical findings (one traced the code and reproduced the behaviour; one checked the physics and the design intent), and one verifier for the rest. A final critic looked for anything missed. The verifiers built closed-loop plant simulations and fetched primary sources: VESC bldc 5.02/5.03 `commands.c`, `mc_interface.c` and `mcpwm_foc.c`, and MicroPython `py/gc.c`, `ports/rp2`, `machine_adc.c` and `rp2_flash.c`.

**Design.** Eight extractors went through the spec, drawing, BOM, firmware architecture doc, research notes, both review rounds and the v1 hand-over:
- 732 items extracted;
- 73 earlier review findings re-checked adversarially;
- 85 candidate contradictions between documents, of which 14 survived.

**Result.** 68 firmware findings: 63 confirmed and 5 refuted. By severity: 1 critical, 8 high, 27 medium, 27 low.

**Tests.** `python -m pytest` in `v2/` passes 43/43 locally. **CI is red and has never run the v2 suite** (F15).

## Verdict

The code follows RGX-2-003's structure closely. That includes the INIT/RUN/LIMP state machine, envelope after strategy, keepalive-by-command, SELECTIVE polling, table CRC with a viper override, PIO period capture, the seqlock snapshot, core-1 UI, the RAM ring and the standstill flush. The host-testable split is good.

It is **not safe to power the motor with it yet**, even for FW-5 with the wheel off the ground:
- The sign convention between commanded current and measured ERPM is broken (F01).
- The placeholder strategy turns assist into regen (F02).
- The throttle reads about 9 % at rest (F05).

Several braking-path behaviours also contradict the spec. None of the on-target gates (FW-0 to FW-7, C-0) and none of the spec §11 measurements have been done. That matches the README, but three documents still say "no code exists yet" (§4).

---

## 1. Fix before the motor is ever powered

| # | ID | Severity | Problem | Fix (summary) |
|---|---|---|---|---|
| 1 | **F01** | critical | **The direction sign is applied to telemetry but not to the command.** `pack_set_current` sends the strategy's signed amps as-is, while `DIR_SIGN` is applied only to ERPM. VESC current and ERPM share one frame (`DIR_MULT`), and with the carrier held the rotor counter-rotates in *both* assist and regen. So "+ amps = assist" is only consistent when `DIR_SIGN = −1`, but config ships `+1`, and B-2 treats it as a free bench value. **A1 wired so counter-rotation reads −ERPM:** a 0.5 s touch of the rear brake latches −40 A of *motoring* torque, the clutch keeps the carrier grounded, and in simulation the bike accelerates from 20 to 54 km/h with no rider input. **A1 wired the other way:** slip pegs at 1 and there is no regen at all. The k cross-check uses `abs()`, so it cannot notice either case. | Map at the protocol boundary: `i_wire = −DIR_SIGN·i_cmd`. Better, fix A1's direction so that +I drives the wheel forward and make `DIR_SIGN = −1` a derived constant. Rewrite B-2 and add the invert setting to the D6 checklist. Add a runtime latch: if `sign(i_cmd)` disagrees with the VESC's motoring-positive `i_motor` for more than 2 frames, enter LIMP. |
| 2 | **F02** | high | **The placeholder turns assist into regen.** `braking = brake or (ω > 30 and s < 0.97)` is tested before the throttle. The clutch grounds the carrier during assist, so s = 0 and regen fires. Reproduced: half throttle at 19 km/h gives **−40 A** instead of +20 A. `test_assist_flows` hides it by feeding a positive ERPM that is physically impossible (F34). | Infer braking from slip only with the throttle at idle plus a hold-off. Move mode arbitration into the envelope (D10). Generate test ERPM from a Willis/clutch plant. |
| 3 | **C01** | high | **With no lever sensor, the rear lever does nothing while the throttle is held.** The clutch already holds the carrier, so pulling the lever changes no measurable signal. "Throttle + brake → regen wins" (RGX-2-003 §3) cannot be implemented, and a stuck throttle has no brake cutoff. `BRAKE_FITTED = False` is the default. | Make the lever sensor, or a cutoff switch on each lever, **mandatory hardware** (drawing Rev E), or refuse assist without it. Put "brake wins" in the envelope. If the sensor stays optional, record the risk in spec §12. |
| 4 | **F05** | high | **The throttle zero point is the fault-window floor (0.20), with no deadband.** The throttle idles at about 0.26 × V_REF, which reads about 9.5 %, so about **3.8 A of assist while parked**. Because the throttle never reads idle, the logger never sees standstill and never flushes. | Separate calibration (`THROTTLE_IDLE` ≈ 0.262, `THROTTLE_FULL` ≈ 0.79) from the fault window. Add a deadband. Build the ADCs from `Pin` objects (F29), then re-measure. |
| 5 | **F03** | high | **A throttle fault puts the loop in LIMP, which also zeroes regen**, in one unslewed step. A loose throttle wire removes the rear brake. This contradicts spec §10.3 and drawing note 6, which say *assist only*. | Keep RUN; force throttle to 0 and show a flag. Correct RGX-2-003 §3 to match. Add a loop test: throttle fault while braking still gives a negative command. |
| 6 | **F18** | medium | **No throttle arming.** A held or stuck throttle gives assist immediately at boot and immediately after any LIMP recovery. | Add an `assist_armed` flag that needs the throttle at idle for about 200 ms after INIT or LIMP, or after a throttle fault. |
| 7 | **F04** | high | **COMM_FW_VERSION is sent once, with no retry and no INIT timeout.** A lost reply (likely on a cold boot, since A1 and the Pico power up together) leaves the bike in INIT for the whole ride. The display shows the normal RIDE page meanwhile (F22). | Re-send every 200–250 ms while in INIT. Add an INIT timeout with a shown reason. Show SYSTEM in every state other than RUN. |
| 8 | **F09** | high | **The regen taper ends at exactly A1's 40 V overvoltage trip.** It acts on *terminal* voltage (OCV + I·R) with 10–30 ms feedback delay and a loop gain of 3.7–7.3. Hard braking trips A1 OV before the bank is full, so the rear brake pulses. | End the taper with margin (for example 38.5–39 V), or taper on `v_oc = v_in − i_in·R_total`, with hysteresis. |
| 9 | **C02, F20** | low / medium | **The slew limit fights the safety clamps.** It runs *after* the clamps, so every promised zero (bank full, brake wins, throttle failed) takes up to 200 ms. It also scales with measured `dt`, so after a stall it allows a full-scale step. | Slew only the strategy's request, then apply the safety clamps (always allowed to move toward 0). Clamp `dt` to one tick. |
| 10 | **F21, F50** | medium / low | **Raw `ticks_ms` subtraction.** At the 2³⁰ ms wrap (12.4 days of uptime) the envelope outputs about −2e8 A, which MicroPython's `struct` silently truncates into a rising current. Separately, a NaN command crashes the loop. | Use `ticks_diff` everywhere. Clamp `dt`. Re-apply the caps after slew. Range-check and NaN-check inside `pack_set_current`. |
| 11 | **F16, F19** | medium / low | **A1 provisioning.** No tool provisions or verifies the D6 A1 settings. The owned A1 is recorded at FW 6.6, 1000 ms timeout, ±50 A, with a v1 LispBM script still installed. D6 also omits `l_in_current_min`, the battery-cut settings and `l_min_vin`, and the envelope has no low-voltage assist floor. The chain is A1 brownout → Pico reset → INIT (F04). | Write `v2/tools/provision_a1.py`: read the FW tuple, stop and erase LispBM, set and verify the D6 fields (including direction, 200 ms timeout, 0 A timeout brake current and cut values), and save a snapshot. Add a V_lo assist floor to the envelope. |
| 12 | **F15** | medium | **CI has never run v2.** The v1 step fails because `test_settings_guard` reads `v1-legacy/README.md`, but the table moved to `README-v1.md`. Runs 36466908779 and 36467082911 are both red, and the v2 step was skipped. | Split v1 and v2 into separate jobs (or add `if: always()`), point the guard at `README-v1.md`, and add an `mpy-cross` compile step. |

## 2. Fix before C-0 commissioning rides

- **F11 (medium): no read-only observer mode.** The link sends SET_CURRENT at 100 Hz in every state, which overrides or fights A1's "ADC and UART" app. D15's C-0 cannot run as designed.
- **F44 / F42 / F45 / F43: C-0 logs would not be usable.**
  - The bench stream cannot be switched on, and would still write flash if it were.
  - Logs have no header (no FW tuple, format version or constants).
  - There is no host-side decoder.
  - Records lack brake state, LIMP reason, temperature, k estimate and link health.
- **Design (sweep F9):** C-0 rides have **no controlled rear braking**; body-diode rectification gives uncontrolled braking that fades. The procedure has to say front brake only.

## 3. Fix before FW-6/FW-7 (display, logging, first ride)

| Area | Findings |
|---|---|
| **Logging can kill core 1** | **F13 (high).** No log rotation: littlefs fills after about 1.5 h of riding, `_flush` raises ENOSPC, and the core-1 thread dies on every later ride (frozen display, no logging). **F25:** core 1 has no exception guard and no heartbeat. **F14:** the ring is flushed only when it is ≥ half full *and* at standstill; power-off is S1, so short rides and every ride's tail are lost. **F23:** one flush is an unbounded write of up to 96 KB (up to about 424 ms per erase, over A1's 200 ms timeout). **F12:** standstill uses wheel speed and throttle only (a lost SPD signal counts as stopped). **F41:** boot-id collisions. |
| **Parser robustness** | **F06.** A false 0x02 start never backtracks. Payload fields containing 0x02 (for example i_motor 5.1–7.7 A) can hold it misaligned; in closed loop each glitch costs about 0.3–0.5 s of LIMP. **F26:** no cap on LEN, so one false start swallows up to 258 B (about 200 ms). **F35:** the corruption property test passes only by RNG luck. **F53, F57:** plausibility checks and counter classes. |
| **Wheel speed** | **F10.** Speed goes stale during hard braking: it updates once per period and never decays with elapsed time, so the slip estimate drifts toward 1 and regen fades. **F31:** no glitch or bounce rejection. **F30:** `SPD_MIN_PERIOD_US = 5000` means 252 km/h, not the 60 km/h its comment says. **F54:** first sample after boot is a partial phase. **F59:** the PIO counter runs out after 71.6 min. |
| **Crossover guard** | **F08 (high).** Zeroing the command only makes A1 release its bridge; above the back-EMF crossover the body diodes rectify anyway. The guard is not a mitigation. Either record the exposure as accepted or add a real sink, and annunciate when `v_in` rises with `i_cmd = 0`. |
| **Display and instrumentation** | **F40:** no LINK page, so FW-7's "LINK page green" has nothing to judge. **F22, F62:** INIT shows the RIDE page, and SYSTEM lacks temps, FW tuple and last fault. **F39:** tick timing cannot support FW-2 (no worst tick, no skipped-tick count). **F38:** "RTT" is quantised to the tick and pairs replies with the wrong request. **F48, F55:** the k cross-check's health flag is never used, its feed gate is never met, and wheel-speed lag biases it. **F56:** `rx_overrun` measures the wrong thing. **C03:** the VSYS log cannot catch BEC sag. |
| **Timing budget** | **F27:** 200 A/s slew adds up to 200 ms to full regen, missing from the D9 latency budget. **F28:** RX is drained after the decision, so telemetry is 18–28 ms old, over D9's ≤ 20 ms. **F49:** stale v_in can drive the envelope for up to about 278 ms. |
| **Strategy contract** | **C04:** no `reset()` / on-enter hook, so a stateful law resumes with stale state after LIMP. **C05:** logged `i_in` uses the VESC sign (positive = discharging), opposite to the project convention. |

## 4. Runtime and performance, to be measured at FW-0 to FW-2

- **F32:** "allocation-free" is not true on rp2. Floats are heap objects, and the link path, snapshot copies and core-1 rendering allocate every tick.
  - Checked in `py/gc.c`: **`gc.disable()` makes heap exhaustion raise MemoryError instead of collecting.**
  - F24's specific consequence (a latched LIMP) was refuted, but free-heap headroom is unmeasured and should be an FW-2 output.
- **F33:** each scheduled `gc.collect()` scans the 96 KB ring, adding about 2–3 ms to one tick in ten.
- **F58:** the parser and decode are interpreted, not viper as D11 decided, so they may miss FW-1's < 200 µs.
- **F29:** `ADC(3)` by channel number leaves the pad pull-down on, so VSYS reads about half. Use `ADC(Pin(29))`.
- **F36, F37, F46, F51, F63:**
  - no `hal.py`, so `sensors.py` and `main.py` have 0 % host coverage;
  - most §3/§4 requirements have no test;
  - no deploy script;
  - the WDT makes redeploys over mpremote a race;
  - imports run outside the boot guard;
  - the single-core fallback cannot be built from `ui.py` as written.

## 5. Refuted (verified and rejected)

| ID | Claim | Why rejected |
|---|---|---|
| F07 | Bank full → no rear brake, unaccepted | Real, but a recorded design decision (spec §6, decision log): the fade is accepted. Rider annunciation of regen withdrawal is still missing (sweep F10). |
| F24 | `gc.disable()` MemoryError latches LIMP | The platform claim is true, but the latch cannot survive a real exhaustion. See F32. |
| F47 | No MicroPython version pin | The pin is FW-0's *output* (D1), and FW-0 has not run. Expected state. |
| F60 | Pico W VSYS caveat | The design fixes a plain RPI_PICO. |
| F61 | Core-1 stack overflow | Did not reproduce. |

---

## 6. Design side: what's missing ([design-gap-sweep.md](design-gap-sweep.md))

**Nothing measured, nothing on target.**
- FW-0 to FW-7 and C-0 are all pending.
- All eleven spec §11 measurements are pending, including the G020 teardown (gate 1; failure obsoletes the design) and kV / R / pole pairs (gate 2).
- Every BOM Inspection row is blank.

`K_RATIO = 5.0`, `POLE_PAIRS = 10`, `DIR_SIGN`, `WHEEL_CIRC_M` and `CROSSOVER_KMH` are placeholders.

**Contradictions that affect firmware** (all survived refutation):

| Topic | Conflict |
|---|---|
| Throttle fault action | RGX-2-003 §3 (LIMP) vs spec §10.3 and drawing note 6 (assist only). The code follows the wrong one (F03). |
| 40 V | 40 V is both the taper end and A1's overvoltage fault, with no margin (F09). |
| A1 firmware line | D5/D6 assume shipped FW 5.x; the owned A1 is 6.6 / HW 410. Read literally, D6 would force a downgrade. |
| k | README says ≈ 4.7; BOM and spec say 5.0. They differ by 6.4 %, beyond the ±5 % SENSOR tolerance. |
| Brake-lever sensor | RGX-2-003 D9 and the decision log say it is "queued into Rev E", but it is on neither the BOM queued-changes sheet nor the spec. |
| Bench item numbers | B-1 and B-3 to B-8 are undefined, and B-2/9/10/11 collide with spec §11 item numbers. |
| Design package zip | `regenx-v2-design-package.zip` lacks RGX-2-003 and carries a stale README. |

**Open design gaps with firmware impact** (earlier review findings still unresolved):
- no low-voltage assist floor (A1 brownout resets the Pico);
- no low-speed regen cutoff;
- the 40 A assist config versus the §2 boost invariant, which needs ≤ 26.2 A at nominal ESR;
- the D6 A1 checklist is incomplete;
- no rider warning when regen is withdrawn (LIMP, taper, guard, A1 timeout);
- maximum flash erase time (~400 ms) exceeds A1's 200 ms timeout;
- no WDT safe-boot escape for bench work;
- no core-1 liveness check;
- "standstill" is undefined in the spec.

**Stale status text:**
- the RGX-2-003 header says "Design only — no code exists yet";
- `v2/README.md` Status says "Firmware starts after measured values…" while its Layout row says the core is green;
- `design/README.md` says RGX-2-003 contains a "v1 postmortem".

## 7. Suggested order

1. **Sign and direction (F01).**
   - Decide the A1 direction convention and derive `DIR_SIGN`.
   - Rebuild the test fixtures from a Willis/clutch plant (F34).
   - Add the `i_motor` sign-plausibility latch.
2. **Braking-path semantics:** F02, C01 (lever-sensor decision), F03, F18, F05 and F29.
3. **Loop hygiene:** F04 and F22, the C02/F20 slew order, F21 and F50, F09.
4. **CI (F15)**, so the above lands with green v2 tests.
5. **A1 provisioning tool (F16/F19).** Then B-9 and FW-3 on the bench.
6. **C-0 readiness:** F11, F42, F44, F45, F43.
7. **FW-6/7 readiness:** logging (F13, F14, F23, F25, F12), parser (F06, F26, F35), wheel speed (F10, F31, F30, F54), LINK page and instrumentation (F40, F39, F38, F48).
8. **FW-0 to FW-2 on a Pico:** measure heap headroom, GC pause and worst tick (F32, F33, F58), then pin the MicroPython release.
