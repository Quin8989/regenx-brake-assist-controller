# Carrier-brake mechanism — concept study

**Status: study in progress.** This is spec §11 gate 1 — "carrier accessible to a
caliper on a rear hub" — the one assumption whose failure obsoletes the whole
electrical design. The study's job is to make teardown day a set of specific
questions with a mechanism design waiting on each answer, and to define the CAD
phase that follows.

---

## 1. Objective and success criteria

Fit a rider-actuated friction brake to the G020's planet carrier such that:

| # | Criterion |
|---|---|
| 1 | Coasting unchanged — carrier overruns freely, rotor stationary, zero drag |
| 2 | Brake lever force modulates carrier holding torque continuously (slip is the control variable) |
| 3 | Torque capacity ≥ 80 N·m at the carrier (§4 sizing) |
| 4 | Fits the 135 mm rear dropout envelope with cassette and wheel disc brake intact |
| 5 | Brake reaction torque has a defined path to the frame, not through the wheel |
| 6 | Stock freewheel behaviour in drive (assist) preserved |
| 7 | Serviceable — pads/rotor replaceable without motor teardown |

---

## 2. Stock G020 architecture — what is now known

Bafang geared-hub teardowns (BPM class, same family) establish:

- Sun gear on the rotor shaft; three nylon planet gears (~42 T) on the carrier;
  steel ring gear pressed into the hub shell. Ring and shell rotate together —
  consistent with the project's kinematic model.
- **The one-way clutch is integrated with the planet-carrier assembly and grips
  the axle**: "spins freely in one direction but grips firmly on the shaft in
  the other." Sold as a unit ("clutch with 42 T nylon gears"); old version has
  4 key slots, new version 1 key slot, keyed to the axle.

### The rotating-frame map — what is actually static

Every part of a hub motor belongs to one of three concentric motion classes:

| Class | Members | Motion |
|---|---|---|
| **Static** | axle, stator, cable, torque arm | fixed to frame |
| **Carrier** | planet carrier + pins + (stock) one-way clutch | freewheels; the member to brake |
| **Wheel** | hub shell, **both side covers**, ring gear, freehub + cassette, spokes, disc rotor | wheel speed |

Two consequences that shape everything below:

1. **The side covers rotate.** Nothing static can be mounted to or through a
   cover except at the axle centerline. Any mechanism element that reacts
   braking torque must ultimately connect to the **axle** (which already has a
   torque arm) or to the **frame** via a member brought out *concentric with
   the axle* through a cover's centre bore.
2. **The carrier is axially adjacent to one cover and radially buried** —
   planets mesh outward to the ring, ring is pressed into the shell. The only
   naturally accessible carrier surface is its outboard **face**, across a few
   millimetres of gap to the (rotating) cover.

So every candidate is one of exactly two torque-path families:

- **Path 1 — bring the carrier out:** a tube fixed to the carrier passes
  through the cover's centre bore (the cover then bears on the tube instead of
  the axle), carrying a braking surface *outside* the shell; reaction to the
  frame/torque-arm plate. This is Grin's architecture — their 80 mm rotor sits
  on exactly such a tube — but they designed the motor around it.
- **Path 2 — react to the axle inside:** a friction or positive element
  between the carrier and an axle-grounded structure, entirely within the hub;
  reaction through the axle and the existing torque arm. **No external
  bracket, nothing visible but the actuator entry.**

If the G020 follows this pattern — **Case B**, and everything points that way —
the stock motor is already the Freegen-compatible architecture:

```
DRIVE:  sun torque → carrier reaction rearward → clutch grips axle → ring/shell driven
COAST:  shell drags carrier forward through the mesh → clutch overruns → rotor stationary
REGEN:  friction brake holds the carrier → torque path restored → sun spins, generates
```

**The conversion is then purely additive: bring a brake surface off the
carrier. No clutch is added, removed or reversed.** The stock clutch *is* the
freewheel the design preserves.

**Case A (fallback):** if the G020 instead clutches ring-to-shell with a rigid
carrier, the conversion is invasive — free the carrier, add bearings and a
separate one-way clutch (HF/CSK class, ≥80 N·m on the available bore), and lock
the ring solid to the shell. Substantially harder; the teardown decides. Prior
art (below) working on Bafang-family geared hubs strongly suggests Case B.

---

## 3. Prior art

