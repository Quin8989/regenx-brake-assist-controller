# Cap bank and precharge

Notes on the GDCPH 16 V 20 F modules (6 × 2.7 V 120 F cells) and what the
precharge path should actually look like.

---

## 1. Series modules divide capacitance — the bank is smaller than assumed

Each module is 6 × 120 F cells in series: **16.2 V, 20 F**. Wiring N of them in
series multiplies voltage and divides capacitance:

| Modules in series | Rated V | Bank capacitance |
|---|---|---|
| 2 | 32.4 V | 10 F |
| 3 | 48.6 V | **6.67 F** |

`CAPACITANCE_F = 20.0` in `firmware/config/settings.py` is the **per-module**
figure, not the bank. Every energy number in the earlier notes used 20 F and is
therefore about **3× too high**. Corrected, for a 3-series stack operated
10 → 42 V:

```
E_usable = ½ · 6.67 · (42² − 10²) = 5 549 J = 1.54 Wh      (was 16 640 J)
```

### What changes

| Quantity | Old (20 F) | Corrected (6.67 F) |
|---|---|---|
| Usable energy | 16 640 J | **5 549 J** |
| Rear-wheel stops to fill | ~28 | **~9** |
| Controller idle (3 W) drains bank in | 91 min | **31 min** |
| Idle burns one braking event in | 13 min | **3.3 min** |
| Bootstrap 0 → 12 V | 1 440 J | **480 J** |
| Bootstraps from an 18.5 Wh pack | ~37 | **~110** |

Mixed news. The idle problem is **four times worse** than stated — three minutes
parked costs a braking event. But bootstrapping is far cheaper, and the bank
fills in about a third the time.

### Does it still meet the brief?

A useful boost — 100 kg from 10 → 20 km/h — is 1 159 J at the wheel, roughly
1 656 J from the bank at ~70 % round trip.

**Corrected 2026-08-02.** The figure below was 3.3 boosts. It was wrong twice, and
both errors flattered the design.

**The 70 % excludes bank ESR**, which is charged for elsewhere in the same
analysis ("loss in caps at 40 A: ~400 W") and then quietly omitted here. At 40 A
through 180–360 mΩ the loss fraction is `I·R/V̄ ≈ 0.36`, so the real round trip is
nearer 45 %.

**The 10 V floor is unreachable under load.** ESR drops 7–14 V at 40 A, so A1 hits
its 8 V start-up floor with **~19 V still on the bank**. The energy below that is
stored but not extractable at boost current.

```
40 A boost:  floor 18.8 V →  4 155 J usable × 0.63 × 0.70 / 1 157 J  ≈  1.6 boosts
20 A boost:  floor 13.4 V →  4 735 J usable × 0.80 × 0.70 / 1 157 J  ≈  2.3 boosts
```

**~2 boosts, not 3.3.** Accepted by the owner as still meeting "a few good
boosts", so the 3-series stack stands. Note what it implies for the control law:
a single braking event recovers roughly *half* a boost, so the bank is not
self-sustaining on gentle riding — it needs either hard braking or a long descent
to refill.

Gentler boosts are markedly more efficient because the ESR loss scales with
current: half the current buys 40 % more boosts from the same stored energy.
That is a control-law lever, not a hardware one, and it belongs to the deferred
scoring work.

If doubling is ever wanted, a second parallel string (3S2P, 6 modules) gives
13.3 F, halves ESR, and roughly triples the boost count — it raises usable energy
*and* lowers the reachable floor at the same time.

---

## 2. Are these modules safe in series?

Workable, with two real gaps.

### No balancing between modules

Each module balances its own 6 cells. Nothing balances **module against module**.
Capacitance and leakage mismatch makes the stack divide unevenly, and one module
can be pushed past its 16.2 V rating while the bank-level voltage still looks
fine.

The onboard passive balancers do provide crude correction — an over-voltage
module shunts current and pulls itself down — but they only engage near the
per-cell threshold (~2.65 V) and they pass milliamps.

**Regen pushes tens of amps.** Passive balancing is orders of magnitude too slow
to matter during a braking event. Peak module voltage during regen is set by
capacitance matching, not by the balancers, which only tidy up afterwards.

### How real is it? Put numbers on it

