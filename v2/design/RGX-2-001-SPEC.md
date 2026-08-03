# ReGenX v2 — Design Specification

**Document** RGX-2-001 Rev C · **Drawing** RGX-2-100 Rev D · 2026-08-02

---

## 1. System definition

Bicycle regenerative brake-assist. Rear geared hub motor with a friction brake on
the planet carrier. Carrier freewheels when unbraked. Braking holds the carrier,
restoring the torque path wheel → ring → planets → sun → rotor. Motor generates
into a supercapacitor bank. Bank returns energy as brief acceleration assist.

A **one-way clutch between the carrier and the axle** grounds the carrier against
the assist reaction torque (`T_carrier = −(1+k)·T_sun`) so motor torque reaches
the wheel, and overruns in the opposite direction so the carrier freewheels when
coasting. The friction brake acts on the same carrier for regen. Without this
clutch, assist torque spins the carrier and no torque reaches the wheel.

No traction battery. No propulsion energy store other than the bank.

### Planetary kinematics

```
k          = Z_ring / Z_sun
ω_sun      = (1 + k)·ω_carrier − k·ω_ring
ω_carrier  = (ω_sun + k·ω_ring) / (1 + k)      = (ω_motor + k·ω_wheel) / (1 + k)
T_sun : T_ring : T_carrier = 1 : k : −(1 + k)
```

Line 3 is line 2 rearranged. Check: carrier locked, `ω_carrier` = 0 ⟹
`ω_sun` = −k·ω_ring, and line 3 returns 0.

Ring and wheel shell are the same member. `T_sun = 0` ⟹ all torques zero.
Carrier slip fraction equals loss fraction: `P_heat / P_total = s`.

---

## 2. Design invariants

| | Requirement |
|---|---|
| Boost count | ≥ 2 boosts of 10 → 20 km/h from a full bank, ESR-loaded |
| Closed loop | Electronics draw does not consume the harvest; pack refills from braking |
| Buffer | Supercapacitors carry high-power transients; no propulsion battery |
| Off state | Mechanical switch only. No sleep mode |

Fixed by owner: supercapacitor store, rear wheel, carrier brake, Bafang G020,
Flipsky Mini FSESC4.20, 3 × GDCPH 16 V 20 F modules, MicroPython, no bleed
resistor, no MCU supply independent of A1.

Regen control law is out of scope of this document.

---

## 3. Governing equations

### Bank

```
C_bank    = C_module / N_series
R_esr     = N_cells · r_cell
R_ext     = R_wire + R_F1 + R_S1 + R_joints
R_total   = R_esr + R_ext
V_lo      = V_startup + V_margin + I_boost · R_total
E_usable  = ½ · C_bank · (V_hi² − V_lo²)
η_esr     = 1 − I_boost · R_total / V̄          V̄ = (V_hi + V_lo)/2
N_boosts  = E_usable · η_esr · η_conv / ΔKE
ΔKE       = ½ · m · (v₂² − v₁²) + (F_roll + F_drag)·d
```

`I_boost` enters twice: raising `V_lo` and reducing `η_esr`.

`V_lo` is the bank open-circuit voltage at which A1's *terminal* voltage reaches
its start-up threshold. `R_ext` and `V_margin` are both required: without
`R_ext` the conductors, fuse, switch and joints between bank and controller are
unaccounted for; without `V_margin` the design operates at the brownout boundary
by construction.

`V_margin` = 1.0 V.

### Bank series division

Series capacitors divide voltage as 1/C. For `N` modules with one low by
fraction `s`:

```
share_low = (1/(1−s)) / (N − 1 + 1/(1−s))
V_abs     = V_module_max / share_low
```

`V_hi` is bounded by module capacitance spread, not cell rating. The module is
the matched unit (cells within a module are balanced by its integral circuit),
so the module-level division model below governs; a cell-level spread model was
removed as inapplicable.

### Precharge

```
V_final   = V_pack − V_F(D1)
I_0       = V_final / R1
P_R1(pk)  = I_0² · R1
t(V)      = R1 · C_bank · ln( V_final / (V_final − V) )
E_R1(V)   = ½ · C_bank · V · (2·V_final − V)
```

Derivation of `E_R1`, from `i(t) = (V_final/R)·e^(−t/RC)`:

```
E_R = ∫i²R dt = ½·C·V_final²·(1 − e^(−2t/RC))
V(t) = V_final·(1 − e^(−t/RC))  ⟹  e^(−t/RC) = 1 − V/V_final
E_R = ½·C·V·(2·V_final − V)
```

