# ReGenX v2 — design review brief

**You are being asked to criticise this design, not validate it.** Assume the
author is wrong somewhere and find where. Prior review has already caught several
real defects, so more remain.

Accompanying file: `v2-schematic.html` — drawing RGX-2-100 Rev A, three sheets.
Open it in a browser.

---

## 1. What the system is

A bicycle regenerative brake-assist. A rear geared hub motor has a friction brake
fitted to its **planet carrier**. Normally the carrier freewheels, so the bike
coasts with zero drag. Braking holds the carrier, restoring a torque path
wheel → ring → planets → sun → rotor, and the motor generates into a
supercapacitor bank sized for a few short boosts.

This is not an e-bike. There is no traction battery. The bank captures braking
energy and returns it as brief acceleration assistance.

---

## 2. Owner's constraints — settled, do not re-litigate

These are the project owner's decisions, not conclusions from analysis. Arguing
against them wastes review effort.

| | |
|---|---|
| Energy store | **Supercapacitors**, not a battery |
| Wheel | **Rear** |
| Mechanism | **Carrier brake**, preserving the freewheel |
| Scale | "A few good boosts", not range |
| Motor | Bafang G020 |
| Controller | Flipsky Mini FSESC4.20 (already owned, no purchase) |
| Bank | 3 × GDCPH 16 V 20 F modules already owned, no additional purchase |
| Firmware | MicroPython |
| Sleep mode | **None.** A mechanical master switch is the only off state |
| Data logging | Deferred, out of scope |
| Regen control law | **Deferred.** It is the output of separate simulation work and is deliberately not specified |

---

## 3. Circuit as designed

### Power

```
BT1 ──F3──R1──D1──┬── C1/C2/C3 ──┬── F1 ── S1 ──┬── A1  (motor controller)
                  │  (bank)      │              └── F2 ── U1 ── D2 ── back to BT1
                  └── bank rests at BT1 voltage − one diode drop, ~14 V
```

- **BT1** 4S Li-ion, ~15 Wh, is a *keep-alive*, not a supply. Its only job is to
  hold the bank above the controller's start-up voltage.
- **S1** is the sole master switch, in the bank positive. Open = everything
  downstream physically disconnected.
- **U1** is a CC/CV buck trickle-charging BT1 from the bank. It self-gates: it
  needs V_in ≥ V_out + 2 V so it cannot run below 18.4 V bank.
- No fitted bleed resistor. No contactor. No boost converter.

### Signals

- **A1's 5 V BEC powers U2** (the MCU). No separate supply.
- Motor's three hall sensors go to **A1** for commutation. Motor speed reaches U2
  as ERPM over the UART link.
- The motor's **shell speed sensor** (6 pulse/rev, open-collector) is the only
  motor signal wired to U2, on GP13 with the internal pull-up.
- Bank voltage comes from A1 telemetry (`v_in`), not a divider.
- Throttle is **supplied from 3V3**, so its ratiometric output cannot exceed the
  ADC range. One 100 kΩ pulldown for open-circuit detection.
- Display is I²C, 3.3 V.

Nine MCU pins used in total.

---

## 4. Arithmetic to check

Every number below is load-bearing. Verify them.

| Quantity | Value | Derivation |
|---|---|---|
| Bank capacitance | **6.67 F** | 20 F ÷ 3 (series divides) |
| Usable energy, 10→40 V | **5 000 J** | ½C(V₁²−V₂²) |
| Boost (100 kg, 10→20 km/h) | 1 159 J at wheel, ~1 656 J from bank @ 70 % | ΔKE |
| Boosts available | **~3.1** | 5 000 / 1 656 |
| A1 idle draw | ~3 W | measured elsewhere, 255 mA @ 12 V |
| Bank drain by A1 idle | **28 min** | 5 000 / 3 |
| Bank rest voltage | ~14 V | BT1 − V_D1 |
| Bank leakage at 14 V | ~0.2 mA ≈ 3 mW | scaled from 0.05–0.5 mA/100 F spec |
| Standby duration | **~200 days** | 15 Wh / 3 mW |
| S1 left on, stationary | **3.4 W from BT1, 4.4 h** | A1 idle fed through R1 |
| R1 peak dissipation | 42–55 W | (V_pack−0.7)²/R |
| R1 total energy, flat bank | 650–850 J | ½CV² |
| R1 temperature rise | 24–32 K | E / (m·c), 50 W alu-clad ≈ 30 g |
| Bank ESR | 180–360 mΩ | 10–20 mΩ/cell × 18 |
| Fault current at 40 V | **110–220 A** | V / ESR |
| Loss in caps at 40 A | ~400 W (25 %) | I²R |
| Shorted D1 → BT1 | **4.9 A** | (40−16.8)/4.7 |
| A1 BEC loading | 54 mA of 1500 mA | 3.6 % |
| U2 3V3 loading | 70 mA of ~300 mA | 23 % |
| Precharge equilibrium, BT1 at floor | 11.5 V vs 8 V target | V² − V_s·V + P·R = 0 |
| Shell sensor update at 200 rpm | 20 Hz, 50 ms | 6 PPR |

