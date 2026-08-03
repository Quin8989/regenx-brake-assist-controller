# ReGenX v2 — full system design

The definitive spec. Everything below is settled enough to build from; open items
are listed in §12 and none of them block firmware.

Scope: v2. `firmware/` is v1 and is not modified.

---

## 1. What it is

A rear geared hub whose **planet carrier** is fitted with a friction brake.
Normally the carrier freewheels and the bike coasts with zero drag. Braking holds
the carrier, restoring a torque path wheel → ring → planets → sun → rotor, and the
motor generates into a supercapacitor bank sized for a few boosts.

Controlled by regulating **carrier slip speed** — the one quantity that is both
directly derivable from two motor sensors and exactly proportional to energy lost
as heat.

---

## 2. Hardware inventory

| Item | Part | Status |
|---|---|---|
| MCU | Raspberry Pi Pico 2 (RP2350), MicroPython | locked |
| Motor controller | Flipsky Mini FSESC4.20 | locked, owned |
| Motor | Bafang G020, rear, single-stage ~5:1 | locked |
| Bank | 3 × GDCPH 16 V 20 F, series = **6.67 F** | locked, owned |
| Housekeeping pack | 4S Li-ion, ~15 Wh, 13.5–16.8 V usable | locked |
| Master switch | mechanical DPST or key, low current | locked |
| Contactor | normally-open, bank → controller | part open |
| Precharge | 4.7 Ω + silicon ultrafast diode | locked |
| Throttle | hall, 0–5 V | locked |
| Brake lever sensor | pressure or travel | optional |
| Display | SPI | part open |
| Logging | microSD, SPI | locked |
| Analog mux | 74HC4051 8:1 | locked |
| ADC buffer | rail-to-rail op-amp, e.g. MCP6002 | locked |
| Link | RS-422 transceiver pair, or CAN if broken out | locked |

---

## 3. Power architecture

### Two rails

```
PACK ──[MASTER SWITCH]──┬── buck → 5 V ── sensors, mux, display
                        ├── buck → 3.3 V ── MCU, SD
                        ├── contactor coil driver
                        └── 4.7 Ω ── diode ──┐
                                             │
BANK (3 modules, 6.67 F) ────────────────────┴──[CONTACTOR]── CONTROLLER ── MOTOR
```

**Master switch** breaks the pack feed only. Everything downstream dies with it,
including the contactor coil, so the controller disconnects from the bank without
sequencing. Off is a true 0 W. No sleep mode.

**Contactor is normally-open** so it drops out by itself when coil power goes.
A latching part would hold closed and leave the controller draining the bank —
the exact failure the switch exists to prevent.

**Precharge is passive and self-terminating.** Below pack voltage the diode
conducts and the bank charges; above it, the diode blocks. Nothing commands it,
nothing switches it off. It also holds a floor under the bank while riding, so a
large boost cannot pull the bus below the controller's start-up threshold.

### Voltages

| Node | Range |
|---|---|
| Bank | `V_LO` (~9–12 V, TBM) → **40 V ceiling** |
| Pack | 13.5 – 16.8 V (floor enforced; below 13.5 V the diode drop makes the precharge target unreachable) |
| Rails | 5 V, 3.3 V |

Bank ceiling is 40 V, not 43 V, for module-mismatch margin — see
`cap-bank-and-precharge.md`.

### Contactor coil

Pack is 13.5–16.8 V, so a nominal 12 V coil would run hot at the top of the
range. Either select a coil rated across that span, or feed the coil from a
regulated 12 V, or PWM after pull-in. **Decide with the part.**

---

## 4. Signal architecture

### Into the MCU

| Signal | Source | Conditioning | Notes |
|---|---|---|---|
| Hall A/B/C | Motor, tapped in parallel with controller | 10 k/20 k divider to 3.3 V | RP2350 is **not** 5 V tolerant |
| Shell speed | Motor white wire, 6 PPR | divider to 3.3 V | Confirm whether a thermistor shares this line on the G020 |
| Throttle | Hall throttle | mux ch0 | 0–5 V |
| Brake lever | pressure/travel | mux ch1 | optional |
| Bank node 1 | module 1 top | divider + buffer, mux ch2 | |
| Bank node 2 | module 2 top | divider + buffer, mux ch3 | |
| Bank node 3 | bank top (=40 V) | divider + buffer, mux ch4 | |
| Pack voltage | pack + | divider + buffer, mux ch5 | |
| Controller telemetry | RS-422 / CAN | transceiver | supervision only, not in the control loop |