During a fast charge the stack divides inversely with capacitance. For three
modules where one is low by X %, the worst module takes
`(1/C_min) / Σ(1/C_i)` of the bank voltage:

| Capacitance spread | Worst module at 43 V | Rating 16.2 V |
|---|---|---|
| matched | 14.33 V | 12 % margin |
| one module −10 % | 15.36 V | 5 % margin |
| one module −20 % | **16.54 V** | **over rating** |

So: harmless if the modules are well matched, **destructive at 20 % mismatch** —
and 20 % is entirely plausible for cheap modules with unspecified cells.

The passive balancers do not save you here. They pass milliamps and only engage
near 2.65 V/cell; regen pushes tens of amps. They tidy up afterwards, they do not
control the peak.

**Two cheap fixes together make it a non-issue:**

- **Drop the bank ceiling to 40 V.** At 20 % mismatch the worst module then sees
  15.4 V — safe. Costs ~13 % of usable energy (6 166 J → 5 336 J at the top end).
- **Sense each module** and derive the ceiling from `max(V_module)` rather than
  the bank total, so the firmware is safe regardless of what the matching turns
  out to be.

Better still, measure each module's actual capacitance before stacking and match
them. A constant-current discharge and a stopwatch is enough.

### Mitigations, cheapest first

1. **Sense each module's voltage** — three switched dividers into three ADC
   channels. Fault on any single module exceeding its limit rather than trusting
   a bank-level reading. Cheap, and it catches the exact failure a bank-level
   sensor cannot see. **Do this one.**
2. **Keep headroom.** 3 × 16.2 V = 48.6 V rated; operating at 43 V means each
   module averages 14.3 V, about 12 % margin. Tolerable, not generous.
3. Add active inter-module balancing if per-module sensing shows real drift.

### The balance boards may be part of the original idle problem

These boards balance passively with per-channel LED indicators. Both the bleed
resistors and any continuously-lit LEDs draw current directly across the cells —
**and no master switch reaches them**, because they are wired to the cells.

This is a plausible second contributor to "the caps drained too quickly," beside
the controller's 3 W.

> **Bench test, before buying anything.** Charge the bank, disconnect everything
> else, and log terminal voltage over several hours. That separates cap leakage
> plus balancer draw from controller idle, and tells you whether the boards need
> higher-value bleeds or replacing. It costs an afternoon and it settles the
> question the rest of this design is guessing at.

---

## 3. The blocking diode — keep it, but change the type

Using a diode so the precharge source can charge the bank without the bank's
voltage appearing on the precharge circuit is **correct and standard**. Keep it.
Without it a 43 V bank backfeeds into the converter output and potentially into
the pack, which is a genuine hazard.

The part choice is not obvious, though, because the two loss mechanisms have very
different duty cycles:

| | Schottky (~0.4 V) | Silicon ultrafast (~1.0 V) |
|---|---|---|
| Forward loss per precharge | ~32 J | ~80 J |
| Reverse leakage at 43 V | ~100 µA → 4.3 mW | ~5 µA → 0.2 mW |
| Leakage cost per day parked | **~370 J** | ~18 J |

Forward loss is paid for 80 seconds, once per ride. Reverse leakage is paid
continuously, and Schottky leakage climbs steeply with temperature.

**Use a silicon ultrafast rectifier, not a Schottky.** It costs ~48 J more per
precharge and saves ~350 J per day parked — most of a bootstrap.

---

## 4. A simplification: drop the boost converter entirely

The precharge only has to reach controller-boot voltage (~12 V). If the
housekeeping pack already sits above that, no boost is needed — just a resistor
and the diode.

**4S Li-ion: 12.0 V empty → 16.8 V full.** Even at the bottom of its range it
clears 12 V after the diode drop. So:

```
pack ──[ master switch ]──[ R ]──[ diode ]──> cap bank
```

That is the entire precharge circuit. No converter, no control loop, no MCU
sequencing, no soft-start logic.

Behaviour falls out for free: caps below pack voltage and they charge; caps above
and the diode blocks. Wired **after** the master switch, so it draws nothing when
off.

### Sizing — corrected

