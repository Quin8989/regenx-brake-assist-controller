# Decision log — ReGenX v2

**Scope: v2 only.** Nothing here modifies the v1 firmware, which now lives
frozen in `v1-legacy/firmware/`. Numbers that look like config values are
*targets for the new build*, not edits to the old one.

Parts are still being mixed and matched. A "decided" entry fixes an approach, not
a part number — see `design-parameters.md` for what is genuinely locked and what
recomputes when a part changes.

Newest first. Each entry records what was decided, why, and what it rules out.
Reversing a decision means adding a new entry, not editing an old one.

---

## 2026-09-29 — Firmware simplified after review 1 (RGX-2-003 Rev C)

**Decided.** The firmware was reviewed (`reviews/firmware-1/`), and the fixes
were made by removing machinery rather than adding checks. Production code
went from 1 226 lines / 16.1 KB bytecode to about 835 lines / 10.8 KB. Tests
went from 43 to 272, now written against a Willis/clutch plant. An
independent re-review of the rewrite found 8 defects, all fixed before merge.

- **Sign fixed by provisioning, not configured.** A1's direction is set so
  +current drives the wheel forward. With the carrier held, the rotor then
  turns at +k·wheel, so ERPM ≥ 0 whenever torque flows. `DIR_SIGN` is deleted.
  Review F01 showed that a free sign could turn a brake touch into sustained
  motoring. B-2 is now "set A1's direction" (`tools/A1-SETUP.md` item 4).
- **States derived, not tracked.** RUN iff `LINK_RECOVER_FRAMES` consecutive
  clean telemetry frames have arrived; otherwise LIMP (0 A). A strategy
  exception or an out-of-range/NaN return is DEAD, latched. INIT is gone: RUN
  no longer waits for the FW handshake, which is re-requested until answered
  (F04).
- **Throttle faults stop assist only** (spec §10.3; corrects Rev B §3). The
  throttle reads 0 when outside its window and arms only after it has been
  seen at idle, so there is no throttle fault state at all (F03, F05, F18).
- **No time arithmetic in control.** Timeouts count ticks and the slew is per
  tick, so ticks_ms wrap and stall-scaled steps cannot happen (F20, F21).
- **One voltage rule replaces the taper and the crossover guard.** The regen
  and assist caps keep A1's terminal inside [9, 39] V using
  v_oc = v_in + i_in·R_BANK. This is stable for any true ESR ≤ R_BANK, ends
  1 V below A1's 40 V OV trip, and adds the brownout floor (F09, F19). The
  crossover guard is removed: zeroing the command cannot stop body-diode
  rectification (F08, spec §9). Bank-full fade stays accepted.
- **Slew then clamp; the slew limits build-up only.** Any reduction, including
  a released throttle or a reversal to regen, is immediate, and the clamps act
  in the same tick (C02). There is no regen below ~3 km/h (design sweep F8).
- **Deleted:** seqlock (a torn display/log sample between two ticks is
  harmless), the live k cross-check (k is fitted offline from logs), the RTT
  metric, the full COMM_GET_VALUES fallback, gc.disable() (it turned heap
  exhaustion into MemoryError), and the boot guard (A1's timeout plus the WDT
  are the failure policy).
- **Parser rescans after a false start** and caps LEN at 80. A complete frame
  later in the buffer proves an earlier incomplete start false. Every
  single-bit flip and 200 garbage seeds deliver the next good frame (F06, F26,
  F35).
- **Logging:** one write session per stop, in ≤ 4 KB chunks, never while
  moving. The oldest rides are deleted below 200 KB free, and the file is
  closed at once on any write error, so the littlefs finaliser never runs on
  core 0. It writes a header and catches every exception on core 1 (F13,
  F14, F23, F25, F42).
- **C-0 observer mode:** `SEND_CURRENT = False` (F11).

Open for the owner: fit the lever sensor (without it the carrier lever cannot
override a held throttle, C01); C6 1 nF → 100 nF for shell-sensor glitch
immunity (F31).

---

## 2026-08-02 — Firmware implementation started; host core green

