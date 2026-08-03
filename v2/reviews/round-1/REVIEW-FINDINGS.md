# ReGenX v2 — review findings

Against `v2-schematic.html` (RGX-2-100 Rev A) and the seven findings documents in
`regenx-v2-review-package.zip`.

**Headline.** The circuit is broadly sound and the topology argument in
`power-architecture.md` is correct — the keep-alive loop does what it claims. But
Rev A silently deletes five conditioning and protection circuits that the research
documents specified, justified, and in one case flagged as the mitigation that a
different settled decision depended on. Those deletions are the top of this list.
Separately, the boost count is overstated by roughly 2× because the bank's own ESR
was priced into one line of the arithmetic and then left out of another.

Nothing here argues against a §2 constraint.

---

## Severity 1 — will destroy hardware or injure someone

### F1. GP13 has no conditioning at all, and the note that protects it rests on an unverified property of a third-party sensor

`hardware-design.md` §4 specifies this circuit, and explains why:

```
white wire ──┬──[10 kΩ]── 3.3 V
             ├──[1 kΩ]──┬── GP13
             │          └──[1 nF]── GND
```

Sheet 3 has **none of it**. The line runs from J1 pin 6 through W1 straight into
GP13, with Note 4 delegating the pull-up to firmware.

Three separate problems, in order of how badly they end:

**a) 5 V exposure.** The shell sensor is powered from A1's 5 V through J2 pin 1.
The research's safety argument is one sentence: *"The SS43F is an open-collector
output."* If that part is anything else — push-pull, or open-drain with an
internal pull-up to its own 5 V rail, both common in Bafang harnesses — then 5 V
lands on GP13 through zero series impedance. RP2040 GPIO are not 5 V tolerant;
current into the pad clamp is limited only by the sensor's drive strength. That is
a dead MCU on first power-up, and Sheet 3 Note 2 ("NO 5 V SIGNAL SHALL BE APPLIED
TO ANY U2 PIN") is asserting a property nobody has measured.

Note this is *not* the assumption listed in brief §5. §5 lists "shell-speed wire
carries no thermistor." The load-bearing assumption is the output structure, and
it is not on the list.

**b) The internal pull-up is 6–8× too weak.** RP2040's is 50–80 kΩ. The research
chose 10 kΩ. This node runs inside the motor cable, alongside three hall lines and
in the same loom management as the phase leads, on a bike whose v1 headline
failure was EMI (`hardware-design.md` §9). A 50–80 kΩ node there will pick up
false edges, and every false edge is a corrupted wheel-speed sample feeding the
one measurement the whole control problem is built on.

**c) No filter.** The research's 1 kΩ + 1 nF gives ~1 µs against a signal topping
out near 20 Hz — four orders of magnitude of margin, for two components.

**Fix:** restore the research's circuit verbatim. It is three passives, and the
1 kΩ series element also happens to limit injection to ~1.1 mA if the sensor does
turn out to drive 5 V — so it fixes (a) as a side effect. Before that, put a scope
on the white wire with the motor spun by hand and settle what the output structure
actually is.

---

### F2. The service bleed was deleted, and the reason given for deleting it does not apply to the bleed that was deleted

`hardware-design.md` §8.4: *"The bank holds ~5.3 kJ at 40 V with the master switch
off — enough to vaporise a tool tip across the terminals. The master switch is a
power switch, not a service disconnect."* §9b then specifies the part:

```
bank + ──[momentary switch]──[47 Ω, 50 W]── bank −
```

Rev A Note 5: `NO FITTED BLEED. DISCONNECT BT1 BEFORE SERVICING.`

Brief §6.3 justifies this as *"A fitted bleed cannot work — R1 holds the bank up
against it."* That is true of a **permanently-connected** bleed and false of the
**momentary-switch** bleed the research specified. And it is answered by the
procedure the note itself mandates: once BT1 is disconnected, R1 sources nothing,
and 47 Ω takes the bank from 40 V to under 2 V in ~16 minutes.

What Rev A leaves behind is worse than no bleed — it is a service instruction that
sounds complete and is not. Disconnect BT1 and the bank is still at whatever it
was, self-discharging with a time constant of **~5.4 days** at the assumed 0.2 mA.
Park after a ride, open S1, disconnect BT1, come back tomorrow: the bank is still
near 40 V with 5.3 kJ behind 180 mΩ.