**Six analog channels against ~4 usable ADC pins**, hence the 74HC4051 mux into a
single ADC input. Dividers use ~1 M so they draw tens of µA; source impedance
that high needs a rail-to-rail buffer before the ADC.

**Module voltages are measured cumulatively** from bank negative:
`V_mod1 = N1`, `V_mod2 = N2 − N1`, `V_mod3 = N3 − N2`. Same divider ratio on all
three keeps the design uniform at the cost of resolution on the lower nodes.

**Halls are powered by the controller**, so they are live only when the contactor
is closed. Acceptable: speed is not needed during PRECHARGE.

### Out of the MCU

| Signal | Destination | Notes |
|---|---|---|
| Current command | Controller, RS-422 / CAN | `SET_CURRENT`, signed |
| Contactor coil | MOSFET low-side + flyback diode | |
| Display | SPI | core 1 |
| SD card | SPI | core 1 |
| Mux select A/B/C | 74HC4051 | 3 GPIO |

---

## 5. Sensing design

### Speeds — the core measurement

Both speeds come from the motor. **Measure period between edges, not frequency**
— period gives good resolution at low speed, which is where braking ends.

| | Source | Edges per wheel rev | Update at 200 rpm |
|---|---|---|---|
| Motor | Hall phase A | pole pairs × 2 × ratio (~80–430, TBM) | fast |
| Wheel | Shell sensor | **6** | 20 Hz |

Both captured by **PIO state machines** timestamping edges — hardware, zero CPU.

**Wheel speed is the weak link.** 6 PPR means 50 ms between edges at 200 rpm, and
wheel speed can move ~3.5 % in that window under 0.5 g. Between edges, predict
forward using the last known deceleration rather than holding the stale value.

### Carrier slip

```
ω_carrier      = ( k·ω_wheel − ω_motor ) / (1 + k)          k = Z_ring/Z_sun ≈ 5
ω_carrier_free = k·ω_wheel / (1 + k)
slip           = ω_carrier / ω_carrier_free                  0 = locked, 1 = free
```

Carrier locked means `ω_motor = k·ω_wheel` exactly, so the difference is zero.

**This is a difference of two similar large numbers** — at 25 km/h both terms are
~1000 rpm and 10 % slip is a ~100 rpm gap. Sensor noise and staleness both land
directly on the small residual, which is why the slip setpoint is 10–15 % rather
than 5 %.

### Other inputs

- **Throttle**: linear map with deadband, N-sample debounce on validity.
- **Brake lever**: optional. Feedforward and intent, not the core loop — slip
  regulation alone gives proportional response.
- **Voltages**: oversampled, slow-filtered. Nothing here changes fast.

---

## 6. Control design — **deferred, not specified here**

> **Scope note.** The regen control law is deliberately *not* being designed up
> front. Determining it is the entire purpose of the simulator and scoring work,
> and specifying gains, setpoints or even loop structure before that would be
> guessing dressed as design.
>
> What follows is retained only as **constraints the hardware must not preclude** —
> the physical limits any strategy will run into. Treat as requirements on the
> hardware, not as a chosen algorithm.
>
> Hardware design lives in `hardware-design.md`.

### Constraints any strategy inherits

### 6.1 Regen — slip regulator

The rider's lever sets braking force **mechanically**. The regulator's only job is
to hold the carrier just barely slipping, so the band stays on the kinetic side of
its friction curve and the motor absorbs everything else.

```
slip_error = slip_measured − SLIP_SETPOINT
i_regen   -= Kp·slip_error + Ki·∫slip_error dt
```

**Note the sign.** More regen current means more reaction torque on the carrier,
which pushes it harder against the band, which produces *more* slip. So:

- slipping more than target → **reduce** current
- nearly locked → **increase** current until it just begins to slip

This is what AIMD approximated with probe-and-backoff. The regulator does it
continuously, proportionally, and from a directly measured quantity.

`P_heat / P_total = slip` exactly, so the setpoint *is* the loss fraction.
15 % slip recovers 85 %.

### 6.2 Current ceiling must scale with speed

Braking power peaks and then goes negative:

```
P_net = E·I − 1.5·R·I²      peak at I = E/(3R)
```

