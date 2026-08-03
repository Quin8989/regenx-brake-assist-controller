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