**Fix:** restore §9b's bleed, permanently mounted and labelled as the research
asked. Add a test point across the bank and make the procedure explicit on the
drawing: *open S1 → disconnect BT1 → hold bleed 60 s → verify < 2 V at TP1 →
work.* A discharge step with no verification step is not a procedure.

---

### F3. Per-module voltage sensing was deleted, but the decision it was protecting was kept

These two were a package, and only one half survived into Rev A:

- `system-design.md` §6.4: *"Above the ceiling regen simply stops. No dump
  resistor, no phase-short handover."*
- `system-design.md` §7: *"Module overvoltage is checked **per module**, not on
  the bank total — three modules in series with no inter-module balancing means
  the bank can look fine while one module is over."*
- `cap-bank.md` §2, on per-module sensing: *"Cheap, and it catches the exact
  failure a bank-level sensor cannot see. **Do this one.**"*

Rev A takes bank voltage from A1 telemetry (`v_in`) only. There is no per-module
sense, no inter-module balancing, and no dump. The `BANK_OV` fault in
`system-design.md` §7 — *"any module > `V_MOD_MAX`"* — has no input.

`cap-bank.md` already did this arithmetic: at 20 % capacitance spread the worst
module exceeds 16.2 V while the bank total still reads safe. Rev A cannot see it.
And at rest the situation is worse than the charging case the research modelled:
at 14 V the bank sits at 0.78 V/cell, far below the ~2.65 V at which the onboard
passive balancers conduct (`power-architecture.md` §3 says so approvingly). So the
DC voltage division across the three modules at rest is set by **leakage mismatch
with zero corrective mechanism**, indefinitely.

I am not arguing for the dump resistor — that is a recorded decision in
`decisions.md`. I am arguing that the decision was explicitly conditioned on
per-module sensing, and the sensing is gone.

**Fix:** three dividers into GP27/GP28 and one spare, per `hardware-design.md`
§5 (100 k / 7.87 k, 100 nF each). Nine MCU pins are used; the Pico has the
channels. If the mux is wanted to save pins, the research already sized that too.
Alternatively — and cheaper — measure the three modules' capacitance before
stacking and match them, which `cap-bank.md` §2 also recommends. Do at least one.

---

### F4. A shorted bank leaves R1 at ~53 W continuous and F3 will not clear it

R1's fault case is not the precharge transient — that one is bounded and fine (see
§Arithmetic). It is a sustained short across the bank, or a shorted D1 with the
bank down, with BT1 connected:

```
I = (16.8 − 1.0) / 4.7 = 3.36 A          P_R1 = 3.36² × 4.7 = 53 W, continuous
```

F3 is 3 A fast-acting. 3.36 A is **112 % of rating**. Fuses are specified to carry
110 % indefinitely; a fast-acting element at 112 % does not open. So the fault is
not cleared, and a 50 W aluminium-clad resistor — whose free-air rating without a
heatsink is realistically 10–15 W — sits at 53 W. That reaches several hundred °C,
on a bicycle, adjacent to a Li-ion pack.

The awkward part: **the fault current and the normal precharge current are the
same 3.36 A.** No fuse value can discriminate between them. This is why F3 cannot
be made to work by re-rating it (see also F11).

**Fix:** discriminate thermally, not by current. A 100–110 °C bimetal cutout
(KSD9700 class, well under £1) bonded to R1's body, in series with the R1 branch.
The drawing's own arithmetic proves it will not nuisance-trip: the precharge
raises R1's case 24–32 K, so from 40 °C ambient it peaks near 72 °C with margin to
spare. A sustained fault walks straight past it. Also specify R1's mounting
thermally rather than as "chassis mount" — 55 W transient into an unspecified
bracket is not a specification.

---

### F5. F1 and S1 are 32 V DC-class parts on a 40–48.6 V DC bus, and Sheet 2 Note 3 cannot be satisfied

Note 3: `F1 SHALL HAVE ≥58 V DC INTERRUPT RATING.` Parts list: `60 A ANL`.

