# Motor controller — candidates

Judged against `controller-requirements.md`. Motor: Bafang G020.

---

## 1. The minimum-voltage spec eliminates most of the field

The bank swings from `V_lo` (~9–12 V) to 40 V. Almost every e-bike controller is
built around a battery that never goes below ~30 V, and sets its floor
accordingly. That single spec does most of the filtering.

| Controller | Voltage range | Minimum | Verdict |
|---|---|---|---|
| **Flipsky Mini FSESC4.20** | 8 – 60 V | **8 V** | **Chosen — already owned** |
| Flipsky FSESC 6.6 | 8 – 60 V | **8 V** | Equal on voltage, ~$300 CAD |
| Trampa VESC 6 MkVI | 8 – 60 V | **8 V** | Equal on voltage, ~$350 CAD |
| Flipsky 75100 / Ubox 85 V | 14 – 84 V | **14 V** | High-voltage front end, floor too high |
| Grin Baserunner V6 | 19 – 60 V | **19 V** | Floor too high |
| Grin Phaserunner V6 | 24 – 72 V | **24 V** | Disqualified |

> **Correction.** An earlier revision claimed VESC 6 reaches 6 V and recommended
> it on that basis. **Wrong** — 6 V is the DRV8323 gate driver's undervoltage
> lockout, not the board's. Every VESC, 4.12 or 6, any vendor, bottoms out at 8 V
> because that is where the front-end supply gives out. There is no board in the
> family that starts lower, so **the VESC 6 offers no advantage on the spec that
> drove this entire selection.**
>
> Separately, high-voltage variants are *worse*: a board designed for 84 V input
> has a front end that will not regulate at 10 V. The 75100 and 85 V Ubox are out
> for the same reason as the Grin controllers.

### Why the Grin controllers fall out

Genuinely painful, because they are the natural e-bike fit: proportional regen
that is properly supported, Vancouver stock, real documentation, and Grin
understand this application better than anyone.

But the floor is fatal to the precharge design:

```
precharge energy = ½ · C · V²        C = 6.67 F

to 12 V (VESC)          480 J
to 19 V (Baserunner)  1 204 J     2.5×
to 24 V (Phaserunner) 1 921 J     4×
```

Worse than the energy cost, **a 4S pack cannot reach either target through a
diode.** 16.8 V full minus 0.8 V leaves 16.0 V — short of 19 V, far short of 24 V.
Choosing a Grin controller means putting the boost converter back, along with its
inrush trap, its output disconnect and its control logic — all of which the
resistor-and-diode design deleted.

A ~6S pack (18–25.2 V) could feed a Baserunner directly, so it is not absolutely
excluded. But it costs a larger pack, 2.5× the precharge energy, and 10 V of
unusable bank range at the bottom.

### VESC 6 over VESC 4.12

Both clear the requirement. The 6-class is preferred on:

- **Lower floor** — DRV8323 undervoltage lockout at 6 V versus the DRV8302's 8 V.
  Every volt of `V_lo` is precharge time and pack drain.
- **Better current sensing and gate drive**, which matters for smooth low-speed
  torque during braking.
- Higher voltage headroom for regen transients.

Note that manufacturers commonly rate their boards 3S+ (≈11 V) rather than to the
chip's floor, so **the usable minimum is a per-board question, not a family
question.** It needs bench measurement whatever is bought.

---

## 2. Requirements check — VESC 6 class

| Requirement | Status |
|---|---|
| Minimum start-up voltage | 6 V chip floor; per-board, needs measuring |
| LVC settable very low | **Yes** — v1 already runs battery cut at 10 V / 9 V |
| Max ≥ 50 V | Yes, 60 V typical |
| ≥ 50 A continuous, ≥ 100 A peak | Yes on 6-class hardware |
| Commandable proportional regen | **Yes** — `SET_CURRENT` with negative values |
| Current/torque control mode | Yes, native |
| Sensored FOC | Yes |
| Bafang hall mapping | Programmable |
| Command rate ≥ 100 Hz | Yes |
| CAN | Yes — and it fixes v1's actual problem |
| Configurable regen current limit | Yes — `Battery Current Max Regen` |
| Configurable regen voltage ceiling | Yes — `Maximum Input Voltage` |

