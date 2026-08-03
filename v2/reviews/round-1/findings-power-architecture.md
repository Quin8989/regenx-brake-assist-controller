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
PACK (+) ──[4.7 Ω]──[diode]──┬── BANK (+) ──[MASTER SWITCH]──┬── VESC (+)
                             │                               └── buck 8–40 V → 5 V → Pico
                        bank rests here, ~14 V

PACK (−) ────────────────────┴── BANK (−) ─────────────────────── VESC (−)   common
```

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
| Bank leakage at 14 V | ~0.2 mA |
| Drawn from pack | **~3 mW** |
| 15 Wh pack lasts | **~200 days** |

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
BANK ──[master switch]──> LM2596HV CC/CV ──[diode]──> PACK (+)
                          16.4 V, 0.2 A
```

**It gates itself.** The module needs `Vin ≥ Vout + 2 V`, so with the output at
16.4 V it cannot operate until the bank exceeds **18.4 V**. No comparator, no
enable pin, no firmware involvement — the converter's own dropout does it.

- **16.4 V, not 16.8** — 4.1 V/cell rather than 4.2 markedly extends Li-ion life.
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
| **Master switch** | Bank positive, high current — marine-style disconnect |
| **Fuse** | Bank positive, ahead of the switch — see `hardware-design.md` §9a |
| **4.7 Ω, 50 W** | Keep-alive current limit |
| **Ultrafast silicon diode** ≥100 V ≥5 A | Blocks backfeed; sets the ~14 V rest point |
| **LM2596HV CC/CV module** | Pack trickle charger |
| **Diode** on charger output | Blocks pack→bank backfeed |
| **4S Li-ion, ~15 Wh + BMS** | Keep-alive store |
| Pack-voltage divider | Pico reports pack health while running |

**The Pico needs no supply of its own** — the VESC's 5 V / 1.5 A BEC covers its
~200 mA, and hands it the same ground reference the shell sensor uses.

Only added active part: **one $5 charger module**.

---

## 7. Loose ends

**Switch inrush.** Closing onto the VESC's input capacitance (~1 000 µF) at 40 V is
~0.8 J — a small spark. A marine switch rated 100 A+ takes this in its stride, but
if contact wear becomes visible, an anti-spark provision (series NTC, or a bleed
resistor across the switch) is the fix.

**Flat pack.** If the pack dies the bank falls below 8 V, the VESC will not boot,
and the buck will not run either — so the Pico cannot even report why. The bike is
still a bicycle. Mitigation is a charge port and a habit, not circuitry. The
pack-voltage divider gives a warning while the system is still running.

**Trickle charger, bank → pack.** Now **optional** rather than structural — 200
days of standby does not need topping up mid-season. If fitted, it should only
enable above ~25 V bank, otherwise energy circulates between pack and bank through
the keep-alive path and is lost to the resistor.

**Service bleed.** At rest the bank holds only ~654 J at 14 V, far less hazardous
than the ~5 300 J of a full bank. But after a ride it can be at 40 V with the
switch open. A manual bleed behind a momentary switch is still wanted, and still
should be labelled.
