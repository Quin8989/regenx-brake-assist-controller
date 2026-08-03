# Energy and idle budget

Everything here follows from one number: the bank is small. Design decisions that
would be rounding errors on a 500 Wh e-bike are decisive at 4.6 Wh.

---

## 1. What the bank actually holds

20 F, usable 10 → 42 V:

```
E = ½C(V_hi² − V_lo²) = ½·20·(42² − 10²) = 16 640 J = 4.62 Wh
```

Reference events, 100 kg bike + rider:

| Event | Energy |
|---|---|
| One stop from 25 km/h (total, both wheels) | 2 410 J |
| Rear wheel's share of that stop (~25 %) | ~600 J |
| Descent, 5 % grade at 30 km/h | 409 W |
| Time to fill the bank on that descent (rear only, ~25 %) | **~2.7 min** |

So capture is not the constraint — **storage is**. This is the argument that made
the rear wheel acceptable: the front wheel's larger braking share would fill a
bank that is already full.

---

## 2. The idle drain problem, quantified

The v1 symptom — "it drained the caps too quickly on idle" — is almost entirely
one component.

A **Flipsky mini FSESC 4.20** (the v1 controller) idles at **255 mA @ 12 V ≈ 3 W**
with the motor stopped. Holding its reset line low only drops it to 44 mA, so
there is no usable firmware-side sleep.

Against a 16 640 J bank:

| | Time |
|---|---|
| Full bank → empty, controller idling alone | **91 minutes** |
| To burn one hard stop's harvested energy (2 410 J) | **13 minutes** |

That is the whole problem. Every 13 minutes parked, the system throws away a
braking event. Nothing else in the v1 build comes close.

### Target

| Standby goal | Allowed idle draw |
|---|---|
| 7 days | 27 mW |
| 30 days | 6.4 mW |

Three orders of magnitude below where v1 sat.

---

## 3. Getting there

**The controller must be hard-disconnected when parked.** Not slept — physically
switched off the bank. A **latching relay or bistable contactor** is the right
part: it draws current only during the pulse that changes its state, and zero in
both stable positions. A high-side MOSFET with a charge pump is the alternative
but leaks continuously.

Sequencing note: re-closing onto a live bank inrushes into the controller's own
input capacitance. Needs a small precharge resistor and a short bypass delay —
separate from, and much smaller than, the main bank precharge.

Remaining budget with the controller gated:

| Item | Idle draw |
|---|---|
| MCU in deep sleep (RP2350 dormant) | ~0.5 mW |
| Wide-input DC-DC quiescent (see below) | ~µW–mW |
| Sharp Memory LCD, static image | **~10 µW** |
| Wake sensor (reed or variable-reluctance pickup) | 0 |
| **Subtotal** | **< 1 mW** |

At which point the dominant leak is no longer the electronics:

| Item | Idle draw |
|---|---|
| Supercapacitor self-discharge (~0.2 mA string @ 42 V) | ~8 mW |
| Passive balancing resistors, if sized naively (10 kΩ/cell) | ~11 mW |

**The caps and their balancing become the floor**, at roughly 10–20 mW → one to
three weeks of standby. That is a good place for the floor to be. It also means
balancing resistor sizing is an energy decision, not just a safety one — prefer
active/shunt balancing or high-value bleeds.

### Parts that fit the wide input range

The bank swings 10 → 43 V, which rules out most low-Iq regulators (they start at
15 V or higher).

- **Renesas RAA211805** — 7–80 V in, 5 V/300 mA out, ultra-low Iq. The 7 V floor
  covers the whole bank range with margin. Best fit found.
- **TI LM5164** — 100 V capable, 10 µA in sleep, but **15 V minimum input**, so
  it dies before the bank does. Only usable above a raised cutoff.

### Display

The earlier recommendation of a 1000-nit IPS TFT is **withdrawn** — a backlight
that bright costs 100–300 mW continuously, which is 10–30× the entire idle
budget.

**Sharp Memory LCD (MIP)** instead: ~10 µW static, 1–5 Hz refresh, transflective
and readable in direct sunlight (brighter sun = better contrast, no backlight
needed). It is the rare part that is simultaneously the low-power choice and the
sunlight-readable choice.

E-paper is lower still (0 W static) but too slow for live telemetry.

### Waking up

Wake on wheel rotation, using a **passive pickup that costs nothing at rest** — a
reed switch or a variable-reluctance sensor generating its own pulse. The
high-resolution wheel-speed sensor needed for slip control draws real current and
should only be powered once awake.

---

## 4. Braking when the caps are full

This is not a comfort problem. It is a **loss of braking**, and it follows
directly from the gearset.

From the torque split, `T_carrier = −(1+k)·T_sun`. Set `T_sun = 0` — motor
producing no torque, because the bank cannot accept current — and:

```
T_sun = 0  →  T_carrier = 0  and  T_ring = 0
```

**With no motor torque, the carrier brake transmits nothing.** Clamping the band
simply decelerates the carrier until it stops; the rotor then spins freely at
`−k·w_ring` and no braking torque reaches the wheel. You do not get a weak brake.
You get no brake.

This is the same differential logic that killed Magic Drive, arriving from the
other direction, and it matches Grin's FAQ answer that a Freegen wheel loses
braking entirely if the electronics fail.

### The energy has to go somewhere

Braking *is* energy removal. If the caps are full, another sink is mandatory:

**a. Short the motor phases (dynamic braking).** Energy dissipates as `1.5·R·I²`
in the windings. No external sink, no stored energy needed, and the controller
already supports it — `COMM_SET_CURRENT_BRAKE`. Note v1 deliberately avoided this
opcode because it wastes energy; in this mode wasting energy is the objective.

Capacity, at `R_phase = 0.082 Ω`:

| Phase current | Dissipation |
|---|---|
| 20 A | 49 W |
| 40 A | 197 W |
| 58 A | 409 W (matches the 5 % descent) |

Fine for stops, marginal for sustained descent, and it heats the motor.

**b. Bus dump resistor.** MOSFET + power resistor across the bank, PWM'd. To
absorb ~500 W at 42 V: ~3.5 Ω, ~12 A, aluminium-housed wirewound on a heatsink.
Protects the motor thermally and holds brake feel constant regardless of bank
state.

**c. Fail-safe: normally-closed contactor across the phases.** On total power
loss it shorts the windings, so a clamped carrier back-drives into a dead short
and gives speed-proportional braking with nothing alive. **Unverified** — needs
analysis of short-circuit torque at speed and of what happens if it closes at
road speed with the carrier locked.

### Recommended ladder

1. Bank has room → regen, energy stored
2. Bank full → phase-short dynamic braking, energy into motor copper
3. Motor thermally limited → bus dump resistor
4. Total electronics failure → passive phase short (c), plus the front brake

On the rear wheel, step 4 degrading to "front brake only" is acceptable. That
was a large part of the case for choosing rear.

---

## 5. Motor selection consequence

The carrier-brake concept needs a **single-stage** planetary with an accessible
carrier.

- **Shengyi SX1 / SX2** — single-stage, helical, built-in freewheel clutch, T20
  torx side plate with an O-ring seal, known to come apart cleanly. Best
  candidate found so far.
- **Bafang G310 / G311** — inrunner with **double-stage** planets. Rules them out.

Clutch direction varies between models even within a manufacturer (the G062
clutch runs opposite to the G060), so it must be confirmed by inspection rather
than datasheet.

**Open:** whether a rear single-stage hub can present the carrier to a drive-side
caliper, or whether this needs an internal band actuated through the hollow axle.
Not resolvable without opening candidate motors.