An earlier draft quoted "3τ ≈ 3.3 min," which was wrong twice over. 3τ is the
time to reach 95 % of the *asymptote*, not the time to reach the 12 V target, and
it ignored the diode drop that lowers the asymptote in the first place.

The right expression, with the diode included:

```
V_cap(t) = V_final · (1 − e^(−t/RC))        V_final = V_pack − V_f
t = RC · ln( V_final / (V_final − V_target) )
```

With C = 6.67 F, V_f ≈ 0.8 V, target 12 V:

| R | τ | I peak | pack 16.8 V | 14.8 V | 13.5 V | 12.0 V |
|---|---|---|---|---|---|---|
| 10 Ω | 66.7 s | 1.4 A | 1.5 min | 2.2 min | 3.2 min | **never** |
| **4.7 Ω** | 31.3 s | 3.0 A | **0.7 min** | **1.0 min** | **1.5 min** | **never** |
| 2.2 Ω | 14.7 s | 6.4 A | 0.3 min | 0.5 min | 0.7 min | **never** |

**Use 4.7 Ω** — about a minute on a nominal pack, 3 A peak, ~10 W average and
~42 W momentary into a 50 W wirewound. Resistor value sets speed and peak current
only; the energy burnt is the same either way.

### The diode drop breaks the bottom of the pack

Note the last column. A 4S pack at 12.0 V minus a 0.8 V diode asymptotes at
11.2 V and **never reaches the 12 V target, at any resistor value or any
duration.** The earlier claim that 4S "clears 12 V even at the bottom of its
range" forgot the diode.

**Fix: reserve the bottom of the pack.** Hold the usable range at 13.5 – 16.8 V
(≈ 3.4 – 4.2 V/cell), enforced by the BMS or in firmware. That guarantees
V_final ≥ 12.7 V and a worst case of 1.5 min. It also happens to be good practice
for Li-ion longevity — sitting at 3.4 V/cell rather than 3.0 V costs little
capacity and buys cycle life.

If bench measurement shows the controller boots below 12 V, that margin problem
eases quadratically.

### Energy per bootstrap

```
charge moved    Q = C·V = 6.67 · 12 = 80 C
from pack       14.8 · 80 = 1 184 J
stored in caps  ½ · 6.67 · 12² = 480 J
burnt in R+diode          704 J        (41 % efficient)
```

4S 1000 mAh = 53 280 J → **~45 bootstraps**. Still comfortably Option A.

### What it costs

- The resistive charge is ~50 % efficient. Irrelevant here: 480 J wasted per ride
  against a 14.8 Wh pack, and it buys the removal of a whole converter.
- 4S needs a BMS. Standard, ~$3, and a solved problem for Li-ion.
- Buck from 12–16.8 V to 5 V/3.3 V is easy and efficient — better than from 2S.
- Precharge stops at pack voltage, so it cannot be raised later to give an
  immediate first boost without adding the converter back.

**Recommended.** It replaces a boost converter, its inrush problem, its output
disconnect, and its control logic with two passive components.

---

## 4a. Can we just buy the right thing?

Target: **5.5–30 kJ, ≤ 48 V for the FSESC 4.20's 50 V ceiling, self-balancing,
cheap.** Short answer: nothing off-the-shelf hits all four, because the market
splits either side of this size.

### The market, in three tiers

| | Example | Energy | Balancing | Why it does / doesn't fit |
|---|---|---|---|---|
| **Consumer** | GDCPH 16 V 20 F car-audio module | 2.6 kJ each | 6S board built in | Cheap and available, but 16 V — needs stacking, which is where the problem comes from |
| **Industrial** | Maxwell BMOD0165 P048, Eaton XLR-48 | **190 kJ** | Full active balancing + monitoring + alarm | Technically perfect and **~14 kg, four figures**. 6× more energy than the top of our range. Out on mass alone, never mind price |
| **The gap** | 18 × 2.7 V cells at 300–400 F | 15–23 kJ | — | Exactly right, sold as loose cells |

Nobody sells a 1 kg, 20 kJ, 48 V, balanced module for hobby money. The
industrial vendors jumped straight from 16 V consumer parts to 190 kJ bus-sized
modules.

### Option 1 — lowest effort, uses what you own

**3S2P: six GDCPH modules.** 48.6 V, 13.3 F.

