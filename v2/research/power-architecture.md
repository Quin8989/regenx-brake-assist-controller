# Power architecture

Supersedes earlier revisions of this file, which specified two switched rails, a
contactor and a precharge state machine. All three are gone.

---

## 1. The two problems this has to solve

**Supercapacitors do not hold charge.** A 2.7 V EDLC leaks 0.05–0.5 mA per 100 F,
and published self-discharge is ~50 % voltage in 30–72 hours. Voltage is squared
in the energy term, so 50 % voltage lost is **75 % of the energy gone**. Within a
ride the bank is excellent; across a night it is empty.

**The VESC needs ~8 V to boot**, and it powers itself from the bank. A flat bank
therefore means a controller that will not start, so no regen, so the bank stays
flat. **The system cannot restart itself from its own storage.**

A small Li-ion pack solves both, but not as a separate rail — as a **keep-alive
that holds the bank above the VESC's floor.**

---

## 2. Topology

```
PACK (+) ──[4.7 Ω]──[S2]──[diode]──┬── BANK (+) ──[MASTER SWITCH]── VESC (+)
                                   │                                   │
                              bank rests here, ~14 V                   └── 5 V BEC → Pico

PACK (−) ──────────────────────────┴── BANK (−) ────────────────────── VESC (−)   common
```

**The Pico is fed from the VESC's own 5 V BEC, not from a buck off the bank.**
Earlier revisions of this file showed a separate buck; that rail is gone. The
consequence — the MCU has no supply independent of the controller — is accepted
and quantified in §8.

**One switch. One pole. In the bank positive, downstream of the precharge
junction.** Open it and both the VESC and the Pico are physically disconnected —
nothing downstream can draw anything.

The pack is permanently connected through the resistor and diode. It is never
switched, and it never powers the electronics directly.

---

## 3. Standby — switch open

The pack holds the bank at **pack voltage minus one diode drop, ~14 V**.

| | |
|---|---|
| Bank leakage at 14 V | ~0.37 mA |
| Drawn from pack | **~5 mW** |
| 15 Wh pack lasts | **~120 days** |

The leakage figure is derived from the published self-discharge spec, not the
per-cell leakage line: 50 % voltage in 30–72 h implies τ ≈ 69 h, hence ~37 kΩ
across 6.67 F and ~0.37 mA at 14 V. An earlier revision used 0.2 mA and claimed
200 days. **Both numbers are estimates until the bank is measured** — see
`design-parameters.md`.

Three consequences, two of them unplanned:

**No precharge wait.** 14 V is already above the VESC's 8 V floor. Flip the switch
and the controller boots immediately. There is no precharge sequence to run, no
timer, and no state to be in.

**The balance boards go quiet.** 14 V across three modules is 4.7 V per module,
**0.78 V per cell** — far below the ~2.65 V threshold at which the passive
balancers begin conducting. So the balance-board drain that was a suspect in v1's
idle problem simply does not occur at resting voltage.

**Less cap stress.** Storing at 14 V rather than 40 V is easier on the dielectric
than parking them fully charged.

The bank also retains ~654 J at 14 V — not a boost (that needs ~1 650 J), but not
nothing either.

---

## 4. Riding — switch closed

**Normal range.** Bank works between ~14 V and the 40 V ceiling. Regen charges it,
boosts discharge it, and the diode blocks any backfeed toward the pack.

**Floor holding.** If a large boost pulls the bank below pack voltage, the diode
starts conducting again and the pack trickles in through the 4.7 Ω. Current is
`(14.0 − V_bank)/4.7` — about 0.85 A at 10 V. **The bank cannot fall below the
VESC's start-up threshold mid-ride.** Emergent from the topology, and useful.

**Diode leakage.** At 40 V bank against a 14.8 V pack the diode blocks 25 V
reverse. A silicon ultrafast leaks ~5 µA — 0.125 mW. Negligible, and the reason
the part is silicon rather than Schottky: forward loss is paid briefly, reverse
leakage is paid always.

---

## 5. What this deletes

