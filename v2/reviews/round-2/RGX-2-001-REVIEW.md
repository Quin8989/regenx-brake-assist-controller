# ReGenX v2 — Design Review

**Reviewing** RGX-2-001 Rev A · RGX-2-100 Rev B · package dated 2026-08-02
**Review date** 2026-08-02
**Scope** Electrical schematic and parts only. Mechanical, firmware, control law and
vehicle dynamics are excluded at the reviewer's direction.

---

## Scope and method

Every figure in §4 was recomputed from §3 rather than checked against its presentation.
Every reference designator, value, net endpoint and pin assignment in §7 and §8 was
compared against both schematic parts lists and against the SVG geometry of sheets 2 and 3.
Protection elements in §9 were evaluated against the currents and voltages in §4, for
current rating and voltage rating separately.

Excluded by instruction: anything already listed in §11 (unverified parameters) or §12
(known open); the absence of the regen control law.

**Parked, not dismissed.** Two hard errors fall outside the electrical scope and are
recorded here in one line each so they are not lost:

- **§1, third kinematic line.** `ω_carrier = (k·ω_wheel − ω_motor)/(1 + k)` contradicts the
  Willis relation on the line directly above it, which rearranges to
  `ω_carrier = (ω_motor + k·ω_wheel)/(1 + k)`. The sign on `ω_motor` is wrong.
  `planetary-freegen-sim.html:240` implements the correct form; the spec does not.
  Propagates to all slip arithmetic — with the carrier perfectly locked the spec's form
  returns `s = 2.00` where the answer is `0.00`.
- **§4.2, ΔKE.** `½m(v₂²−v₁²)` omits rolling resistance and aerodynamic drag over the
  ~2.2 s boost. Roughly 85 J against 1157 J, so N_boosts is ~7 % optimistic.

---

## Summary of findings

| # | Finding | Class |
|---|---|---|
| 1 | F1 (50 A) does not protect 10 AWG (40 A); firmware substituted for OCP | Safety / coordination |
| 2 | S1 make rating 150 A vs 222–270 A actual | Safety / part rating |
| 3 | Module balancing cannot act at 40 A; 48.6 V absolute is really 45.4 V | Safety / part rating |
| 4 | `E_R1` formula wrong; 2 of 3 rows compute an unreachable voltage; R1 over its rating | Arithmetic / part rating |
| 5 | Keep-alive figures computed at the most favourable pack voltage only | Arithmetic |
| 6 | `R1_max` uses the wrong criterion; collapse margin overstated 2.8×; R1 value contradicts itself | Equation / drawing conflict |
| 7 | `V_lo` omits wiring, fuse, switch and connector resistance — zero brownout margin | Equation |
| 8 | C4 hold-up computed at 0.27 W while the same drawing states 0.35 W | Arithmetic |
| 9 | §4.2 ESR dissipation: 400 W / 25 % should be 432 W / 27 % | Arithmetic |
| 10 | Two contradictory decay constants in one table (69 h vs 5.4 days) | Arithmetic |
| 11 | Shorted-D1 fault evaluated at 22 V, not the 40 V ceiling; no §9 row | Coordination |
| 12 | THR has no series limiter; D3 clamps above RP2040 absolute maximum | Part rating |
| 13 | U1 `I_CC` sized against standby leakage, not the operating drain | Part sizing |
| 14 | Drawing vs spec disagreements (7 items) | Documentation |
| 15 | Unprotected and unacknowledged (6 items) | Coverage |

---

# Findings

## 1. F1 (50 A) does not protect the 10 AWG bank conductor (40 A)

**Confidence: high.** §9 states the contradiction inside its own cell.

> `Bank terminal short, 111–222 A | F1 50 A Class T | Interrupts at 160 V DC. 10 AWG rated ≤40 A bundled; A1 limited to 40 A in configuration`

A 50 A fuse cannot protect a conductor of 40 A stated ampacity. The row closes the gap
with "A1 limited to 40 A in configuration" — a firmware setting on the device being
protected against. Overcurrent protection must be independent of the thing it protects from.

**F1 is correctly chosen for the fault it is listed against.** Worst case, max ESR, least energy:

```
I₀     = 40 / 0.36                = 111 A
τ      = R·C = 0.36 × 6.667       = 2.40 s
∫I²dt  = I₀²·τ/2 = 12 321 × 1.20  = 14 785 A²s
```

against a JJN-50 melting I²t of order 400 A²s. Clears comfortably. 160 V DC against a
48.6 V bank is correctly rated, as is the interrupting rating. The defect is the
**overload** case, which §9 never evaluates.

**Failure scenario.** VESC configuration reloaded from defaults, or battery-max left equal
to motor-max. A1 draws its rated 50 A continuous from a bank at 30 V. F1 sits at 100 % of
rating and does not open — a 50 A Class T carries 50 A indefinitely. The 10 AWG carries
125 % of its stated bundled ampacity, inside a bundle that per Sheet 3 also contains three
motor phase leads and the sensor cable. Insulation runs to thermal failure with no
protective element in the path.

**Should be instead.** Upsize the bank circuit to **8 AWG** (~55 A bundled) and keep
F1 = 50 A. This is the cheap fix and it also recovers I·R drop that finding 7 depends on.
Dropping F1 to 40 A instead puts a 40 A fuse at a 40 A continuous load, which nuisance-opens,
forcing the A1 limit to ~30 A and breaking §2's boost count.

**Also correct the threat label.** F1 is downstream of BANK+. A short at the bank terminals
themselves — C1/C3 lugs, ring terminals, the 10 AWG run from the bank to F1 — is **not** in
F1's zone of protection. That segment is unfused by anything. Relabel the row "short
downstream of F1" and state explicitly that the bank-to-F1 segment is protected only by
construction.

---

## 2. S1's make rating (150 A) is below the make current the design's own table implies

**Confidence: high.** §4.7 uses two different ESR values for the same physical event on
adjacent lines.

```
Bank terminal short at 40 V         | 111–222 A (148 A nominal)
S1 make into A1 input capacitance   | ~148 A
```

Both are the bank driving a near-zero impedance. The first line is honest about the ESR
range. The second silently takes the nominal value — and **S1's specification
(`make ≥150 A`) was then written against that number.**

```
R_esr = 270 mΩ (nominal), 40 V   :  I = 40   / 0.27 = 148 A   ← what S1 was specified against
R_esr = 180 mΩ (minimum), 40 V   :  I = 40   / 0.18 = 222 A   ← 48 % over S1's rating
R_esr = 180 mΩ, 48.6 V absolute  :  I = 48.6 / 0.18 = 270 A   ← 80 % over
```

**Failure scenario.** Rider finishes a descent with the bank near 40 V, opens S1, closes it
again 30 s later. A1's bulk input capacitance is at 0 V — §5: "open, A1 and U2 are physically
isolated." Peak make current 222 A into ~700 µF:

```
τ            = 0.18 × 700 µF  = 126 µs
contact energy = ½CV² = ½ × 700e-6 × 1600 = 0.56 J per make
```

Repeated makes at 1.5× the contact rating weld S1 closed.

**The consequence is worse than the switch.** §5: "S1 is the sole disconnect." §2 fixes no
bleed resistor. A welded S1 means the bank cannot be isolated at all, and Sheet 2 NOTE 7's
safe-to-work procedure ("DISCONNECT BT1, DISCHARGE EXTERNALLY, VERIFY < 2 V BEFORE WORK")
loses its ability to break the A1 branch.

**Should be instead.** Rate S1 from the **minimum** ESR at the **absolute** bank voltage:
`make ≥300 A, ≥100 A continuous, ≥60 V DC break`. Correct §4.7's second line to `111–270 A`
so the two rows agree. Add a §9 row for S1 make — there is none.

---

## 3. Module balancing cannot act at the design's currents, and the stated absolute is wrong

**Confidence: high on the arithmetic; high on the current-capability point.**

Two separate errors in one §9 row:

> `Bank overvoltage | Module integral balancing | Ceiling 40 V working vs 48.6 V absolute; modules matched ≤10 %`

### (a) The stated absolute is wrong

§4.1 gives `V_hi = 48.6 V absolute (3 × 16.2 V)`. That assumes the three modules divide the
bank equally. §9 allows "modules matched ≤ 10 %." Series capacitors divide voltage as 1/C:

```
one module 10 % low:
  Σ(1/C)            = 2/C + 1/(0.9C) = 3.1111/C
  low module share  = 1.1111 / 3.1111 = 0.35714
  reaches 16.2 V at = 16.2 / 0.35714 = 45.4 V bank
```

**The absolute ceiling is 45.4 V, not 48.6 V.** Headroom above the 40 V working point is
5.4 V, not 8.6 V — a 37 % reduction in the margin every other figure leans on. At 40 V
working the low module sits at 14.29 V (2.381 V/cell), which is fine; the error is confined
to the absolute — but the absolute is the number §9 quotes as protection.

### (b) The balancing is inoperative at the design's own currents

"Integral balancing" in a 16 V EDLC module is intra-module cell balancing — passive bleed or
active shunt, milliampere class. It does not do two things it is credited with:

- It does **not** divide voltage between the three series modules. Module-to-module split is
  set by capacitance during charging and by leakage at rest. Nothing balances module to
  module, and §2 fixes no bleed resistor.
- At the 40 A currents in §4.2, a milliampere-class shunt diverts nothing. Balancing is
  effective only at rest or at trickle currents.

§11 lists module **capacitance** spread as unverified. It does not list **balance-current
capability or leakage spread**, and §9's protection claim depends on both.

**Failure scenario.** Bank charged to 40 V on a descent, bike parked. With S1 open the bank
decays toward BT1's clamp at 13.8 V with:

```
τ = R_leak·C = 37 kΩ × 6.667 F = 246 667 s = 68.5 h
time above 20 V = 68.5 × ln(40/20) = 47.5 h
```

Over two days at elevated voltage the module split drifts toward the ratio of the modules'
leakage resistances rather than their capacitances. EDLC leakage spread is routinely 2–3:1.
At a 2:1:1 spread the high-resistance module takes `2/4 × 40 = 20 V` against a 16.2 V rating
— 3.33 V/cell against a 2.7 V rating. The intra-module shunts will eventually clamp it, but
only at milliamps, and only if they are the active-shunt rather than passive type.

**Should be instead.**

- §4.1 absolute → **45.4 V**, derived from the ≤10 % allowance actually specified.
- §9's element for bank overvoltage must be something that acts at 40 A. Within §2's
  constraints that means **A1's configured maximum input voltage set to 40 V**, so the
  controller stops sourcing regen current independent of anything downstream. List module
  balancing only as a rest-state cell-matching function.
- Add module balance-current capability and leakage spread to §11's measurement schedule.
  Item 2 currently measures capacitance only.

---

## 4. `E_R1` formula is wrong; two of three rows compute an unreachable voltage; R1 exceeds its rating

**Confidence: high. Pure arithmetic.**

§3 gives `E_R1 = ½ · C_bank · V_target²`. That identity — resistor dissipates what the
capacitor stores — holds **only when the capacitor is charged to the full source voltage.**
Here `V_target` = 14 V while `V_final` is 12.5 / 13.8 / 15.8 V. The correct integral:

```
i(t) = (V_final/R)·e^(−t/RC)
E_R  = ∫i²R dt = ½C·V_final²·[1 − e^(−2t/RC)]
V(t) = V_final(1 − e^(−t/RC))   ⟹   e^(−t/RC) = 1 − V/V_final

E_R  = ½·C·V·(2·V_final − V)
```

Check: at `V = V_final` this collapses to `½CV_final²`. ✓

**And §3 states the constraint that kills two rows outright:** "`V_final > V_target`, else
unreachable at any `R1` or duration." Four lines later §4.3 reports `E_R1` → 14 V for
`V_final` = 12.5 V and 13.8 V. **Both are below 14 V. The bank never gets there.** The table
reports 653 J for reaching a voltage the circuit cannot reach, in two of three rows,
contradicting the specification's own constraint.

Corrected — energy dissipated in R1 for a full cold start (0 V to `V_final`, which is what
actually happens):

| `V_pack` | `V_final` | 14 V reachable? | §4.3 states | correct `E_R1` | ΔT at 30 g alu |
|---|---|---|---|---|---|
| 13.5 V | 12.5 V | **no** | 653 J | `3.3333 × 156.25` = **521 J** | 19 K |
| 14.8 V | 13.8 V | **no** | 653 J | `3.3333 × 190.44` = **635 J** | 24 K |
| 16.8 V | 15.8 V | yes | 653 J | `3.3333 × 249.64` = **832 J** | 31 K |

For the one reachable case, energy to reach 14 V specifically is
`½ × 6.667 × 14 × (31.6 − 14)` = **821 J**, not 653 J — 26 % low.

**Consequence for the part.** Sheet 2's parts list carries "650 J per cold start" into R1's
specification. True worst case is **832 J**, 28 % higher. The stated ΔT range of 24–32 K
happens to survive (19–31 K corrected), so R1's thermal mass is not the problem — but 650 J
is the number a heatsink gets sized from.

**Separately, R1's peak dissipation exceeds its rating.** §4.3 gives P_R1 peak = 53 W at
`V_pack` = 16.8 V (`15.8² / 4.7` = 53.1 W ✓). §7 specifies a **50 W** part. Normal cold start
runs R1 at **106 % of rating for ~22 s, every time**. That is the specified duty, not a fault.

Note also that a 50 W alu-clad rating assumes an adequate heatsink at 25 °C ambient. §7 says
"heatsink or chassis bonded" and specifies no area. In free air the same part is good for
15–20 W.

