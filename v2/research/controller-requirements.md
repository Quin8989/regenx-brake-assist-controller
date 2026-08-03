# Motor controller — requirements

What we need before going shopping. Written first deliberately, so candidates get
judged against a spec rather than the spec getting bent around whatever turns up.

Motor: Bafang G020, rear, single-stage ~5:1, 6 PPR shell sensor, Bafang hall
mapping.

---

## 1. The reframe that shrinks the requirement

The obvious architecture has the controller read the halls, compute RPM, report
it over the link, and the MCU does slip control from that telemetry. That makes
the link latency part of the control loop.

**Better: the MCU taps the hall lines directly, in parallel with the controller.**
Hall outputs are logic-level and already pulled up, so a second reader does not
disturb them. The RP2350's PIO counts both the halls and the 6 PPR shell sensor
in hardware, at zero CPU cost.

Consequences:

- Slip is computed **entirely locally** from two signals the MCU owns. No link
  latency, no telemetry rate limit, no dependency on controller firmware.
- The controller's job collapses to: **accept a commanded current, quickly and
  reliably.** Everything else is ours.
- Telemetry from the controller becomes useful-but-not-critical — bus voltage,
  actual current, temperature, faults. Wanted for supervision and logging, not in
  the control path.

This removes most of what made v1's link fragile, and it widens the candidate
list considerably.

---

## 2. Hard requirements

### Voltage

| | Requirement | Why |
|---|---|---|
| **Minimum start-up voltage** | **as low as possible — this is a headline spec** | Sets `V_lo`, and therefore precharge time and pack drain. 8 V vs 12 V is 27 s vs 61 s per precharge |
| Minimum running voltage | ≤ 10 V | Bank swings low by design |
| Low-voltage cutoff | **settable very low, or disable-able** | Most e-bike controllers hard-cut around 30 V for a 36 V battery. That would make the design impossible |
| Maximum | ≥ 50 V, **60 V preferred** | 40 V ceiling plus regen transient across ~60 mΩ bank ESR |

The bank is not a battery. It swings from `V_lo` to 40 V in normal use, and one
boost can pull it 40 → 33 V. **The controller must treat a moving supply as
normal**, not trip, not oscillate its power limiting, not assume a battery
sitting at nominal.

### Current

| | Requirement |
|---|---|
| Phase current | ≥ 50 A continuous |
| Peak | ≥ 100 A |
| Power | ~1500 W |
| **Regen current** | **Commandable and proportional**, not on/off |

### Control

- **Current (torque) control mode.** Not duty, not speed. We command amps.
- **Sensored FOC.** We brake down to low speed, where sensorless collapses.
- **Bafang hall mapping**, or programmable mapping.

### Interface — either path is acceptable

The requirement is **proportional control of accel and regen from the MCU**. Two
ways to satisfy it, and both are in scope:

**Path A — digital current command.** Real-time current command at ≥ 100 Hz over
CAN or UART, with an open documented protocol. VESC-style.

**Path B — analog command.** MCU drives the controller's throttle input and a
**proportional regen input** with two analog voltages (DAC, or PWM plus a filter).
This is how a stock e-bike controller is normally driven, and it works.

Path B is not the poor relation it looks like. A slow-moving, low-pass-filtered
analog level is arguably **more EMI-tolerant than a serial frame**, which is lost
entirely if corrupted. It also removes the protocol, the CRC and the framing from
the control path.

Path B's real costs:
- **Ground offset.** MCU and controller must share a reference that is not
  carrying motor current, or the command voltage shifts under load. Needs
  deliberate star grounding, or an isolated command path.
- **No acknowledgement.** Nothing confirms the command landed.
- Regen must be a **proportional input**, not a brake cutoff switch. Most e-bike
  controllers have brake inputs that *kill* power rather than command regen —
  that is not sufficient.

CAN is a **preference, not a requirement**. Telemetry — bus voltage, current,
temperature, faults — is wanted for supervision and logging on either path, but
is not in the control loop.

### Regen into capacitors

- Configurable regen current limit
- Configurable regen voltage ceiling with **taper**, not a hard fault
- Must tolerate a fast-rising bus. At 40 A into 6.67 F that is ~6 V/s

---

## 3. What it does *not* need

Worth stating, because these are what most e-bike controllers sell on:

- Throttle input — the MCU owns the throttle
- Brake lever input — MCU owns it
- PAS / cadence sensor input
- Display protocol / UART display
- Battery BMS communication
- Speed limiting, legal-mode presets
- Its own speed sensor input — the MCU reads the 6 PPR wire directly
- Low idle draw — the contactor handles that

---

## 4. Nice to have

- Scriptable or extensible on-controller logic
- Motor temperature sensing (G020 thermistor, if fitted)
- Small and weather-resistant
- Configurable without proprietary tooling

---

## 5. Deal-breakers

1. **Fixed low-voltage cutoff above ~15 V** — makes the design impossible
2. **Regen that is on/off only**, or a brake input that cuts power rather than
   commanding proportional regen
3. **Sensorless only** — we brake to low speed
4. No way to command accel *and* regen from the MCU by either path above

A closed protocol is no longer disqualifying on its own, provided analog
throttle and proportional regen inputs exist.

---

## 6. Open questions for the search

- Does any candidate publish a **minimum start-up voltage**? Rarely specified,
  and it directly sets our precharge budget. May need bench measurement.
- CAN availability at this size and price.
- Whether regen behaviour into a capacitor bank has ever been characterised, or
  whether everything assumes a battery.
- Whether the G020's thermistor shares a wire with the speed signal (as the
  Shengyi SX does) and how that is separated.