At `V = V_final` this reduces to `½C·V_final²`. The identity
`E_R1 = ½C·V_target²` holds **only** for a full charge to the source voltage and
must not be used for partial charge.

Constraint: `V_final > V_target`, else unreachable at any `R1` or duration.

### Keep-alive equilibrium

Steady state with A1 drawing `P_idle` from the bank through R1:

```
V_bank² − (V_pack − V_F)·V_bank + P_idle·R1 = 0
```

Real roots require `V_pack − V_F ≥ 2·√(P_idle·R1)`, but at that limit the roots
coincide at `V_bank = (V_pack − V_F)/2`, which is **below A1's start-up voltage**.
Existence of an equilibrium is not the binding constraint — the equilibrium must
also be one in which A1 runs. Requiring the upper root ≥ `V_startup`:

```
R1_max = V_startup · (V_pack − V_F − V_startup) / P_idle
```

Collapse pack voltage for a given `R1`:

```
V_pack(collapse) = V_startup + V_F + P_idle·R1 / V_startup
```

### Hold-up

```
t_hold = ½ · C4 · (V_bec² − V_sys,min²) / P_load
```

---

## 4. Computed performance

### 4.1 Bank

| | |
|---|---|
| `C_bank` | 6.667 F (20 F ÷ 3 series) |
| `R_esr` | 180–360 mΩ (18 cells × 10–20 mΩ) |
| `R_ext` | 6.9 mΩ (8 AWG 1.5 m 3.1 + F1 0.8 + S1 1.0 + 4 joints 2.0) |
| `V_hi` working | 40 V |
| `V_hi` absolute | **45.4 V** at the specified ≤10 % module spread (share 0.35714) |
| Module voltage at 40 V working | 14.29 V worst case (2.381 V/cell) |
| Stored at 40 V | 5 333 J |
| Stored at 14 V rest | 653 J |

`V_hi` absolute is **not** 3 × 16.2 = 48.6 V. That figure assumes equal division,
which the ≤10 % spread allowance contradicts.

### 4.2 Boost

`m` = 100 kg, 10 → 20 km/h, `ΔKE` = 1 157 J, `η_conv` = 0.70, `V_startup` = 8 V,
`V_margin` = 1.0 V, `R_ext` = 6.9 mΩ.

| `I_boost` | `R_esr` | `V_lo` | `E_usable` | `η_esr` | `N_boosts` |
|---|---|---|---|---|---|
| 20 A | 180 mΩ | 12.74 V | 4 792 J | 0.858 | **2.49** |
| 20 A | 270 mΩ | 14.54 V | 4 629 J | 0.797 | **2.23** |
| 20 A | 360 mΩ | 16.34 V | 4 444 J | 0.740 | **1.99** |
| 40 A | 180 mΩ | 16.48 V | 4 428 J | 0.735 | **1.97** |
| 40 A | 270 mΩ | 20.08 V | 3 990 J | 0.631 | **1.52** |
| 40 A | 360 mΩ | 23.68 V | 3 465 J | 0.539 | **1.13** |

**Invariant §2 is satisfied for `I_boost` ≤ 26.2 A at nominal ESR.** At 40 A and
nominal ESR the system delivers 1.52 boosts. **At worst-case ESR the invariant is
not met at any current** — 1.99 at 20 A is the maximum. Bank ESR is therefore a
pass/fail measurement, not a tolerance (§11 item 3).

`ΔKE` above omits rolling and aerodynamic resistance over the ~2.2 s boost:
`(F_roll + F_drag)·d` ≈ 9.1 N × 9.2 m ≈ **85 J**, so `N_boosts` is a further ~7 %
optimistic. Not folded into the table pending a measured `C_rr` and `C_d·A`.

Bank ESR dissipation at 40 A:

| `R_esr` | `I²R` | fraction of 1 600 W |
|---|---|---|
| 180 mΩ | 288 W | 18 % |
| 270 mΩ | **432 W** | **27 %** |
| 360 mΩ | 576 W | 36 % |

Averaged over a boost at nominal ESR: `1 − η_esr` = **37 %**.

### 4.3 Precharge

`R1` = 4.7 Ω, `V_F(D1)` = 1.0 V, bank from 0 V.