**Should be instead.** Replace `E_R1 = ½C·V_target²` with `E_R1 = ½·C·V·(2·V_final − V)` in
§3. Restate §4.3's column as full-charge energy. Specify R1 at **100 W**, or move to 8 Ω
(peak `15.8²/8` = 31.2 W) — but read finding 6 before choosing 8 Ω. Specify the heatsink
area, not "heatsink or chassis bonded."

---

## 5. Keep-alive figures are computed at the most favourable pack voltage, presented unqualified

**Confidence: high.** The stated numbers reproduce exactly at `V_pack` = 16.8 V and at no
other point.

§4.5 reports, under a generic label:

```
S1 closed, stationary — bank settles   14.85 V
— current from BT1                     0.202 A, 3.39 W
— BT1 life                             4.4 h
```

Solving `V_bank² − (V_pack − V_F)·V_bank + P_idle·R1 = 0` with `V_F` = 1.0 V, `R1` = 4.7 Ω
(the BOM value), `P_idle` = 3 W, so `P_idle·R1` = 14.1:

| `V_pack` | discriminant | `V_bank` (upper root) | `I_R1` | BT1 output | life at that rate |
|---|---|---|---|---|---|
| **16.8 V** (full charge) | `249.64 − 56.4` = 193.24 | **14.85 V** | **0.202 A** | **3.39 W** | **4.42 h** |
| 14.8 V (nominal) | `190.44 − 56.4` = 134.04 | 12.69 V | 0.236 A | 3.50 W | 4.29 h |
| 12.0 V (BMS UV cut) | `121 − 56.4` = 64.6 | 9.52 V | 0.315 A | 3.78 W | 3.97 h |

Row 1 is §4.5 exactly. **The figures were computed at 4.2 V/cell — the single most
favourable point in the pack's discharge curve, held for minutes after a charge — while
every other figure in §4.5 uses ~14 V.**

Two consequences:

- **The rest voltage is 12.69 V, not 14.85 V.** §6 RUN asserts "bank is already above A1's
  floor," which remains true (12.69 > 8), so nothing breaks — but 12.69 V is the number that
  every ride starts from, and it is below the D1 conduction threshold, which matters for
  finding 13.
- **The drain rises as the pack empties.** 3.39 W → 3.50 W → 3.78 W. The equilibrium is
  regenerative in the unfavourable direction. Integrating across the discharge at ~95 % usable
  energy:

```
average drain ≈ 3.56 W
life ≈ 14.25 Wh / 3.56 W = 4.0 h
```

**4.0 h, not 4.4 h — 9 % optimistic.**

**Should be instead.** State §4.5's keep-alive row at `V_pack` = 14.8 V nominal with the
16.8 V and 12.0 V cases as bounds, exactly as §4.3 and §5 already do for precharge and for
D1 conduction. The inconsistency is that §4.3 and §5 both tabulate all three pack voltages
and §4.5 silently picks one.

---

## 6. `R1_max` uses the wrong criterion; collapse margin overstated 2.8×; R1's value contradicts itself

**Confidence: high.**

### (a) The criterion is wrong

§3 derives `R1_max = (V_pack − V_F)² / (4 · P_idle)` from requiring the quadratic to have
real roots. At `V_pack` = 12.0 V that gives `121/12` = **10.083 Ω**, matching §4.4's 10.1 Ω.

But at the discriminant limit the two roots coincide at `V_bank = (V_pack − V_F)/2` = **5.50 V**.
**A1's floor is 8 V.** An equilibrium exists and A1 is dead in it. The existence of a real
root is not the binding constraint.

The binding constraint is that the upper root must be at or above A1's start-up voltage:

```
V_startup² − (V_pack − V_F)·V_startup + P_idle·R1 = 0   at the limit

R1_max = V_startup · (V_pack − V_F − V_startup) / P_idle
       = 8 × (11 − 8) / 3
       = 8.0 Ω
```

So the correct `R1_max` is exactly the 8 Ω §4.4 calls "specified" — but §4.4 derives 10.1 Ω
from the wrong formula and then presents 8 Ω as a choice with margin. **It is not margin. 8 Ω
is the limit, with zero margin at the BMS UV cut.** Verify directly:
`V² − 11V + 24 = 0` → roots `(11 ± 5)/2` = **8.0 V** and 3.0 V. The upper root sits exactly
on A1's floor.

### (b) A mislabelled quantity and an overstated margin

§4.4 reads `Supply collapse | V_bank < 7.51 V ⟹ V_pack < 8.51 V`. The 7.51 is:

```
√(4 · P_idle · R1) = √(4 × 3 × 4.7) = √56.4 = 7.5100
```

which is **`(V_pack − V_F)` at the discriminant limit, not `V_bank`.** Actual `V_bank` at that
point is `7.51/2` = **3.76 V**. The label is wrong.

Using the correct criterion (`V_bank` ≥ 8 V):

```
R1 = 4.7 Ω :  4.7 = 8(V_pack − 9)/3  →  V_pack = 10.76 V   margin to 12.0 V BMS cut = 1.24 V
R1 = 8.0 Ω :                              V_pack = 12.00 V   margin = 0 V
```

**§4.4's "Margin to collapse 3.49 V" (= 12.0 − 8.51) overstates the real margin by 2.8×.**
The correct figure is 1.24 V at 4.7 Ω, or zero at 8 Ω.

### (c) Three statements of R1's value, no governing one

| Source | Value |
|---|---|
| §7 component schedule | 4.7 Ω ±5 % |
| Sheet 2 parts list | 4.7 Ω ±5 % |
| §4.3 precharge computation | 4.7 Ω |
| §4.4 R1 bounds | "**8 Ω specified**" |
| Sheet 2 NOTE 2 | "R1 RANGE 4.7 Ω MIN TO 8 Ω MAX" |

**The BOM is the side to trust for a build: 4.7 Ω.** Which makes §4.4's entire upper-bound
analysis decorative, and means the "8 Ω specified" line should be struck.

**Should be instead.** Keep **R1 = 4.7 Ω**, uprate it to 100 W per finding 4, and replace
§3's `R1_max` with `V_startup·(V_pack − V_F − V_startup)/P_idle`. 4.7 Ω is the right choice of
the two: it preserves 1.24 V of collapse margin where 8 Ω has none. The cost is peak
dissipation, which is a resistor-wattage problem and cheap to solve; zero collapse margin is
not.

---

## 7. `V_lo` omits wiring, fuse, switch and connector resistance — zero brownout margin by construction

**Confidence: high on the mechanism; medium on `R_ext`, which is an estimate.**

§3: `V_lo = V_startup + I_boost · R_esr`. Bank ESR only. The voltage A1 actually sees is:

```
V_A1 = V_bank(oc) − I·(R_esr + R_wire + R_F1 + R_S1 + R_connectors)
```

