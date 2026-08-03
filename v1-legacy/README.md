# v1 — legacy prototype

The first working ReGenX build, retired in favour of the v2 clean-slate rebuild.
**Frozen: nothing here is modified, and nothing in v2 imports from it.** It is
kept because parts of it are worth mining and because its failures shaped v2.

The original project README is preserved as [`README-v1.md`](README-v1.md).

## Layout

| Path | Contents |
|---|---|
| `firmware/` | v1 MicroPython firmware (Pico), ~21 modules |
| `sim/` | Physics simulator, scoring, strategies, JAX tuner, neural teacher |
| `scripts/` | VESC provisioning/characterisation, PySR pipelines, WSL/CUDA tooling |
| `tests/` | Test suite (343 passing). Run from this directory: `python -m pytest -q` |
| `data/` | Simulation input traces (drill, phase2, ride, PySR imitation) |
| `logs/` | Ride logs and VESC config snapshots from the v1 bike |
| `pytest.ini`, `pyrightconfig.json` | Tooling config, paths relative to this directory |

## What is worth mining

**The simulator and scoring stack (`sim/`, `scripts/`) — the main asset.** The
v2 regen control law is deliberately unspecified in the hardware design because
it is the *output* of this scoring work. The physics model, strategy framework,
JAX tuner, neural teacher and PySR pipelines are the starting point for that
phase — slated for revamp, not rewrite. Note the peak-hold LispBM timebase was
found to be miscalibrated, and v2 replaces AIMD with slip-speed regulation, so
retune against v2's sensing model before trusting old scores.

**Ride logs and traces (`logs/`, `data/`)** — real ride data for replaying
against v2 control candidates.

**VESC tooling (`scripts/vesc_*.py`)** — provisioning and motor
characterisation scripts, directly reusable for the v2 bench phase.

**VESC config snapshots (`logs/*.bin`)** — the v1 controller configuration, a
reference when configuring v2's (battery current limit, voltage limits).

## Why v1 was retired — what v2 fixes

The VESC↔Pico link degraded badly when motor current flowed. Diagnosis found it
was primarily firmware, not EMI:

- unsized UART receive buffer
- a 70 ms blocking LCD re-init in the control path
- synchronous flash writes every 100 ms while moving
- O(n²) frame resync and pure-Python CRC
- zero link diagnostics

Separately, the electronics drained the supercap bank at idle (the controller's
~3 W), and the display glitched. v2's answers: one master switch in the bank
positive with a Li-ion keep-alive, a 9-pin hardware design with PIO capture,
and firmware constraints written into the spec (RGX-2-001 §10) before any code.