| `V_pack` | `V_final` | `I_0` | `P_R1` peak | `t` → 8 V | 14 V reachable | `E_R1` full charge | ΔT |
|---|---|---|---|---|---|---|---|
| 13.5 V | 12.5 V | 2.66 A | 33 W | 32 s | **no** | 521 J | 19 K |
| 14.8 V | 13.8 V | 2.94 A | 41 W | 27 s | **no** | 635 J | 24 K |
| 16.8 V | 15.8 V | 3.36 A | 53 W | 22 s | yes | **832 J** | 31 K |

`E_R1` is energy for a full charge to `V_final`, which is what physically occurs.
For the one case that reaches 14 V, `E_R1`(14 V) = 821 J.

ΔT at 30 g aluminium. R1 sizing shall use **832 J**.

**`P_R1` peak of 53 W exceeds a 50 W part.** A cold start from a flat bank runs
R1 at 106 % of a 50 W rating for ~22 s — specified duty, not a fault. R1 is
therefore specified at **100 W** (§7). A 50 W alu-clad rating additionally assumes
an adequate heatsink at 25 °C ambient; in free air the same part carries 15–20 W.

A cold start occurs only after BT1 reconnection or extended storage. A normal
start begins at the ~12.7 V rest point (§4.5) with no precharge phase at all
(§6) and no R1 duty.

### 4.4 R1 bounds

**R1 = 4.7 Ω.** Single governing value; §7, the drawing parts list and §4.3 all
agree.

`R1_max` from the upper-root criterion at `V_pack` = 12.0 V (the BMS UV cut):

```
R1_max = 8 · (12.0 − 1.0 − 8) / 3 = 8.00 Ω
```

Verify: `V² − 11V + 24 = 0` → roots 8.0 V and 3.0 V. The upper root sits exactly
on A1's floor, so **8 Ω is the limit with zero margin**, not a value with margin.

| `R1` | Collapse at `V_pack` | Margin to 12.0 V BMS cut |
|---|---|---|
| **4.7 Ω** (specified) | 10.76 V | **1.24 V** |
| 8.0 Ω | 12.00 V | 0 V |

4.7 Ω is selected over 8 Ω: peak dissipation is a resistor-wattage problem, zero
collapse margin is not.

The real-roots criterion `(V_pack − V_F)²/(4·P_idle)` gives 10.1 Ω and must not be
used — at that limit the equilibrium is `V_bank` = 5.50 V, below A1's 8 V floor.
`√(4·P_idle·R1)` = 7.51 V is `(V_pack − V_F)` at that limit, not `V_bank`.

### 4.5 Standby and idle

| Condition | Value |
|---|---|
| Bank leakage at 14 V | 0.37 mA, 5.2 mW at the bank (`R_leak` ≈ 37 kΩ) |
| — drawn from BT1 at 14.8 V | **5.5 mW** (same 0.37 mA at pack voltage) |
| Bank decay, BT1 disconnected | τ = `R_leak`·`C` = 68.5 h = **2.85 days** |
| BMS quiescent | 30–50 µA at 14.8 V ≈ 0.6 mW |
| BT1 self-discharge | ~3 %/month ≈ 0.6 mW |
| Total standby drain | 5.5 + 0.6 + 0.6 = **6.7 mW** |
| BT1 standby life, S1 open | **93 days** from 15 Wh |
| A1 idle | 3 W |

S1 closed, stationary — solved across the pack range:

| `V_pack` | `V_bank` | `I_R1` | BT1 output | Life |
|---|---|---|---|---|
| 16.8 V (full) | 14.85 V | 0.202 A | 3.39 W | 4.42 h |
| **14.8 V (nominal)** | **12.69 V** | **0.236 A** | **3.50 W** | **4.29 h** |
| 12.0 V (BMS cut) | 9.52 V | 0.315 A | 3.78 W | 3.97 h |

Integrated across the discharge: average 3.56 W against **14.25 Wh usable**
(95 % to the BMS cut), giving **4.0 h**.

Bank rest voltage is **12.69 V** at nominal pack, above A1's 8 V floor and below
U1's 19.2 V conduction threshold.

### 4.6 U1 trickle charger

| | |
|---|---|
| Conduction threshold | `V_out` + 2 V = **19.2 V** bank |
| `V_out` setpoint | 17.2 V |
| Delivered to BT1 | 16.4 V = 4.10 V/cell (after `V_F(D2)` = 0.8 V) |
| `I_CC` | 0.2 A, 3.28 W delivered |
| Input power at η = 0.85 | **3.86 W** |
| Daily standby replacement | **161 mWh** (6.7 mW × 24 h) in **2.9 min** above threshold |