ANL is a 32 V DC part across every mainstream manufacturer. There is no 58 V ANL.
The note and the part number are mutually exclusive, and whoever buys to this
drawing will fit a 32 V ANL and believe the note has been honoured.

`hardware-design.md` §9a dismissed Class-T explicitly — *"Well within an ordinary
ANL/MIDI interrupt rating. Class-T is not needed."* That reasoning checks the
**kiloamp** interrupt capacity and never checks **volts**. The kA figure is right;
the conclusion doesn't follow. At 40 V DC into a 1.8 s L/R-dominated decay, a fuse
rated for 32 V may strike and sustain an arc rather than clear.

S1 has the same defect one notch quieter: `≥48 V DC, marine battery-switch type`.
The bank's absolute maximum is 3 × 16.2 = **48.6 V**, so the requirement is
already exceeded at the rating, and many marine switches in that form factor are
32 V DC parts anyway.

**Fix:** F1 → Class T, e.g. Bussmann JJN-50 or JJN-60 (160 V DC). Cheap, stocked,
and it makes the note true. S1 → a switch with a stated ≥60 V DC **break** rating,
not an automotive/marine 32 V part. And record the coupling both notes depend on:
`hardware-design.md` §9a says *"set [A1's battery current limit] near 40 A"* —
that firmware setting is what keeps F1 out of the operating envelope, and it
appears nowhere on the drawing. Put it in a note.

On the value itself: 60 A does not protect 10 AWG (which wants ≤40 A in a chassis
run) and does not permit A1's 150 A peak. Pick a lane. Given the bank's ESR makes
anything over ~40 A thermally pointless anyway (F7), the coherent answer is **cap
A1 at 40 A in firmware, fuse at 50 A Class T, keep the 10 AWG.**

---

### F6. There is no charge port for BT1, so the 4.4-hour case bricks the system rather than inconveniencing it

`hardware-design.md` §9a: *"The pack needs a way to be charged externally — for
first fill, and for when the trickle has not kept up."* Rev A has no such
connector on any sheet.

Follow brief §6.2 through to its end. S1 left on, BT1 flat in ~4.4 h (~3.8 h
including U2 — see §Arithmetic). BT1's BMS latches on undervoltage. The bank
collapses. U1 is fed from SW+, i.e. **from the bank**, so it now has no input.
Nothing in the system can put energy into BT1. There is no path back.

This reframes both §6.2 and §6.7. The 4.4-hour drain is not "accepted
inconvenience, recharge later" — there is no *later*, because there is no charger
input. And §6.7's framing of U1 versus a charge port as alternatives is a false
choice: **U1 cannot bootstrap from a flat bank, so the port is mandatory
regardless of whether U1 is fitted.**

**Fix, in order:**
1. A charge connector on BT1, upstream of F3 — a 4S balance-charge header or a
   barrel jack into the BMS, per `hardware-design.md` §9a. Two pounds. Not
   optional.
2. Consider a hardware undervoltage lockout on the BT1→R1 branch, opening below
   ~13.2 V, so the pack retains a reserve and self-recovers. This is a protection,
   not a sleep mode, and does not touch §2.

Related margin note: R1 = 4.7 Ω against a constant-power load has an upper bound
as well as the lower one Note 2 gives. Collapse (no real solution to
V² − V_s·V + PR = 0) occurs at V_s ≈ 7.9 V, i.e. BT1 ≈ 8.8 V. A BMS cutting at
2.5 V/cell (10.0 V) leaves only ~1.2 V of margin, and the failure mode below it is
not a clean stop — it is A1 relaxation-oscillating, browning out and restarting
against the bank. Specify the BMS undervoltage at 3.0 V/cell, and amend Note 2 to
give R1's range rather than only "DO NOT SUBSTITUTE LOWER VALUE."

---

## Severity 2 — the numbers do not support the claims

### F7. The boost count is overstated by roughly 2×. The 10 V floor is unreachable under load, and the 70 % figure excludes bank ESR

Two independent errors compounding, and the drawing already contains the evidence
for both.

**The floor.** Brief §4 computes usable energy over 10 → 40 V. With bank ESR at
180–360 mΩ, terminal voltage under a 40 A boost sits 7.2–14.4 V below open
circuit. To keep A1 above its assumed 8 V floor at 40 A you need an open-circuit
bank of **~18–22 V**. The bank cannot be discharged to 10 V under any current that
constitutes a boost. Realistic usable window is 40 → 20 V:

```
½ × 6.667 × (40² − 20²) = 4 000 J        not 5 000 J
```

**The efficiency.** The 70 % figure is ESC × motor × gearbox (0.95 × 0.82 × 0.90 =
0.70). Bank ESR is a *separate* multiplicative term, and brief §4 prices it
elsewhere in the same table — "Loss in caps at 40 A: ~400 W (25 %)" — then omits
it here. Averaged over a 40 → 20 V discharge at 40 A the ESR loss fraction is
I·R/V̄ = 10.8/30 = **36 %**, not 25 % (25 % is the value at the top of the range
only).

```
40 A boost:   4 000 J × 0.64 × 0.70 / 1 157 J  ≈  1.6 boosts
20 A boost:   4 733 J × 0.82 × 0.70 / 1 157 J  ≈  2.3 boosts
```

**1.6 to 2.3, against a claimed 3.1.** (The claimed figure is also arithmetically
3.0, not 3.1 — 5 000 / 1 656 = 3.02.)

This does not break the concept. "A few good boosts" survives at 2. But it moves
the design from "meets the goal with no margin" — `cap-bank.md`'s own words — to
below it, and the fix is a decision, not a calculation. `cap-bank.md` §4a Option 1
already has the answer costed: **3S2P, three more of a module already proven in
the build, 13.3 F, and it halves the relative capacitance spread that F3 is about.**
Given §2 forbids additional bank purchase, the honest statement is: at 3 modules
the deliverable is ~2 boosts, and the owner should decide whether that clears the
bar with that number in front of them rather than 3.1.

**Related:** the same ESR term applies on the capture leg. Round-trip is ~0.53² ≈
**28 %**, so a braking event does not fund a boost of equal ΔV — it takes roughly
two. Worth stating plainly since it sets how the thing will actually feel.

---

### F8. U1's self-gating threshold is 19.2 V, not 18.4 V, and its current setpoint is unspecified

`power-architecture.md` §6 derives 18.4 V from 16.4 + 2. But Sheet 2 Note 4
correctly raises the converter's output to **17.2 V** to net 16.4 V at BT1 after
D2 — and the `V_in ≥ V_out + 2 V` constraint applies to the converter's own
output. The threshold is **19.2 V**. The number was carried over from before the
diode-drop correction was applied.

Consequences are modest but real: `power-architecture.md` §7 wanted the charger to
enable only above ~25 V; the drawing is at 19.2 V, further from that than believed.

More seriously, `I_CC` is not on the drawing. The parts list says the module is
adjustable `0.2–3 A`; `power-architecture.md` §6 specifies **0.2 A**. BT1 is ~1 Ah.
An installer building to this drawing and leaving the pot where it arrived can
charge a 1 Ah pack at 3 C, and will blow F2 doing it — at 19.2 V input, a 3 A /
17.2 V output draws ~2.9 A, against a 2 A fuse.

**Fix:** correct the threshold in the docs, and put `SET V_OUT 17.2 V, I_CC 0.2 A`
on Sheet 2 as a note. An adjustable part with no setpoint on the drawing is not
specified.

---

### F9. C4/C5/L1 gives 4 ms of hold-up. It is decoupling. It does not make U2 independent of A1

Direct answer to §6.1, with the number:

```
E = ½ × 100 µF × (5² − 1.8²) = 1.09 mJ
t = 1.09 mJ / 0.27 W = 4.0 ms
```

Four milliseconds. That covers switching noise and a BEC load step. It does not
cover an A1 fault, which cuts the BEC entirely for seconds. So U2 resets exactly
when you most want it not to — mid-boost, mid-brake, or on an overcurrent trip —
and it resets *silently*, because the display it would report through is on the
same rail.

The filter itself is correctly built, and I want to say so: BEC → L1 → C4 → C5 →
VSYS is the right order, and Sheet 3's note putting C5 adjacent to the VSYS pin is
right. It is simply solving a different problem from the one §6.1 asks about.