---

## 5. Unverified assumptions

**None of the following has been measured.** Treat conclusions resting on them as
provisional.

| Assumption | Impact if wrong |
|---|---|
| G020 gear ratio ~5:1, pole pairs unknown | Slip arithmetic, ERPM conversion |
| G020 kV and phase resistance unknown | Back-EMF crossover, current ceiling |
| A1 start-up voltage assumed 8 V | Bank floor, whether the keep-alive works at all |
| A1 BEC is 5 V / 1.5 A and is exposed on a connector | **U2 has no power if wrong** |
| G020 shell-speed wire carries no thermistor | Conditioning on GP13 |
| Module capacitance spread ≤10 % | 40 V ceiling may be unsafe |
| Bank leakage ~0.2 mA at 14 V | Standby duration |
| A1 telemetry ERPM lag is tolerable | Slip estimate quality |
| Carrier can be brought out to a caliper on a rear hub | **Whole mechanical concept** |
| SSD1306 OLED readable enough in daylight | Usability |
| Bafang hall mapping works with VESC | Commutation |

---

## 6. Points I most want challenged

1. **Single point of failure on U2's supply.** The MCU is powered by A1's BEC. If
   A1 browns out, U2 resets. Is C4/C5/L1 adequate, or does U2 need independence?
2. **S1 left on flattens BT1 in 4.4 h.** Accepted deliberately (no sleep mode).
   Is that defensible, or does it need a hardware timeout?
3. **No bleed provision.** A fitted bleed cannot work — R1 holds the bank up
   against it. Service procedure is "disconnect BT1 first". Adequate for a
   40 V / 5 kJ bank?
4. **F1 at 60 A** with 110–220 A available fault current and 10 AWG wiring.
   Right value? Right type?
5. **F3 (3 A at the pack)** was added solely to cover a shorted D1. Is it
   necessary, or is BMS over-voltage sufficient?
6. **Throttle at 3.3 V.** Proven on the owner's v1 throttle, but many hall
   throttles specify 5 V. Reliability across units?
7. **Trickle charger U1.** BT1 lasts ~200 days without it. Does it earn its
   place, or is a charge port simpler?
8. **Ground topology.** Star at A1 V−. U2's ground arrives via the BEC. Bank
   currents up to 50 A share that return. Is anything referenced wrongly?
9. **Wheel speed at 6 PPR** is the resolution floor of the whole control problem.
   Is deriving slip from a 20 Hz signal plus link-delivered ERPM viable at all?
10. **The keep-alive loop.** BT1 → R1 → D1 → bank → S1 → F2 → U1 → D2 → BT1.
    Claimed safe because D1 and D2 conduction windows are disjoint. Verify.

---

## 7. Already known open — no need to report these

- Carrier access on a rear hub is unresolved and needs a teardown
- Control law deliberately unspecified
- Part numbers not selected for S1, F1 holder, display
- Whether CAN is broken out on this A1 board
- Logging deferred

---

## 8. Review output wanted

Specific, ranked findings. For each: what is wrong, the failure scenario, and
what it should be instead. Arithmetic errors and safety defects first. Say
plainly if a decision in §2 makes the rest unworkable — that is worth knowing
even though the decision itself is fixed.

Do not soften findings. If the design is wrong, say so.

---

## 9. Supporting material

If the full research folder was supplied:

| File | Contents |
|---|---|
| `findings/decisions.md` | Decision log with rationale |
| `findings/hardware-design.md` | Component stack, conditioning, protection |
| `findings/power-architecture.md` | Two-rail derivation, keep-alive |
| `findings/cap-bank-and-precharge.md` | Bank sizing, module mismatch, diode selection |
| `findings/motor-selection.md` | G020 vs alternatives, sensor resolution |
| `findings/controller-candidates.md` | Why the FSESC4.20 was retained |
| `findings/system-design.md` | System spec, state machine, firmware shape |
| `simulations/planetary-freegen-sim.html` | Interactive planetary model |