**Scope of the standby balance.** The figures above close for the S1-open case
only. With S1 closed the drain is 3.50 W — 673 × the 5.2 mW standby leakage — and
U1 at 0.2 A does not offset it:

| Bank voltage | D1 | U1 | BT1 |
|---|---|---|---|
| ≥ 19.2 V | off | on | gains 3.28 W |
| 13.8 – 19.2 V | off | off | neutral |
| < 13.8 V | on | off | loses 3.50 W |

```
break-even:  f_above ≥ 1.067 · f_below
```

Bank rest is 12.69 V, so every ride starts in the draining region. Nothing in the
electrical design constrains `f_above`.

**U1 is a top-up, not a charger.** J4 is the charge path (§9). §2's closed-loop
invariant is demonstrated for standby and is **not** demonstrated for the
operating case.

### 4.7 Fault currents

| Case | Current |
|---|---|
| Bank short at 40 V, downstream of F1 | 111–222 A |
| Bank short at 45.4 V absolute | 126–252 A |
| S1 make into A1 input capacitance | **126–252 A** (at the 45.4 V absolute ceiling) |
| Sustained bank short through R1 | 3.36 A, **P_R1 = 53 W continuous** |
| Normal cold-start precharge | 3.36 A |
| Shorted D1, bank at **40 V** | **4.94 A** |
| Shorted D1, bank at 45.4 V | 6.09 A |

Fault and normal precharge currents are identical. Overcurrent protection cannot
discriminate. S2 provides thermal protection.

F1 clears its fault with margin — worst case (max ESR, least energy):

```
I₀    = 40 / 0.36           = 111 A
τ     = R_esr·C = 0.36 × 6.667 = 2.40 s
∫I²dt = I₀²·τ/2             = 14 785 A²s     vs JJN-50 melting I²t ~400 A²s
```

**F1's zone of protection begins at F1.** The bank-to-F1 conductor and all ring
terminals at the bank end are protected by construction only.

**Shorted D1 drives 4.94 A backwards into BT1** — 4.9 C into a ~1.01 Ah pack from
a source with no CC/CV control and no termination. F3 at 5 A time-delay sits at
99 % of rating and does not open. Clearing element is BT1's BMS alone.

### 4.8 Supply rails

| Rail | Source | Capacity | Load | Utilisation |
|---|---|---|---|---|
| +5 V | A1 BEC | 1 500 mA | 70 mA = **0.35 W** | 4.7 % |
| +3V3 | U2 onboard regulator | ≥300 mA | 30 mA | 10 % |

VSYS hold-up, computed at the stated 0.35 W load:

| C4 | Energy | Hold-up |
|---|---|---|
| 100 µF nominal | 1.088 mJ | **3.1 ms** |
| 100 µF at −20 % | 0.870 mJ | **2.5 ms** |
| 220 µF nominal | 2.394 mJ | 6.8 ms |
| 220 µF at −20 % | 1.915 mJ | 5.5 ms |

**C4 = 220 µF specified** (§7). At 100 µF the rated-tolerance figure is 2.5 ms and
end-of-life is under 2 ms. Decoupling in either case, not A1-fault ride-through.

### 4.9 Sensor resolution

Shell sensor 6 pulse/rev, 2.1 m wheel circumference.

| Road speed | Wheel | SPD frequency | Period |
|---|---|---|---|
| 10 km/h | 1.32 rev/s | 7.9 Hz | 126 ms |
| 20 km/h | 2.65 rev/s | 15.9 Hz | 63 ms |
| 30 km/h | 3.97 rev/s | 23.8 Hz | 42 ms |

---

## 5. Power topology

```
BT1 ──F3──R1──S2──D1──┬── C1/C2/C3 ──┬── F1 ── S1 ──┬── A1 ── M1
 │                    │              │              │
 J4              rests ~14 V         │              └── F2 ── U1 ── D2 ─┐
                                     │                                  │
                                     └── A1 5 V BEC ── L1/C4/C5 ── U2   │
                                                                        │
 BT1 ←──────────────────────────────────────────────────────────────────┘
      (D2 returns between F3 and R1)
```

BT1 is permanently connected in operation and never switched. S1 is the sole
operating disconnect, in the bank positive; open, A1 and U2 are physically
isolated. **The F3 inline holder is the BT1 service break** — removing the fuse
isolates the pack for the drawing's Note 7 procedure, resolving what would
otherwise be a contradiction between "never switched" and "disconnect BT1
before work".