**Fix, if independence is actually wanted:** a second small buck from **SW+**
(5–40 V in, 5 V out — an MP1584 or a second LM2596HV, ~£3), OR-ed into VSYS
against the BEC through two Schottkys. SW+ is downstream of S1, so the master
switch still kills everything, and U2 then survives down to ~5 V of bank instead
of dying at A1's 8 V floor. Sheet 1 already draws this architecture — see F12.

**Free diagnostic regardless:** the Pico wires VSYS/3 to ADC3 on-board. Sample it,
log the minimum across a ride, and you will know within one outing whether the
BEC actually sags. That converts §6.1 from an argument into a measurement, and it
costs nothing.

---

## Severity 3 — regressions from the research record

### F10. The throttle lost its divider, its filter capacitor and its TVS, and gained an unverified 3.3 V supply

`hardware-design.md` §9b specifies: **+5 V from the VESC BEC**, a 15 k / 22 k
divider, a 100 nF to ground, and a **TVS**. Rev A: 3V3 supply, one 100 kΩ
pulldown, nothing else.

The pulldown and the ratiometric reasoning are good — ratiometric output against
an AVDD-referenced ADC genuinely cancels, and open-circuit detection works. But:

- **The 3.3 V supply is a per-unit gamble** (§6.6 asks this; the answer is no, not
  reliable). Common linear hall ICs in these throttles specify 4.5 V minimum. The
  owner's v1 unit working proves one unit works, not that a replacement bought in
  two years will.
- **No cap at the ADC pin.** An unfiltered analog line to a handlebar, on a bike
  whose v1 headline failure was EMI. The research put 100 nF on every divider
  output and explained why (charge reservoir for the SAR).
- **No TVS** on the one wire that leaves the enclosure to a user-handled control.

**Fix, which resolves all three and costs two resistors:** supply the throttle
from 5 V per the research, divide down, and — since GP27 is free — **read the 5 V
rail through an identical divider on a second ADC channel and compute the ratio in
firmware.** That restores full ratiometric cancellation without depending on the
BEC's tolerance, works with any throttle on the market, and the divider clamps a
5 V fault to a safe level on the way in. Add the 100 nF and the TVS back.

Also add the range check as a drawing note, since it is a safety behaviour and not
merely firmware detail: *firmware shall reject THR outside 0.20–0.85 × V_ref and
fail to zero assist.* Out-of-range-high is the SIG-shorted-to-supply case, and it
is the one that gives unrequested throttle.

---

### F11. F3 was deleted in the research for the right reason, then re-added at a value that nuisance-blows

`hardware-design.md` §9a: *"**Why the pack fuse was deleted:** the keep-alive path
is already limited to ~3 A by the 4.7 Ω resistor, and a direct terminal short is
covered by the BMS's over-current protection. It was redundant."*

Rev A re-adds it as F3, 3 A fast-acting, and brief §6.5 says it exists "solely to
cover a shorted D1." Both the deletion reasoning and the re-addition reasoning are
partly right, and the value is wrong either way:

- **Normal precharge is 3.36 A** for ~90 s (τ = RC = 31.3 s) — 112 % of rating on
  every cold-bank start. That is inside the fuse's own may-open band. It will not
  blow the first time; it will drift and blow eventually, and the failure looks
  like a dead bike with no obvious cause.
- **The shorted-D1 case it was added for only reaches 4.9 A at a full bank.** At a
  22 V bank the same fault gives (22 − 16.8)/4.7 = 1.1 A and F3 never opens — so
  the BMS over-voltage is doing the work anyway, exactly as the research said.
- **The sustained-fault case it should cover, it cannot** (F4).

**Answer to §6.5:** F3 is not necessary for the shorted-D1 case — BMS OV is
sufficient there, and F3 only helps in the narrow full-bank corner. Keep a fuse,
but redefine its job as *wire protection for the 18 AWG pack branch* and size it
**5 A time-delay**. Time-delay, not fast-acting: a precharge circuit is the
textbook case for it. Then handle the sustained fault thermally per F4.

---

### F12. Sheet 1 sources U2's 5 V BEC from the supercapacitor bank

```
<path class="wp" d="M560 216 V 420"/>   <!-- C1–C3 block → U2 block -->
<text class="ts" x="576" y="320">5 V BEC</text>
```

