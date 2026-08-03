# ReGenX — regenerative brake-assist for bicycles

A rear geared hub motor with a friction brake on its planet carrier. The carrier
freewheels when unbraked, so the bike coasts with zero drag; braking holds the
carrier, restoring a torque path from wheel to rotor, and the motor generates
into a supercapacitor bank sized for a few short boosts. No traction battery.

## Repository layout

| Directory | What it is |
|---|---|
| [`v2/`](v2/) | **The active project.** Clean-slate rebuild — design complete, bench phase next |
| [`v1-legacy/`](v1-legacy/) | The retired first prototype: firmware, simulator/scoring stack, tests, ride data. Kept for reference and mining — see its README for what is worth taking |

## Status — 2026-08-02

The v2 electrical design is **released and twice externally reviewed**:

- Specification **RGX-2-001 Rev C** and schematic **RGX-2-100 Rev D** —
  [`v2/design/`](v2/design/)
- Next phase is the **bench measurement schedule** (spec §11), gated on the
  G020 teardown and kV measurement. Firmware follows once measured values
  replace the assumed ones.

## Ground rules

- `v1-legacy/` is frozen. Nothing in v2 imports from it.
- The design documents in `v2/design/` are authoritative. The working notes in
  `v2/research/` are history and rationale; where they disagree with the
  released design, **the design wins**.
- Design decisions are only made by adding entries to
  [`v2/research/decisions.md`](v2/research/decisions.md), newest first.