**Staying with VESC is not repeating v1's mistake.** The EMI failure was the
*link* — TTL UART through a noisy environment — not the controller. Moving the
same controller family onto CAN addresses the actual fault.

---

## 2a. Does VESC firmware actually support this regen? Yes

Three independent confirmations that commanded, variable-torque regen is native:

**The firmware does it by design.** In current control mode a negative current
command produces braking torque, and "setting the desired negative current at all
speeds above 0 should give a constant braking torque at all (positive) speeds."
That is precisely the interface the slip regulator needs — command amps, get
torque, independent of speed.

**All controlled braking is regenerative.** From a VESC developer: *"There is no
way to brake in a controlled way without pushing the current back into the
battery."*

**v1 already proved it on the bench.** The 2026-04-21 ride logs show
`regen_command_request` reaching the full 40 A ceiling with measured motor current
following, on an FSESC4.20. The mechanism is not speculative here — it has run on
this project's own hardware.

Both interface paths exist natively: digital (`SET_CURRENT` with negative values)
and analog (ADC app in `Current No Reverse Brake ADC2` mode, which maps lever
travel to proportional braking).

### Two current limits, and why they help us

| Setting | Controls |
|---|---|
| `Motor Current Max Brake` | Phase current during braking — i.e. **torque** |
| `Battery Current Max Regen` | Current into the **bank** |

They are independent because "the battery current is a fraction of the motor
current, depending on the modulation (duty cycle)." At low speed that means
**strong braking torque with modest charge current** — useful, since the bank's
tolerance and the rider's braking demand are different constraints.

> **Correction.** v1's `send_current` docstring claims `COMM_SET_CURRENT_BRAKE`
> "dissipates energy as heat" through phase shorting, and earlier revisions of
> these notes repeated it. **It is wrong.** Brake current is regenerative like any
> other controlled braking. Phase shorting is the *handbrake/parking* mode, which
> only operates when stationary.
>
> This matters if full-bank handling is ever revisited: there is **no easy
> dissipative braking mode** on a VESC while moving, because controlled braking
> inherently pushes current to the bus. It makes "just taper regen when full" more
> clearly the right call, not less.

---

## 3. The real limitation: braking power peaks, then goes negative

A generator-application report describes exactly the failure a slip regulator
could walk into:

> "the generated power ramps up with the brake current, hits a peak, and then
> drops back down, eventually dropping below zero and **actually drawing power**."

This is not a VESC bug, it is the physics of the machine:

```
P_net = E·I − 1.5·R·I²

peak at   I = E / (3R)
zero at   I = E / (1.5R)      — beyond this the controller consumes power
```

Copper loss grows as I² while harvest grows only as I, so past a speed-dependent
current there is no more power to be had, and past twice that current the bus is
being drained to brake the wheel.

Using SX2 constants as a stand-in until the G020 is measured (7.4 rpm/V,
R_phase ≈ 59.5 mΩ):

| Speed | Back-EMF | Peak-power current | Net-zero current |
|---|---|---|---|
| 25 km/h | 27 V | 152 A | 304 A |
| 10 km/h | 11 V | 61 A | 122 A |
| **5 km/h** | **5.4 V** | **30 A** | **61 A** |

**At road speed this is irrelevant — the peak is far above any current we would
command. At walking pace it is entirely reachable.** With a ~50 A ceiling, a
regulator chasing a slip setpoint down to a stop can easily command past the peak
and start discharging the bank to brake.

**Firmware consequence: the regen current ceiling must scale with speed**, capped
at something like `E/(3R)` rather than a fixed 40–50 A. This is the same insight
that produced the efficiency-optimal current model in v1's settings comments —
worth carrying forward rather than rediscovering.

### Also from that report

Two configuration notes with real effect: enabling **"Sample in V0 and V7"** under
FOC Advanced stabilised measurements that were otherwise "all over the place," and
**lowering the observer KI** improved stability. Both are relevant to a machine
being run as a generator at varying speed.