### Keep-alive loop non-circulation

| `V_pack` | D1 conducts | D2 conducts | Overlap |
|---|---|---|---|
| 13.5 V | `V_bank` < 12.5 V | `V_bank` ≥ 19.2 V | none |
| 14.8 V | `V_bank` < 13.8 V | `V_bank` ≥ 19.2 V | none |
| 16.8 V | `V_bank` < 15.8 V | `V_bank` ≥ 19.2 V | none |

Minimum separation 3.4 V.

---

## 6. Operating states

| State | Condition | Behaviour |
|---|---|---|
| OFF | S1 open | A1 and U2 isolated. BT1 holds bank at ~14 V through R1/D1 |
| RUN | S1 closed | Full range. Bank 8 V–40 V. No precharge sequence — bank is already above A1's floor |
| BANK FULL | `V_bank` → 40 V | Regen tapers to zero. No dump, no phase short |
| MCU RESET | VSYS < 1.8 V | Assist and regen commands go to zero |

No PRECHARGE state. No FAULT contactor state. No sleep state.

---

## 7. Component schedule

### Power — Sheet 2

| Ref | Description | Specification |
|---|---|---|
| BT1 | Battery, secondary | 4S Li-ion, 14.8 V nom, 16.8 V max, ~15 Wh. BMS: balance, OV 4.25 V/cell, **UV 3.00 V/cell**, OC |
| J4 | Connector, charge | 4S balance-charge input to BT1 BMS, upstream of F3 |
| F3 | Fuse | 5 A time-delay, ≥32 V DC, **in accessible inline holder — removing F3 is the BT1 service disconnect**. 18 AWG branch. Duty 3.36 A / 90 s |
| R1 | Resistor, power | 4.7 Ω ±5 %, **100 W** wirewound alu-clad. Heatsink ≥150 cm² or equivalent chassis area, 25 °C ambient. Duty 53 W / 22 s per cold start, 832 J |
| S2 | Switch, thermal cutout | NC bimetal, open 100–110 °C, **manual reset**, ≥5 A, ≥50 V DC. Bonded to R1 body |
| D1 | Diode, rectifier | Si, I_F(AV) ≥ 6 A, V_RRM ≥ 100 V, I_R ≤ 10 µA @ 25 °C, V_F ≈ 1.0 V @ 3.4 A. 6A10 |
| C1–C3 | Capacitor, EDLC module | 16.2 V, 20 F (6 × 2.7 V 120 F, integral balancing). Series 6.667 F, ESR 180–360 mΩ |
| F1 | Fuse | 50 A Class T, 160 V DC (JJN-50) + block |
| S1 | Switch, disconnect | SPST, ≥100 A cont, ≥60 V DC break, **make ≥300 A** (min ESR at 45.4 V absolute) |
| F2 | Fuse | 2 A slow-blow, ≥58 V DC. 22 AWG branch |
| U1 | Converter, DC-DC | LM2596HV CC/CV. V_in 5–57 V, dropout 2 V. Set V_out 17.2 V, I_CC 0.2 A |
| D2 | Diode, rectifier | Si, I_F(AV) ≥ 1 A, V_RRM ≥ 100 V, V_F ≈ 0.8 V @ 0.2 A. 1N4007 |
| A1 | Assembly, motor controller | Flipsky Mini FSESC4.20. 8–60 V, 50 A cont / 150 A pk, sensored FOC, 5 V 1.5 A BEC, 3.3 V UART, idle 3 W. **Battery current limit 40 A; maximum input voltage 40 V.** Factory input pigtails ~12 AWG with XT60 — the 8 AWG harness terminates at the mating XT60. **Pigtails shall not be extended**; they are the narrowest element in the bank circuit and are acceptable only short and unbundled |
| — | Wire, bank circuit | **8 AWG**, ~55 A bundled, ring terminals. Keyed or size-differentiated at the C1–C3 module ends |
| — | Wire, branch | 18 AWG pack; 22 AWG charger; 22 AWG BEC pair (+5 V and return) |

### Signal — Sheet 3