```
usable 10 → 40 V = ½ · 13.3 · (1600 − 100) = 9 975 J
```

Comfortably inside the target range, and paralleling helps twice over: it doubles
the energy, and it **halves the relative capacitance spread** of each series
element, because a parallel pair averages out. That directly shrinks the
imbalance problem quantified in §2 rather than working around it.

Still three elements in series, so per-module sensing and the 40 V ceiling stay.
But it is three more of a part already proven in the build, and no new
engineering at all.

### Option 2 — the "right" bank, one balancer

**18 × 350 F 2.7 V cells** in series: 48.6 V, 19.4 F.

```
usable 10 → 40 V = ½ · 19.4 · 1500 = 14 550 J     ≈ 8–9 boosts
```

Roughly **2.7× the energy of the current 3-series stack for about 2× the mass**
(~1.1 kg of cells), and one continuous balancing scheme across all 18 taps
instead of three isolated islands — which removes the inter-module gap entirely
rather than mitigating it.

For balancing with genuinely minimal effort, **shunt-balancing MOSFETs** (ALD
SAB series, e.g. ALD810025 — four channels per package, five packages covers 18
cells) are purpose-built for this. They conduct only above the cell threshold and
draw nanoamps below it, so unlike resistor bleeds they cost nothing at idle.
Given §2's finding that the stock balance boards may be part of the original idle
drain, that matters. **Availability and price unverified — check Mouser/Digi-Key
Canada before committing.**

Cell sourcing splits sharply: Maxwell/Eaton through Digi-Key Canada is
~$15–25/cell (≈ $270–450), AliExpress-grade is ~$4–8/cell (≈ $72–144) with
unspecified provenance and wider matching spread.

### Option 3 — Chinese 48 V modules

48 V supercapacitor modules do exist from Alibaba-tier suppliers, which would be
a single pre-assembled part with its own balancing. Plausibly $100–200. **Not
investigated** — specs, cell quality and balancer behaviour would all need
verifying, and the same unspecified-cell risk applies as with the 16 V modules.
Worth a look if a single part number matters more than knowing what is inside.

### Recommendation

**Option 1 if the goal is to stop engineering and start riding** — three more of
a known part, ~10 kJ, no new problems.

**Option 2 if the bank is being rebuilt anyway** — it is the configuration the
industrial 48 V modules use internally, at a size that suits a bicycle, and it is
the only option that actually eliminates the balancing question rather than
bounding it.

### Lithium-ion capacitors — the one worth considering

LICs hybridise a Li-ion anode with an EDLC cathode. Against EDLC:

| | EDLC | LIC |
|---|---|---|
| Self-discharge | ~50 % voltage in 30–72 h | **< 5 % per month** |
| Energy density | baseline | 4–5× |
| Cell voltage | 2.7 V | 3.8 V max, **2.2 V min** |
| ESR / peak power | lower ESR, higher power | slightly worse, still seconds-scale |

**That retention figure changes the project.** The whole bootstrap subsystem
exists because an EDLC bank is flat after two days. At <5 %/month the bank still
holds yesterday's charge, so:

- Precharge becomes a rare cold-start, not a per-ride ritual
- "No assist for the first several braking events" largely goes away
- The two-rail split gets simpler, though the Li-ion is still wanted for
  electronics and first-fill

There is a catch that turns out to be an advantage. LICs cannot be run to zero —
about 2.2 V/cell floor. Thirteen in series gives **28.6 → 49.4 V**, a much
narrower swing. Less of the range is usable, so more capacitance is needed for
the same joules. But the bus never drops below 28 V, which is exactly the region
where an EDLC bank stops being able to drive the motor usefully. Measured as
*energy you can actually spend on assist*, the gap narrows considerably.

**Not verified: cost and Ontario availability.** LICs are pricier and less widely
stocked than EDLC modules, and that may kill it. Worth a sourcing check before
committing, because if the price is tolerable it removes an entire subsystem's
worth of justification.

---

## 5. Open

- Confirm the actual module count and configuration in the existing build; §1
  assumes 3-series.
- Measure bank self-drain with everything disconnected (§2).
- Measure the controller's true minimum boot voltage — sets whether 4S is enough
  headroom.