| Removed | Why it is no longer needed |
|---|---|
| Contactor | The master switch cuts bank→VESC directly |
| Contactor coil + 1–2 W standby | — |
| High-side MOSFET, gate driver, charge pump | Considered as the coil's replacement; both obsolete |
| Flyback diode, gate pulldown | — |
| Two-pole / aux-contact switch | One pole does it |
| PRECHARGE state machine | Bank is already above the floor |
| Separate housekeeping rail | Pico runs from the bank |
| Sleep mode | Already deleted; nothing reintroduces it |

The pack's job narrows to one sentence: **hold the bank above 8 V so the
controller can boot.**

---

## 6. Pack trickle charger

Optional but cheap, and it makes the system genuinely self-sufficient.

```
BANK ──[master switch]──[F2]──> LM2596HV CC/CV ──[D2]──> PACK (+)
                                17.2 V, 0.2 A
```

**It gates itself.** The module needs `Vin ≥ Vout + 2 V`, so with the output set
to 17.2 V it cannot operate until the bank exceeds **19.2 V**. No comparator, no
enable pin, no firmware involvement — the converter's own dropout does it.

- **17.2 V at the module, 16.4 V at the pack.** D2 drops ~0.8 V, so the setpoint
  must be raised to land the pack at 4.1 V/cell rather than 4.2 — markedly better
  for Li-ion life. An earlier revision set 16.4 V at the module and quoted an
  18.4 V gating threshold; both were one diode drop out.
