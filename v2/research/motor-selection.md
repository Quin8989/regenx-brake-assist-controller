# Motor selection

Requirements: rear, single-stage planetary, accessible carrier, motor halls plus
a multi-pole shell speed sensor.

---

## 1. Multi-pole shell sensors are standard, not rare

The earlier worry that built-in wheel-speed sensors are 1 pulse/rev was wrong.
**6 PPR is the industry norm** on geared hubs from both major suppliers:

- **Bafang** geared hubs with a clutch carry 6 speed magnets in the outer case.
- **Shengyi SX** — Grin's spec sheet states the white wire "toggles to 0 V
  **6 times per revolution**," with a 10 K NTC thermistor wired in parallel on the
  same line.

So the requirement is met by ordinary motor selection. No add-on magnet ring
needed to get started.

---

## 2. Shengyi SX2 — the candidate

Grin stocks and documents it, which means real published specs rather than
marketplace guesswork. They are in Vancouver, so it ships domestically.

| | SX2 (rear) |
|---|---|
| Planetary | **Single stage**, helical cut |
| Gear ratio | **4.78 : 1** — 86T ring, 18T sun (→ 34T planets) |
| Motor | 15 pole pairs, **outrunner** |
| Speed sensor | **6 pulses/rev** on white wire, thermistor in parallel |
| Clutch | Built in — freewheels with no drag |
| Disassembly | T20 torx, O-ring sealed side plate |
| Disc mount | 6-bolt ISO, correct offset |
| Freehub | Shimano cassette, 9/10/11 speed MTB |
| Axle | M12 × 1.25, 10 mm flats, 136.5 mm |
| Mass | 3.18 kg |
| kV | 7.4 rpm/V (8T std) or 9.8 (6T fast) |
| Phase-to-phase R | 119 mΩ std / 72 mΩ fast |
| Phase-to-phase L | 180 µH std / 115 µH fast |
| Connector | Z910, 22 cm, end-of-axle exit |

### Why it fits

**`k = 4.78` is almost exactly the 4.8 already in the sim.** The physics model,
the planetary simulator, and every derived number carry over essentially
unchanged. That is luck, but useful luck.

**Single stage is explicit.** Grin contrast it directly against the Bafang
G310/G311, which are "an inrunner with doublestage planets" — the configuration
that rules those out for carrier braking.

**Easy, sealed disassembly.** T20 torx and an O-ring on the side plate joint,
which matters when the carrier is going to be modified and reassembled.

### Two lines from Grin worth reading closely

> "the SX hubs use double deep-groove ball bearings instead \[of thrust
> bearings]. This means **in principle it will be possible to have a locked
> clutch version of the SX motor that is capable of regenerative braking** and
> reverse mode like we have done with the GMAC motor."

Grin have evaluated this hub for reverse torque through the gear train and think
it is up to it. That is a GMAC-style clutchless conversion rather than a
carrier brake, but the mechanical question — can this gearset take sustained
reverse load — has been answered in the affirmative by people who build these.

> "Bolts that are too long risk protruding inside the motor shell and **scraping
> against the clutch**."

A packaging hint, and a promising one. The disc rotor mounts on the **non-drive
(left) side**, and disc bolts that are too long reach the clutch — so **the
clutch sits immediately behind the left side plate, on the same side as the disc
mount.** That is the geometry the carrier-brake concept needs, on the side a
caliper can actually reach.

**Not proof.** It locates the clutch, not the carrier, and says nothing about
whether the carrier can be brought out to a rotor. But it is the first
encouraging evidence on the rear-access question, and it is checkable with one
teardown.

---

## 3. Bafang G020 / G060 — the cheaper fallback

Single-stage, ~5:1, rear, 6 PPR speed sensor, widely available and cheaper.
Nylon planet gears rather than helical steel, less published detail, and hall
mapping that is the de-facto standard.

Adequate, and worth having as the sacrificial teardown motor rather than cutting
into a more expensive hub first.

**Ruled out: Bafang G310 / G311** — double-stage planets, two carriers, no clean
member to brake.

---