**Grin Freegen (ebikes.ca)** — the direct template. An **80 mm aluminium rotor
connected to the clutch planet carrier through a hub**, braked by a
**right-side-mounted caliper**; *all* braking torque passes through that rotor
and caliper. Braking starts as regen resistance and blends toward friction as
clamp increases — mechanically identical to this project's slip-control
concept. Offered front and rear (Magic Drive). Confirms: the carrier of a
Bafang-class geared hub can carry a small rotor, in the axial budget of a
normal bike, with a commodity caliper.

**ChargeBike** — an independent variable-regen geared-hub braking system that
still freewheels, same mechanism family. Two independent implementations mean
this is an engineering exercise, not an invention.

What this project does differently: Grin blends regen and friction under
electronic control tied to charge state; here the rider's lever directly sets
carrier holding force, slip is measured (shell 6 PPR vs ERPM), and the VESC
current command against slip is the deferred control law. The mechanism is the
same; the control philosophy differs.

---

## 4. Sizing

### Torque

The friction brake can never see more than the gear-referred generating torque
(plus transients): `T_carrier = (1+k)/k · T_wheel,regen`. With k ≈ 5 and the
motor's 45 N·m wheel rating:

```
T_carrier,max ≈ 1.2 × 45 ≈ 54 N·m      →  design capacity 80 N·m (≈1.5× margin)
```

Once the carrier is fully held (slip → 0) the brake is static and transmits
only what the motor reacts — over-squeezing cannot overload it, it just ends
modulation. Failure of the brake (slips, fades) fails safe: less regen, and the
wheel's own disc brake is untouched and independent (criterion 4).

At an 80 mm rotor (40 mm radius, pads at ~30 mm effective): 80 N·m needs
~2.7 kN clamp-friction force — high for 80 mm. **At 120–140 mm rotor (pads at
~50–58 mm) it needs 1.4–1.6 kN, comfortably inside a mechanical MTB caliper's
envelope.** Default to 120 mm unless the axial budget forces 80 mm (Grin's
choice suggests 80 mm works with their caliper; check pad compound and clamp).

### Thermal and wear — why this is not a brake

Slip heat is the loss fraction: `P_pad = s · P_braking`, and the control law
*wants* slip small. The friction interface therefore sees a duty an order of
magnitude or two below a real brake:

| | Carrier friction interface | Bicycle disc brake |
|---|---|---|
| Energy per stop | `s` × stop energy ≈ **100–300 J** at working slip | full ~3.5 kJ |
| Rubbing speed | carrier ≤ ~165 rpm at r ≤ 50 mm ≈ **≤ 1 m/s** | 20+ m/s |
| Power, sustained event | 50–150 W for seconds | kW |
| Season's wear energy | ~200 stops × 200 J ≈ **0.04 MJ** — negligible pad volume | ~0.7 MJ |
| Fade requirement | none at these energies | central |

**Consequences for the mechanism:** no rotor mass is needed for heat, no
exotic pad compound is needed for fade, wear allows near-zero running
clearance and one-time bed-in of non-conforming surfaces, and μ-stability
matters more than energy capacity. The friction surface can be the carrier's
own steel. What survives from brake practice is only the *force* problem:
producing 1.5–3 kN of normal force from ≤ ~1 kN of cable tension — every
candidate below is checked against that.

The one duty limit that remains: continuous high-slip drag (a long descent
ridden on the carrier brake at large slip) — noted for the manual regardless
of mechanism.

---

## 5. Mechanism candidates — organized by torque path

An earlier draft of this section placed calipers, bands and cams against the
side cover or an exposed carrier OD. **That was a geometry error: the cover
rotates, and the carrier OD is buried inside the ring gear** (§2). The field
is rebuilt here around the only two legal torque paths. Force checks use the
nominal 54 N·m, μ ≈ 0.35, against a ~1 kN cable-tension budget.

### Path 1 — carrier brought outside on a concentric tube

The Grin architecture: tube fixed to the carrier face, out through the cover
bore (cover re-bears on the tube), braking element outside, reaction to a
frame/torque-arm bracket.

- **1a. Pinch or small disc on the tube flange** — proportional, commodity
  pads, Grin-proven *on a motor designed for it*.
- **1b. Band on a tube drum** — thinnest external option; anchor orientation
  is a tuning knob between lever effort (self-energized, ~0.23 kN) and
  linearity (de-energized, ~1.4 kN via a 2:1 crank).