| Ref | Description | Specification |
|---|---|---|
| M1 | Motor, geared hub | Bafang G020, rear. Single-stage planetary ~5:1. Halls ×3 + shell speed 6 PPR. **No thermal protection fitted** |
| U2 | Assembly, MCU | Raspberry Pi Pico, RP2040. VSYS 1.8–5.5 V, +3V3 ≥300 mA. GPIO 3.3 V, **not 5 V tolerant**, integral Schmitt. ADC 12-bit ref AVDD; ADC3 = VSYS/3 |
| J1 | Connector, motor sensor | 6-way, M1 supplied. +5 V, GND, H1–H3, SPD |
| J2 | Connector, controller hall | JST-PH 6-way. Pin 6 unconnected |
| W1 | Cable assembly | J1 → J2, 5 conductors through; SPD branched to R6 |
| R5 | Resistor | 10 kΩ ±5 %, 1/4 W. SPD pull-up to +3V3 |
| R6 | Resistor | 1 kΩ ±5 %, 1/4 W. Series. Fault injection limit 1.7 mA at 5 V |
| C6 | Capacitor | 1 nF, ≥50 V, C0G. R6·C6 = 1 µs; signal band ≤30 Hz |
| J3 | Connector, throttle | 3-way. Hall, ratiometric, supplied from +3V3 |
| **R7** | Resistor | **1 kΩ ±5 %, 1/4 W. THR series, between J3 SIG and the C7 node.** Fault injection limit 1.7 mA at 5 V. R7·C7 = 100 µs vs ~10 Hz signal band |
| R2 | Resistor | 100 kΩ ±5 %, 1/4 W. Pulldown, open-circuit detection. R2·C7 = 10 ms |
| C7 | Capacitor | 100 nF, ≥16 V, X7R. ADC charge reservoir, at the pin |
| D3 | Diode, TVS | Unidirectional, V_RWM 3.3 V, **V_C ≤ 4.5 V at 1 A**. RP2040 GPIO absolute maximum is IOVDD + 0.5 = 3.8 V |
| R3, R4 | Resistor | 1 kΩ ±5 %, 1/4 W. UART series. Fault injection limit 1.7 mA at 5 V; rise 30 ns at 10 pF |
| L1 | Ferrite bead | ~600 Ω @ 100 MHz, I ≥ 1 A, DCR ≤ 0.1 Ω |
| C4 | Capacitor | **220 µF ±20 %**, ≥10 V, aluminium electrolytic, **105 °C, ≥2 000 h endurance**. Hold-up 6.8 ms nominal, 5.5 ms at −20 % |
| C5 | Capacitor | 100 nF, ≥16 V, X7R. Adjacent to U2 VSYS |
| DS1 | Display | SSD1306 128×64 OLED, I²C 400 kHz, 3.3 V, addr 0x3C, ~20 mA |
| — | Wire, signal | 24 AWG. SPD twisted with GND; UART twisted pair |

---

## 8. Interface schedule

| Net | From | To | Type |
|---|---|---|---|
| BANK+ | C1 positive | F1 → S1 → A1 V+ | power, 8 AWG |
| SW+ | S1 | A1 V+, F2 | power, 8 AWG |
| GND | A1 V− star | BT1 −, C3 −, U1 − | power. 8 AWG C3 − leg; 18 AWG BT1 − leg; 22 AWG U1 − leg |
| +5V | A1 BEC | U2 VSYS via L1, C4, C5 | power, 22 AWG pair |
| +5V_RTN | U2 GND | A1 V− star | return, 22 AWG. **Sole U2 ground path** |
| +3V3 | U2 3V3 out | J3, DS1, R5 | power, 24 AWG |
| SPD | J1 pin 6 | U2 GP13 via R6 | digital, 5 V at source, 6 PPR, 8–24 Hz |
| THR | J3 SIG | U2 GP26 **via R7** | analog, 0–3.3 V |
| THR_RTN | J3 GND | U2 local signal ground | return, 24 AWG |
| DS1_RTN | DS1 GND | U2 local signal ground | return, 24 AWG |
| UART_TX | U2 GP0 via R3 | A1 UART RX | digital 3.3 V, 115200 |
| UART_RX | A1 UART TX | U2 GP1 via R4 | digital 3.3 V, 115200 |
| SDA / SCL | U2 GP4 / GP5 | DS1 | I²C 3.3 V, 400 kHz |
| H1–H3 | J1 pins 3–5 | J2 pins 3–5 | digital 5 V, A1 domain |
| CHG | J4 | BT1 BMS charge input | power, 18 AWG |

Motor speed reaches U2 as ERPM over UART. Bank voltage reaches U2 as `v_in` over
UART. No divider on either.

**U2's ground reaches the star via the BEC return only.** No separate heavy
conductor to U2 — that would form a loop around the motor-current path. Signal
returns (THR_RTN, DS1_RTN) land locally at U2, then to the star through
+5V_RTN.

