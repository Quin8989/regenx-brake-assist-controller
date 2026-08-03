# ReGenX v2 — released design

Bicycle regenerative brake-assist. Carrier-brake mechanism, supercapacitor
store, no traction battery. **This directory is authoritative** — where any
other document disagrees with these files, these win.

## Contents

| File | Contents |
|---|---|
| [`RGX-2-001-SPEC.md`](RGX-2-001-SPEC.md) | Specification, **Rev C**. Parameters, governing equations, computed performance, component and interface schedules, protection coordination, unverified parameters, measurement schedule |
| [`RGX-2-100-schematic.html`](RGX-2-100-schematic.html) | Drawing RGX-2-100, **Rev D**. Three sheets, parts lists, notes, interface schedule, revision history. Open in a browser |
| [`planetary-freegen-sim.html`](planetary-freegen-sim.html) | Interactive planetary model: torque directions for assist, coast and regen. Open in a browser |
| `regenx-v2-design-package.zip` | The four files above, bundled for sending to a reviewer |

## Reading order

1. Spec §1 — system definition and planetary kinematics
2. `planetary-freegen-sim.html` — the mechanism
3. Spec §3–§4 — equations and computed performance
4. `RGX-2-100-schematic.html` — the as-drawn circuit
5. Spec §11 — what has not been measured, and the ordered measurement schedule

## Scope

Hardware only. The regen control law is deliberately unspecified — it is the
output of separate simulation/scoring work. Spec §11 lists every unverified
parameter; §4 figures that depend on them are provisional until the bench
phase replaces them with measurements.

## Drawing conventions

ASME Y14.44 reference designators, ASME Y14.100 title blocks. Semiconductor
symbols per IEEE 315; resistors and fuses are IEC-style rectangles — mixed
convention, stated so it is a decision and not an accident.

Zone grid A–D / 1–4 on both edges, dividing the inner drawing frame equally,
origin top-left, columns numbered left to right — a stated deviation from the
ASME sheet convention, retained because every zone reference in both documents
is written against it. Junction dots mark connections; crossings without dots
are not connected. Signals crossing a sheet boundary carry a net name. Flags
reference the notes block on their own sheet.

Positive current into the bank is charging. Positive motor torque accelerates
the wheel. Carrier slip is `ω_carrier` relative to the freewheel direction.