Estimating the omitted terms for the drawn topology:

```
10 AWG, ~1.5 m round trip @ 3.28 mΩ/m   ≈ 4.9 mΩ
F1 Class T element                       ≈ 0.8 mΩ
S1 contacts                              ≈ 1.0 mΩ
4 × ring-terminal joints @ ~0.5 mΩ       ≈ 2.0 mΩ
                                   R_ext ≈ 8.7 mΩ
```

At the tabulated worst case (40 A, nominal ESR, `V_lo` = 18.8 V):

```
V_A1 = 18.8 − 40 × 0.27 − 40 × 0.0087
     = 18.8 − 10.80 − 0.35
     = 7.65 V
```

**Below A1's 8 V floor.** The boost terminates on A1 undervoltage before the tabulated `V_lo`
is reached. By construction `V_lo` is defined as the exact point where A1's terminal voltage
equals its start-up voltage — the design deliberately runs to the brownout boundary with
**zero margin**, and then omits the terms that push it over.

Effect on the boost table, with `R_total` = 0.2787 Ω:

| case | §4.2 states | corrected |
|---|---|---|
| 40 A, nominal ESR | `V_lo` 18.8 V, N = 1.59 | `V_lo` 19.15 V, N = **1.55** |
| 20 A, nominal ESR | `V_lo` 13.4 V, N = 2.28 | `V_lo` 13.57 V, N = **2.26** |
| §2 invariant limit | ≤ **27.8 A** | ≤ **26.9 A** |

The boost count barely moves — 2–3 %. **The finding is not the count. It is that there is no
brownout margin at all**, and the equation that sets the operating floor has no term for the
conductors, fuse, switch and connectors between the bank and the controller.

**Should be instead.**

```
V_lo = V_startup + V_margin + I_boost · (R_esr + R_ext)
```

with `V_margin` ≥ 1 V and `R_ext` measured on the built harness. Recomputing at 40 A / nominal
with a 1 V margin gives `V_lo` = 20.15 V and N = 1.52 — the invariant is unaffected, and A1
now has real headroom. Upsizing to 8 AWG per finding 1 cuts `R_ext` to ~6 mΩ and buys back
0.1 V.

---

## 8. C4 hold-up is computed at 0.27 W while the same drawing states 0.35 W

**Confidence: high.**

Sheet 3 parts list, two rows apart:

```
A1  | ... BEC load 70 mA of 1500 mA
C4  | ... VSYS hold-up 4.0 ms at 0.27 W to the 1.8 V floor
```

`70 mA × 5 V = 0.350 W`. The hold-up figure uses `0.27 W` = 54 mA. **Same rail, same drawing,
two different loads.** §4.8 repeats both.

The energy is right:

```
½C(V_bec² − V_sys,min²) = ½ × 100e-6 × (25 − 3.24) = 1.088 mJ   ✓
t at 0.27 W = 1.088e-3 / 0.27 = 4.03 ms                          ✓ as stated
t at 0.35 W = 1.088e-3 / 0.35 = 3.11 ms                          ← correct
```

**3.1 ms, not 4.0 ms.** And that is the nominal-capacitance figure. C4 is specified as
"100 µF, ≥10 V, aluminium electrolytic" with no tolerance:

```
at −20 % tolerance (80 µF)          : 2.5 ms
at end of life (−50 %, typical)     : 1.6 ms
```

§9 correctly calls this "**Decoupling only**," which is the right caveat. But the drawing
states 4.0 ms as a property of the part, and the true worst case is a quarter of that.

**Should be instead.** State 3.1 ms nominal and 2.5 ms at rated tolerance, or increase C4 to
**220 µF** (6.8 ms nominal, 5.5 ms at −20 %) — same footprint class, negligible cost. Specify
tolerance and an endurance rating (105 °C, ≥2000 h) since the part sits on a rail whose
purpose is ride-through.

---

## 9. §4.2's ESR dissipation figure matches no ESR in the design

**Confidence: high.**

> "Bank ESR dissipation at 40 A: 400 W instantaneous (25 % of delivered power), 36 % averaged
> over a boost."

```
at R_esr = 180 mΩ : I²R = 1600 × 0.18 = 288 W
at R_esr = 270 mΩ : I²R = 1600 × 0.27 = 432 W   ← nominal
at R_esr = 360 mΩ : I²R = 1600 × 0.36 = 576 W
```

**400 W corresponds to 250 mΩ, which is not the nominal, the minimum, or the maximum.**
The correct nominal is **432 W**, and the worst case is **576 W**.

The percentage is wrong for the same reason:

```
fraction of bank-sourced power at 40 V = 432 / 1600 = 27 %   (not 25 %)
```

The averaged figure is a rounding slip in the wrong direction:

```
1 − η_esr = 1 − 0.632653 = 0.3673 → 37 %   (stated 36 %)
```

**Should be instead:** "432 W instantaneous at nominal ESR (27 % of bank-sourced power),
576 W at worst-case ESR; 37 % averaged over a boost." The stated figure understates
worst-case bank heating by 44 %, which matters for finding 15(a).

---

## 10. Two contradictory decay constants in one table

**Confidence: high.**

§4.5, lines 1 and 3:

```
Bank leakage at 14 V        | 0.37 mA, 5.2 mW (τ ≈ 69 h, R_leak ≈ 37 kΩ)
Bank decay, BT1 disconnected| τ = 5.4 days
```

5.4 days = 129.6 h. From the table's own parameters:

```
R_leak = 14 / 0.37e-3 = 37 838 Ω
τ      = R·C = 37 838 × 6.6667 = 252 253 s = 70.1 h = 2.92 days
```

Using the rounded 37 kΩ: `37 000 × 6.6667` = 246 667 s = 68.5 h, matching line 1's "≈69 h."

**Line 3 is wrong by 1.85×. The correct value is 2.9 days.**

Line 1 is internally consistent (`0.37 mA × 14 V = 5.18 mW` ✓). No operational harm — the
error is in the conservative direction, and Sheet 2 NOTE 7 already mandates external discharge
and verification below 2 V regardless. But two values for one quantity in one table is a
correctness defect, and 5.4 days is the figure someone will quote when asked how long the
bank stays live.

---

## 11. Shorted-D1 fault is evaluated at the wrong operating point, and §9 has no row for it

**Confidence: high.**

§4.7: `Shorted D1, bank at 22 V | 1.1 A`. Arithmetic checks: `(22 − 16.8)/4.7 = 1.106 A` ✓.

But every other row in §4.7 uses the 40 V bank ceiling — the table's own first row is "Bank
terminal short **at 40 V**." At 40 V:

```
I = (40 − 16.8) / 4.7 = 23.2 / 4.7 = 4.94 A
```

BT1 is ~15 Wh at 14.8 V ≈ **1.01 Ah**. **4.94 A is a 4.9 C charge into a 4S Li-ion pack**,
flowing backwards through F3, from a source with no CC/CV control and no charge termination.