x = 560 is the C1–C3 block; A1 is at x = 830–1050. Sheet 1 draws the MCU supply
coming out of the capacitor bank, labelled as A1's BEC. Sheet 3 and the interface
schedule both correctly show A1 → U2.

This is not a stray line. It is a leftover from the superseded topology in
`power-architecture.md` §2, which drew `BANK (+) ──[MASTER SWITCH]──┬── VESC (+)`
and `└── buck 8–40 V → 5 V → Pico` — and that document still contains **both**
architectures, §2 showing the buck and §7 saying *"The Pico needs no supply of its
own."* The contradiction was never resolved; it was just drawn twice.

Worth noting where it lands: the block diagram accidentally documents the fix F9
recommends. That is a reason to resolve it deliberately rather than by erasing the
line.

---

### F13. J2 pin 6 is the VESC's motor-temperature input, and disabling it is not on the drawing — nor is any motor thermal protection

`hardware-design.md` §4 gets this right: *"Mate them directly and the speed signal
lands on the VESC's temperature input, which will read it as a thermistor and
report nonsense or fault."* Rev A Note 1 handles half of it — pin 6 stays
unconnected — but leaving a VESC temp input open with sensing **enabled** reads as
an out-of-range thermistor and can fault or throttle. That firmware requirement
belongs on the drawing next to Note 1.

The larger gap is what that leaves: **the design has no motor thermal protection
of any kind.** For this application specifically that is uncomfortable. During
regen the motor's copper loss is real — order 400 W at 30 A phase into a G020's
windings, comparable to the 400 W going into bank ESR — and a small geared hub
with the gearbox loaded has a poor thermal path out of the stator. Sustained
descents are exactly the use case the product exists for, and exactly where a
2.5 kg hub cooks.

**Fix:** inspect the G020 harness for a thermistor (`hardware-design.md` §12
already lists this as an open question). If present, land it on J2 pin 6 and turn
VESC motor-temp sensing on — the input is right there and currently wasted. If
absent, add the firmware disable note and log A1's reported MOSFET temperature as
a weak proxy, and say plainly in the docs that motor temperature is unprotected.

---

### F14. Two silent part downgrades against specs the record marks as settled

**MCU.** `hardware-design.md` §1 and §3 specify **Raspberry Pi Pico 2 (RP2350)**,
and `system-design.md` §2 marks it **"locked"**. Rev A's parts list says
**"Raspberry Pi Pico, RP2040."** RP2040 is workable — it has PIO and its pads have
Schmitt inputs, so the two justifications that mattered survive — but it halves the
RAM (264 KB vs 520 KB, which under MicroPython is not nothing) and drops one PIO
block. Change a locked part or change the record; do not leave them disagreeing.

**D1.** The record specifies **"≥100 V, ≥5 A ultrafast silicon (e.g. UF5404 /
MUR520)"**. Rev A specifies `I_F(AV) ≥ 3 A ... 1N5404 or equiv` — standard
recovery, and 2 A less headroom. The recovery characteristic genuinely does not
matter here (this is a DC blocking application, and the research's "ultrafast" call
was over-specified), but the **current rating does**: D1 carries the full 3.36 A
precharge for ~90 s at ~1.0 V forward, so ~3.4 W in an axial package whose free-air
junction-to-ambient is 40–50 °C/W. That is at or past the junction limit.

**Fix:** 6A10 or equivalent 6 A axial, or any 5 A+ part with a tab. Pennies.

---

## Arithmetic verification

Everything in brief §4, checked.