---

## 3b. Uncontrolled regen below back-EMF

> "The VESC cannot cut back the inverse diode flow when the generated voltage is
> higher than the battery voltage."

When motor back-EMF exceeds bus voltage, current flows through the MOSFET body
diodes regardless of what the controller commands. **Regen becomes uncontrolled
in that regime** — not off, but unmodulatable.

This is not hypothetical for us. Taking the SX2's published 7.4 rpm/V as a stand-in
until the G020's is measured, back-EMF at 25 km/h (≈200 wheel rpm) is roughly
**27 V**. So with the bank freshly precharged to 12 V, any braking at speed puts us
straight into the uncontrolled region.

### Consequences

- **The slip regulator loses authority** until the bank charges above back-EMF.
  Braking force becomes whatever the physics gives, and the band takes the
  excess as slip and heat.
- It is **self-limiting**: uncontrolled regen charges the bank fast. From 12 V to
  27 V is ~1 950 J, a few seconds at meaningful braking power. Control returns
  once bus exceeds back-EMF.
- It **partly offsets the case for a very low `V_lo`.** A lower floor shortens
  precharge but lengthens the uncontrolled window at the start of a ride. The
  optimum is not simply "as low as possible" after all.

Not a blocker — the mechanical brake still limits torque, and the episode is
brief and self-correcting. But it belongs in the control design, and the firmware
should know when it is in that regime rather than fighting a loop it cannot win.

**To resolve:** measure the G020's actual kV, then compute the crossover speed at
each candidate `V_lo`. That converts this from a caveat into a number.

---

## 4. Decision — keep the Mini FSESC4.20

Once the voltage floor turned out to be 8 V across the whole family, the case for
buying anything collapsed.

| Requirement | Mini FSESC4.20 |
|---|---|
| Low start-up voltage | 8 V — equal best in the family |
| LVC settable very low | Yes — v1 ran 10 V / 9 V |
| ≥ 50 A cont / ≥ 100 A peak | 50 A / 150 A rated |
| Max ≥ 50 V | 60 V |
| Commandable proportional regen | **Proven — v1 logs reached 40 A** |
| Sensored FOC | Yes |
| Configurable regen limits | Yes |
| Cost | **owned** |

Also 80 g and 67 × 39 × 18 mm, which matters on a bike, and its behaviour is
already familiar.

**What ~$300 CAD would have bought:** three-shunt current sensing instead of two
(better low-speed FOC, which is exactly when braking to a stop), the DRV8323
instead of the DRV8302, and 60 A continuous instead of 50 A. Real, but secondary
to a voltage floor that does not move.

### Two caveats carried forward

**The 50 A continuous rating is optimistic.** An 80 g board with a small heatsink
will not hold 50 A without airflow — realistically 20–30 A. Our duty cycle is
short boosts and braking events, so peak governs and 150 A is ample. Do not design
around 50 A continuous.

**The DRV8302 is this generation's known weak point** and the usual failure mode.
The DRV8323 is more robust. That is a reliability argument for upgrading later,
not a capability one now.

**The EMI history is not a reason to replace the board.** The fault was the link —
TTL UART through a noisy environment — not the controller. RS-422 or CAN fixes it
on hardware already in hand.

---

## 5. Open

- **Is CAN broken out on this specific board?** VESC 4.12 hardware has CAN on the
  STM32, but the Mini's footprint may leave it as pads or omit it. If absent,
  RS-422 over the existing UART is the fallback — which was the plan regardless.
- **Measure minimum start-up voltage.** Start-up, not running — switching
  supplies have hysteresis, and only the higher figure matters for precharge.
- **Measure G020 kV and phase resistance.** Fixes both the back-EMF crossover
  (§3b) and the speed-dependent current ceiling (§3).
- Confirm the hall input works with Bafang mapping without a custom table.
- Verify regen into a capacitive bus. Every vendor assumes a battery, which holds
  roughly constant voltage while charging. A cap bank does not.