F3 is a **5 A time-delay**. At 4.94 A it sits at 99 % of rating and will not open.
**§9 has no row for a shorted D1 at all.** The only element that clears it is BT1's integral
BMS on OV or OC — a single point of protection with no backup, for a fault §4.7 explicitly
tabulates and then evaluates at the one voltage that makes it look benign.

At the corrected absolute of 45.4 V (finding 3): `(45.4 − 16.8)/4.7` = 6.09 A. Only then does
F3 have any authority, and a 5 A time-delay at 122 % takes minutes.

**Should be instead.**

- Tabulate the shorted-D1 case at 40 V: **4.94 A**.
- Add a §9 row naming BT1's BMS as the clearing element, and state that F3 does not
  discriminate at 40 V.
- Consider **F3 → 3 A time-delay**. Check against the precharge duty: §4.3 peak is 3.36 A
  decaying exponentially over ~22 s, so 112 % of a 3 A time-delay for a fraction of that
  window — well inside a time-delay characteristic. At the shorted-D1 current of 4.94 A
  (165 % of 3 A) it opens in roughly 10–60 s. That gives F3 real authority over this fault
  while still passing cold start. This is a genuine improvement available for the price of a
  different fuse.

---

## 12. THR has no series limiter, and D3 clamps above the RP2040's absolute maximum

**Confidence: high.**

Sheet 3: J3 SIG at (200, 578) runs **straight** to U2 at x = 640 (GP26). R2 is a shunt
pulldown, C7 a shunt capacitor, D3 a shunt TVS. **There is no series element in the signal
path.**

Compare the other two external signals on the same sheet:

| net | leaves enclosure | series limiter |
|---|---|---|
| SPD | yes | R6, 1 kΩ |
| UART_TX / UART_RX | no (inside enclosure to A1) | R3, R4, 1 kΩ |
| **THR** | **yes — §9 says so explicitly** | **none** |

§9's own row reads `Throttle transient | D3 TVS + C7 | Line leaves the enclosure`. It is the
only line the design identifies as leaving the enclosure, and the only one with no series
impedance.

**D3 is specified by the wrong parameter.** §7 and Sheet 3 both give
"Unidirectional, V_RWM 3.3 V, V_BR ≥ 3.5 V" — breakdown voltage, but **not clamping voltage
`V_C`**, which is the parameter that determines what the pin sees. A 3.3 V TVS in SOD-323
(SMAJ3.3A class) clamps at **`V_C` ≈ 6.0 V** at rated peak pulse current.

```
RP2040 GPIO absolute maximum = IOVDD + 0.5 = 3.8 V
D3 clamping voltage          ≈ 6.0 V
                        over  = 58 %
```

**Failure scenario.** Throttle cable routed near the motor phase leads — Sheet 1 NOTE 3 warns
about exactly this — and a 40 A commutation transient couples in. D3 conducts and holds the
line at ~6 V. GP26's internal ESD structure conducts to the 3.3 V rail with only the cable's
own impedance limiting the current. Injection current is unbounded. Latch-up or permanent pin
damage, on the input that commands assist.

**Should be instead.**

- Add **R7 = 1 kΩ ±5 %, 1/4 W in series in THR**, between J3 SIG and GP26, upstream of the
  C7 node — matching R3/R4/R6 and giving the same 1.7 mA fault limit the rest of the design
  uses. `R7·C7 = 100 µs`, which is irrelevant against a throttle's ~10 Hz bandwidth, and C7
  remains directly at the pin as the ADC charge reservoir, so ADC source impedance is
  unaffected.
- Respecify D3 by **clamping voltage: `V_C` ≤ 4.5 V at 1 A**, not by `V_BR`.

---

## 13. U1's `I_CC` is sized against standby leakage, not against the operating drain

**Confidence: high on the arithmetic; the duty-cycle conclusion depends on ride profile.**

§4.6's energy balance is exact — for standby:

```
U1 delivered  = 0.2 A × 16.4 V = 3.28 W
2.3 min       = 0.03833 h
replacement   = 3.28 × 0.03833 = 0.1257 Wh = 126 mWh   ✓ as stated
standby drain = 5.2 mW × 24 h  = 125 mWh                ✓ matched
```

But the **S1-closed** drain is 3.50 W (finding 5) — **673× the standby drain** — and §4.6
does not address it. §2's closed-loop invariant is asserted for the operating case and
demonstrated only for the standby case.

From the design's own numbers, while riding:

| bank voltage | D1 | U1 | BT1 |
|---|---|---|---|
| ≥ 19.2 V | off | on | **gains 3.28 W** |
| 13.8 – 19.2 V | off | off | neutral |
| < 13.8 V | on | off | **loses 3.50 W** |

```
break-even:  f_above × 3.28 ≥ f_below × 3.50
             f_above ≥ 1.067 × f_below
```

**The bank's rest state is 12.69 V — below 13.8 V — so every ride starts in the draining
region**, and returns there whenever regen is idle long enough. Nothing in the electrical
design constrains `f_above`.

Separately, §4.6's "3.4 W input" for 3.28 W output implies **96 % efficiency**. An LM2596HV
buck producing 17.2 V from a 19–48 V input runs at **80–88 %**. Realistic input:
`3.28 / 0.85` = **3.86 W**.

**Should be instead.** Size `I_CC` from a ride-duty analysis, not from standby leakage. The
fix costs nothing in parts:

```
at I_CC = 1.0 A:  delivered = 16.4 W,  break-even f_above ≥ 0.21 × f_below

U1 input current at 85 % efficiency:
  from 25 V bank  : 16.4 / (0.85 × 25)   = 0.77 A   ← under F2's 2 A ✓
  from 48.6 V bank: 16.4 / (0.85 × 48.6) = 0.40 A   ✓
U1 dissipation    : 16.4 × (1/0.85 − 1)  = 2.9 W    ← needs heatsinking
```

LM2596HV is a 3 A part, so 1 A out is within it. **0.5–1.0 A is the right region.** Correct
§4.6's input power to 3.86 W at the current setpoint, and add U1's dissipation and heatsink
requirement to §7 — neither appears anywhere.

---

## 14. Drawing vs spec disagreements

For each: which side I think is correct.

### (a) §8 omits two nets the drawing carries

The schematic interface schedule has `THR_GND` and `DS1_GND`, both "return, per NOTE 8."
§8 has neither.

**The drawing is correct.** Sheet 3 NOTE 8 mandates local signal returns, and a mandated
return is a net that must be scheduled. Add both rows to §8.

### (b) The GND row contradicts Sheet 3 NOTE 8 — on both documents

Both §8 and the schematic schedule read:

```
GND | A1 V− star | BT1 −, C3 −, U1 −, U2 GND | power, 10 AWG
```