| Quantity | Claimed | Verdict |
|---|---|---|
| Bank capacitance | 6.67 F | ✅ 20 / 3 |
| Usable energy 10→40 V | 5 000 J | ✅ as written — but the 10 V floor is unreachable (**F7**) |
| Boost ΔKE | 1 159 J | ✅ 1 157 J |
| From bank @ 70 % | 1 656 J | ⚠️ arithmetic ✅; the 70 % **excludes bank ESR** (**F7**) |
| Boosts available | ~3.1 | ❌ 5 000/1 656 = **3.0**; realistically **1.6–2.3** (**F7**) |
| A1 idle draw | ~3 W | ✅ 255 mA × 12 V = 3.06 W (excludes U2's ~0.35 W) |
| Bank drain by idle | 28 min | ✅ |
| Bank rest voltage | ~14 V | ⚠️ ✅ at BT1 nominal; **15.9 V** at BT1 full. Drawing gives one figure for a range |
| Bank leakage | 0.2 mA / 3 mW | ⚠️ plausible, unmeasured. `power-architecture.md` §1's own "50 % in 30–72 h" figure implies **~0.43 mA** |
| Standby duration | ~200 days | ⚠️ **~100 days** on that leakage figure, before BMS quiescent (20–100 µA) and Li-ion self-discharge (~2 %/mo) |
| S1 left on | 3.4 W, 4.4 h | ✅ arithmetic; **3.8 h** including U2 + DS1 on the same rail |
| R1 peak dissipation | 42–55 W | ⚠️ ✅, but **55 W exceeds R1's 50 W rating**, and V_D1 at 3.4 A is ~1.0 V not 0.7 V |
| R1 energy, flat bank | 650–850 J | ✅ |
| R1 temperature rise | 24–32 K | ⚠️ ✅ as a bulk-average; ignores the element-to-case gradient at t = 0. Check against the part's single-pulse rating |
| Bank ESR | 180–360 mΩ | ✅ |
| Fault current at 40 V | 110–220 A | ✅ |
| Loss in caps at 40 A | ~400 W (25 %) | ⚠️ ✅ **at 40 V**; **36 %** averaged over a 40→20 V discharge |
| Shorted D1 → BT1 | 4.9 A | ⚠️ ✅ **at a full bank only**; 1.1 A at a 22 V bank, where F3 never opens (**F11**) |
| A1 BEC loading | 54 mA / 1500 | ✅ |
| U2 3V3 loading | 70 mA / ~300 | ✅ |
| Precharge equilibrium | 11.5 V vs 8 V | ✅ 11.4 V. Margin to collapse is thinner than it looks — see **F6** |
| Shell sensor at 200 rpm | 20 Hz, 50 ms | ✅ |
| **U1 gating threshold** | 18.4 V | ❌ **19.2 V** (17.2 + 2, not 16.4 + 2) — **F8** |

---

## Answers to §6

**1. U2 supply SPOF.** Not adequate. 4 ms of hold-up (**F9**). Fix is a second
buck from SW+, OR-ed in. Instrument ADC3 first — it is free and settles it.

**2. S1 left on, 4.4 h.** Not defensible as drawn, but the problem is not the
4.4 hours — it is that there is no recovery path afterwards (**F6**). Add the
charge port and it becomes a defensible annoyance. A hardware timeout is not
needed; a UVLO on the keep-alive branch is the cheaper answer.

**3. No bleed provision.** Not adequate, and the stated reason is a category
error — it argues against a permanent bleed, while the research specified a
momentary one (**F2**). Restore it, add a test point, and write a procedure with a
verification step.

**4. F1 at 60 A.** Wrong type (ANL is a 32 V DC part and the note demands 58 V),
and the value protects neither the wire nor A1's peak (**F5**). Class T at 50 A,
with A1's battery current capped at 40 A in firmware and that fact on the drawing.

**5. F3 at the pack.** Not necessary for the fault it targets; BMS OV covers it,
as the research concluded before Rev A re-added it (**F11**). Keep a fuse for the
18 AWG branch at 5 A **time-delay**, and handle the sustained fault thermally
(**F4**).

**6. Throttle at 3.3 V.** Not reliable across units (**F10**). 5 V supply, divide
down, read the rail on a second ADC channel for ratiometric cancellation. Restore
the filter cap and TVS while you are in there.

**7. Trickle charger U1.** It earns its place, but for a different reason than
stated — the 200-day standby figure is a *parked* number, and in use BT1 also
funds the bank floor after every deep boost (~311 J per event that drops the bank
below rest). But U1 is **not** an alternative to a charge port, because U1 is bank-
fed and cannot bootstrap from flat (**F6**). Keep both. Specify `I_CC` (**F8**).

**8. Ground topology.** Broadly right, and the star-at-A1-V− call is correct. Two
refinements. Note 2 is too blunt: signal grounds (DS1, J3, R2) must be **local to
U2** and reach the star via the single BEC return, not run individually to A1 — as
drawn, "ALL GROUNDS SHALL STAR AT A1 V−" invites someone to do exactly that and
build a loop. And the interface schedule omits DS1's and J3's ground returns
entirely. Nothing is referenced *wrongly*; the throttle in particular is safe
because its reference travels with it. But the drawing does not say enough to
prevent it being built wrongly.

**9. Wheel speed at 6 PPR.** Viable, with a caveat the brief has not named. The
resolution floor is *not* the problem — period timing on the RP2040 resolves a
50 ms interval to microseconds. The problem is **magnet spacing error**: a
6-magnet ring is easily ±5 % irregular, so single-period speed carries ±5 %
jitter, and averaging it out costs a full revolution (~300 ms at 20 Hz) — too slow
for a 1–3 s braking event.

The fix is standard and cheap: **learn the six inter-magnet intervals during
steady coasting, store the correction factors, and apply them per-pulse.** That
restores single-pulse latency (50 ms) at full accuracy.

The second-order issue is worse and unaddressed: you are differencing wheel speed
against A1's telemetry ERPM to estimate carrier slip, and the two measurements
have **unequal, unknown, unsynchronised lag** — VESC's ERPM is itself filtered
(~10–30 ms) on top of UART round-trip. Differencing two laggy signals to extract a
small difference is how you get a noisy slip estimate that looks like a control
problem. Mitigations: raise the UART to 460800, use `COMM_GET_VALUES_SELECTIVE`
masked to ERPM + `v_in` + current rather than the full 70-byte frame, and timestamp
both measurements at capture so the control law can compensate rather than assume
simultaneity.

One thing already right, and worth keeping visible: `hardware-design.md` puts GP13
on **PIO**, which is the correct answer under a MicroPython constraint — Python-
level IRQ handlers will eat 5–20 ms GC pauses straight into your period
measurement. That decision is in the record but not on the drawing. Put it in
Note 4 alongside the pull-up.

**10. The keep-alive loop.** **Verified — the claim holds.** D1 conducts for
V_bank < ~13.9 V. D2's source only produces above V_bank ≈ 19.2 V (17.2 V out,
+0.7 V through D2 needs BT1 < 16.5 V, and U1 needs 19.2 V in). The windows are
disjoint with ~3.6 V of separation and no circulating path exists. I also checked
the sub-case that usually breaks these: the LM2596 does not cleanly gate off below
dropout — it runs at max duty and passes V_in − ~1 V — but even in that mode D2's
onset stays above D1's cutoff, so the conclusion survives. D2 correctly returns to
the node **between F3 and R1**, keeping charge current out of the 4.7 Ω. That is a
good detail and it is drawn right.