Nine U2 pins used, with header positions:

| Signal | GPIO | Header pin |
|---|---|---|
| UART_TX | GP0 | 1 |
| UART_RX | GP1 | 2 |
| SDA | GP4 | 6 |
| SCL | GP5 | 7 |
| SPD | GP13 | 17 |
| THR | GP26 / ADC0 | 31 |
| VSYS | — | 39 |
| 3V3 OUT | — | 36 |
| GND | — | 3, 8, 13, 18, 23, 28, 33, 38 |

---

## 9. Protection coordination

| Threat | Element | Coordination |
|---|---|---|
| Short downstream of F1, 111–252 A | F1 50 A Class T | 160 V DC, ∫I²dt 14 785 A²s vs ~400 A²s melting. **Zone begins at F1** — the bank-to-F1 conductor is protected by construction only |
| Bank conductor overload, 40–50 A | F1 + 8 AWG | 8 AWG ~55 A bundled > F1 50 A. Protection is independent of A1's configuration |
| Sustained bank short through R1, 3.36 A / 53 W | **S2 thermal cutout** | Fuses cannot discriminate — fault current equals precharge current. Manual reset latches the fault |
| S1 make, 126–252 A | S1 make rating ≥300 A | Rated at minimum ESR and the 45.4 V absolute ceiling. 0.56 J contact energy per make |
| Pack branch short, 18 AWG | F3 5 A time-delay | Carries 3.36 A / 90 s precharge without opening |
| **Shorted D1, 4.94 A into BT1** | **BT1 BMS OV/OC only** | F3 does not discriminate at 40 V (99 % of rating). Single point of protection, no backup |
| Charger branch short, 22 AWG | F2 2 A slow-blow | — |
| BT1 cell OV / UV / OC | BT1 integral BMS | UV 3.00 V/cell = 12.0 V, 1.24 V above the 10.76 V collapse point at R1 = 4.7 Ω |
| Bank overvoltage | **A1 configured maximum input voltage, 40 V** | Acts at boost/regen currents. Module integral balancing is intra-module cell matching at milliampere class and does **not** divide voltage between modules. **Caveat:** an A1 overvoltage fault stops switching, but the bridge body diodes still rectify back-EMF into the bank whenever back-EMF exceeds bank voltage. The crossover speed depends on kV, which is unmeasured — this is why kV is measurement gate 2 (§11) |
| Module-to-module voltage division | **None** | Set by capacitance on charge and by leakage at rest. ≤10 % capacitance match required; leakage spread unmeasured (§11) |
| Bank overtemperature | **None fitted** | 432–576 W in ESR at 40 A; ~85 J and ~8.5 K per cell per boost, again on regen. ESR rises with temperature |
| Module reverse installation | **None fitted** | Reversing one module in a 3-series stack applies 20 V to each of the others and reverse-biases the third. Vent-or-rupture failure |
| SPD 5 V injection into U2 | R6 1 kΩ + RP2040 clamp | 1.7 mA at 5 V |
| UART 5 V injection into U2 | R3, R4 1 kΩ | 1.7 mA at 5 V |
| Throttle transient | **R7 1 kΩ** + D3 TVS + C7 | Line leaves the enclosure. R7 bounds injection at 1.7 mA; D3 specified by `V_C` ≤ 4.5 V against a 3.8 V absolute maximum |
| Throttle open circuit | R2 100 kΩ pulldown | Reads 0 V, τ = 10 ms |
| A1 brown-out | C4 / C5 / L1 | 6.8 ms hold-up at 220 µF. **Decoupling only.** MCU reset zeroes all commands |
| M1 overtemperature | **None fitted** | A1 motor-temperature sensing disabled in configuration |
| Annunciation of any protective operation | **None fitted** | F1/F2/F3 opening, S2 trip and BMS cut are neither sensed nor displayed |

---

## 10. Firmware requirements

Constraints only. Control law out of scope.

1. U2 GP13 read by PIO period capture. Not a Python-level interrupt.
2. U2 GP13 internal pull-up enabled in addition to R5.
3. Throttle rejected outside 0.20–0.85 × V_REF; assist zeroed on violation.
4. A1 battery current limit set to 40 A in controller configuration.
5. A1 motor-temperature sensing disabled in controller configuration.
5a. A1 maximum input voltage set to 40 V in controller configuration. This is the
    only element that limits bank voltage at regen currents.