Sheet 3 NOTE 8 reads: "SIGNAL GROUNDS RETURN LOCALLY TO U2, THEN TO STAR VIA THE BEC RETURN
**ONLY**."

U2's ground reaches the star via the **22 AWG BEC return**, not a 10 AWG conductor.

**NOTE 8 is correct.** A separate heavy ground to U2 alongside the BEC return forms a ground
loop around the motor-current path — precisely what NOTE 8 exists to prevent. Both schedules
should drop `U2 GND` from the 10 AWG row. The `+5V` row should also be annotated as a
two-conductor pair (+5 V and its return, 22 AWG); the return currently appears in no schedule
at all.

### (c) Sheet 3 NOTE 2 contradicts the SPD net's declared type

NOTE 2: "U2 GPIO ARE 3.3 V. **NO 5 V SIGNAL SHALL BE APPLIED TO ANY U2 PIN.**"

SPD row, both schedules: "digital, **5 V domain at source**", terminating at GP13. §9 carries
a row for "SPD 5 V injection into U2" with R6 as the mitigation and 1.7 mA as the limit.

**The schedules and §9 are correct; NOTE 2 is written as an absolute prohibition when the
design deliberately accepts a 5 V-domain source behind a 1 kΩ limiter.** As written the note
prohibits the circuit that is drawn.

Should read: "U2 GPIO ARE 3.3 V AND NOT 5 V TOLERANT. ANY 5 V DOMAIN SIGNAL SHALL ENTER
THROUGH A SERIES LIMITER OF ≥1 kΩ." That version is enforceable and matches the drawing.

### (d) Three zone errors, in a revision that claims to have fixed zones

Sheet row boundaries are A = 34–242, B = 242–450, C = 450–658, D = 658–866.

| Part | Listed | Symbol y-extent | Correct |
|---|---|---|---|
| C4 (Sheet 3) | A3 | 268–280 | **B3** |
| C5 (Sheet 3) | A3 | 268–280 | **B3** |
| S1 (Sheet 2) | A3 | 246–272 | **B3** |

Rev B's changelog states "Zone references corrected." These three survived that pass. S1 is
marginal (4 px past the boundary); C4 and C5 are not.

### (e) R1's value is stated three ways

Covered in finding 6(c). §7 and Sheet 2 say 4.7 Ω; §4.4 says "8 Ω specified"; Sheet 2 NOTE 2
says a 4.7–8 Ω range. **The BOM governs: 4.7 Ω.** Strike the "8 Ω specified" line.

### (f) C4's hold-up figure

Sheet 3 parts list carries "4.0 ms at 0.27 W" while the same list's A1 row states 70 mA
(0.35 W). Covered in finding 8. **Neither side is right — the correct value is 3.1 ms.**

### (g) R1's energy figure

Sheet 2 parts list carries "650 J per cold start." Covered in finding 4. **Correct worst case
is 832 J.**

---

## 15. Unprotected and unacknowledged

Electrical only. Nothing here appears in §11 or §12.

### (a) No bank thermal limit or sensing

At 40 A the bank dissipates 432 W in its own ESR (576 W at maximum ESR) — 24–32 W per cell
across 18 cells. Per full boost:

```
ΔQ         = C·ΔV = 6.667 × (40 − 18.8) = 141.3 C
t          = 141.3 / 40 = 3.53 s
E_esr      = I²R·t = 1600 × 0.27 × 3.53 = 1525 J
per cell   = 1525 / 18 = 85 J
ΔT per cell ≈ 85 / (0.010 kg × 1000 J/kg·K) = 8.5 K
```

...and regen does the same thing again on the way in. §9 has no row for bank overtemperature.
EDLC life is strongly temperature-dependent and ESR **rises** with temperature, so the loop is
regenerative. Nothing measures it and nothing limits it.

### (b) No annunciation of any protective operation

F1, F2 or F3 opening; S2 tripping; BMS UV or OV cut — none is sensed or displayed. DS1 exists
and has spare I²C bandwidth.

Specifically: **S2 is auto-reset.** A sustained fault through R1 cycles indefinitely — R1
heats to 100–110 °C, S2 opens, R1 cools, S2 recloses ~20–30 K lower, repeat — with no latch
and no indication. §9 is right that S2 prevents thermal runaway, but the fault is **modulated,
never cleared, and never reported**, until BT1 is flattened:

```
15 Wh / (3.36 A × 16.8 V) = 15 / 56.4 W ≈ 16 min to the BMS UV cut
```

The user's first symptom is a dead keep-alive pack with no fault indication.

### (c) S2 has no voltage rating

§7: "NC bimetal, open 100–110 °C, auto-reset, ≥5 A." No voltage. Open-circuit voltage across
S2's contacts is `V_pack − V_bank` ≤ 16.8 V, so any standard bimetal (250 VAC class) is
adequate — **low risk, but S2 is the only switching element in the package with no voltage
rating**, and the review brief asked specifically about voltage qualification.

### (d) §4.5's 119-day standby life omits two drains

The figure divides 15 Wh by the bank's 5.2 mW leakage alone.

```
bank leakage, drawn from BT1 at 14.8 V (not 14 V)  ≈ 5.5 mW
4S BMS quiescent, 30–50 µA at 14.8 V               ≈ 0.6 mW
BT1 self-discharge, ~3 %/month                      ≈ 0.6 mW
                                             total ≈ 6.7 mW

15 Wh / 6.7 mW = 2239 h = 93 days
```

**~93–98 days, not 119 — 20 % optimistic.**

### (e) The bank-to-F1 segment is unfused

Covered in finding 1. The 10 AWG between the bank terminals and F1, including all ring
terminals at the bank end, has no protective element in front of it and is exposed to the full
111–222 A available fault current.

### (f) No mis-wire protection on the module stack

C1–C3 are polarised EDLC modules interconnected with 10 AWG and ring terminals. No keying,
no polarity marking requirement, and no assembly-verification note. Reversing one module in a
3-series stack applies 40 V across two modules (20 V each, against 16.2 V) and reverse-biases
the third — reverse voltage on an EDLC is a vent-or-rupture failure. §7, §9 and the notes are
silent; §12 does not list it.

Cheap fix: a Sheet 2 note requiring polarity verification and a measured stack voltage before
the final interconnect is made, and differently-sized or keyed terminals at the module ends.

---

# What is correct

Stated for confidence, since a review that only lists defects gives no sense of what was
actually checked. All of the following were recomputed or traced and are right:

**Arithmetic**

- **§4.2's boost table is fully self-consistent.** All 36 cells reproduce exactly from §3's
  equations. Spot check, 40 A / 270 mΩ: `V_lo` = `8 + 10.8` = 18.8 ✓; `E_usable` =
  `3.3333 × (1600 − 353.44)` = 4155 J ✓; `η_esr` = `1 − 10.8/29.4` = 0.6327 ✓;
  `N` = `4155 × 0.6327 × 0.7 / 1157.4` = 1.59 ✓. The 27.8 A invariant limit is also correct
  as computed (N = 2.00 at that current), before finding 7's correction.
