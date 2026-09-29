# ReGenX v2

Clean-slate rebuild: carrier-braked (Freegen-style) regen on a **rear** geared
hub, buffered by supercapacitors, returning energy as a few short boosts.

## Layout

| Path | What it is | Authority |
|---|---|---|
| [`design/`](design/) | The **released design**: spec RGX-2-001 Rev D, drawing RGX-2-100 Rev E, BOM RGX-2-002, firmware architecture RGX-2-003 Rev C | **Authoritative** |
| [`firmware/`](firmware/) | The v2 firmware (MicroPython, per RGX-2-003 Rev C, ~845 lines). Host core green and cross-compiles for the RP2040; on-target gates FW-0..FW-2 pending a Pico | Implementation |
| [`tools/`](tools/) | Host side: `deploy.sh` (mpremote, WDT-safe), `decode_log.py` (ride logs → CSV with speed and slip), `A1-SETUP.md` (VESC Tool checklist, including motor direction) | — |
| [`tests/`](tests/) | CPython test suite for the firmware's pure core, driven through a Willis/clutch plant (`python -m pytest` from `v2/`). Runs in CI as its own job, with an `mpy-cross` build | — |
| [`research/`](research/) | Working analysis, decision log, sourcing notes — the *why* behind the design | History; where it disagrees with `design/`, the design wins |
| [`reviews/`](reviews/) | External review rounds, kept verbatim as received | Historical record |

## Status

Design phase **complete** — two external review rounds applied. Next is the
bench measurement schedule (spec §11), gated on:

1. **G020 teardown** — carrier access is the whole mechanical concept
2. **kV / phase resistance / pole pairs** — kV sets the back-EMF crossover
   above which the bank charges through the controller's body diodes

The firmware exists and is host-tested. Values that need the bench are marked
`[BENCH]` in `firmware/config.py`. The regen control law is deliberately out of
scope: it is the output of the scoring work in `../v1-legacy/sim/`, to be
revamped against v2's sensing model. `strategy.SlipRegulator` holds carrier
slip at a setpoint with the throttle as the override; its gains are `[BENCH]`
until the sim tunes them.

## Research notes index

| File | What it is |
|---|---|
| [`research/decisions.md`](research/decisions.md) | **Decision log**, newest first. Decisions are made by adding entries here |
| [`research/carrier-brake-mechanism.md`](research/carrier-brake-mechanism.md) | **Active study** — carrier-brake mechanism concept, teardown question list, CAD plan (spec §11 gate 1) |
| [`research/hardware-design.md`](research/hardware-design.md) | Component stack, pin map, conditioning, protection — the working draft behind the spec |
| [`research/power-architecture.md`](research/power-architecture.md) | One-rail keep-alive topology and its derivation |
| [`research/cap-bank-and-precharge.md`](research/cap-bank-and-precharge.md) | Bank sizing, module matching, precharge, boost-count analysis |
| [`research/design-parameters.md`](research/design-parameters.md) | The spec as equations + dependencies, so a part swap recomputes rather than invalidates |
| [`research/motor-selection.md`](research/motor-selection.md) | Why the Bafang G020; shell-sensor resolution |
| [`research/controller-requirements.md`](research/controller-requirements.md) / [`controller-candidates.md`](research/controller-candidates.md) | Controller spec and shopping; why the owned Mini FSESC4.20 was retained |
| [`research/energy-and-idle-budget.md`](research/energy-and-idle-budget.md) | Cap sizing, idle-drain analysis, braking-when-full |
| [`research/system-overview.md`](research/system-overview.md) / [`system-design.md`](research/system-design.md) | System narrative and spec. State-machine sections predate the contactor deletion — superseded where marked |
| `research/archive/` | Fully superseded studies, kept for the record |

## Conventions — used in every document

Planetary members and signs:

- **sun** = motor rotor, **ring** = wheel shell, **carrier** = braked member
  (one-way clutch to ground + friction brake)
- `k = Z_ring / Z_sun ≈ 4.7`; **positive = forward wheel motion**

```
ω_sun     = (1+k)·ω_carrier − k·ω_ring
ω_carrier = (ω_sun + k·ω_ring) / (1+k)
T_sun : T_ring : T_carrier = 1 : k : −(1+k)     (external torques, sum = 0)
```

Two consequences most of the design falls out of:

1. **Sun and ring counter-rotate** when the carrier is held — the rotor turns
   opposite to the wheel in both assist and regen.
2. **If the sun carries no torque, nothing carries torque.**
   `T_c = −(1+k)·T_s`, so a carrier brake with a free motor transmits nothing.

Positive current into the bank is charging. Carrier slip fraction equals loss
fraction: `P_heat / P_total = s`.