**Decided.** Final pre-code verification passed (21/21 numeric checks: mask
0x818C → 25 B selective response, 24 % wire duty, latency table, Willis
identities, PIO 1 µs resolution with no 32-bit wrap inside 71 min, record
layout 24 B, throttle window counts). Protocol constants taken from the v1
implementation as *wire facts* validated on this exact controller — opcodes,
selective bit table, scales — explicitly not as design inheritance.

Implementation landed in `v2/firmware/` (8 modules) with `v2/tests/`
(**43 passing** under CPython, added to CI beside the v1 suite): viper-ready
table CRC16 with pure fallback, O(1)-resync streaming parser, selective
telemetry parse, link scheduler with RTT/health counters, Willis kinematics
with the live k cross-check, safety envelope, INIT/RUN/LIMP state machine,
seqlock snapshot, ring logger codec, PIO period-capture program, SSD1306
core-1 UI, and the fenced 100 Hz main loop.

**One design change made by a failing test:** the parser drops VESC
long-frame (0x03) support. The long-start byte is identical to the frame-END
byte, so honouring it lets a trailing END swallow the next frame's start
after any resync — v1 documented exactly this ambiguity and kept the
ambiguous path. Nothing our command set can receive exceeds 78 B; 0x03 in
hunt state is junk, counted for diagnostics. The property tests (single-bit
corruption can never deliver a wrong payload; recovery costs ≤ 1 following
frame) now pass by construction.

Remaining before hardware: gates FW-0 (pinned-release dual-core soak),
FW-1/FW-2 on-target timing proofs — need only a Pico.

---

## 2026-08-02 — Firmware architecture rescrutinized (RGX-2-003 Rev B)

**Decided.** Rev A was challenged by the owner as borrowing v1 choices without
justification. The challenge was correct. Rev B restructures the document as a
decision register — alternatives, numbers, and the measurement that would
overturn each choice — after targeted research on the VESC software surface
and the RP2040/MicroPython platform. What changed:

- **Corrected a Rev A error:** RP2040 executes from XIP flash, so a flash
  write stalls *both* cores (~45 ms/sector erase). Standstill-only flushing is
  now understood as protecting core 0, not merely avoiding writes-in-motion.