Past `E/(3R)` more current recovers less; past `E/(1.5R)` the bank is being
*drained* to brake the wheel. At road speed the peak is unreachable; at walking
pace it is not — with a 50 A ceiling, a regulator chasing slip down to a stop will
command straight past it.

```
I_MAX_REGEN(ω) = min( I_CEILING, k_safety · E(ω)/(3R) )        k_safety ≈ 0.8
```

`E(ω)` from measured kV. **This is not optional** — it is the difference between
recovering energy and spending it.

### 6.3 Uncontrolled region

When back-EMF exceeds bus voltage, current flows through the MOSFET body diodes
regardless of command. Regen becomes unmodulatable.

With the bank freshly precharged and the rider braking at speed, we start there.
It is self-limiting — uncontrolled regen charges the bank fast, and control
returns once bus > back-EMF.

**Firmware must detect it and stop integrating**, rather than winding up against a
loop it cannot win:

```
if E(ω) > V_bank:  hold integrator, flag UNCONTROLLED, command 0
```

### 6.4 Voltage taper

```
V < TAPER_START (37 V)         full regen
TAPER_START..TAPER_END (39 V)  linear scale to zero
V ≥ TAPER_END                  no regen
```

Above the ceiling regen simply stops. No dump resistor, no phase-short handover.
The rider's front brake covers the shortfall — which is a large part of why the
motor is on the rear.

### 6.5 Assist

```
i_assist = throttle_fraction · I_MAX_ASSIST
```

Clamped by the same speed-dependent ceiling and by available bus voltage. Assist
is inhibited below `V_LO`.

### 6.6 Loop rates

| Loop | Rate | Where |
|---|---|---|
| Control (slip / assist / limits) | **200 Hz** | core 0 |
| Command TX to controller | 200 Hz | core 0 |
| Telemetry RX | as it arrives | core 0 |
| Voltage / state supervision | 20 Hz | core 0 |
| Display | 5 Hz | core 1 |
| Logging | 20 Hz, batched | core 1 |

200 Hz is 10× the wheel-sensor rate and far faster than any mechanical time
constant here. The controller's own FOC loop runs at 20 kHz+; we only set its
setpoint.

**Core 0 runs control and nothing else.** Display and SD live on core 1 and are
structurally incapable of stalling the control loop — the v1 failure that caused
the UART overflows.

Use **measured elapsed time** for `dt`, never a nominal constant. Timers
accumulate (`last += period`) rather than resetting to `now`, so the period does
not drift.

---

## 7. State machine

Two operating states plus a fault overlay.

| State | Entry | Behaviour |
|---|---|---|
| **PRECHARGE** | bank < `V_LO` | Contactor open. No assist, no regen. Wait. |
| **READY** | bank ≥ `V_LO` + hysteresis | Contactor closed. Assist and regen available across the full range. |
| **FAULT** | any latched fault | Contactor open, zero commands. |

Precharge is passive — the MCU only watches. Hysteresis of a few volts prevents
chattering at the threshold.

### Faults

| Fault | Trigger | Latching | Action |
|---|---|---|---|
| `BANK_OV` | any module > `V_MOD_MAX`, or bank > 40 V | no, debounced | stop regen |
| `LINK_LOST` | no telemetry for 500 ms | no | zero commands |
| `THROTTLE_RANGE` | ADC out of range, N consecutive | no, debounced | inhibit assist |
| `CTRL_FAULT` | controller reports non-zero | no, debounced + dwell | zero commands |
| `INTERNAL` | uncaught exception | **yes** | contactor open |

**Every fault is debounced.** v1 tripped on single samples, which on a bike near
motor phases means constant false trips. Minimum dwell in FAULT before clearing,
so a flapping condition cannot oscillate the contactor.

Module overvoltage is checked **per module**, not on the bank total — three
modules in series with no inter-module balancing means the bank can look fine
while one module is over.

---

## 8. Firmware architecture

Target ~800 lines across 6 modules. Most of the reduction from v1's ~3200 is
deletion, not compression.

| Module | Responsibility | ~Lines |
|---|---|---|
| `main.py` | Scheduler, core split, watchdog | 80 |
| `link.py` | Controller transport: zero-copy ring buffer, table CRC, error counters | 180 |
| `sense.py` | PIO speed capture, mux ADC, debounced validity | 160 |
| `control.py` | Slip regulator, assist, speed-dependent ceiling, taper | 150 |
| `power.py` | State machine, contactor, fault debouncing | 110 |
| `ui.py` | Display, SD logging — **core 1 only** | 120 |