- **`ΔKE` = 1157 J** ✓ — `½ × 100 × (5.5556² − 2.7778²)` = 1157.4 J.
- **§4.1 bank figures** ✓ — 6.667 F, 180–360 mΩ, 5333 J at 40 V, 653 J at 14 V, all correct.
- **§4.3 precharge — `I₀`, `P_R1` peak and `t`→8 V are all nine correct.** e.g. `V_pack` = 16.8:
  `I₀` = `15.8/4.7` = 3.36 A ✓; `P` = `15.8²/4.7` = 53.1 W ✓;
  `t` = `31.333 × ln(15.8/7.8)` = 22.1 s ✓. Only the `E_R1` column is wrong (finding 4).
- **§4.9 sensor resolution — all nine figures correct.** 10 km/h: `2.7778/2.1` = 1.323 rev/s ✓,
  `× 6` = 7.94 Hz ✓, period 126 ms ✓. Same for 20 and 30 km/h.
- **§4.4's `R1_max` = 10.1 Ω is arithmetically correct** for the formula given
  (`121/12` = 10.083) — the formula is what's wrong, not the division.
- **§4.6's charger arithmetic** ✓ — 16.4 V = 4.10 V/cell, below the 4.25 V/cell BMS OV cut;
  19.2 V threshold = `V_out` + dropout; 126 mWh in 2.3 min all check.
- **§4.7's fault currents** ✓ — 111/148/222 A at 40 V for 360/270/180 mΩ; 3.36 A sustained;
  1.1 A for the shorted-D1 case *at the voltage stated*.
- **§9's 1.7 mA injection limit** ✓ — `(5 − 3.3)/1 kΩ`, and conservative: the real RP2040 clamp
  sits nearer 4.0 V, giving ~1.0 mA.
- **Module arithmetic** ✓ — 6 × 2.7 V = 16.2 V; 120 F / 6 = 20 F; 20 F / 3 = 6.667 F;
  18 cells × 10–20 mΩ = 180–360 mΩ.

**Circuit topology**

- **D2's return node is correct.** SVG path `M794 542 H 330 V 272` lands on the junction dot at
  x = 330 — between F3's exit at x = 290 and R1's entry at x = 330 — matching §5's "D2 returns
  between F3 and R1." And because U1 only conducts at `V_bank` ≥ 19.2 V, D1 is reverse-biased
  whenever U1 is active, so no charge current leaks into the bank. The non-circulation table in
  §5 is right, with 3.4 V of minimum separation.
- **R5 pulls up on the sensor side of R6; C6 shunts on the MCU side.** Correct order for an RC
  filter behind a fault limiter. Verified the low-level margin with the internal pull-up also
  enabled per §10 item 2: `V_IL` at GP13 works out to **84 mV** against a 0.8 V threshold.
- **L1 → C4 → C5 → VSYS ordering is correct** — bead first, then bulk, then local decoupling.
- **RP2040 pin mapping is right throughout.** GP0/GP1 = UART0 TX/RX ✓; GP4/GP5 = I2C0 SDA/SCL
  ✓; GP26 = ADC0 ✓; ADC3 = VSYS/3 ✓.
- **SVG wire coordinates land on the correct pin labels** on Sheet 3: SDA y=434 → GP4 (y=440);
  SCL y=460 → GP5 (y=466); TX y=308 → GP0 (y=314); RX y=334 → GP1 (y=340); THR y=578 → GP26
  (y=582); SPD y=352 → GP13 (y=356).
- **The throttle reference is ratiometrically correct** — J3 supplied from +3V3, ADC referenced
  to AVDD, same rail. Supply drift cancels. This is a good detail and easy to get wrong.
- **R2 + C7 open-circuit detection works** — `τ = 10 ms`, reads 0 V well within a control cycle,
  and 100 kΩ loads a low-impedance hall throttle by ~1 %.
- **J4 upstream of F3** ✓; **J2 pin 6 unconnected with A1 temperature sensing disabled** ✓.

**Part voltage qualification** — the specific question asked. All correct:

- F1: 160 V DC vs 48.6 V bank ✓, with adequate interrupting rating
- F2: ≥58 V DC vs 48.6 V ✓ — a deliberate and correct choice, not a default
- F3: ≥32 V DC vs ≤16.8 V ✓
- D1, D2: 100 V `V_RRM` vs ~37 V maximum reverse ✓
- S1: ≥60 V DC break vs 48.6 V ✓ (the **current** rating is the problem — finding 2)
- C1–C3: 16.2 V modules, 14.29 V worst-case at the 40 V working point ✓
- Sheet 2 NOTE 3 ("F1 SHALL BE CLASS T. ANL AND MIDI (32 V DC) SHALL NOT BE USED") is exactly
  the right note to have written. 32 V DC parts on a 48.6 V bank is the classic mistake and
  it has been correctly headed off.

---

# Schematic drawing critique

Appearance, readability, ease of understanding. Separate from the technical findings.

## What works

The presentation is better than most hobby schematics and better than some professional ones.
Specifically worth keeping:

- **Zone grid on both edges of every sheet**, with parts lists carrying zone references. This
  is the single highest-value convention in the package.
- **Sheet 2's main power path runs left to right on one horizontal at y = 272.** You can read
  BT1 → F3 → R1 → S2 → D1 → BANK+ → F1 → S1 → A1 in one sweep without backtracking. This is
  the best-laid-out part of the whole set.
- **The R1/S2 thermal coupling indicator** — the dashed accent path with the "thermal" label —
  communicates the bonding requirement better than a note would, and it is the kind of thing
  most drawings omit entirely.
- **Sheet 1's interconnect annotations** ("F3 R1 S2 D1 / SHT 2") tell you both what is in the
  path and where to find it. Good practice.
- **The typography and colour system** — mono/sans split, accent reserved for reference
  designators, and light/dark handled through both `prefers-color-scheme` and `data-theme`
  overrides so the viewer's toggle wins in both directions.
- **Per-sheet notes blocks** rather than one global block. Correct.

## Sheet 1 — block diagram

- **Crossings are indistinguishable from connections.** The BEC path (`M900 216 V 404 H 610 V
  470`) and the "to BT1" path (`M830 510 H 700 V 240 H 290 V 216`) cross each other and cross
  the SW+ vertical at x = 980. There are no hop markers and no junction dots at the crossings.
  On a block diagram carrying power nets this is the one ambiguity you cannot afford. Add hops
  or route around.
- **The M1 shell-speed line runs 645 px along y = 720**, the full width of the sheet, passing
  just above the notes block at y = 740. At a glance it reads as a border artifact rather than
  a signal. Route it above the U2 block.
- **The halls path doubles back on itself** (`M1180 190 H 1140 V 250 H 1000 V 216`) and
  terminates on the A1 box edge at almost the same point SW+ leaves. Two nets, one apparent
  junction.