6. VSYS logged via ADC3.
7. Bank voltage and motor ERPM read from A1 telemetry, not from local sensing.
8. Regen taper to zero as bank approaches 40 V.
9. UART RX buffer explicitly sized. No synchronous flash writes while moving.
10. MicroPython.

---

## 11. Unverified parameters

No entry below has been measured. Every dependent figure in §4 is provisional.

| Parameter | Assumed | Depends on it |
|---|---|---|
| G020 gear ratio | ~5:1 | Slip arithmetic, ERPM conversion |
| G020 pole pairs | unknown | ERPM conversion |
| G020 kV, phase resistance | unknown | Back-EMF crossover, current ceiling, copper loss |
| A1 start-up voltage | 8 V | `V_lo`, boost count, keep-alive viability |
| A1 BEC 5 V / 1.5 A exposed on a connector | yes | **U2 has no supply if wrong** |
| M1 shell-speed sensor output structure | open-collector | GP13 survival — verify by scope before connection |
| M1 shell-speed wire carries no thermistor | yes | GP13 conditioning |
| Module capacitance spread | ≤10 % | 45.4 V absolute ceiling |
| Module balance-current capability | unknown | Whether balancing is passive bleed or active shunt |
| Module leakage spread | unknown | Rest-state voltage division between modules |
| Bank ESR | 180–360 mΩ | **Boost count — pass/fail, not tolerance.** `V_lo`, fault current, S1 make rating |
| `R_ext` on the built harness | 6.9 mΩ estimated | `V_lo`, brownout margin |
| Bank leakage | 0.37 mA at 14 V | Standby life |
| `C_rr` and `C_d·A` | assumed ~85 J per boost | `N_boosts`, ~7 % |
| A1 telemetry ERPM lag | tolerable | Slip estimate quality |
| Carrier accessible to a caliper on a rear hub | yes | **Whole mechanical concept** |
| Bafang hall mapping compatible with VESC | yes | Commutation |
| J3 operates at 3.3 V supply | yes | Throttle function |
| SSD1306 daylight legibility | adequate | Usability |

### Measurement schedule

Ordered by what each result gates. Items 1 and 2 obsolete the design if they
fail; run them first.

1. **G020 teardown for carrier access.** A negative result obsoletes everything
   above — the entire electrical design is downstream of this mechanism.
2. **M1 kV, phase resistance, pole pairs.** kV sets the back-EMF crossover speed
   above which the bank charges through A1's body diodes regardless of
   configuration (§9 bank-overvoltage caveat).
3. A1 start-up voltage, rising and falling.
4. C1, C2, C3 capacitance individually; match ≤10 % before stacking. Also record
   each module's leakage current at 13 V after 24 h, and identify whether the
   balance circuit is passive bleed or active shunt.
5. **Bank ESR by step-load. Pass/fail against §4.2** — at 360 mΩ the boost
   invariant is unmet at every current.
6. `R_ext` end-to-end, bank terminal to A1 terminal, by four-wire at 20 A.
7. Bank self-discharge over 72 h.
8. M1 shell-speed sensor output structure by scope, unpowered and powered.
9. G020 gear ratio by hand rotation count.
10. A1 BEC voltage and current capability at connector.
11. A1 telemetry round-trip latency at 115200.

---

## 12. Known open

- Carrier access on a rear hub — unresolved, requires teardown
- Regen control law — deliberately unspecified
- Part numbers not selected: S1, S2, F1 block, J4
- CAN availability on this A1 board unconfirmed
- Data logging — out of scope

### Accepted without mitigation

Recorded so that absence of protection is a decision rather than an omission.

| Gap | Basis |
|---|---|
| Bank-to-F1 conductor unfused | Short run, protected by construction. Fusing it requires a second fuse at the bank terminals |
| A1 factory pigtails ~12 AWG inside a 50 A-fused circuit | Fixed by the product. Short, unbundled, not extended. XT60 rated 60 A continuous |
| No bank temperature sensing or limit | Prototype. ESR/temperature feedback is regenerative but the duty cycle is low |
| No annunciation of protective operation | Requires firmware and a display that may be dark. S2's manual reset makes the sustained-fault case self-evident |
| §2's closed-loop invariant not demonstrated for the operating case | U1 is a top-up; J4 is the charge path |
| No module reverse-installation interlock | Mitigated by keyed/size-differentiated terminals and an assembly verification step, not by circuitry |
| `ΔKE` excludes rolling and aerodynamic drag | ~7 % on `N_boosts`, pending measured coefficients |