### Data flow

```
sense.py ──> State ──> control.py ──> link.py ──> controller
                │
             power.py (gates)
                │
                └──> ui.py (core 1, read-only)
```

One shared state object, written by exactly one owner per field. `ui.py` only
reads.

### Carried over from the v1 review

Sized `rxbuf` with `timeout=0`; index-based ring buffer with `memoryview` and no
slicing; table-driven CRC; live link error counters (CRC, framing, resync,
overflow) surfaced on the display; batched SD writes never in the control path;
`ticks_add` for all deadline arithmetic; small ints not strings for enums; a
firmware version stamp.

---

## 9. Parameters

One config module. Everything tunable, nothing magic inline.

```python
# Geometry
GEAR_K              = 5.0      # Z_ring/Z_sun — CONFIRM by tooth count
POLE_PAIRS          = ?        # CONFIRM
SHELL_PPR           = 6
WHEEL_RADIUS_M      = 0.33

# Motor electrical — MEASURE
KV_RPM_PER_V        = ?
R_PHASE_OHM         = ?

# Bank
CAP_BANK_F          = 6.67     # bank, NOT per module
V_CEILING           = 40.0
V_TAPER_START       = 37.0
V_TAPER_END         = 39.0
V_MOD_MAX           = 15.4     # per module, 20 % mismatch margin
V_LO                = ?        # controller start-up + margin — MEASURE

# Control
SLIP_SETPOINT       = 0.12     # 10–15 %; below that the sensor cannot resolve it
SLIP_KP, SLIP_KI    = ?, ?     # tune on bench
I_CEILING_A         = 40.0
I_MAX_ASSIST_A      = 40.0
CEILING_SAFETY      = 0.8      # fraction of E/(3R)

# Timing
CONTROL_HZ          = 200
SUPERVISE_HZ        = 20
DISPLAY_HZ          = 5
LOG_HZ              = 20
```

---

## 10. Failure behaviour

| Failure | Consequence | Mitigation |
|---|---|---|
| Pack flat | No precharge, no electronics | Bike still rides — it is a bicycle |
| Bank full | Regen tapers to zero | Front brake |
| Electronics dead mid-ride | **No regen braking on the rear** | Front brake. A large part of why the motor is on the rear |
| Link lost | Zero commands, controller times out | Controller has its own 0.5 s timeout |
| Shell sensor lost | Slip uncomputable | Inhibit regen, flag. Do not guess |
| One module over | Regen stops | Per-module sensing catches what bank-level cannot |
| Carrier slips fully | Band heat, no capture | Regulator reduces current; band is a normal brake |

**Bank stores ~5.3 kJ at 40 V even with the master switch off** — enough to
vaporise a tool tip. The master switch is a power switch, **not a service
disconnect**. Separate manual bleed, labelled.

---

## 11. Bring-up order

Each stage is independently verifiable, and each unblocks the next.

1. **Bench measurements** — G020 kV and R, controller start-up voltage, module
   capacitance, bank self-drain. No new parts needed; sets four parameters.
2. **Sensing only** — MCU + motor, no power. Verify hall and shell edge capture,
   compute `ω_carrier` on a hand-spun wheel with the carrier free. Slip should
   read ~1.0.
3. **Power path** — pack, switch, precharge, contactor. Verify precharge time and
   that the contactor drops out with the switch.
4. **Link** — commands and telemetry, error counters at zero under load.
5. **Open-loop regen** — fixed small current, wheel on a stand, confirm sign and
   that energy flows into the bank.
6. **Closed-loop slip** — tune Kp/Ki on the stand before the road.
7. **Road**.

---

## 12. Open items

None block firmware; all are parameters or parts.

| Item | Blocks | How resolved |
|---|---|---|
| Carrier access to a caliper | **Mechanical build** | G020 teardown |
| G020 kV, R, pole pairs, ratio | Current ceiling, back-EMF crossover | Bench |
| Controller start-up voltage | `V_LO`, precharge time | Bench |
| Module capacitance spread | 40 V vs 38 V ceiling | Bench |
| Bank self-drain | Balance-board question | Bench |
| CAN broken out on the Mini? | Link choice | Inspect board |
| Thermistor on the speed wire? | Sense conditioning | Inspect harness |
| Display part | — | Choose on merit; idle power no longer constrains |
| Contactor part | Coil drive design | Choose |