- **No net-name flags on sheet 1** for BANK+ or GND, though the README's own conventions state
  "Signals crossing a sheet boundary carry a net name."
- The "to BT1" label sits at x = 712, far from either endpoint of a wire that spans 540 px.
  Label at both ends.

## Sheet 2 — power distribution

- **Four of six flags have no leader line to their subject.** Flag 3 floats below F1, flag 4 in
  empty space near the A1 ground drop, flag 6 to the left of C2 with nothing connecting it,
  flag 1 above the ground rail. Only flag 2 (above R1) reads unambiguously. Add short leaders.
- **The switch symbols collide with the junction-dot convention.** S1 and S2 are drawn as two
  4 px circles with a diagonal between them. The drawing also uses 4 px filled circles as
  actual junction dots (`.dot`). Same visual token, two meanings, at the same size. Make the
  switch terminals larger and unfilled.
- **Fuse and resistor symbols are nearly identical.** F3, R1, F1 and F2 are all plain
  rectangles; the fuses are distinguished only by a single thin line drawn through
  (`M240 272 H 290`). At print scale that line is easy to miss. Use the IEEE 315 fuse symbol.
- **F2's label placement breaks the pattern** — it sits at x = 1031, outside and right of the
  symbol, while every other label on the sheet is centred above. F2's element line is also
  drawn at y = 441 across a box spanning 430–452, so it is not centred within its own symbol.
- **C1–C3 are boxes, not capacitor symbols.** Defensible for modules — but then the values that
  drive half this review (6.67 F, 40 V working, ESR 0.18–0.36 Ω) appear **nowhere on the
  drawing**, only in the parts list. Add a dashed enclosure around the three with those figures
  on it.
- Notes block (x 70–1010) and title block (x 1026) are separated by 16 px. Tight enough to read
  as a single band at reduced scale.

## Sheet 3 — MCU and interfaces

- **Pin labels have no stubs and no pin numbers.** GP13 and GP26 are labelled at x = 652 while
  their wires terminate at x = 640 on the box outline — nothing visually ties label to wire. And
  on a Pico the *header* pin number matters as much as the GPIO name: GP13 = pin 17, GP26 =
  pin 31, VSYS = pin 39, 3V3 OUT = pin 36, GND = pin 3/8/13/… Add both.
- **The +3V3 net is drawn three different ways on one sheet** — a flag at J3, a flag at R5, and
  a 372 px routed wire (`M890 644 H 1262 V 492`) to DS1. Pick one convention. If flags, DS1
  gets a flag and that long wire disappears entirely.
- **Label placement is inconsistent between adjacent identical parts.** R3's label sits above
  its symbol (y = 288), R4's below (y = 362). R2's is to the right (x = 312), R5's above
  (x = 196), R6's above (x = 349). Nothing distinguishes the cases.
- **D3 is drawn as a rectifier, not a TVS.** Plain triangle and bar. IEEE 315 gives Zener/TVS a
  bent cathode bar. A builder reading the symbol alone fits a 1N4148. Given finding 12 — where
  the part is also specified by the wrong parameter — the symbol should carry its weight.
- **Signal ground and power ground use the same symbol.** There are four separate ground stacks
  on this sheet (C6, C7, U2, J3 rail). Drawing them separately is correct practice, but NOTE 8
  mandates a specific return topology and the symbols don't encode it. Use a distinct
  signal-ground symbol so the topology is visible without reading the note.
- **The W1 dashed boundary encloses empty space.** The box at (222–318, 52–236) sits between J1
  and J2; the five pass-through conductors cross it and the SPD branch exits below-left. As
  drawn, W1 appears to contain nothing. Either enclose the conductors and the branch point, or
  drop the box and rely on the parts list.
- **A1 appears three times across the set** (Sheet 2 power, Sheet 3 BEC, Sheet 3 UART) with no
  designator suffix. Use A1-1 / A1-2 / A1-3, or a single block with named ports.

## Both documents — presentation

- **`svg { min-width: 1260px }` inside `overflow-x: auto`** means on any display under ~1300 px
  the sheets scroll horizontally and the zone grid — whose entire purpose is to let you say "S1
  is in B3" — is cut off at one edge. A schematic you have to pan is a schematic you cannot
  read as a whole. Add a zoom control, or split Sheet 2 into power and protection.
- **Parts lists read as complete but are not.** No quantity column, and only four manufacturer
  part numbers across the set (JJN-50, 6A10, 1N4007, LM2596HV). §12 confirms S1, S2, the F1
  block and J4 are unselected — the parts lists should carry a "TBD" marker so the gap is
  visible at the point of use.
- **Revision history: Rev A and Rev B carry the same date** (2026-08-02), which makes the
  history uninformative. And Rev B's "Zone references corrected" is contradicted by finding
  14(d) — three zone errors survive.
- **No hole for `V_C`, tolerance, or endurance anywhere in the parts lists.** Findings 8 and 12
  both come down to a parameter the schedule has no column for. Add tolerance and the
  application-critical parameter to the specification column as a matter of course.

## planetary-freegen-sim.html

Out of the current scope; two observations recorded for later.

- **The road-speed slider maxes at 3 km/h**, while every speed that matters in §4.9 is
  10–30 km/h. The operating range is entirely off the end of the control, so the tool cannot
  show the condition it exists to explain. The 5× slow motion already handles legibility —
  raise the range.
- **The energy-split card overstates its own claim.** "The split depends only on slip, never on
  how hard you brake — `P_heat/P_total = s`" reads as a physical law, but it is circular: slip
  is *determined by* brake force against reaction torque. It is fine as a parametric display;
  the caption should say the split is a function of slip, not that it is independent of braking.
- Minor: in regen mode at `slip = 0` the clutch badge reads "Overrunning — no help" while the
  carrier is stationary. A stationary carrier is not overrunning.

---

# Recommended order of work

1. **Finding 1** — 8 AWG bank circuit. Cheapest fix, largest safety return, and it also
   improves finding 7.
2. **Finding 2** — respecify S1 at ≥300 A make. Part substitution only.
3. **Finding 12** — add R7 = 1 kΩ in THR and respecify D3 by `V_C`. Two components.
4. **Findings 4 and 6** — settle R1 at 4.7 Ω / 100 W, correct `E_R1` and `R1_max` in §3.
5. **Finding 3** — correct the absolute to 45.4 V and move bank overvoltage protection to A1's
   configured input maximum.
6. **Findings 5, 8, 9, 10, 11, 13** — arithmetic corrections and the F3 / `I_CC` respecifications.
7. **Finding 14** — documentation reconciliation, then re-issue as Rev C with a distinct date.
8. **Finding 15** — decide which of the six gaps to close and which to move to §12 as
   deliberately accepted. Several are legitimately acceptable for a prototype; none is
   currently written down as accepted.
