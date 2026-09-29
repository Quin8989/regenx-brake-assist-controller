# ReGenX v2 — Firmware Architecture

**Document** RGX-2-003 **Rev D** · against RGX-2-001 Rev D / RGX-2-100 Rev E /
RGX-2-002 Rev A · 2026-09-29

Implemented in `v2/firmware/` (host tests in `v2/tests/`). The regen control
law remains deferred; this document defines the machine it plugs into.

## Rev C and D amendments (2026-09-29)

Rev C came out of `reviews/firmware-1/`. Rev D, the same day, records the
owner's simplifications: no ride log, one screen that shows problems only when
they occur, one telemetry reply per tick, no observer stage. Rows marked
(Rev D) are new or changed in Rev D. These supersede the Rev B text below
wherever they conflict. Rationale is in `research/decisions.md`, 2026-09-29.

| Topic | Rule | Supersedes |
|---|---|---|
| Sign | A1 is provisioned so +current drives the wheel forward (`tools/A1-SETUP.md` item 4). With the carrier held the rotor then turns at +k·wheel: ERPM ≥ 0 whenever torque flows, and s = 1 − ERPM/(pp·k·ω_wheel). No `DIR_SIGN`. | D10 `DIR_SIGN`, B-2 |
| States | RUN iff `LINK_RECOVER_FRAMES` consecutive clean telemetry frames (fault = 0); else LIMP at 0 A, auto-recovering. (Rev D) The firmware version is never requested. | §3 States, INIT, FW handshake |
| Throttle | Outside 0.20–0.85 it reads 0 (assist stops, regen untouched; spec §10.3). Assist arms only after the throttle has read idle, at power-on and after any out-of-window reading. Mapping uses measured idle/full, with a deadband. | §3 "throttle window fault → LIMP" |
| Envelope | The slew limits only torque build-up (2 A/tick away from zero); any reduction or reversal through zero is immediate. Then clamp. The clamps only shrink \|i\|, so they act in the same tick. Regen and assist caps hold A1's terminal in [9, 39] V via v_oc = v_in + i_in·R_BANK. No regen below ~3 km/h. | §3 envelope: 38→40 V taper, crossover guard |
| Time | Control never reads a clock. Timeouts count ticks; the slew and the PI step are per tick. | T9 `dt` measured |
| Snapshot | One `array('f')`, core 0 the only writer. A reader may mix two consecutive ticks, which is harmless for the display. (Rev D) It holds only what the display reads. | D12 seqlock |
| Deleted | Live k cross-check, RTT metric, full COMM_GET_VALUES fallback, `gc.disable()`, boot guard. (Rev D) The ride log, observer mode, the firmware-version request, the VSYS reading, the worst-tick metric, and the viper CRC: the plain table CRC costs well under 1 ms of the 10 ms tick, and a late tick shows on the display. | D10 cross-check, D5 (a), D11 viper, gate FW-1 timing |
| Parser | (Rev D) Only the one reply is accepted: start byte, LEN = 18, `COMM_GET_VALUES_SELECTIVE`, CRC, end byte. Anything else is skipped a byte at a time; a start that fails its CRC or end byte counts as bad. Every frame being the same length, a start still waiting for bytes can never be followed by a complete frame, so scanning stops there and keeps the tail. | D11 "O(1) resync, no backtracking"; Rev C rescan with LEN ≤ 80 |
| Telemetry | (Rev D) One request every tick, mask `0x8189`: FET temperature, `i_in`, ERPM, `v_in`, fault (18-byte reply). About 20 % of each wire direction. Motor current is not requested: nothing reads it. | D5 50 Hz poll, 1 Hz temperature |
| Temperature | (Rev D) A1's FET temperature arrives with every reply. The display flags it above `TEMP_HOT` (80 °C, just under A1's own 85 °C current cutback) or below `TEMP_COLD` (−10 °C, where the bank's resistance exceeds `R_BANK`). Display only: A1 protects itself. | D5 1 Hz temperature |
| Logging | (Rev D) None (owner). v1's RAM logging could not hold data at a useful resolution, and its buffer caused RAM trouble. Bench data comes from VESC Tool over USB to A1. | D13; Rev C flush rules; `tools/decode_log.py` |
| Regen law | One law, built into `control.py` as `request()`: velocity-form PI on carrier slip toward `SLIP_SET` (`SLIP_KP`, `SLIP_KI` [BENCH], tuned in the sim). While the rider holds the carrier, regen grows until the brake just slips, so braking torque follows the squeeze. Any throttle ends regen and commands assist. No swappable strategy module, no DEAD state: a fixed PI on bounded inputs cannot raise or return NaN. | D10 strategy contract; "control law deferred"; §3 strategy-exception rule |
| Lever sensor | None (owner decision 2026-09-29). Rider intent is the carrier slip itself; the throttle overrides regen. Accepted: while the throttle is held the carrier lever cannot brake (the clutch already holds the carrier), and regen onset is set by 6 PPR slip sensing (D9 "slip-only" column). | D9 recommendation (GP14) |
| C-0 | (Rev D) Dropped (owner). Without a log it would only prove the drivetrain, which the bench does from VESC Tool with the wheel off the ground. The Pico always commands current. | D15, gate C-0, B-10, `SEND_CURRENT` |
| UI | (Rev D) One screen: speed, bank voltage and commanded current, then one line per problem while it lasts, most serious first: `NO LINK` or `VESC FAULT n`, `HOT`/`COLD`, `BAD FRAMES`, `LATE TICKS`, `SCREEN ERR`. The last three are counts since power-on. | D14 three pages; Rev C RIDE and status pages |
| VSYS | (Rev D) Not read. Drawing sheet 3 NOTE 7 and spec §10 item 6 ("VSYS logged via ADC3") are queued for withdrawal at the next drawing and spec revisions. BEC sag is characterised on the bench instead (spec §11 item 10). | Sheet 3 NOTE 7 |

**Rev B** restructures the document as a **decision register**: every choice
lists the alternatives considered, the numbers that decided it, and the
measurement that would overturn it. Rev A errors corrected: it claimed core 1
could block freely (false — RP2040 executes from XIP flash, so **a flash write
stalls both cores**, §D13); it inherited UART-at-115200 and poll-based
telemetry from v1 without examination (now validated on their own merits,
§D3–D5); it ignored the lever-sensor fast path even though lever→torque
latency was a founding project goal (§D9); and it treated MicroPython
dual-core as free (it has a documented instability history that demands
version pinning and a soak gate, §D2).

---

## 1. Fixed constraints (not re-decided here)

| Constraint | Source |
|---|---|
| MicroPython | Owner decision (validated for feasibility in D1, not re-litigated) |
| Pico / RP2040, 9 pins as drawn | RGX-2-100 Rev D |
| A1 = Flipsky Mini FSESC4.20, UART broken out, 5 V BEC | owned hardware |
| Spec §10 items 1–10 | RGX-2-001 Rev C |
| Control law deferred to scoring work | owner decision |

Everything else below was decided fresh, v1 precedent explicitly not accepted
as evidence.

---

## 2. Decision register

### D1 — Runtime: MicroPython, validated, with a measured escape hatch

**Alternatives:** C (pico-sdk), Rust (rp-hal), CircuitPython, MicroPython.

**Analysis.** The workload is small: at 100 Hz the loop does ~50 float ops of
kinematics, a ≤70 B parse, and a ≤30 B build. Interpreted MicroPython executes
roughly 100–300 k simple ops/s; the pure-Python risk areas are byte-wise
loops (CRC: 8 shift-ops × 70 B ≈ 5–7 ms — v1's measured disease) and GC pauses.
Both have in-language cures: `@micropython.viper` compiles typed integer code
to native Thumb (documented 10–50× on byte loops → CRC in µs), and a
*scheduled* `gc.collect()` on a pre-allocated heap bounds pauses to single
milliseconds. C/Rust are strictly faster but forfeit the owner's iteration
speed on a control law that will be retuned constantly.

**Decision:** MicroPython, **exact version pinned in the repo** (a specific
tested release, chosen at FW-0 after the dual-core soak — forum evidence shows
thread stability varies by release, e.g. regressions reported in v1.23 that
v1.22.x lacked). Viper for CRC + parser inner loop; `@micropython.native` for
control math.

**Overturned by:** gate FW-2 measuring worst-case tick > 6 ms or GC pause
> 5 ms after optimization. Escape: move parser+CRC into a frozen native module
(same architecture, one module swapped) before abandoning the runtime.

### D2 — Concurrency: dual-core, treated as a risk to be retired, with a designed single-core fallback

**Alternatives:** (a) single-core cooperative; (b) single-core + uasyncio;
(c) dual-core `_thread`.

**Analysis.** v1 died because slow I/O (display, flash) shared the control
thread — the countermeasure must make that structurally impossible, which
favours (c): core 0 control, core 1 display/log/bench. But MicroPython's rp2
port runs threads **without a GIL** (true SMP, one thread per core) and has a
documented history of dual-core memory-corruption issues (GitHub #7124 class)
and release-to-release regressions. Rev A treated this as free; it is not.

(b) is rejected outright for the control loop: cooperative async gives no
bound on who holds the scheduler — it is v1's failure mode with nicer syntax.

**Decision:** dual-core, hardened: pinned MicroPython release; core 1 code
style constrained (all objects pre-created at init, no allocation in steady
state, so cross-core allocator contention ≈ 0); the only shared structure is
the seqlock snapshot (D12); **gate FW-0 is a 24 h dual-core soak** (loop +
display + logger churn) that must pass before any feature lands on top.

**Fallback, fully specified now:** single-core time-sliced UI — display
refresh drops to 1 Hz, each frame sent as 8 × 128 B I²C chunks (~3.3 ms each)
placed in the loop's ≥6 ms slack, logger unchanged (RAM ring), bench stream
rate-limited. Same modules, `ui.py` driven by the core-0 scheduler instead of
a thread. Chosen automatically if FW-0 fails on two pinned releases.

**Overturned by:** FW-0 soak failure → fallback above.

### D3 — Physical link: UART retained on evidence; CAN documented as the escape

**Alternatives:** (a) UART (drawn); (b) CAN via can2040 (PIO software CAN) +
SN65HVD230 transceiver; (c) PPM/ADC analog command paths.

**Analysis.** v1 blamed EMI; the postmortem found firmware (64 B buffer +
multi-ms stalls). The physical layer was convicted without evidence.
Rev D's drawing already hardens the drawn UART: R3/R4 1 kΩ series, twisted
pair, single-point ground. CAN would be genuinely superior physics
(differential, arbitration, VESC pushes `CAN_PACKET_STATUS` unpolled) — but
costs: a transceiver the drawing doesn't have, a software CAN stack (can2040)
consuming a PIO block + significant IRQ load, and **unconfirmed hardware**:
the Mini's spec sheet lists CAN among supported interfaces, but whether this
board populates transceiver + connector is exactly spec §12's open item.
(c) throws away telemetry and computed regen — not a control channel, see D15
for its real use.

**Decision:** UART, with link-health counters (§6.5) as the instrument that
finally measures the wire instead of guessing. CAN is the *designed escape*:
if bench/ride data shows CRC-error bursts correlated with phase current
despite healthy buffers, the migration is transceiver + can2040 + `vesc.py`
transport swap — no architecture change. Bench item **B-11: probe the Mini
for CAN transceiver/pads.**

**Overturned by:** link-health data, not anecdote.

### D4 — Baud: 115200, justified by budget, not inheritance

**Alternatives:** 115200 / 230400 / 460800 (app-configurable in VESC Tool).

**Analysis.** Wire cost at 115200 (≈11.5 B/ms): worst tick TX 30 B + RX 30 B
≈ 5.2 ms spread across two 10 ms ticks → ~26 % duty. Latency share of baud:
one response frame = 2.6 ms, against a lever→torque budget dominated by
40–130 ms of sensing (D9). Quadrupling baud buys ~2 ms end-to-end — noise —
while shrinking per-bit EMI margin next to phase wires the project was burned
by (even if the conviction was wrong, the margin is free).

**Decision:** 115200. Revisit only if D5's push upgrade plus new sensors ever
push wire duty past ~60 %.

### D5 — Telemetry: poll `GET_VALUES_SELECTIVE`; push is the upgrade path, not the default

**Alternatives:** (a) full `COMM_GET_VALUES` poll (~70 B); (b)
`COMM_GET_VALUES_SELECTIVE` poll (~30 B with our 5-field mask); (c) VESC-side
push via LispBM script emitting custom frames.

**Facts established:** SELECTIVE has existed since immediately after FW 3.41;
Flipsky ships FSESCs with FW 5.2 — so (b) needs no firmware update. (c) needs
FW 6.x (LispBM) — possible on 4.12-class hardware but a bigger provisioning
step (D6), and v1's only LispBM experience shipped a miscalibrated timebase
that poisoned a whole tuning campaign.

**Analysis.** (b) halves (a)'s wire and parse cost for identical information.
(c) saves only the ~8 B request and one scheduling hop (~1–2 ms fresher data)
at the price of custom code on *both* sides of the link and a VESC-side
failure mode the health counters can't see into.

**Decision:** (b), mask = ERPM, `v_in`, avg input current, avg motor current,
fault code; FET temp polled at 1 Hz. (a) retained in the parser as trivial
fallback. (c) documented as the measured upgrade: adopt only if RTT/freshness
data from riding shows the request hop matters.

### D6 — A1 firmware provisioning: stay on the shipped 5.x line

**Alternatives:** (a) leave shipped FW (5.2-class); (b) update to newest
4.12-class 6.x build.

**Analysis.** Everything this architecture needs exists in 5.2 (SELECTIVE,
UART app, timeout, current control). 6.x adds LispBM (not needed per D5) at
the cost of the classic clone-flashing hazards (build/DRV mismatch bricks
motor detection) that Flipsky's own compatibility guidance warns about.
Fewer variables during bring-up beats features we don't use.

**Decision:** keep shipped major version; record the exact FW tuple at every
INIT handshake into the log header; update only within Flipsky-blessed builds
and only for cause. A1 config checklist: battery limit 40 A, max input 40 V,
motor-temp sensing off (spec §10), UART app 115200, **timeout 200 ms,
timeout-brake-current 0**.

### D7 — Command/keepalive scheme

`SET_CURRENT` every 10 ms tick (100 Hz) — the command stream *is* the
keepalive; telemetry request every other tick (50 Hz). A1's 200 ms app timeout
then guarantees: dead Pico, wedged loop, cut harness → motor current released
in ≤ 0.2 s with no cooperation from our side. Our side mirrors it: telemetry
silence > 250 ms → LIMP (zero commands, keep parsing, auto-recover after 10
clean frames). Two independent layers, either sufficient.

### D8 — Loop rate: 100 Hz, and honesty about what it buys

50 Hz would satisfy the keepalive (10× margin) and the sensors (D9). 100 Hz is
kept because it costs < 40 % of one core and buys: 20× keepalive margin, 10 ms
command granularity for the slew limiter, and headroom for a future
lever-sensor fast path (D9) whose whole point is millisecond reaction. The
loop rate is explicitly **not** the latency lever — the sensor cadence is.

### D9 — Latency budget, and the one change that actually shortens it

Lever→felt-torque, worst cases at 20 km/h:

| Stage | Slip-only sensing | With lever sensor |
|---|---|---|
| Rider input detectable | 63–126 ms (1–2 wheel pulses to see slip onset) | **≤ 1 ms** (digital edge) |
| Telemetry age (ERPM) | ≤ 20 ms | ≤ 20 ms |
| Control tick | ≤ 10 ms | ≤ 10 ms |
| Command wire + VESC ramp | ~5 ms | ~5 ms |
| **Total** | **~100–160 ms** | **~35 ms** |

The founding goal was minimal lever→torque delay; the table shows the 6 PPR
shell sensor — not any protocol or loop choice — is the bottleneck, and that a
**brake-lever sensor collapses the budget by ~4×**: it announces intent
instantly, regen ramps immediately, and slip regulation then *trims* torque as
its measurement arrives.

**Decision:** architecture treats the lever input as a first-class strategy
input (`brake` in the contract, D10), present-or-absent at runtime.
**Recommendation to owner (hardware delta, not taken unilaterally):** promote
the spec's optional brake sensor to fitted — one hall/reed switch or the
common e-bike cutoff-style lever sensor on a spare GPIO (e.g. GP14 + pull-up),
one wire, queued into drawing Rev E alongside the Higo change.

### D10 — Kinematics, signs, and a free self-check

```
ω_motor   = ERPM / POLE_PAIRS × DIR_SIGN        # mech rpm, forward +
ω_carrier = (ω_motor + k·ω_wheel) / (1 + k)     # Willis (spec §1)
ω_free    = k·ω_wheel / (1 + k)
s         = clamp(ω_carrier / ω_free, 0, 1)     # 1 freewheeling · 0 held
```

Strategy contract (final):

```python
class Strategy:
    def update(self, s, omega_wheel, v_bank, throttle, brake,
               i_motor, dt) -> float:
        """Commanded motor current, A. + assist, − regen. Pure, no alloc."""
```

`DIR_SIGN` is bench-set; a wrong guess is *visible by construction* (slip pegs
at 1 during braking instead of tracking).

**Free cross-check (new in Rev B):** during assist the clutch grounds the
carrier (`ω_carrier = 0`), so `|ω_motor| = k·|ω_wheel|` exactly (the rotor
counter-rotates; Willis with `ω_c = 0`). Every assist episode therefore
calibrates `k` and validates the shell sensor live; drift outside ±5 % raises
a SENSOR flag on the LINK page. Note the corollary: **s = 0 during assist too**
(carrier held by the clutch instead of the brake) — slip is only meaningful
with mode context, which is why the strategy receives `throttle` and `brake`
alongside `s`, and the envelope arbitrates the mode.

### D11 — Parser and CRC data structures

**Alternatives:** pure Python (v1 — convicted), viper table CRC + state-machine
parser, RP2040 DMA sniffer CRC.

The DMA sniffer computes CRC-16-CCITT in hardware but wants contiguous DMA'd
blocks; VESC frames arrive as an unsynchronized byte stream where CRC runs
over a mid-frame slice — restructuring RX around DMA to save microseconds the
viper version already achieves is complexity without payoff.

**Decision:** `readinto` a fixed 1 KB buffer; explicit
`HUNT → LEN → PAYLOAD → CRC → END` state machine, O(1) resync (bad byte →
HUNT at next byte, a counter increments); viper CRC16 over a 512 B
`bytes` table. Frame budget < 200 µs, proven at gate FW-1 before anything
stacks on it. No allocation after init anywhere on the RX path.

### D12 — Cross-core snapshot: seqlock, validated against the platform

Single writer (core 0), single reader (core 1), two pre-allocated
`array('f')` + version counter: writer bumps (odd) → writes spare → bumps
(even) → flips index; reader copies then re-checks, retries on mismatch.
Validity argument, not vibes: Cortex-M0+ is in-order with no data cache, so
store order is visible order; `array('f')` element stores are single aligned
32-bit C stores (no torn floats); the counter protocol catches any interleave.
No lock core 0 can ever block on. ~30 floats + the health-counter block.

### D13 — Logging, with the XIP correction

**Rev A error, corrected:** RP2040 executes from external flash (XIP). A flash
erase/program suspends XIP — **both cores stall**, ~45 ms per 4 KB sector
erase. "Core 1 flushes without touching core 0" was false.

**Decision:** unchanged mechanism, corrected justification: binary ring in RAM
(24 B records via `struct.pack_into`, 96 KB — 6.8 min at 10 Hz ride rate,
rate drops to 1 Hz at standstill), flash flush **only** at verified standstill
(wheel = 0 ∧ throttle idle > 3 s) — which is now understood as protecting
*core 0* from erase stalls, not just avoiding writes-while-moving. During a
flush the loop keeps its WDT margin (2 s ≫ 45 ms) and deadline misses at
standstill are counted and excused. USB bench mode streams instead and never
touches flash. Wear: ≤ 96 KB/ride into a ≥ 1 MB littlefs region with wear
leveling → decades.

### D14 — Display

SSD1306 128×64 I²C @ 400 kHz on core 1 (or the D2 fallback slot), framebuf,
5 Hz. Pages: RIDE (speed, bank V, power, slip bar), LINK (health counters,
RTT, deadline misses, GC max pause), SYSTEM (VSYS, temps, state, last fault,
FW tuple). I²C errors: count, lazy re-init, never scheduled, never on core 0.

### D15 — Commissioning mode C-0: the VESC's ADC app as scaffolding (new)

Before any Pico control: configure A1's **"ADC and UART"** app, throttle wired
temporarily to A1's ADC input — the bike rides as a plain e-bike on VESC-native
throttle with the Pico as a **read-only observer** streaming telemetry + wheel
speed. This: (1) proves motor/halls/battery-limits with zero new code in the
loop; (2) collects real ride corpora for the scoring sim *before* the control
law exists; (3) burns in the link and sensors. Then throttle moves to GP26 and
the app flips to UART for FW-5+. The ADC/UART *hybrid* as a permanent
architecture (VESC-native assist + UART regen) was considered and rejected:
two writers to one current setpoint with no defined arbitration, and the
assist latency it would save is ~15 ms on the path where 15 ms is
imperceptible (acceleration), none on the braking path where latency matters.

---

## 3. Architecture (result of D1–D15)

```
CORE 0 — control (100 Hz, allocation-free, fenced; WDT 2 s)
┌────────────────────────────────────────────────────────────┐
│ sensors.py     PIO wheel capture · throttle · brake · VSYS │
│ vesc.py        viper CRC/parser · SELECTIVE poll · TX      │
│ kinematics.py  Willis · slip · assist-time k cross-check   │
│ control.py     INIT/RUN/LIMP · safety envelope · slew      │
│ strategy.py    deferred law behind the D10 contract        │
└──────────────┬─────────────────────────────────────────────┘
               │ seqlock snapshot (D12)
┌──────────────▼─────────────────────────────────────────────┐
│ CORE 1 — ui.py: SSD1306 5 Hz · RAM ring logger             │
│   standstill-only flash flush (XIP-aware) · USB bench      │
│   [D2 fallback: same module, time-sliced on core 0]        │
└────────────────────────────────────────────────────────────┘
```

Module map, tick budget, and rules are unchanged from Rev A except as amended
above: 8 flat modules ≈ 1 150 lines; per-tick budget ≤ 4 ms worst case against
10 ms; no allocation, sleep, print, file I/O, or `ui` import on core 0; a
strategy exception → zero current + LIMP, never a crash.

**Safety envelope** (owned by `control.py`, applied after the strategy):
40 V regen taper (38→40 linear); crossover guard — regen 0 when
`v_bank > 39 V ∧ v > CROSSOVER_KMH` (28 km/h until kV is measured); battery
current ≤ 40 A; slew limit; envelope also arbitrates throttle+brake
simultaneous → regen wins.

**States:** INIT (self-check, FW handshake) → RUN (sole state commanding
nonzero current) → LIMP (link silence, strategy exception, throttle window
fault, VESC fault; auto-recover where the cause clears, latch where it
doesn't). No PRECHARGE, no contactor states — the hardware deleted them.

---

## 4. Testing

- Host-first: `kinematics.py`, `strategy.py`, parser state machine, envelope —
  CPython-clean behind a 30-line `hal.py`; unit tests in `v2/tests/` beside
  the v1 suite in CI.
- Parser property tests: random corruption/truncation/reordering must yield no
  false-accepts and no stuck states.
- Replay: v1 ride logs + C-0 observer corpora as regression inputs.
- On-target gates below; every budget number in this document is enforced by a
  counter visible on the LINK page.

## 5. Implementation gates

| Gate | Deliverable | Retires |
|---|---|---|
| **FW-0** | Pin MicroPython release; 24 h dual-core soak (loop skeleton + display churn + ring writes) | D2's platform risk — before anything else |
| FW-1 | viper CRC + parser + host property tests + on-target timing (< 200 µs/frame) | D11, postmortems #2/#3 |
| FW-2 | Timed 100 Hz loop + counters + WDT; worst tick / GC pause report | D1/D8 budgets |
| FW-3 | Bench link bring-up: SELECTIVE at 50 Hz, RTT measured, FW tuple logged | D5/D6/D7 |
| **C-0** | ADC-app observer rides; telemetry + wheel corpora collected | D15; feeds the scoring sim |
| FW-4 | PIO capture + throttle + (if fitted) brake input, drill-spin | §7, D9 |
| FW-5 | Kinematics + envelope + placeholder strategy, wheel-off-ground; k cross-check live | D10 |
| FW-6 | Core 1 display + logger + XIP-aware flush | D13/D14 |
| FW-7 | First UART-controlled ride, LINK page green | the v1 failure mode, dead |

FW-0..FW-2 need only a Pico and USB — start before the motor ships.

## 6. Bench items this document adds

| # | Item | Feeds |
|---|---|---|
| B-9 | Confirm shipped FW tuple; verify SELECTIVE mask parses | D5/D6 |
| B-10 | "ADC and UART" app verified for C-0 observer mode | D15 |
| B-11 | Probe Mini FSESC4.20 for CAN transceiver/pads | D3 escape hatch |
| B-2 | `DIR_SIGN` under carrier-braked counter-rotation | D10 |

## 7. Sources

- [VESC UART protocol documentation](https://vedderb-bldc.mintlify.app/communication/uart-protocol)
- [VescUartLite — SELECTIVE introduced post-FW 3.41](https://github.com/gitcnd/VescUartLite/blob/master/README.md)
- [Flipsky — FSESC/VESC-Tool version compatibility (ships FW 5.2)](https://flipsky.net/blogs/vesc-tool/tips-for-compatible-fsesc-and-vesc_tool-version)
- [Mini FSESC4.20 manual — interface list incl. CAN](https://manuals.plus/asin/B08725X8CT)
- [VESC Project — maximum UART baudrate discussion](https://www.vesc-project.com/node/391)
- [MicroPython rp2 threading notes — no-GIL, one thread/core](https://forums.raspberrypi.com/viewtopic.php?t=326826)
- [MicroPython dual-core stability discussions (#7124 class, version regressions)](https://forums.raspberrypi.com/viewtopic.php?t=374335)
- [Vedder — original VESC UART post](http://vedder.se/2015/10/communicating-with-the-vesc-using-uart/)