- **Dual-core demoted from assumption to bet:** MicroPython's rp2 port runs
  GIL-less true SMP with a documented memory-corruption history (#7124 class)
  and release-sensitive stability. New gate FW-0 (pin release + 24 h soak)
  before anything else; a single-core time-sliced fallback is fully specified.
- **UART kept on evidence, not inheritance:** v1's physical layer was blamed
  without proof; the firmware was the killer. Link-health counters become the
  instrument. CAN via can2040 + transceiver is the designed escape hatch —
  the Mini's spec sheet lists CAN, board population unconfirmed (bench B-11).
- **115200 baud justified numerically** (26 % duty, 2.6 ms/frame vs a
  40–130 ms sensing-dominated latency budget) rather than copied.
- **Poll SELECTIVE over LispBM push:** SELECTIVE exists since post-3.41 and
  Flipsky ships 5.2 — no firmware update needed. Push needs 6.x and custom
  code both sides for ~2 ms; it is the upgrade path, not the default.
- **A1 firmware: stay on shipped 5.x** — everything needed exists there;
  clone-flashing hazards are not bought for features we don't use.
- **The latency insight Rev A missed:** the 6 PPR shell sensor (63–126 ms to
  see slip onset) dominates lever→torque delay — no protocol choice touches
  it. A brake-lever sensor collapses the budget ~4× (≈35 ms). Recommended to
  owner as a hardware delta (spare GPIO, queued for drawing Rev E); strategy
  contract gains a `brake` input either way.
- **New for free:** during assist the clutch makes ω_motor = k·ω_wheel exactly
  — every assist episode live-calibrates k and audits the shell sensor.
- **New commissioning mode C-0:** ride first on the VESC's own ADC-throttle
  app with the Pico as read-only observer — proves the powertrain with zero
  control code and collects the scoring sim's first real corpora. The
  ADC+UART *hybrid* as permanent architecture was rejected (two writers, one
  setpoint, undefined arbitration).

---

## 2026-08-02 — Firmware architecture defined (RGX-2-003 Rev A)

**Decided.** Clean-slate architecture, not a v1 refactor. The v1 postmortem was
re-verified against source before designing: no `rxbuf` (64 B default ring),
O(n²) buffer reslicing, bit-banged pure-Python CRC (~5–7 ms/frame), a
*scheduled* 70 ms blocking LCD re-init in the control path, synchronous flash
appends while riding, and zero link diagnostics. Root cause generalised: **v1
had no time-domain discipline** — any module could spend milliseconds on the
shared thread.

Core decisions:

- **Two-core split.** Core 0 runs a 100 Hz allocation-free control loop with a
  per-tick time budget and fences (no sleep, no print, no file I/O, no display
  import). Core 1 owns display, logging, and bench I/O and is allowed to block.
  Seqlock snapshot between them.
- **Link:** explicit `rxbuf=1024`; incremental O(1)-resync parser; viper
  table-driven CRC16; `GET_VALUES_SELECTIVE` minimal mask at 50 Hz with
  `SET_CURRENT` at 100 Hz as the keepalive; A1 app timeout 200 ms as the
  dead-MCU failsafe; link-health counters in every snapshot and log record.
- **Control law stays deferred** behind a fixed strategy contract
  (`update(s, ω_wheel, v_bank, throttle, i_motor, dt) → amps`); a safety
  envelope in `control.py` clamps it (40 V taper, 28 km/h crossover guard,
  current cap, slew limit). Strategy exceptions → zero current + LIMP.
- **Three states** (INIT / RUN / LIMP). No PRECHARGE, no contactor states —
  the firmware does not resurrect what the hardware deleted.
- **8 flat modules ≈ 1 100 lines** vs v1's 21 / ~3 500; `kinematics.py` and
  `strategy.py` are CPython-clean for host tests; v1 ride logs become the
  regression corpus.
- **Implementation gates FW-1..FW-7**, each landing with its test; FW-1/FW-2
  (parser+CRC timing, loop skeleton timing) need only a Pico and can start
  before the motor arrives.

**Ruled out:** uasyncio for the control loop (jitter unbounded by design);
logging to flash while moving under any buffering scheme; re-using v1's
service/driver layering.

---

## 2026-08-02 — BOM consolidated; every part sourced or owned

**Decided.** RGX-2-002 Rev A. Every drawing item resolved to a buyable part or
an owned one; new spend ≈ $580–700 CAD, motor dominant. Findings that changed
the record:

- **G020 confirmed compatible, with real data:** 20 magnets / 10 pole pairs,
  5:1, and the **6-magnet shell speed sensor is confirmed** — spec §4.9's 6 PPR
  was an assumption and is now sourced. Single winding ~200 rpm @ 36 V ⟹
  kV_wheel ≈ 5.56 rpm/V.
- **Back-EMF crossover quantified:** braking below 28 km/h cannot overcharge a
  full bank even through A1's body diodes; exposure is braking > ~32 km/h with
  a full bank and a faulted controller. Coasting is immune — the Freegen rotor
  is stationary. Control-law requirement logged: full-bank regen tapers to zero
  above ~28 km/h.
- **The motor has one 9-pin Higo cable, not a separate sensor connector.**
  White conductor = shell speed. W1 becomes a Z910 splitter pigtail —
  electrically identical, connector packaging only. **Queued as drawing Rev E.**
- **BMS UV at 3.00 V/cell is only attainable with a configurable BMS** (JBD
  smart, app-set). Fixed-threshold parts cut at 2.5–2.8 V/cell — below the
  10.76 V collapse point, i.e. no protection at all. Bluetooth quiescent trims
  standby to 82–91 days; accepted.
- **S2 spec relaxed ≥50 → ≥24 V DC:** open-contact voltage is ≤ ~23 V, and the
  stock KSD301 manual-reset part is 48 V DC rated.
- **F2 and F3 unified** on the Littelfuse MINI 58 V blade family — one holder
  type, both voltage requirements covered, and at 5 A the precharge duty is
  67 % of rating.
- **S1 resolved:** 8–60 V DC / 275 A / 1250 A-intermittent marine disconnect
  class — the ≥60 V DC line is buyable without resorting to 48 V parts.

**Ruled out:** fixed-threshold BMS boards; ANL-style S1 substitutes; ordering
the motor without vendor confirmation of cassette + speed-sensor variant and a
frame dropout measurement.

---

## 2026-08-02 — Second external review; drawing to Rev D, spec to Rev C

**Decided.** A second review pass on the Rev C package returned three functional
drawing errors, five numeric slips, and a set of consistency findings. All
verified independently before action; all confirmed.

**Functional drawing errors — the drawing described a circuit that doesn't work:**

| Error | Consequence as drawn |
|---|---|
| D3 TVS reversed (anode to THR) | Forward-biased; clamps the throttle at ~0.7 V permanently |
| S2 drawn normally-open | The entire keep-alive/precharge path reads permanently open |
| Sheet 2 flags 2/4 swapped | Each flag pointed at the other's component — introduced when the notes were renumbered in Rev C without moving the flags |

**Numeric corrections:** S1 make current 111–270 → 126–252 A (the 270 used the
48.6 V equal-division voltage the spec itself repudiates); BT1 drain figures
reconciled (5.2 mW at bank / 5.5 mW from pack; 14.25 Wh usable → 4.0 h); U1 daily
replacement 126 → 161 mWh (must also cover BMS quiescent and self-discharge);
"per cold start" corrected to flat-bank starts only — a normal start begins at
the ~12.7 V rest point with no precharge phase.

**Resolved contradiction:** §5 said BT1 is "never switched" while Note 7 ordered
"disconnect BT1 before work". **The F3 inline holder is now the designated BT1
service disconnect** — no new part, both statements now true.

**Added to the spec:** the carrier one-way clutch (§1) — it existed only in the
simulator while the whole assist torque path depends on it; the body-diode
back-EMF caveat on bank overvoltage (§9); measurement schedule reordered with
the G020 teardown and kV measurement as gates 1 and 2.

**Rejected, with reasons:**

- **Deleting U1** (reviewer's "clearly unnecessary subsystem"). The owner chose
  "keep at 0.2 A, restate honestly" when this exact option was posed after the
  first review. The reviewer's argument is stronger than mine was — U1 conducts
  only above 19.2 V while the bank rests at 12.7 V, and §12 concedes the
  operating-case invariant — so the recommendation is logged here for
  reconsideration, but a settled owner decision is not re-litigated one round
  later.
- **Sim speed slider 0–3 km/h.** Owner-specified: real speeds are too fast to
  visualise. Not a mis-scaled control.
- **Re-zoning to ASME origin (lower right).** Three consecutive revisions have
  shipped zone errors; renumbering every reference in five tables to flip a
  convention invites a fourth. The actual convention is now stated in the README
  as a deliberate deviation.

---

## 2026-08-02 — External design review; schematic to Rev B

**Decided.** An outside agent reviewed the Rev A package and returned 14 findings.
Its arithmetic was re-derived independently and held up. Actions taken:

**Errors of mine, corrected without argument.**

| Was | Is | What went wrong |
|---|---|---|
| ~3.1 boosts | **1.6–2.3** | Bank ESR was priced into the fault-current and cap-loss lines of the same table, then omitted from the boost calculation. The 10 V floor is also unreachable under load — at 40 A the ESR alone drops 7–14 V, so the controller hits its 8 V floor with ~19 V still on the bank |
| U1 gates at 18.4 V | **19.2 V** | The diode-drop correction raised the setpoint to 17.2 V but was never propagated to the gating figure |
| Sheet 1 BEC from the bank | **from A1** | Drawing error carried over from the superseded two-rail topology |
| Standby ~200 days | **~120 days** | Leakage of 0.2 mA understated; the published 50 %/30–72 h figure implies ~0.37 mA at 14 V |
| F1 60 A ANL | **50 A Class T** | ANL and MIDI are 32 V DC parts. Rev A's own note demanded ≥48 V DC, which no ANL satisfies |
| F3 3 A fast | **5 A time-delay + S2** | Precharge draws 3.36 A for ~90 s — 112 % of a 3 A fuse, so it nuisance-blows *and* fails to clear a real fault |
| D1 3 A | **6 A** | 3.4 W in an axial package puts the junction past its limit |

**The sharpest finding, which I had missed entirely:** under a sustained bank
short the fault current *equals* the normal precharge current, 3.36 A. **No fuse
value can discriminate between them.** R1 sits at 53 W indefinitely. This is why
S2, a thermal cutout bonded to R1, is now fitted — the failure mode is thermal,
so the protection must be thermal.

**Also added:** J4 charge port (U1 is bank-fed and cannot bootstrap a flat pack,
so a port is mandatory, not an alternative); R5/R6/C6 on the shell-speed input,
which Rev A left with no conditioning at all; C7/D3 on the throttle.

**Owner decisions on the four judgement calls:**

- **No bleed.** Retained. The reviewer was right that the *reasoning* was a
  category error — my equilibrium calculation applies to a permanently connected
  bleed, whereas a momentary one used after disconnecting BT1 works fine. Sheet 2
  Note 7 now states the actual hazard: the bank holds 5 kJ and decays over **days**,
  so "disconnect BT1" alone does not make it safe.
- **No second buck for the MCU.** Retained on the BEC. C4/C5/L1 give **4 ms** of
  hold-up — decoupling, not independence — and the parts list now says so rather
  than implying ride-through.
- **~2 boosts is acceptable.** Closes the question against §2's no-further-purchase
  constraint.
- **No per-module sensing.** Module capacitance matching within 10 % is therefore
  promoted from a suggestion to a mandatory bench step (Sheet 2 Note 6).

**Ruled out:** re-rating F3 as a fix for the sustained-fault case; treating C4 as
brown-out protection; any ANL/MIDI fuse in the bank circuit.

---

## 2026-08-02 — MCU is the Pico, not the Pico 2

**Decided.** Retroactive entry. The change was made when the cost was questioned
and never logged, so the record read "locked: Pico 2" while the drawing said Pico
— which the external review correctly flagged as an unexplained discrepancy.

RP2040 is sufficient: nine pins, one PIO period-capture, one ADC channel, a
115200 UART and an I²C display. Nothing in the workload needs RP2350.

**Process note.** A decision made in conversation and applied to a drawing but not
written here is invisible to anyone reviewing the package. Log first, draw second.

---

## 2026-08-02 — One switch in the bank positive; contactor deleted

**Decided.** A single high-current master switch in the **bank positive**,
downstream of the precharge junction. The pack stays permanently connected through
the 4.7 Ω and diode and is never switched.

```
PACK ──[4.7 Ω]──[diode]──┬── BANK ──[MASTER SWITCH]──┬── VESC
                    bank rests ~14 V                 └── buck → Pico
```

**Deleted:** contactor, coil, coil-driver MOSFET, the high-side MOSFET and gate
driver that were proposed to replace it, flyback diode, gate pulldown, aux-contact
switch, the PRECHARGE state machine, and the separate housekeeping rail.

**Two of my objections were wrong.** I argued precharge would stall against the
VESC's 3 W idle — true at a 12 V target, but the VESC's floor is **8 V**, and at
that target the equilibrium clears across the whole usable pack range. And I
treated the parked-drain as needing automation when the rider simply turns the
switch off.

**Better than "adequate" — three properties fall out:**

- **No precharge wait.** The pack holds the bank at ~14 V, already above the 8 V
  floor, so the VESC boots the instant the switch closes.
- **Balance boards go quiet at rest.** 14 V is 0.78 V/cell, far below the ~2.65 V
  balancing threshold — so the drain suspected in v1 does not occur when parked.
- **~200 days standby.** Bank leakage at 14 V is ~0.2 mA ≈ 3 mW from a 15 Wh pack.

**Cost accepted:** the switch carries bank→VESC current, so it is a marine-style
battery disconnect and the bank leads must reach it. The Pico's buck now runs from
the bank, so it needs 8–40 V input rather than a pack-side part.

The pack's role narrows to: **hold the bank above 8 V so the controller can boot.**
The bank→pack trickle charger becomes optional rather than structural.

---

## 2026-08-02 — Controller: keep the Mini FSESC4.20

**Decided.** No controller purchase. The existing **Flipsky Mini FSESC4.20**
stays.

The whole selection was driven by one spec — minimum operating voltage, because
the bank swings down to ~9–12 V while most e-bike controllers assume a battery
that never drops below 30 V. That spec eliminated the Grin controllers
(Baserunner 19 V, Phaserunner 24 V) and the high-voltage VESC variants (75100 and
85 V Ubox, both 14 V).

**But it does not separate VESC boards from each other.** Every VESC — 4.12 or 6,
any vendor — bottoms out at **8 V**, where the front-end supply gives out. An
earlier note claiming VESC 6 reaches 6 V confused the DRV8323's chip-level UVLO
with the board's floor. So a ~$300 CAD VESC 6.6 buys three-shunt sensing, a
better gate driver and 60 A instead of 50 A — all real, none of it the thing that
mattered.

The Mini already meets every hard requirement, and v1's own ride logs prove
commanded proportional regen works on it at 40 A.

**Carried forward:** the 50 A continuous rating is optimistic for an 80 g board
(assume 20–30 A without airflow; peak governs our duty cycle anyway), and the
DRV8302 is this generation's known failure point — a reliability argument for a
later upgrade, not a capability gap now.

**The EMI history is not a reason to replace it.** The fault was the link, not the
board. RS-422 or CAN on existing hardware addresses it.

---

## 2026-08-01 — Motor: Bafang G020

**Decided.** Rear **Bafang G020** (also sold as RM G020 / SWX02).

- Single-stage planetary, ~5:1 — the configuration carrier braking needs
- 6 PPR shell speed sensor, standard on Bafang geared hubs with a clutch
- Rear, 135–142 mm, widely available and cheap
- Bafang's de-facto-standard hall mapping, which widens controller choice
- Cheap enough to open up, modify and ruin without much regret

Shengyi SX2 remains documented in `motor-selection.md` as the higher-spec
alternative (helical steel gears, 4.78:1, Grin-published specs, Vancouver stock)
if the G020 proves inadequate.

**To verify on the bench:** pole-pair count and exact gear ratio, both of which
feed the slip arithmetic. Ratio is quoted as 5:1 but should be confirmed by tooth
count during teardown.

---

## 2026-08-01 — Sensing from the motor; full-bank handling deferred

**Decided.** Both speeds come from the motor. Slip is their difference:
`ω_carrier = (k·ω_wheel − ω_motor)/(1+k)`. Carrier locked means
`ω_motor = k·ω_wheel`, so the difference is zero and any shortfall is slip.

Motor selection now carries a hard requirement: an **8-wire geared hub** with a
built-in shell speed sensor alongside the motor halls. This is a standard feature
— manufacturers added it because the freewheel decouples rotor from wheel and
motor halls read zero while coasting. No add-on sensor ring needed.

**[open] Resolution.** Built-in shell sensors are often 1 pulse/rev, which is too
coarse: slip is a difference of two ~950 rpm numbers, so ~1 % accuracy is needed
on each. Options are a multi-pole shell sensor, adding magnets to the side cover,
or — best where geometry allows — sensing the carrier directly so slip becomes a
measurement rather than a derived difference.

**Brake lever sensor is optional**, for rider intent and feedforward. Not part of
the core loop; slip regulation alone gives proportional response.

**Full-bank braking: deleted.** Regen simply tapers as the bank approaches its
ceiling. No dump resistor, no phase-short mode, no handover ladder. The rider's
front brake covers the shortfall — one of the reasons rear was chosen. Deferred
rather than dismissed; revisit only if the fade proves objectionable in riding.

**Precharge needs no disconnect.** The diode is self-terminating: it conducts
below pack voltage, blocks above. The master switch is strictly "pack connected
to system." Bonus — the same path holds a floor under the bank while riding, so a
big boost cannot drop cap voltage below the controller's start-up threshold.

---

## 2026-08-01 — 40 V ceiling, floor set by controller start-up

**Decided.** Bank ceiling **40 V**, keeping the existing three modules. No
additional capacitors. Floor drops to whatever the controller's start-up voltage
allows, provisionally ~9 V pending measurement.

Gives ~5 060 J usable ≈ 3.1 boosts — meets the invariant with no margin, which is
accepted in exchange for not buying more caps.

Lowering the floor is worth doing, but **not for the energy** — 10 V → 8 V is only
+2.4 %, since energy goes as V². The two real gains:

- Precharge time roughly halves (91 s → 39 s at the pack's low end).
- The pack floor constraint disappears. At a 12 V target a 4S pack at 12.0 V can
  never reach it; at a 9 V target even 11.0 V works, so the whole pack range
  becomes usable. That is worth more than the cap energy.

**Caveat carried forward:** the number needed is the controller's **start-up**
voltage, not its running voltage — switching supplies have hysteresis and a board
that runs at 8 V may need 9.5 V to start. Also confirm regen actually functions
at the low end, not merely that the board powers up.

40 V is comfortable at ≤ 10 % cell spread and marginally over at 20 %, so module
measurement now decides 40 V vs 38 V rather than deciding whether the build works
at all.

---

## 2026-08-01 — 4S pack, resistor precharge, 40 V ceiling

**Decided.** Housekeeping pack is **4S Li-ion, ~15 Wh**, usable range held to
**13.5–16.8 V**. Precharge is a **4.7 Ω resistor + silicon ultrafast diode** wired
after the master switch. No boost converter.

- The 4S pack already exceeds controller-boot voltage, so the boost, its inrush
  trap, its output disconnect and its control logic all disappear.
- **Pack floor is not optional.** At 12.0 V the diode drop puts the asymptote at
  11.2 V and the 12 V target is unreachable at any resistor value or duration.
  13.5 V floor guarantees it, and suits Li-ion cycle life.
- Diode is **silicon ultrafast, not Schottky** — forward loss is paid for one
  minute per ride, reverse leakage is paid continuously.
- Precharge ~1 min typical, ~45 precharges per pack charge.

**Bank ceiling dropped 43 V → 40 V.** With three series modules and no
inter-module balancing, a 20 % capacitance mismatch puts the worst module at
16.5 V against a 16.2 V rating. 40 V holds it to 15.4 V. Costs ~13 % of usable
energy. Per-module voltage sensing added so the limit derives from
`max(V_module)` rather than the bank total.

**Bank is 6.67 F, not 20 F** — series modules divide capacitance. Usable energy
5 549 J ≈ 3.3 boosts, which meets the "few good boosts" brief with no margin.

**Flagged for sourcing, not decided:** lithium-ion capacitors self-discharge at
<5 %/month against EDLC's ~50 % voltage in 30–72 h. That would retain charge
between rides and make precharge a rare cold-start rather than a per-ride ritual.
Cost and Ontario availability unverified.

---

## 2026-08-01 — Two power rails, Option A, master switch

**Decided.** Split the system into a supercap propulsion rail and a small Li-ion
housekeeping rail. Full design in `power-architecture.md`.

- **Pack: Option A** — 2S, ~15–20 Wh. Housekeeping and bootstrap only, never an
  energy store. The bus→pack charger stays but shrinks to a ~2 W trickle that
  only replaces what the electronics consume.
- **Master switch** — mechanical, breaking the Li-ion feed, so "off" is a true
  0 W rather than a small number.
- **No sleep mode.** Switch on means running, switch off means off. A traffic
  light costs ~180 J of controller idle per minute — under a third of one
  rear-wheel braking event. Simplicity wins over recovering that.
- **Contactor: normally-open, not latching.** A latching part holds its state
  when control power vanishes, so a master switch thrown while it is closed would
  leave the controller draining the bank. Normally-open drops out by itself.
  Costs 1–2 W while closed, which is only while the switch is on.
- **Precharge target: controller-boot voltage (~12 V), not a useful charge.**
  Runs only below `V_MIN`, stops on reaching it. Two states total: PRECHARGE and
  READY. Above `V_MIN` the rider has the full range with nothing gating them.

Accepted: ~75 % of stored energy is lost after two days parked, and there is no
meaningful assist for the first several braking events of a ride. Both are
inherent to a brake-harvesting buffer, not defects.

Dropping sleep also removed most of the ultra-low-power constraints — MCU sleep
current, the bus divider, converter quiescent draw are all zero when the switch
is off and irrelevant when it is on. Only cap balancing and cap self-discharge
survive, because both sit across the cells regardless of any switch.

Open: the caps still hold ~17 kJ with the master switch off. A separate manual
bleed is needed before servicing — a power switch is not a service disconnect.

---

## 2026-08-01 — Rear wheel

**Decided.** The motor goes on the **rear** wheel.

Earlier analysis recommended front on two grounds: braking traction is
front-biased (70–90 % of achievable deceleration), so a regen brake up front has
more energy available to capture; and front kits are far easier to make
universal. Both remain true. Two things outweigh them here:

- **Storage is the bottleneck, not capture.** The bank holds ~4.6 Wh. Rear-wheel
  braking alone fills it in a couple of minutes of descent. There is no shortage
  of energy to harvest, so the front wheel's larger share buys nothing.
- **Losing the rear brake is survivable; losing the front is not.** A
  carrier-braked wheel produces *no* braking torque when the motor cannot accept
  current (see `energy-and-idle-budget.md`). On the rear that degrades to "you
  still have your front brake." On the front it is a serious failure mode, and
  Grin's own FAQ concedes the same limitation.

Also avoids front-fork dropout ejection under reversed regen torque, which was
going to demand torque arms on both sides of a fork that may be aluminium.

**Cost accepted:** less braking energy available, tighter packaging, and a
drive-side caliper or an internal band.

---

## 2026-08-01 — No Magic Drive

**Rejected.** Grin's Magic Drive (pedal sprockets coupled to the planet carrier).

Two disqualifying consequences, both structural rather than fixable:

- **Pedalling costs stored energy.** With pedals on the carrier and the wheel on
  the ring, the gearset is a differential. The motor must supply reaction torque
  at the sun for any pedal torque to reach the wheel — so every pedal stroke
  spends capacitor charge. That directly contradicts a near-closed-loop energy
  system, and with a dead pack the bike cannot be pedalled at all.
- **It removes the cassette.** Cadence decoupling is the selling point, but it
  takes gear shifting away from the rider.

**Not revisited unless** the energy philosophy changes to a conventional
battery e-bike.

---

## 2026-08-01 — No GMAC

**Rejected** for this build. Grin's clutchless geared hub does strong rear regen
today with no mechanical modification and much simpler firmware, but it removes
the freewheel, so the motor is back-driven whenever you pedal unpowered.
Zero-drag coasting is the point of the carrier-brake concept.

Still the correct answer if the project ever needs to ship quickly.

---

## 2026-08-01 — Energy philosophy: buffer, not battery

**Decided.** The system stores enough for **a few short boosts**, not range.
Regen capture → brief assist bursts → repeat. Mostly closed loop, with at most a
small battery for electronics.

Consequences that now rank above raw performance:

- **Quiescent draw is a first-class requirement.** Anything that idles is
  spending harvested energy. See the idle budget note — the v1 controller alone
  ate a full braking event's worth every ~13 minutes.
- **Round-trip efficiency matters more than peak power.** Losses are paid twice.
- The motor is sized for short bursts, not sustained output.

---

## 2026-08-01 — Carrier-brake concept retained

**Confirmed.** Freegen-style: brake the planet carrier to restore a torque path
from wheel to motor, preserving the freewheel for zero-drag coasting.

Requires a **single-stage** planetary. Double-stage motors (e.g. Bafang
G310/G311, an inrunner with two planet sets) are out — two carriers, no clean
member to brake.