- **0.2 A** (the module's minimum CC setting) is ~3.3 W. The pack consumes about
  **66 mWh/day**, so a day's use is replaced in under three minutes of riding
  above 18.4 V. CV termination tapers it to nothing once full.
- **Downstream of the master switch**, deliberately. An always-connected charger
  would circulate energy at rest — take from the bank, keep-alive pushes it back
  through the 4.7 Ω — burning ~150 mW and flattening the pack in ~100 hours.
  Fifty times worse than the 3 mW it was meant to fix.
- **Output diode required.** The LM2596 is non-synchronous but its switch still has
  a body diode SW→Vin, so with output above input the pack would backfeed. The
  diode blocks it and reinforces the turn-on threshold.
- No heatsink at 3.3 W. Module input rating 5–57 V covers the 40 V ceiling.

**Selection trap:** plain LM2596 (40 V) and XL4015 (36 V) modules do **not** have
enough input margin. It must be the **HV** variant or another 60 V-class part.

---

## 7. What remains

| Part | Purpose |
|---|---|
| **S1** master switch | Bank positive, ≥100 A, **≥60 V DC break** — marine-style disconnect |
| **F1** 50 A Class T | Bank positive, ahead of S1. Class T for the DC rating — see §9 |
| **R1** 4.7 Ω, 50 W | Keep-alive current limit. Heatsunk or chassis-bonded |
| **S2** thermal cutout | NC bimetal, 100–110 °C, bonded to R1 — see §9 |
| **D1** Si rectifier ≥100 V, **≥6 A** | Blocks backfeed; sets the ~14 V rest point |
| **F3** 5 A time-delay | Pack branch wire protection. Time-delay: precharge is 3.36 A for ~90 s |
| **LM2596HV CC/CV module** | Pack trickle charger, 17.2 V / 0.2 A |
| **D2** ≥100 V, ≥1 A | Blocks pack→bank backfeed; returns between F3 and R1 |
| **BT1** 4S Li-ion, ~15 Wh + BMS | Keep-alive store. **BMS UV cut at 3.0 V/cell**, not 2.5 |
| **J4** charge port | Mandatory, not optional — see §9 |

**The Pico needs no supply of its own** — the VESC's 5 V / 1.5 A BEC covers its
~70 mA, and hands it the same ground reference the shell sensor uses.

Ordinary generic silicon throughout. D1 does not need to be ultrafast: it carries
a ~90 s DC precharge and then sits reverse-biased. Switching speed is irrelevant;
reverse leakage and forward current rating are what matter.

---

## 8. Single supply for the MCU — accepted, quantified

The Pico runs from A1's BEC. If A1 browns out or its input collapses, the MCU
resets. C4 (100 µF) + C5 (100 nF) + L1 hold VSYS above the 1.8 V floor for

```
E = ½ · 100 µF · (5² − 1.8²) = 1.09 mJ      P ≈ 0.27 W      t ≈ 4.0 ms
```

**4 ms is decoupling, not independence.** It rides out switching noise and the
BEC's response to a load step. It does not survive an A1 fault, and it was never
going to — a supply that could would need to be a separate converter off the bank,
which is the rail this architecture deliberately deleted.

Accepted, because the failure is benign: if the MCU resets, assist and regen
command both go to zero and the bike is a bicycle. Nothing about the failure is
silent or progressive. Firmware records VSYS via ADC3 (Sheet 3 Note 7) so BEC sag
is measurable rather than assumed.

---

## 9. Loose ends

**Switch inrush.** Closing onto the VESC's input capacitance (~1 000 µF) at 40 V is
~0.8 J — a small spark. A marine switch rated 100 A+ takes this in its stride, but
if contact wear becomes visible, an anti-spark provision (series NTC, or a bleed
resistor across the switch) is the fix.

**Flat pack — why J4 is mandatory.** If BT1 dies the bank falls below 8 V and the
VESC will not boot. U1 cannot recover it: **U1 is fed from the bank**, so a flat
pack and a flat bank starve each other and the system cannot bootstrap from either
end. There is no state the rider can reach that restores it.

Earlier revisions framed the charge port and the trickle charger as alternatives.
They are not. U1 keeps a *working* pack topped up; **J4 is the only way back from
a dead one.** Both are fitted.

**S1 left on.** A1's ~3 W idle draws from the bank, which R1 backfills from BT1 at
~3.4 W. BT1 flattens in ~4.4 h. Accepted deliberately — there is no sleep mode and
the switch is the only off state. The consequence is a dead pack, which J4 now
recovers rather than bricking the system.

**Sustained bank short — why S2 exists.** With the bank held at ~1 V, R1 passes
`(16.8 − 1.0)/4.7 = 3.36 A` and dissipates **53 W continuously**, indefinitely.

The trap: **that is the same current as a normal cold-start precharge.** No fuse
value distinguishes them — anything that survives precharge survives the fault,
and anything that clears the fault nuisance-blows on every cold start. The failure
mode is thermal, so the protection is thermal: S2, a normally-closed bimetal
cutout bonded to R1's body, opens at 100–110 °C and self-resets on cooling.

**R1 bounds.** Below 4.7 Ω the fault current climbs. Above ~8 Ω the supply
collapses: solving `V² − V_s·V + P·R = 0` for a real root gives `V_s ≥ 2√(PR)`,
so at 3 W and 4.7 Ω the precharge stalls below ~7.5 V bus, i.e. ~8.2 V at BT1.
The BMS undervoltage cut must sit **above** that — hence 3.0 V/cell (12.0 V), not
2.5 V/cell (10.0 V), leaving ~1.8 V of margin.

**Switch inrush, revisited.** Closing S1 onto A1's input capacitance is ~0.8 J.
More relevant is the make current: at 40 V into a cold bank the initial surge is
limited only by ESR and contact resistance, on the order of 148 A. S1's rating is
a *make* rating, not just a continuous one.

**Service bleed — not fitted.** Owner decision. The consequence must be stated
plainly rather than assumed away: with BT1 disconnected the bank self-discharges
with τ ≈ 5.4 days. **Disconnecting BT1 does not make the bank safe.** After a ride
it can sit at 40 V holding ~5 kJ for days. Service procedure is: open S1,
disconnect BT1, discharge with an external load, and **verify below 2 V with a
meter** before touching anything.

The reasoning that deleted the fitted bleed was partly wrong and is worth
recording. The objection was that R1 would hold the bank up against a bleed
resistor, forming a divider — true, but only for a *permanently connected* bleed.
A momentary bleed used after disconnecting BT1 has no such problem. The decision
stands on its merits (one less high-energy part, one less thing to fail closed);
it does not stand on the divider argument.