**The retrofit problem is the exit, not the brake.** Whichever side the G020's
gear chamber is on, the tube surfaces into occupied space: the cassette side
puts it through the freehub's bore (a ~2–3 mm annulus over the axle, then
emerging outboard of the cassette where ~5 mm remains to the dropout); the
disc side puts its flange into the wheel-rotor and caliper region. Grin
resolved this by designing the hub around the tube. A stock-G020 Path 1 is
therefore **conditional on the teardown finding a gifted exit** (questions 2,
4b) — it is no longer the default.

### Path 2 — react to the axle, entirely inside the hub

> **Interactive visualization:**
> [`carrier-brake-path2-viz.html`](carrier-brake-path2-viz.html) — half-section
> and face view of concept 2a with lever and road-speed sliders; shows the
> coast / slip / held states, the clamp motion, and the torque path. Open in a
> browser.

A friction or positive element between the carrier's outboard face and an
**axle-grounded spider** (a plate whose bore sits on the axle flats — no axle
machining, the flats already exist). All torque exits through the axle and the
torque arm that the motor reaction requires anyway. **No external bracket, no
frame attachment, nothing visible but the actuator entry.** The wheel's own
disc brake is untouched on its own path.

- **2a. Single friction disc (new default).** Friction disc splined to a ring
  on the carrier face, squeezed between the spider and a pressure plate —
  both static, so the clamp force is internal to the static assembly and the
  carrier sees pure torque, no net thrust into the gear train. Two friction
  faces at r ≈ 27 mm: F ≈ 2.9 kN, produced by a ball-ramp or cam at 10–20×
  from the cable — near-zero running clearance is permissible (§4), so the
  short stroke costs nothing. Axial stack ≈ 10–13 mm: **the deciding
  measurement is the carrier-face-to-cover gap (q5)**; if short, the options
  are a dished replacement cover or concept 2e.
- **2b. Multi-plate pack (2a scaled).** Alternate carrier-splined and
  spider-splined plates, motorcycle-clutch style: 4–6 faces drop the clamp to
  0.9–1.3 kN and every plate is a laser-cut flat. Better modulation authority,
  thicker stack — only if 2a's force or feel disappoints.
- **2e. Expanding shoe against a drum lip on the carrier face.** If axial
  space is the famine, go radial: the carrier adapter ring carries a short
  drum lip (~8 mm deep), and one *trailing* shoe expands against its inner
  surface from a cam on the spider. N ≈ 3.9 kN from cam advantage; axial cost
  is only the lip depth. Proportional, enclosed; shoe reaction lands on the
  spider, not the carrier bearings.
- **2f. Band inside, around the same drum lip** — spider-anchored. Wrap access
  around a face-mounted lip is awkward; kept only as a variant of 2e.
- **2c. Pawl + ratchet ring (L, internal).** Positive latch, zero friction
  material, engages while overrunning; release under load assisted by
  firmware zeroing regen current for ~50 ms. See lever-feel note below.
- **2d. Cone ring.** High gain (1/sin α), compact — but sticky engagement and
  jam risk; dominated by 2a. Kept for the record.

**Actuation entry (Path 2), two variants:**

- **(i) Sliding collar at the cover bore** — a keyed sleeve on the axle flats
  moves 1–2 mm axially, driven by a small cam lever between cover and
  dropout; the cover's bearing rides on the sleeve OD (static-to-rotating
  interface, exactly what the stock cover-to-axle bearing already is).
- **(ii) Pushrod through a hollowed axle end** — the Sturmey-Archer solution,
  a century of precedent. Centre-drilling 6 mm through a ~12 mm axle end
  retains ~94 % of bending section (`1 − (6/12)⁴`). Cleanest exterior of all:
  the cable dies into the axle nut like a 3-speed. Conditional on which axle
  end is free of the phase-cable channel (q4c).

### Lever feel

The friction concepts (1a/1b/2a/2b/2e) give real, physical brake feel for
free — force at the lever *is* clamp force, torque follows it, and modulation
is the mechanism working, not a simulation.

**The latch (2c/L) can only ever fake it.** The honest engineering answer is a
pedal-feel simulator — a spring-stack cartridge in the lever line shaped to a
brake's force-travel curve, with a force sensor commanding regen current —
which is exactly how automotive brake-by-wire builds feel, and it works. But
it makes the lever sensor mandatory, the feel synthetic, and the torque wholly
electronic. Given the owner's requirement that the lever feel like a brake,
**L is demoted to a curiosity unless the friction concepts fail at teardown.**