## 4. What 6 PPR means for the control loop

Resolution is now asymmetric but usable.

| | Edges per wheel revolution |
|---|---|
| Motor halls (15 pp × 6 states × 4.78) | **~430** |
| Shell speed sensor | **6** |

At 200 rpm the wheel sensor updates at 20 Hz — 50 ms between edges. Wheel speed
itself is precise at each edge (measure the period); the error is staleness, and
under 0.5 g braking the wheel moves ~3.5 % in that window.

Slip is a difference of two ~960 rpm numbers, so that staleness sets the floor on
usable slip setpoint:

| Slip setpoint | Signal | Error from 50 ms staleness |
|---|---|---|
| 5 % | 48 rpm | ~70 % — **unusable** |
| 10 % | 96 rpm | ~35 % — marginal |
| 15 % | 144 rpm | ~24 % — workable |

**Conclusion: target 10–15 % slip, not 5 %.** Since heat fraction equals slip
fraction, that still recovers 85–90 % of braking energy — a good trade for
control that works with a stock sensor.

If tighter slip is wanted later, the options remain: add magnets to the side
cover during the carrier modification, or sense the carrier directly, which
removes the difference-of-large-numbers problem entirely.

---

## 4a. G020 sensing — confirmed

Both signals the slip calculation needs are present:

| Signal | Source | Status |
|---|---|---|
| Motor speed | Motor hall sensors on the rotor | Standard on sensored hubs |
| Wheel speed | **6 magnets in the case, read by a Honeywell SS43F** | **Confirmed for SWX02 / RM G020** |

Corroborated by controller configuration practice: KT controllers are set to
`P2 = 6` for the SWX02, which is the pulses-per-wheel-revolution parameter.

The shell sensor necessarily reads the **shell, not the rotor** — that is the
entire reason it exists. Hall-derived speed goes to zero while coasting because
the clutch disengages the rotor, so manufacturers added a sensor that keeps
reading. Exactly our requirement, for exactly our reason.

---

## 4b. Topology — settled, one configuration only

**The ring gear and the hub shell are the same piece.** In a geared hub motor the
internal ring teeth are machined into or pressed into the shell, so the planets
drive the wheel directly. That fixes the whole arrangement:

```
sun     = motor rotor          (input)
ring    = hub shell            (output — same part as the wheel)
carrier = one-way clutch to axle   (reaction member)
ratio   = Z_ring / Z_sun
```

There is no alternative layout in these motors. Confirmed by Grin's published SX2
figures: 86T ring ÷ 18T sun = 4.78, exactly the quoted 4.78:1. That arithmetic
only closes if the ring is the output.

By the same reasoning, the G020's "5:1" means `Z_ring/Z_sun = 5`, same
architecture. This is also the topology already assumed in `sim/physics.py`, so
the existing model was correct.

> **Correction.** An earlier revision of this file claimed geared hubs come in two
> topologies and that the G020 might be unusable. That was wrong. The
> two-configuration idea was taken from patents covering *multi-speed
> internally-geared hubs* (Shimano Nexus/Alfine type), which do swap the grounded
> member to obtain multiple ratios. Hub motors do not. The hand-spin test given in
> that revision was also useless — both hypothetical cases behave identically from
> outside, which should have been the clue that the distinction did not apply.

### What actually matters about the clutch

The carrier's one-way clutch holds it against **backward** rotation, which is what
makes motoring work, and lets it overrun **forward**, which is what makes coasting
free. The brake has to resist the forward direction — precisely the direction the
clutch does not. That asymmetry is the real mechanism, and it is unaffected by the
correction above.

---

## 5. Open

- **Can a brake surface be brought out from the carrier to where a caliper
  reaches?** The carrier sits inside the hub on its clutch. This is the real
  mechanical question and always was — needs a teardown.
- Hall mapping differs from Bafang, so the controller needs programmable hall
  mapping — Grin's own Baserunner/Phaserunner do; a VESC would need checking.
- Thermistor shares the speed wire. Convenient with Grin controllers that split
  it; anything else has to separate the two signals or ignore one.
