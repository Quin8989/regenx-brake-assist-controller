# Design parameters — v2

**Scope: this is the v2 spec. Nothing here describes or modifies the existing
firmware.** `firmware/` and its `settings.py` belong to v1 and stay untouched
until v2 is built.

Parts are still being mixed and matched. So this document holds **relationships,
not settled numbers** — the equations, what each parameter depends on, and the
current working values marked as provisional. Swap a part and the affected rows
recompute instead of the document going stale.

---

## 1. The invariant

Everything below is negotiable. This is not:

> **An almost-closed-loop supercapacitor regen system that captures braking
> energy and returns it as a few good boosts.**

Concretely, the three things a candidate build has to satisfy:

| | Test |
|---|---|
| **Few good boosts** | ≥ 2 boosts of 10 → 20 km/h from a full bank, **ESR-loaded** |
| **Almost closed loop** | Electronics do not consume the harvest; the pack refills from braking, not a wall socket |
| **Supercap buffer** | Caps carry the high-power transients; no propulsion battery |

A part choice that keeps all three is acceptable regardless of what it does to
any individual number below.

---

## 2. What is locked, what is not

| Locked | Still open |
|---|---|
| Rear wheel | Motor model, single-stage planetary required |
| Carrier-brake (Freegen-style) concept | Whether carrier reaches a drive-side caliper |
| Supercap propulsion buffer | Bank configuration and ceiling |
| Small Li-ion housekeeping rail, Option A | Pack S-count and capacity |
| Mechanical master switch, no sleep mode | Contactor part |
| Resistor + diode precharge, no boost converter | R value, diode part, target voltage |
| Slip-speed control replaces AIMD | Controller model |

---

## 3. The parameter web

Each equation names what it depends on. Change an input, recompute the row.

### Bank

```
C_bank    = C_module / N_series                    (series divides capacitance)
R_esr     = N_cells · r_cell                       18 cells × 10–20 mΩ
V_lo      = V_startup + I_boost · R_esr            reachable floor, NOT the datasheet floor
E_usable  = ½ · C_bank · (V_hi² − V_lo²)
η_esr     = 1 − I_boost · R_esr / V̄                V̄ = (V_hi + V_lo)/2
N_boosts  = E_usable · η_esr · η_conv / ΔKE        ΔKE ≈ 1 159 J for 10→20 km/h @ 100 kg
```

**`V_lo` and `η_esr` are both functions of boost current** — this is the trap the
first revision fell into. Writing `V_lo` as a constant 10 V and folding all losses
into a single `η` double-counts the conversion chain and omits ESR entirely,
overstating the result by roughly 2×. ESR appears **twice**: once raising the
floor, once eating energy on the way out.

`V_hi` is set by cell matching, not by the cell rating. For an 18-cell string
with one cell low by spread `s`:

```
V_cell_worst = V_hi · (1/(1−s)) / (17 + 1/(1−s))        must stay ≤ 2.7 V
```

`V_lo` is set by the **controller's start-up voltage** — not its running
voltage. Switching supplies have hysteresis; a board that runs at 8 V may need
9.5 V to start, and only the start-up figure matters for precharge.

### Precharge

```
V_final = V_pack − V_f
t       = R · C_bank · ln( V_final / (V_final − V_target) )
E_pack  = V_pack · C_bank · V_target
E_stored= ½ · C_bank · V_target²
```

Two hard constraints fall out:

- **`V_final` must exceed `V_target`**, or the target is unreachable at any
  resistor value or duration. This is what sets the pack's usable floor.
- `R` sets time and peak current only. The energy burnt is the same at any `R`.

### Idle

```
t_drain = E_usable / P_idle
```

The controller idles around 3 W, which is why it sits behind the master switch.
(An earlier revision said "behind a contactor" — the contactor was deleted; S1
does the job.)

---

## 4. Current working values — provisional

Derived from the parts currently on the bench. Every one of these moves if a
part changes.

| Parameter | Working value | Set by | Firm? |
|---|---|---|---|
| `C_bank` | 6.67 F | 3 × GDCPH 16 V 20 F in series | measured config |
| `V_hi` | 40 V | cell matching spread | **needs module measurement** |
| `R_esr` | 180–360 mΩ | 18 cells × 10–20 mΩ | **needs bench measurement** |
| `V_startup` | 8–9 V | controller start-up voltage | **needs bench measurement** |
| `V_lo` @ 40 A | ~18.8 V | `V_startup + I·R_esr` | follows |
| `V_lo` @ 20 A | ~13.4 V | `V_startup + I·R_esr` | follows |
| `E_usable` @ 40 A | ~4 155 J | above | follows |
| `N_boosts` @ 40 A | **~1.6** | `η_esr` 0.63, `η_conv` 0.70 | below old invariant |
| `N_boosts` @ 20 A | **~2.3** | `η_esr` 0.80, `η_conv` 0.70 | meets revised invariant |
| Pack | 4S Li-ion, ~15 Wh | Option A | provisional |
| `V_f` | 1.0 V @ 3.4 A | generic Si rectifier | provisional |
| `R` | 4.7 Ω | precharge time target | provisional |
| `t_precharge` | 26–67 s | above, across pack range | follows |
| Precharges per pack | ~60 | above | follows |

### Sensitivities worth knowing

- **Lowering `V_lo` barely adds energy** — 10 V → 8 V is +2.4 %, because energy
  goes as V². Its real value is halving precharge time and removing the pack
  floor constraint entirely.
- **`V_hi` is the sensitive one.** 40 V vs 38 V is 10 % of usable energy, and the
  choice between them is decided purely by measured cell spread.
- **A second parallel string doubles everything** and is the only lever that
  creates real margin. Currently declined — no additional caps.

---

## 5. Measurements that would firm this up

Three bench tests collapse most of the uncertainty above.

1. **Module capacitance, all three.** Constant-current discharge and a stopwatch:
   `C = I·t/ΔV`. Decides `V_hi` — 40 V if spread ≤ 10 %, 38 V if worse.
2. **Controller start-up voltage.** Ramp a bench supply down until it drops out,
   then back up until it boots. Take the second number. Decides `V_lo`, and
   confirm regen actually functions there rather than just powering on.
3. **Bank self-drain.** Charge, disconnect everything, log voltage for hours.
   Separates cap leakage plus balance-board draw from controller idle — and
   settles whether the stock balance boards were part of the original problem.

None needs new parts. All three are afternoon jobs.