A system truth that applies to *every* concept, stated once: with dead
electronics a held carrier produces no braking torque (`T_sun = 0` ⟹ all
zero, §1 of the spec). The carrier lever is never the fail-safe — the wheel's
own brake is. "Brake feel" is ergonomics and control bandwidth, not safety.

### Rejected

- **Full disc + external caliper (original selection):** overkill per §4's
  duty numbers, and Path 1's exit problem stands regardless of what hangs on
  the tube.
- **Eddy-current:** torque → 0 at stall; cannot hold. Physics.
- **Hysteresis / magnetorheological:** proportional and wearless, but
  kilograms, money, and a powered controller — and MR pairs only with the
  demoted L philosophy anyway.
- **Viscous coupling:** self-regulating slip is cute physics, but the
  torque-slip characteristic is fixed by fill and geometry, not by the rider.
- **Wrap-spring:** binary with worse release behaviour than a pawl.
- **Braking through the gear mesh** (auxiliary pinion): no access — planets
  mesh only sun and ring.

### Shortlist and selection rule

| | **2a disc-to-spider** | 2e shoe-in-lip | 2b plate pack | 1a/1b external |
|---|---|---|---|---|
| Torque reaction | axle (existing arm) | axle | axle | new frame bracket |
| Visible additions | none (collar or axle nut) | none | none | tube, rotor/drum, caliper, bracket |
| Proportional feel | real | real | real, most authority | real |
| Axial cost | 10–13 mm — **the risk** | ~lip depth only | most | outside (but exit famine) |
| Custom parts | spider, ring, disc, plate, cam — all flat/turned | ring+lip, shoe, cam, spider | + plates | tube (precision), bracket |
| Depends on teardown | q5 gap | q3/q5 | q5 | q2, q4b — a gifted exit |

**Default: 2a**, falling back to **2e** if the axial gap is short, **2b** if
modulation wants more authority, and Path 1 only if the teardown reveals an
easy exit on an uncongested side. The internal family wins on the strength of
the geometry itself: the axle is the only static member in reach, it already
carries a torque arm, and reacting to it deletes the bracket, the exposed
rotating parts, and the frame interface in one move.

---

## 6. Geometry and integration

- **Torque reaction (Path 2):** through the axle into the **existing torque
  arm** — the same member that already reacts up to 45 N·m of drive torque now
  also sees up to ~54 N·m of braking reaction *in the opposite sense*. Size
  the arm and its frame interface for the reversal (fatigue, clearance take-up
  clunk), not just the magnitude. No other frame interface exists.
- **Axial budget:** the whole internal mechanism lives in the
  carrier-face-to-cover gap plus whatever a replacement (dished) cover buys.
  The gap measurement (q5) is the single number the concept choice hangs on.
  The 135 mm OLD is consumed by shell, cassette (~38 mm) and bearings — Path 2
  adds *nothing* to it except possibly the collar lever's few millimetres at
  the cover bore.
- **The spider:** bore broached/filed to the axle-flat profile, so it mounts
  with zero axle machining, located axially by the existing spacer stack. It
  carries the friction reaction and the actuation cam — it is the one part
  that must be stiff, and it is a flat laser-cut candidate.
- **Sealing:** the gear chamber is greased and splash-sealed by the cover. The
  collar variant slides at the cover bore (add a lip seal on the sleeve); the
  pushrod variant keeps the stock sealing untouched — a point in its favour.
- **Cassette / wheel-rotor side:** untouched in Path 2. The wheel's own disc
  brake, freehub and chainline see no change — criterion 4 by construction.
- **Actuation:** rider's existing rear lever and cable to the collar lever or
  axle-end cap. The optional lever sensor (spec §2) stays optional — feel and
  function are mechanical; the sensor only enriches the control law.

---

## 7. Teardown day — the question list

Every question has a mechanism decision waiting on it.