D1 also provides reverse-polarity protection for BT1 as a side effect, which the
docs do not claim but which is worth recording.

---

## Drawing housekeeping

Low severity, but the brief asked for errors.

- **Zone references wrong in five places.** Using the drawing's own zone ticks
  (x: 34/368/701/1034/1366; y: 34/242/450/658/866):
  D1 is **A2** (listed A1) · F1 is **A3** (listed A2) · D2 is **C3** (listed C2) ·
  U2 spans **A2–C3** (listed A2–C2) · L1 is **A4** (listed A3).
- **Interface schedule gaps.** No row for DS1 GND or J3 GND. The `SPD` row omits
  its voltage domain — the very question F1 is about — while the `H1–H3` row
  correctly states "5 V, A1 domain."
- **R3/R4 at 100 Ω** are edge-damping values with no fault margin. At 1 kΩ they
  limit injection to ~1.7 mA if A1's UART ever presents 5 V, and 1 kΩ × ~30 pF is
  30 ns — irrelevant at 115200 baud. Free improvement.
- **Sheet 2 Note 2** bounds R1 from below only. There is an upper bound near 11 Ω
  (**F6**). State the range.
- **S1 make current.** With the bank left at 40 V and S1 open, the bank stays near
  40 V for days (τ ≈ 5.4 d). Reclosing S1 onto A1's discharged input capacitance
  is a ~148 A make. Marine switches take it, but it pits contacts. Either accept
  and specify a make rating, or note the habit of closing S1 only near rest
  voltage.
- **Sheet 1** does not name F3/R1/D1 on the BT1→bank path, though Note 5 requires
  net names across sheet boundaries.