| # | Question | Decides |
|---|---|---|
| 1 | Clutch location: carrier-to-axle (Case B) or ring-to-shell (Case A)? | Whole conversion path |
| 2 | Which side cover exposes the carrier? Right (cassette) or left? | Adapter side, §6 flip |
| 3 | Carrier material, thickness, existing holes/keyways usable for an adapter | Adapter attachment: bolt pattern vs key vs press |
| 4 | Radial clearance between carrier OD and shell/side-cover bore | Max adapter/lip pass-through diameter |
| 4a | Carrier outboard face: usable annulus (ID/OD), flatness, attachment features | 2a/2b friction radius and the carrier-side ring design |
| 4b | Freehub bore ID over the axle + spacer stack drawing, both ends | Whether a Path 1 tube exit exists at all |
| 4c | Which axle end is free of the phase-cable channel; length and hardness for a 6 mm centre bore | Pushrod actuation (ii) vs collar (i) |
| 4d | Cover centre-bore construction: bearing size, seat, seal | Collar-sleeve design; dished-cover feasibility |
| 5 | Axial free space between carrier face and side-cover inner face | Whether the adapter exits through the cover or the cover is remade |
| 6 | Axle: diameter, flat width, thread spec, spacer stack drawing | Torque-arm plate, bracket, spacers |
| 7 | Side-cover fasteners (impact driver reported necessary) and gasket/seal type | Re-sealability, service (criterion 7) |
| 8 | Clutch engagement direction vs cassette drive direction | Confirms drive/coast/regen senses match §2 |
| 9 | Carrier runout when spun | Rotor mounting tolerance |

**Measurement kit:** digital calipers, thread gauges, phone camera on grid
paper for every stage of disassembly, dial indicator if available. Photograph
before every removal — the record is the CAD input.

---

## 8. CAD plan

**Tool: Fusion 360 (personal license, free) recommended** — parametric,
assemblies, STEP/DXF export for fabrication quotes, largest tutorial base.
FreeCAD 1.x is the open-source alternative if licensing independence matters
more than speed. Either exports what fabrication needs.

Modeling order — interfaces first, invention second:

1. **Axle + dropout + cassette envelope** from teardown measurements — the
   fixed world everything must fit inside.
2. **Carrier interface** — the as-measured carrier face, holes, keyways.
3. **The concept-specific custom parts**, per the §5 selection — for the 2a
   default: spider (flat, axle-flat bore), carrier ring (turned or flat +
   spacers), friction disc (waterjet from lined sheet), pressure plate, cam or
   ball-ramp, and the collar sleeve or axle-end pushrod. All flat or simple
   turned parts; nothing needs 5-axis anything.
4. **Torque-arm plate** — sized for reversing torque (§6), laser-cut steel.
   Path 2's only frame-side part, and it was already required for the motor.
5. **Stack-up check** against the 135 mm envelope with cassette and caliper
   modelled as vendor STEP files where available.
6. **3D-printed fit checks** of adapter and plate before any metal is cut.
7. **Fabricate:** adapter → local machine shop or JLCCNC (STEP + drawing);
   plate → SendCutSend/local laser (DXF); rotor and caliper → off-the-shelf.

Deliverables become the **RGX-2-2xx mechanical series** (200 = assembly,
201 = adapter, 202 = bracket/torque-arm plate) once the concept survives the
teardown.

## 9. Sequence and gates

```
now            → this study; order motor (BOM)
motor arrives  → teardown (§7 list) — GATE: Case B confirmed, carrier reachable
pass           → measure → CAD interfaces → adapter + plate design → print fit-check
               → fabricate → assemble → spin tests (drive/coast/regen senses)
fail (Case A)  → invasive-conversion feasibility decision before any further spend
```

The electrical bench phase (spec §11) runs in parallel — nothing above blocks
it except the teardown itself sharing the motor.

## 10. Sources

- [Grin — Freegen + Magic Drive product info](https://ebikes.ca/product-info/grin-kits/freegen-magic-drive.html) — 80 mm Al rotor on carrier hub, right-side caliper, all braking torque through the caliper
- [EBR forum — Grin Freegen regen on geared hub](https://forums.electricbikereview.com/threads/grin-freegen-regen-on-geared-hub.56102/)
- [Endless Sphere — ChargeBike variable-regen geared hub](https://endless-sphere.com/sphere/threads/new-geared-hubmotor-variable-regen-e-braking-system-that-still-freewheels-by-chargebike.122120/)
- [Bruce Teakle — Bafang BPM teardown and freewheel repair](http://bruceteakle.blogspot.com/2018/02/reversing-bafang-8fun-bpm-motor.html) — clutch grips the shaft; carrier-assembly clutch design
- [Endless Sphere — Bafang BPM specs, teardown, pics](https://endless-sphere.com/sphere/threads/bafang-bpm-geared-hub-specs-teardown-and-pics.51237/)
- [GreenBikeKit — Bafang hub motor clutch assemblies](https://www.greenbikekit.com/bafang-bldc-hub-motor-clutches.html) — clutch sold integral with carrier + nylon gears, axle-keyed
