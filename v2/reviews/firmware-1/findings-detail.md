# Firmware review 1: finding details

Companion to [README.md](README.md). This file has one section per finding, with the claim, a failure scenario, the evidence, a suggested fix and the adversarial verdict(s).

How the verdicts work:

- High and critical findings were checked by two independent verifiers. One traced the code and reproduced the behaviour; the other checked the physics and the design intent.
- Medium and low findings were checked by a single code-trace verifier.
- **Severity** is the verifier-adjusted value: the highest severity among the verdicts that confirmed the finding.
- Paths starting `scratch/` point to throwaway reproduction scripts from the review session. They are not in the repo.


---

## Confirmed

### F01: SET_CURRENT goes to the VESC without a DIR_SIGN / counter-rotation mapping. Only DIR_SIGN = -1 is physically consistent, config ships +1, and the tests lock in the runaway convention

- **Severity:** critical
- **Area:** safety
- **Location:** `v2/firmware/config.py:40`
- **Status:** confirmed
- **Reference:** RGX-2-001 §1 (carrier locked => w_sun = -k*w_ring; T_sun = 0 => all torques zero); v2/README.md Conventions 1-2; RGX-2-003 D10, D6, §6 B-2; VESC bldc 5.02 mc_interface.c:50/693, mcpwm_foc.c:851-865, 1167-1170

**Claim.** VESC SET_CURRENT is signed in the VESC's own rotation frame. +I is torque toward +ERPM, and m_invert_direction flips the commanded current and the reported rpm together (DIR_MULT). The firmware sends the strategy value unchanged under its '+ = assist, - = regen' contract (control.py:189 -> vesc.py:288 -> vesc.py:93 int(amps*1000)).

With the carrier held (clutch in assist, brake in regen) the rotor counter-rotates: w_m = -k*w_w. kinematics.py:12 defines w_m = ERPM*DIR_SIGN/pp, so sign(ERPM) = -DIR_SIGN in both assist and regen. The correct wire current is therefore I_vesc = -DIR_SIGN*i_cmd, and the shipped mapping is right only when DIR_SIGN = -1.

What keeps it wrong:
- config.py:40 ships +1 with an ambiguous comment ('ERPM sign for forward assist').
- Bench step B-2 sets DIR_SIGN from whatever ERPM orientation A1 happens to have, instead of constraining A1's direction.
- Nothing couples the actuation sign to the telemetry sign. D10's 'visible by construction' argument covers only the telemetry path.
- The live k cross-check uses abs() (kinematics.py:74) and its health flag is never read, so nothing catches the error at runtime.
- v1 ran with positive RPM for forward carrier-locked motion, which is the v2 DIR_SIGN = -1 case.

**Failure scenario.** Closed-loop plant simulation (Willis relations + one-way clutch + VESC sign semantics).

(a) A1 reads counter-rotation as negative ERPM, with DIR_SIGN = +1. This is what test_regen_ramps_when_carrier_dragged models, and what B-2 produces for that orientation. The rider touches the rear brake for 0.5 s at 20 km/h. s drops to 0 and the Placeholder commands -40 A. At negative ERPM that is motoring torque, and its carrier reaction pushes in the clutch's locking direction. After the lever is released the clutch keeps the carrier grounded, s stays 0, and the '-40 A regen' persists. The bike goes from 20 to 54 km/h in 4.5 s with no rider input, and the rear lever cannot stop it. The throttle in this orientation gives +I: the clutch overruns and no assist reaches the wheel.

(b) A1 oriented so +I = assist, with DIR_SIGN = +1. Carrier-held counter-rotation maps to motor_rpm = +k*w, so s clamps to 1 and the strategy never commands regen. The lever path gives -40*(1-1) = 0. Holding the rear brake for 3 s at 20 km/h produces i_cmd = 0 throughout, i.e. zero rear braking.

The VESC reports average motor current as motoring-positive, so in case (a) the real i_motor would read about +36..40 A while the firmware believes it is regenerating. The test fixtures feed i_motor with the command's sign, which hides this.

**Evidence.** Firmware:
- config.py:40 `DIR_SIGN = 1  # [BENCH] B-2`
- vesc.py:93 `struct.pack_into(">i", out, 3, int(amps * 1000))` applies no sign.

VESC bldc 5.02:
- mc_interface.c:50 `#define DIR_MULT (...m_invert_direction ? -1.0 : 1.0)`
- mc_interface.c:693 `mcpwm_foc_set_current(DIR_MULT * current)`
- get_rpm `return DIR_MULT * ret`
- mcpwm_foc.c:859-860 `m_iq_set = current`
- mcpwm_foc.c:1167-1170: reported current = SIGN(vq*iq)*|i|

Scratch results (scratch/review/):
- demo_sign_plant.py case (a): 't=1.50 v=22.5 i_cmd=-39.9 s=0.00 locked' ... 't=6.00 v=54.1 i_cmd=-39.9 s=0.00 locked'.
- demo_sign_plant.py case (b): i_cmd 0.0 and s=1.00 for the whole hold.
- test_review.py::test_regen_command_is_generating_on_the_wire: '[regen DIR_SIGN=+1] erpm=-6750 SET_CURRENT=-36.00 A I*ERPM=+ (MOTORING)' vs '[regen DIR_SIGN=-1] erpm=6750 SET_CURRENT=-36.00 A I*ERPM=- (generating)'. The wire current is the same for both DIR_SIGN values, so it cannot be right for both.
- Suite re-run with DIR_SIGN=-1: '2 failed, 41 passed' (test_control.py:166, :185).

Other:
- v1-legacy/firmware/services/input_manager.py:58 uses positive RPM for forward carrier-locked motion.
- research/controller-candidates.md:102-104 quotes the VESC current-sign semantics.

**Suggested fix.** 1. Apply the frame mapping once, at the protocol boundary: i_wire = -config.DIR_SIGN*i_cmd (or a derived CUR_SIGN). Alternatively make DIR_SIGN a derived constant (-1) and assert it at boot.
2. Rewrite B-2: set A1's motor direction (phase order or invert flag) so that +I pushes the wheel forward with the carrier grounded, then verify ERPM > 0 under carrier-held counter-rotation. Add the invert setting to the D6 checklist and reword config.py:40.
3. Add a runtime plausibility latch on the VESC average motor current, which is motoring-positive regardless of direction: if i_cmd < -2 A while i_motor > +2 A (or the reverse) for more than 2 telemetry frames, enter LIMP and latch.
4. Add wire-level tests parametrised over DIR_SIGN in {+1,-1}. Generate fixtures from a Willis/clutch plant, report i_motor the way the VESC does, and assert sign(I_wire*ERPM) < 0 for regen and > 0 for assist.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### C01: With no lever sensor (the shipped default), the carrier lever does nothing while the throttle is held, and the firmware cannot detect it: RGX-2-003 §3 'throttle+brake → regen wins' cannot be implemented, and a stuck throttle has no brake cutoff

- **Severity:** high
- **Area:** safety
- **Location:** `v2/firmware/config.py:19`
- **Status:** confirmed
- **Reference:** RGX-2-003 §3 Safety envelope ('arbitrates throttle+brake simultaneous → regen wins'), D9, D10; spec §1 (T_sun=0 ⟹ no torque); research/carrier-brake-mechanism.md:265-268

**Claim.** While the throttle is held, the one-way clutch already grounds the carrier. Squeezing the carrier brake adds a second hold on a member that is already stationary. Carrier speed, rotor ERPM, wheel speed, i_motor, throttle and brake=False are therefore identical with and without the rear lever pulled. The only discriminator the design provides is the lever sensor, and it ships unfitted (BRAKE_FITTED=False, so sensors.py:132 always returns False) and is called 'optional / present-or-absent'. The envelope's arbitration (control.py:69-71) is keyed only on that sensor. With the sensor fitted it only zeroes assist; it never substitutes regen. This is a separate problem from F02. F02 is a wrong ordering inside the Placeholder. Here, no ordering and no stateless strategy can satisfy the requirement: slip-first gives F02 (assist becomes regen), throttle-first gives full assist through a held carrier. F18 (throttle held at boot or stuck) then has no brake-cutoff mitigation at all.

**Failure scenario.** The obvious F02 fix is applied (throttle checked first). The rider holds the throttle, or it sticks (F18), and pulls the rear/carrier lever to stop. The carrier is already held by the clutch, so the lever adds nothing: the motor keeps delivering 32-40 A of assist to the rear wheel, and the rider gets forward drive instead of rear braking until the throttle is released. The same applies when the rider holds the rear lever at a light and rests a hand on the throttle: the bike launches. Only the front brake (and the wheel's own brake, which research/carrier-brake-mechanism.md:267 names as the real fail-safe) remains, and it has no motor cutoff either.

**Evidence.** Scratch demo scratch/review/thr_brake.py drives ControlLoop with consistent held-carrier kinematics, once with and once without the rear lever: "shipped Placeholder: strategy inputs identical (assist vs assist+rear lever): True; i_cmd assist-only=-40.0 A, with rear lever held=-40.0 A" / "throttle-first fix: strategy inputs identical ...: True; i_cmd assist-only=+32.0 A, with rear lever held=+32.0 A" / "config.BRAKE_FITTED = False". The design promises this arbitration at RGX-2-003-FW.md:335-336 ("envelope also arbitrates throttle+brake simultaneous → regen wins") and :241 ("the envelope arbitrates the mode"), and calls the sensor "present-or-absent at runtime" at :207. control.py:70 reads `if brake and cmd > 0.0: cmd = 0.0`, where brake is always False unless the sensor is fitted.

**Suggested fix.** Make the lever sensor (or a brake-cutoff switch on every brake lever) mandatory hardware and treat BRAKE_FITTED=False as a configuration that refuses RUN, or at least refuses assist. Move the 'brake wins' rule into the envelope so it forces regen or zero regardless of the strategy, and add a cutoff input for the wheel's own brake. Record in RGX-2-003 §3 and D9 that the arbitration requirement cannot be met without the sensor, and add it to spec §12 'Accepted without mitigation' if it is kept optional.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F02: The Placeholder decides 'braking' from slip before it looks at the throttle. s = 0 whenever the clutch grounds the carrier, so assist turns into regen above about 3.8 km/h (and under F01's convention this latch sustains the runaway)

- **Severity:** high
- **Area:** safety
- **Location:** `v2/firmware/strategy.py:31`
- **Status:** confirmed
- **Reference:** RGX-2-003 D10 ('s = 0 during assist too ... slip is only meaningful with mode context'); v2/README.md consequence 1; v1 REGEN_HOLDOFF_MS precedent

**Claim.** `braking = brake or (omega_wheel > 30.0 and s < 0.97)` is evaluated before the throttle branch, with no mode context.

During assist the one-way clutch grounds the carrier, so s = 0 (kinematics.py:44-47 and RGX-2-003 D10 both say 's = 0 during assist too'). Engaging assist from a coasting, freewheeling carrier also has to pass s from 1 down through 0.97. So above 30 wheel rpm (about 3.8 km/h), every throttle request is reclassified as braking and commanded as up to -40 A.

D10 says 'the envelope arbitrates the mode', but envelope() reacts only to the lever `brake` flag, and BRAKE_FITTED is False. v1 had the protections v2 dropped: regen only with the throttle off, plus a 300 ms post-assist holdoff (v1-legacy settings.py:234-236, input_manager.py:3-10). After the throttle is released the rotor is still spinning with s near 0, which produces an immediate regen pulse.

The committed assist test hides all of this. It feeds erpm=+1000 at 100 wheel rpm (m = +100 rpm), which is kinematically impossible while the carrier is grounded (it must be -500 rpm). The live k cross-check (control.py:181-182) can therefore never run either. main.py:29 ships this Placeholder for FW-5/FW-7.

**Failure scenario.** ControlLoop with throttle 0.5, wheel at 150 rpm, and ERPM consistent with a grounded carrier (m = -k*w): the strategy returns -40.0 and i_cmd ramps -2, -4 ... -40 A with the throttle open and no brake applied.

Plant simulation with the only physically consistent sign pair (DIR_SIGN=-1, +I = assist), throttle 0.5 from 15 km/h while coasting: i_cmd oscillates between +2 and -2 A with s between 0.94 and 1.0. The carrier never locks, no assist reaches the wheel, and speed decays from 14.8 to 12.8 km/h.

From standstill: assist engages, then at 3.8 km/h s = 0 triggers regen, and speed limit-cycles at about 3.9 km/h.

Under F01 case (a), this same s = 0 latch is what keeps the -40 A 'regen' (really propulsion) on after the rider lets go of the brake.

**Evidence.** strategy.py:31-35.

Scratch results:
- demo_assist_slip.py: 'slip = 0.0', 'strategy output: -40.0', 'i_cmd trajectory: [-2.0, -4.0, ...] ... [-40.0, -40.0, -40.0]'.
- demo_sign_plant.py case A (J_R 2e-3 and 2e-2): speed capped at 3.9 km/h from standstill, zero assist from 15 km/h.
- test_review.py::test_assist_with_consistent_erpm_delivers_assist: '[assist] erpm=-5000 slip=0.000 i_cmd=-40.0 A (throttle 0.5, brake off)', FAIL.

The committed tests contradict each other: test_control.py:164 uses erpm=+1000 at 100 rpm for assist, while test_control.py:179 uses a negative ERPM for regen with the same DIR_SIGN.

**Suggested fix.** 1. Infer braking from slip only when the throttle is at idle and a post-assist holdoff has elapsed (about 300 ms, or until the rotor has decayed). Additionally require that the VESC motor current is not motoring.
2. Move mode arbitration into the envelope, as D10 states.
3. Derive every test ERPM from kinematics (w_m = -k*w_w for a held carrier) instead of hand-picked literals.
4. Add loop tests that assert i_cmd stays positive during consistent assist and that kx.samples > 0.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F03: A throttle-window fault puts the loop into LIMP, which zeroes regen as well as assist. A throttle wiring fault therefore removes the rear (carrier) brake, in one unslewed step

- **Severity:** high
- **Area:** safety
- **Location:** `v2/firmware/control.py:126`
- **Status:** confirmed
- **Reference:** RGX-2-001 §1 (T_sun = 0 => all torques zero), §9 'Throttle open circuit', §10.3; RGX-2-003 §3 States; docs/V2_PLAN.md §2.2 item 1

**Claim.** Spec §10.3 requires only that assist be zeroed when the throttle is outside its window. The sensor already returns 0.0 whenever it reads out of window (sensors.py:102-107), and envelope() zeroes positive commands on throttle_failed (control.py:67-68).

But _update_state enters LIMP(R_THROTTLE) on thr_failed (control.py:126-127), and tick() forces i_cmd = 0 in every non-RUN state (control.py:186-187, and :149 in _enter_limp). That removes regen too. With the carrier brake, zero motor torque means zero rear braking (T_c = -(1+k)*T_s). In the loop, the envelope's throttle_failed branch is effectively dead code.

Other consequences:
- The drop to 0 A bypasses the slew limiter.
- Recovery happens on the first in-window median sample (sensors.py:108-111), so a chattering connector toggles the rear brake on and off.
- A throttle whose idle rests below 0.20 of V_REF (for example a common 0.8-4.2 V unit, 0.16 ratiometric) sits in LIMP whenever released, so there is never any regen.

docs/V2_PLAN.md §2.2 item 1 records this exact v1 defect as one 'v2 must not inherit'. RGX-2-003 §3 lists 'throttle window fault' as a LIMP cause, which conflicts with the spec.

**Failure scenario.** On a descent the rider is braking at -32 A. The J3 connector opens for more than 50 ms (the THR line leaves the enclosure, spec §9), R2 pulls SIG to 0 V, and 50 ms later thr_failed = True. The loop enters LIMP and i_cmd steps from -32 A to 0 in one tick. The carrier brake stops transmitting torque until the throttle reads in-window again, then regen re-ramps at 2 A per tick.

The Placeholder would still be asking for -40 A: slip is 0 with the carrier held.

**Evidence.** Code:
- control.py:126-127 `elif thr_failed: self._enter_limp(R_THROTTLE)`
- control.py:186-187 `if self.state != RUN: self.i_cmd = 0.0`
- control.py:67 `if throttle_failed and cmd > 0.0:` (the envelope handles assist only)

Scratch results:
- demo_limp_paths.py #1: 'braking i_cmd = -32.0; after thr_failed: state 2, reason 2, i_cmd 0.0' (envelope() alone would return -32.0).
- test_sensor_review.py::test_throttle_fault_kills_regen_i_e_rear_brake passes (brake=True, erpm=-k*w, thr_failed -> LIMP, i_cmd 0.0).
- test_review.py::test_throttle_fault_does_not_remove_regen_brake: '[thr-fault] state=2 reason=2 i_cmd=0.0', FAIL.
- throttle_demo.py: 'generic throttle idle 0.16 ... throttle()=0.0000 failed=True'.

Coverage: control.py lines 127 and 139-140 are never executed. test_control.py:78 ('regen unaffected by throttle failure') is tested only at envelope level.

Docs: spec §10.3 'assist zeroed on violation'; docs/V2_PLAN.md §2.2 item 1 'A throttle fault should block assist only.'

**Suggested fix.** 1. Remove R_THROTTLE from the state machine. On a throttle fault, stay in RUN, force throttle = 0 (the envelope and sensor already block assist), and raise a displayed flag, keeping the regen and brake paths live.
2. Give throttle-fault recovery its own debounce and a re-arm requirement (see F18).
3. Fix the RGX-2-003 §3 wording so it matches spec §10.3.
4. Add a loop-level test: establish regen, set thr_failed, and assert i_cmd stays below 0 while assist stays blocked.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F04: COMM_FW_VERSION is sent once at boot, with no retry and no INIT timeout. Leaving INIT requires fw_major, so a lost reply (the normal case on a cold boot) keeps the bike in INIT, with no regen and no assist, for the whole ride

- **Severity:** high
- **Area:** correctness
- **Location:** `v2/firmware/main.py:39`
- **Status:** confirmed
- **Reference:** RGX-2-003 §3 States (INIT: self-check, FW handshake), D6, D7, gate FW-3; RGX-2-001 §9 A1 brown-out row; README consequence 2

**Claim.** control.py:118-121 leaves INIT only when `v.fw_major and v.v_in > 1.0`. fw_major is set only by the COMM_FW_VERSION reply. main.py:39 sends that request exactly once, before the loop, and nothing else ever calls request_fw() (checked with grep). INIT has no timeout, no reason code, and no self-check (throttle at idle, VSYS in range), and the FW tuple is never recorded (D6).

Why the single request is usually lost:
- The Pico is powered from A1's BEC, so the two boot together at every S1 closure and after any A1 brownout.
- VESC 5.02 starts the UART app only after main.c sleeps 100 ms, then runs mc_interface_init(). That calls do_dc_cal(), which is a 1000 ms sleep plus 4000 current samples, before app_set_configuration(). A1's UART is deaf for at least about 1.2 s after power-up.
- A single CRC error on the 23-byte reply, or a parser false start swallowing it (F06), has the same effect.

Telemetry keeps flowing, so v_in is valid and the WDT stays fed. ui.py:152 shows the normal RIDE page for any non-LIMP state (see F22).

**Failure scenario.** The rider closes S1. The Pico sends the FW request at, say, 800 ms, while A1 is still in DC calibration, and the request is dropped. Telemetry requests succeed later, so v_in > 1, but fw_major stays 0 forever. The state stays INIT and i_cmd = 0 for the whole ride. There is no assist, and the rear carrier brake transmits no torque. The display looks normal. Only a power cycle recovers, and a power cycle repeats the same race.

**Evidence.** Code: main.py:39 `loop.link.request_fw()` is the only caller. control.py:119 `if v.fw_major and v.v_in > 1.0:`.

VESC bldc 5.02:
- main.c:221 `chThdSleepMilliseconds(100)`
- main.c:241 `mc_interface_init()`
- main.c:254-255 `app_set_configuration(appconf)`
- mcpwm_foc.c:3127 `chThdSleepMilliseconds(1000)`
- mcpwm_foc.c:3131 `while(m_motor_1.m_curr_samples < 4000)`
- commands.c:189-218: fw_major exists only in the FW reply.

Scratch results:
- demo_limp_paths.py: after 30 s of healthy telemetry with the FW reply lost, state = 0 (INIT), exactly 1 FW request on the wire, i_cmd = 0.0.
- test_review_link.py::test_fw_reply_lost_latches_init_forever passes (60 s).
- rt/test_rt_review.py::test_single_lost_fw_reply_leaves_loop_in_INIT_forever passes: after 60,000 ticks, frames_ok > 25,000, state INIT, SN_FAULT 0.
- test_review.py::test_init_survives_lost_fw_reply: '[init] state=0, FW requests sent=1', FAIL.

test_control.py:84 only injects the reply unsolicited.

**Suggested fix.** 1. While state == INIT and fw_major == 0, re-send COMM_FW_VERSION every 200-250 ms and count the attempts. Alternatively, leave INIT on N fresh telemetry frames and fetch the FW tuple opportunistically.
2. Add an INIT timeout that moves to an annunciated reason shown on the display.
3. Add INIT self-checks: throttle at idle, VSYS in range.
4. Record the FW tuple in the log header (D6).
5. Add host tests for a lost first reply, telemetry-only, and FW-only.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F05: The throttle's fault-window floor (0.20) is also its zero point, with no deadband. The owned J3 throttle at rest reads about 9.5 %, which commands about 3.8 A of assist while parked, and standstill (and therefore log flushing) is never detected

- **Severity:** high
- **Area:** safety
- **Location:** `v2/firmware/sensors.py:111`
- **Status:** confirmed
- **Reference:** RGX-2-001 §10.3, §7 (J3 ratiometric from +3V3), §9; RGX-2-002 BOM J3 ('Verify output span 0.20-0.85 x V_REF'); RGX-2-100 Rev D sheet 3 note 6; RGX-2-003 D13 (flush at wheel = 0 and throttle idle)

**Claim.** Throttle.throttle() returns (frac - THROTTLE_LO)/(THROTTLE_HI - THROTTLE_LO). It uses the spec's fault-rejection window (0.20-0.85 x V_REF) as the calibration span, with no idle or full calibration, no deadband and no clamp. The Placeholder assists on any throttle > 0 (strategy.py:34-35).

The J3 part is 'Owned ... proven on v1'. v1 measured it at 3.3 V: idle about 1073 counts, full about 3238 counts (v1-legacy settings.py:91-95), i.e. 0.262 and 0.791 of full scale. v1 had a separate calibration and a 5 % deadband; v2 dropped both. So v2 reads 0.095 at rest and 0.909 at full (36.4 A, never 40 A).

ui.py:208-209 then treats any normalised throttle above a hard-coded 0.02 as 'moving'. A healthy throttle counts as idle only if its raw reading falls in [0.200, 0.213] of V_REF, a 43 mV window. For any other idle voltage, standstill is never detected, so the D13 flush never runs.

Conversely, a throttle resting below 0.20 trips R_THROTTLE permanently (see F03).

**Failure scenario.** The bike is stationary in RUN with hands off the throttle. The median ADC fraction is 0.262, so throttle() = 0.0954 with failed=False. With wheel = 0 there is no slip-regen, and the Placeholder commands 3.82 A continuously; the slew reaches it in 2 ticks. The clutch grounds the carrier. At the BOM's kV estimate, that is about 6.5 N*m at the wheel, about 20 N at the tyre. The parked bike creeps or pushes against the rider, the bank drains, and the motor, which has no thermal protection (spec §7), carries stall current for as long as S1 is closed.

Separately, standstill is never detected, so the ring overwrites itself every 6.8 min and nothing reaches flash.

**Evidence.** Code: sensors.py:101-111 `frac = med / 65535.0 ... return (frac - config.THROTTLE_LO) / span, False`. config.py:54-55. strategy.py:34. ui.py:208-209 `moving = (sn[SN_WHEEL_RPM] > 0.5 or sn[SN_THROTTLE] > 0.02)`. v1-legacy/firmware/config/settings.py:91-96 (idle ~1073, full ~3238, THROTTLE_DEADBAND = 0.05).

Scratch results:
- throttle_demo.py: 'idle, v1-measured raw=1073 frac=0.2620 -> throttle()=0.0954 failed=False Placeholder cmd=3.82 A' and 'full raw=3238 frac=0.7907 -> throttle()=0.9088 ... 36.35 A'.
- test_sensor_review.py::test_idle_throttle_is_not_zero_phantom_assist passes (i_cmd > 3.5 A at rpm 0).
- test_review.py::test_throttle_at_rest_reads_zero: '1070/4095 -> throttle=0.094 -> Placeholder 3.77 A assist', FAIL.
- demo_flush.py scenario C: 'throttle idle=0.062: flushes=0, bytes=0, ring dropped=7903'.
- sim_logger.py (8-minute ride at J3 idle 0.094): 'flushes=[]'.

**Suggested fix.** 1. Separate calibration from fault detection: add [BENCH] THROTTLE_IDLE (about 0.262) and THROTTLE_FULL (about 0.79).
2. Add a deadband above idle (about 5 % of span) that returns exactly 0.0, and clamp the result to [0,1].
3. Place the fault thresholds below idle and above full, with margin.
4. Define ui standstill from commanded and measured current, wheel and ERPM, not from normalised throttle, and move all thresholds into config.py.
5. Add host tests at the v1-measured idle count that assert zero assist.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F08: The back-EMF crossover guard only zeroes the command, which makes A1 release its bridge. Above crossover the body diodes rectify anyway, so the guard cannot stop the bank charging (and the BOM's 'faulted A1' premise is wrong)

- **Severity:** high
- **Area:** safety
- **Location:** `v2/firmware/control.py:81`
- **Status:** confirmed
- **Reference:** RGX-2-001 §9 bank-overvoltage caveat, §11 gate 2; RGX-2-002 Compatibility rows 'Back-EMF vs bank 40 V / 45.4 V' and Queued change #4; VESC bldc 5.02 mcpwm_foc.c:851-856

**Claim.** In VESC 5.x, a SET_CURRENT below cc_min_current (default 0.05 A) sets MC_STATE_OFF and calls stop_pwm_hw (mcpwm_foc.c:851-856), turning all FETs off. A zero command is therefore electrically the same as the 'faulted A1' in the BOM's exposure line.

By the BOM's own numbers, back-EMF with the carrier held is 40 V at 28.0 km/h and 45.4 V at 31.8 km/h. The guard (v_in > 39 V and speed > 28 km/h) acts only where back-EMF is already at or above bank voltage. Whenever it acts, the rider's carrier brake is still loaded through the body-diode rectifier and the bank keeps charging. That current is uncontrolled: it is set by (BEMF - V_bank)/winding impedance, not by the 40 A limit. FW 5.x has no field weakening, so an active bridge could not hold iq = 0 above crossover either.

The guard helps only in a sliver just above 28 km/h and does nothing above about 32 km/h, which is where the BOM places the real exposure. The firmware neither detects nor annunciates the result.

**Failure scenario.** A long descent at 35 km/h, v_in at 39.5 V, rider holding the rear lever. The guard zeroes the command, A1 releases the bridge, and about 50 V of back-EMF is rectified into the bank. The A1 overvoltage fault at 40 V changes nothing because the bridge is already off. The bank climbs past the 45.4 V absolute ceiling toward the back-EMF voltage, exposing the modules to the vent/rupture class of overvoltage (spec §9), while the firmware believes regen is 0 A.

Between 28 and about 31 km/h, the guard instead replaces controlled rear braking with weak diode braking.

**Evidence.** Code: control.py:81-82 `if v_bank > config.V_CROSSOVER_GUARD and kmh_val > config.CROSSOVER_KMH: f = 0.0`.

VESC: bldc mcpwm_foc.c:852-856 `if (fabsf(current) < ...cc_min_current) { m_control_mode = CONTROL_MODE_NONE; m_state = MC_STATE_OFF; stop_pwm_hw(...); return; }`. mcconf_default.h:165 `MCCONF_CC_MIN_CURRENT 0.05`.

BOM Compatibility sheet: 'Back-EMF vs bank 40 V (full): Crossover 28.0 km/h'; 'Back-EMF vs bank 45.4 V (abs): Crossover 31.8 km/h. Exposure: braking >32 km/h + full bank + faulted A1.'

Spec §9: 'bridge body diodes still rectify back-EMF into the bank whenever back-EMF exceeds bank voltage'.

**Suggested fix.** 1. Record the exposure (braking above about 32 km/h near a full bank, independent of A1 state) as unmitigated in spec §12, or add a real sink (dump resistor or normally-closed phase short).
2. Stop presenting the guard as a mitigation.
3. In firmware, detect v_in rising above V_BANK_MAX while i_cmd = 0 and speed > CROSSOVER_KMH, and annunciate it to the rider ('release rear brake') and to the log.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F09: The regen taper reaches zero exactly at A1's overvoltage trip (40 V), measured on terminal voltage that includes the regen I*R rise, with delayed feedback. Hard braking trips A1 OV well below 'full', and the rear brake pulses on and off

- **Severity:** high
- **Area:** safety
- **Location:** `v2/firmware/control.py:75`
- **Status:** confirmed
- **Reference:** RGX-2-001 §3, §4.1, §6 BANK FULL, §10.5a, §10.8; RGX-2-003 D6; VESC bldc 5.02 mc_interface.c:1555-1563, 2116-2123

**Claim.** The envelope tapers regen from V_TAPER_START 38 V to zero at V_BANK_MAX 40.0 V (config.py:47-48). That equals A1's configured maximum input voltage of 40 V (spec §10.5a, D6), so there is no margin.

v_in is GET_INPUT_VOLTAGE, the raw terminal ADC value. During regen it equals OC + I_in*R_total, with R_total 0.187-0.367 Ohm (spec §4.1). A1 raises FAULT_CODE_OVER_VOLTAGE after 8 consecutive ISR samples, well under 1 ms.

The firmware sees v_in only every 2 ticks, uses it one tick after it is parsed, and slews at 2 A per tick. The taper gain is (40 A / 2 V)*R*duty, about 4-7, so the loop overshoots. i_in is already polled (TELEM_MASK bit 3) but not used, so there is no open-circuit voltage estimate.

**Failure scenario.** Simulation at 20 km/h with the rider holding the carrier hard, using the real ControlLoop and telemetry cadence.
- At nominal R = 0.277 Ohm, terminal voltage peaks at 40.17 V with bank OC at 33 V, 40.34 V at 35 V, and 40.45 V at 37 V.
- At R = 0.367 Ohm, peaks are 40.54-40.78 V.

Each crossing makes A1 stop the bridge and ignore commands for 500 ms. The firmware enters LIMP(R_VESC_FAULT). A1 auto-clears, the firmware exits LIMP on the first fault = 0 frame, regen re-ramps and trips again. The rear brake pulses at about 1-2 Hz, starting 7 V below 'full'.

**Evidence.** Code: control.py:75 `f = (config.V_BANK_MAX - v_bank) / span`; config.py:47 `V_BANK_MAX = 40.0`.

Spec §10.5a: 'A1 maximum input voltage set to 40 V'.

VESC: commands.c:344-345 `buffer_append_float16(send_buffer, GET_INPUT_VOLTAGE(), 1e1, &ind)`; mc_interface.c:1557-1563 (8-sample OV fault_stop); mc_interface.c:2116-2123 (auto-clear); MCCONF_M_FAULT_STOP_TIME 500.

Simulation (demo_taper.py): 'R=0.277 V_oc=33 V @20 km/h: peak terminal 40.17 V, ticks above A1 l_max_vin(40 V): 7'.

**Suggested fix.** 1. End the terminal-voltage taper with margin below l_max_vin (for example 38.5-39 V), with hysteresis.
2. Base the bank-full taper on an open-circuit estimate, v_in - i_in*R_total, using the already-polled i_in.
3. Add a fault hold-off and a fault-count latch (F17).
4. Alternatively, raise A1 l_max_vin through a spec change: 45.4 V absolute is within A1's 60 V rating.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F13: Log files are never rotated. littlefs fills after about 1.5 h of riding, the flush raises ENOSPC, and the core-1 thread dies (display frozen, logging stopped) on every ride after that

- **Severity:** high
- **Area:** platform
- **Location:** `v2/firmware/ui.py:217`
- **Status:** confirmed
- **Reference:** RGX-2-003 D13 (logging, wear/size claim), D14, D2

**Claim.** Each boot's flushes append to /logs/rideNNNN.bin, and nothing ever deletes or caps them. The RPI_PICO filesystem is 1408 KiB, shared with the firmware .py files. Riding logs 24 B at 10 Hz (864 KB/h), so the filesystem fills after roughly 1.5 cumulative riding hours.

f.write then raises OSError(28). _flush and Core1.run have no try/except, so the exception kills the core-1 thread. The ring is never cleared, so the first flush of every later boot raises again. D13's '≤ 96 KB/ride' assumption is wrong because flushes happen mid-ride with no per-ride cap, and D13 considered only flash wear, not capacity.

**Failure scenario.** In roughly the second or third hour of use, the rider stops at a light, a flush starts, and ENOSPC is raised. Core 1 exits and the SSD1306 keeps showing its last frame (for example '0.0 km/h / 24.6 V / 0.0 A AST') indefinitely. Later LIMP or fault states are never displayed, and logging stops. Core 0 keeps feeding the WDT, so nothing recovers. This repeats on every ride until someone deletes files by hand, and doing so then causes boot-id collisions (F41).

**Evidence.** Code: ui.py:221-225 `with open(path, "ab") as f: ... f.write(mv[off:off + REC_SIZE])` then `self.ring.clear()`, with no exception handling. ui.py:227-250 has no try.

MicroPython: ports/rp2/mpconfigport.h:77 / boards/RPI_PICO/mpconfigboard.h `MICROPY_HW_FLASH_STORAGE_BYTES (1408 * 1024)`.

Scratch results:
- rt/enospc.py (MicroPython v1.29 unix port, the real ui.Core1.run(), littlefs on a 1408 KiB RAM block device holding 62 KB of firmware): 'Core1.run() died: OSError (28,) after 1.59 h simulated (1.55 h riding)'; 'ring.count still: 4037 (never cleared; next flush re-raises)'.
- demo_enospc.py: 'Core1.run terminated by OSError(28) at t=303.0 s'; the display still shows the last frame.

**Suggested fix.** 1. Before opening a log file, check os.statvfs and delete the oldest ride files until a free-space floor is met (for example 2x the ring size). Also cap the bytes per boot file.
2. Catch OSError in _flush, count it, clear or trim the ring, and publish a 'LOG FULL/ERR' indicator.
3. Guard core 1 itself (F25).

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F06: The frame parser commits to every 0x02 start byte and never backtracks. One stray byte can blind telemetry indefinitely, which forces LIMP (zero motor torque, so no rear braking)

- **Severity:** medium
- **Area:** protocol
- **Location:** `v2/firmware/vesc.py:180`
- **Status:** confirmed
- **Reference:** RGX-2-003 D11 ('O(1) resync'), §4 ('no stuck states'), D7; README Conventions consequence 2

**Claim.** FrameParser.feed treats any 0x02 seen in HUNT as a committed frame start. It consumes LEN+3 bytes, and on an END or CRC failure it resumes HUNT at the END-position byte only (vesc.py:174-184). The bytes it swallowed as payload and CRC are never re-scanned. VESC's own decoder (bldc packet.c) retries from false_start+1.

When a real payload field contains 0x02, the post-failure HUNT lands on that byte again. Examples: i_motor or i_in raw 0x02xx (5.12-7.67 A), a CRC byte, or erpm 512-767. Because the telemetry stream is periodic, the parser can stay misaligned for as long as that value persists. The docstring's 'O(1) resync' claim (line 114) is false.

The MicroPython rp2 UART delivers framing-error bytes as data, so line noise does reach the parser.

**Failure scenario.** The rider is on light assist at about 6 A. One noise byte arrives in an inter-frame gap and the parser then delivers zero frames. At 250 ms, LIMP(R_LINK) zeroes i_cmd. Sync returns only after the reported current leaves the 0x02 band, and RUN then needs 10 more frames.

In a closed-loop simulation, noise at t=2000 ms gave LIMP at 2260 ms and RUN at 2550 ms: 550 ms without telemetry and 290 ms of forced 0 A. The same lock happens during regen whenever the payload contains 0x02. Zero motor torque means the carrier brake transmits nothing, so one line glitch removes rear braking for about 0.3-0.5 s.

Entering the stream mid-frame (for example after a FIFO overrun) with i_motor = 5.5 A never resyncs at 9 of 24 offsets.

**Evidence.** Scratch results (scratchpad/review/):
- lockin.py: 'i_motor=5.50 A, 1 stray 0x02, 200 good frames follow: FrameParser delivered 0, VESC-style delivered 200 (crc_fail=100)'. It locks for i_motor in 5.25-5.33, 5.50-5.58, ... 7.50-7.58 A, and mid-stream entry fails at 9 of 24 offsets.
- montecarlo.py (3000 trials each): with a stray 0x02 plus a random byte during assist, mean 8.12 frames lost, max 60, and P(LIMP) = 15.1 %. The VESC-style decoder loses 0.
- montecarlo.py, single-bit flip: max 40 frames lost, P(LIMP) = 1.0 %, versus max 1 for the VESC-style decoder.
- sim_closed.py state log: (2260, (LIMP, R_LINK)) -> (2550, (RUN, 0)).
- test_one_stray_byte_blinds_parser_indefinitely: b'\x02' + 500 good frames -> 0 delivered.

References:
- bldc 5.02 packet.c:159-166: 'else if (res == -1) { // Something went wrong. Move pointer forward and try again. handler->rx_read_ptr++; ...}'.
- MicroPython rp2 machine_uart.c uart_drain_rx_fifo (v1.22.2 and v1.29): the FE branch is empty, followed by ringbuf_put.

**Suggested fix.** 1. Keep a raw-byte window of at least 260 B covering the bytes since the current candidate start. On any LEN, END or CRC failure, restart scanning at candidate_start+1, as bldc try_decode_packet does. That costs O(frame) work only on errors.
2. Combine this with the LEN cap (F26).
3. Add property tests for a stray 0x02, mid-frame entry, and payloads containing 0x02 (for example i_motor = 5.5 A), each asserting recovery within one frame.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F10: Wheel speed goes stale during hard braking: the last full period is held, updated only once per period, and never bounded by elapsed time. The slip estimate climbs toward 1 and regen fades before the stop

- **Severity:** medium
- **Area:** correctness
- **Location:** `v2/firmware/sensors.py:69`
- **Status:** confirmed
- **Reference:** RGX-2-003 D9, D10; RGX-2-001 §1, §4.9

**Claim.** wheel_rpm() reports the average of the last complete, non-overlapping high+low pair, and holds it unchanged until the next pair completes or SPD_ZERO_MS (400 ms) passes since the pair was drained.

Problems:
- Nothing bounds the estimate by the time since the last edge.
- Pairs do not overlap, so the estimate updates once per period, not at every edge.
- _last_edge_ms is stamped at drain time, not at edge time.
- SPD_ZERO_MS = 400 sets the lowest reportable speed at 25 wheel rpm (about 3.15 km/h). Below that, the reading flickers to 0 (18 % of ticks at 2.5 km/h).

With the carrier held, kinematics.slip combines fresh ERPM with this stale wheel speed, giving s_est = 1 - w_true/w_stale. That drifts toward 'freewheeling' exactly when the rider brakes hardest.

**Failure scenario.** Cycle-accurate PIO emulation driving the real WheelSpeed at 10 ms ticks: 20 km/h, braking at 0.5 g from t = 1000 ms, carrier fully held (s_true = 0).
- At t = 1600, true speed is 9.4 km/h but 12.2 is reported, so s_est = 0.23.
- From t = 1790 to 2190 ms, the reported speed is frozen at 7.7 km/h while the true speed falls from 5.9 to 0 (the wheel stops at 2134 ms).
- s_est goes 0.23 -> 0.46 -> 0.69 -> 0.92 -> 1.00.

Placeholder regen, 40*(1-s), falls from about 31 A to about 3 A, and 'braking' drops out once s >= 0.97. Less motor torque means less rear braking, so the rear brake fades in the last ~0.35 s of every hard stop.

**Evidence.** Code: sensors.py:62-67 (the period is replaced only when a pair completes; `_last_edge_ms = now_ms`); sensors.py:71-81 (non-overlapping pairing; zero only via SPD_ZERO_MS); config.py:43.

Scratch results (scen2.py): 't=1800 true=5.9 reported=7.7 slip_est(held)=0.23 ... t=2050 true=1.5 reported=7.7 slip_est=0.81 ... t=2100 true=0.6 reported=7.7 slip_est=0.92'; 'E: 2.5 km/h (period 504 ms): 18 % of ticks report 0'. Emulator: scratchpad/review/pio_sim.py.

**Suggested fix.** 1. Use a sliding window: the latest phase plus the one before it. That still spans a full period but updates at every edge.
2. Record the time of the last drained word, and once elapsed time exceeds the last phase, clamp rpm <= 60e6/(PPR*(elapsed + previous phase)) so the estimate decays toward zero.
3. Optionally read the in-progress count with sm.exec('mov(isr, x)'); push().
4. Pass a speed-age or validity flag so slip is not trusted when the wheel sample is older than the ERPM sample.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F11: There is no read-only observer mode for C-0. The link sends SET_CURRENT at 100 Hz in every state, which cuts or fights A1's ADC app during commissioning rides

- **Severity:** medium
- **Area:** design-conformance
- **Location:** `v2/firmware/vesc.py:288`
- **Status:** confirmed
- **Reference:** RGX-2-003 D15, gate C-0, bench B-10

**Claim.** D15 commissioning (C-0) runs A1's 'ADC and UART' app with the Pico as a read-only observer. VescLink.tick packs and writes SET_CURRENT(i_cmd) unconditionally every tick, in INIT, RUN and LIMP (vesc.py:288-289). No config flag or test covers an observer mode.

In VESC 5.x, SET_CURRENT(0) calls mcpwm_foc_set_current(0), which stops PWM (MC_STATE_OFF) until the ADC app's next write. The ADC app writes every 1/update_rate_hz, 2 ms at the default 500 Hz, and has no arbitration against UART commands.

The C-0 corpus also cannot capture rider intent: the throttle is wired to A1's ADC and nothing polls COMM_GET_DECODED_ADC. An observer mode is viable, because app_adc resets the VESC timeout itself.

**Failure scenario.** C-0 wiring (throttle on A1's ADC, GP26 floating to 0 through R2): thr_failed -> LIMP(R_THROTTLE), and the Pico keeps sending SET_CURRENT(0) at 100 Hz. That gives torque dropouts of up to ~2 ms in every 10 ms (about 20 % chopping) throughout the commissioning ride, and the i_motor telemetry recorded for the scoring sim is distorted by the observer itself.

If the throttle is also teed to GP26, the loop reaches RUN, sees s = 0 (A1 is assisting through the clutch), and commands -40 A every 10 ms against the ADC app. That is exactly the 'two writers to one setpoint' situation D15 rejects, and under F01 that -40 A may be motoring.

**Evidence.** VESC bldc 5.02:
- mcpwm_foc.c:851-856 (stop_pwm_hw below cc_min_current)
- app.c:96-99 `case APP_ADC_UART: app_adc_start(false); app_uartcomm_start();`
- app_adc.c:559-561 mc_interface_set_current_rel(...) with no UART arbitration; app_adc.c:426 `timeout_reset();`
- appconf_default.h `APPCONF_ADC_UPDATE_RATE_HZ 500`

VESC release_5_03: commands.c:455-459; mcpwm_foc.c:3172-3190.

Scratch results:
- '[C-0] observer switch in config: []; SET_CURRENT frames in 1 s: 101, values [0.0]', FAIL.
- '[C-0 teed] last Pico SET_CURRENT while A1 ADC app assists: -40.0 A', FAIL.

grep for 'observer|C-0|read.only' in firmware/ finds nothing.

**Suggested fix.** 1. Add config.OBSERVER_ONLY (or a boot-time pin). In that mode, VescLink sends only telemetry requests, the FW handshake and COMM_GET_DECODED_ADC (so rider throttle and brake reach the corpus), and never sends SET_CURRENT. ControlLoop holds an observer state with no command path.
2. Add a test asserting zero SET_CURRENT frames on the wire in observer mode.
3. Tie bench item B-10 to that test.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F14: The ring is flushed only when it is at least half full and the bike is at standstill. Power-off is the S1 switch alone, so short rides, the tail of every ride, and the lead-up to every reset are lost

- **Severity:** medium
- **Area:** correctness
- **Location:** `v2/firmware/ui.py:248`
- **Status:** confirmed
- **Reference:** RGX-2-001 §2 (Off state), §4.8, §6; RGX-2-003 D13, D15, §3 top-level guard

**Claim.** `if still and self.ring.count >= self.ring.n // 2: self._flush()` writes only once 2048 records exist: 3.4 min of riding at 10 Hz, or up to 34 min of standing at 1 Hz.

The only way to turn the bike off is S1 (spec §2 'Off state: mechanical switch only'). C4 holds VSYS up for 6.8 ms (§4.8), far too short to flush. So:
- Every record since the last flush is lost at switch-off.
- A whole ride shorter than about 3.4 min is never written.
- A flush that has started (3 s after stopping, lasting 1-2 s) and is interrupted by S1 loses its unclosed append.
- The last-resort handler (main.py:76-82) and WDT resets discard the ring holding the minutes before a fault, and machine.reset_cause() is never recorded.

D13 says to flush at verified standstill; the half-full threshold is not in the design.

**Failure scenario.** - A 3-minute ride to a shop, 60 s stop, then S1 opened: 0 bytes reach flash (1886 records lost).
- A 20-minute commute with a red light every 3 minutes: the last flush happens at t = 1053 s, and 1263 records (about 2.3 min, including the final braking) are lost at S1-off.
- A 3-minute ride followed by a 10-minute stop still loses the last 378 records.
- A mid-ride exception resets the Pico and leaves no trace of the preceding seconds.

**Evidence.** Scratch results (the real ui.Core1.run with a fake clock and filesystem):
- demo_flush.py A: 'records in ring=1886, bytes on flash=0, flushes=[]'.
- demo_flush.py B: 'flushed recs=9315, unflushed at power-off=1263 ..., flushes at t(s)=[333, 693, 1053]'.
- sim_logger.py: '3 min ride, 10 min standstill ... flushes=[(401000, 2048)] -> lost at S1-off: 378'.

Spec §4.8: C4 hold-up 6.8 ms. main.py:76-82 `sys.print_exception(e)` then `machine.reset()`. No machine.reset_cause() call anywhere in firmware/.

**Suggested fix.** 1. Flush at every verified standstill episode once the dwell has passed, whatever the ring count.
2. Keep a flushed watermark and append only records added since the last flush, instead of clear().
3. Keep n//2 only as an extra trigger for long parked periods.
4. Log machine.reset_cause() and a boot record at boot, and preserve last-resort exception text in RAM for the next flush.
5. Document the worst-case loss as 'records since the last standstill'.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F15: CI never runs the v2 suite: the v1 step fails first, so 'Run v2 firmware tests' is skipped

- **Severity:** medium
- **Area:** tooling
- **Location:** `.github/workflows/tests.yml:19`
- **Status:** confirmed
- **Reference:** RGX-2-003 §4 ('unit tests in v2/tests/ beside the v1 suite in CI'); v2/research/decisions.md 2026-08-02

**Claim.** The workflow runs the v1 tests and then the v2 tests as sequential steps in one job. The v1 step fails because v1-legacy/tests/test_settings_guard.py:31-36 reads v1-legacy/README.md, which reorg commit 6810478 replaced with an orientation README (the old one became README-v1.md). GitHub therefore skips the v2 step.

The v2 suite has never run in CI, despite the decision log ('43 passing ... added to CI beside the v1 suite') and RGX-2-003 §4. CI also never cross-compiles the firmware with mpy-cross, never runs anything under a MicroPython interpreter, and has no coverage gate.

**Failure scenario.** Any v2 regression merges with no signal. The pipeline is already red for an unrelated reason, so a new v2 failure does not change the status.

**Evidence.** GitHub Actions run 36467082911 (commit 1a410c4, job 109079754363): 'Run v1 tests' = failure, 'Run v2 firmware tests' = skipped. Run 36466908779 (commit 6810478) also failed.

Reproduced locally in this review: `cd v1-legacy && python3 -m pytest -q tests/test_settings_guard.py` gives '1 failed' at tests/test_settings_guard.py:36 ('README has no table row for VCAP_REGEN_TAPER_START_V'). The v2 suite gives '43 passed'.

.github/workflows/tests.yml:19-24 runs both as sequential steps of one job.

**Suggested fix.** 1. Split v1 and v2 into separate jobs, or add `if: always()` to the v2 step.
2. Point test_settings_guard at README-v1.md, or retire it since v1 is frozen.
3. Add an `mpy-cross -march=armv6m` compile step for v2/firmware/*.py.
4. Add a MicroPython unix-port job that runs the vesc/kinematics tests, including vesc_fast.
5. Add a coverage report.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F16: No tooling provisions or verifies the D6 A1 settings, and the owned A1's recorded configuration contradicts them: FW 6.6, 1000 ms timeout, 50 A, and a LispBM push script installed

- **Severity:** medium
- **Area:** tooling
- **Location:** `v2/firmware/config.py:35`
- **Status:** confirmed
- **Reference:** RGX-2-003 D5, D6 checklist, D7; RGX-2-001 §10.4, §10.5, §10.5a; bench B-9

**Claim.** No v2 script provisions or checks the D6 checklist: battery 40 A, max input 40 V, motor-temperature sensing off, UART app at 115200, timeout 200 ms, timeout brake current 0. The firmware never reads APPCONF or MCCONF, and VESC_TIMEOUT_MS (config.py:35) is used nowhere.

The last snapshot of the owned controller (v1-legacy/logs/vesc_snapshot_appconf.bin; meta says FW 6.6, HW 410) decodes to app_to_use = UART, timeout_msec = 1000, timeout_brake_current = 0.0, with MCCONF l_in_current_max = 50 A. v1 provisioning also installed an auto-starting LispBM script that pushes COMM_CUSTOM_APP_DATA at 100 Hz, about 19 % of the 115200-baud wire.

The only existing tool, v1-legacy/scripts/vesc_provision.py, hard-guards FW (6,6)/HW '410', sets 50 A and 43 V, and reinstalls that Lisp script. That contradicts D5, D6 and spec §10.4/§10.5a. The test fixture (conftest.py:84-85) assumes FW 5.2.

**Failure scenario.** - The Pico wedges or resets during 40 A assist or regen. A1 holds the last current for 1000 ms instead of the 200 ms that D7 guarantees, five times the designed dead-Pico window.
- The bank can reach 43 V before A1's limit acts, against a 40 V design.
- Unsolicited Lisp frames consume link budget that D4 never counted and inflate frames_ok, which the LINK-recovery logic uses.

**Evidence.** Snapshot decoded with FW 6.06 serialization order (bldc release_6_06 confgenerator.c:220-240): 'timeout_msec 1000; timeout_brake_current 0.0; app_to_use byte@33 = 3 (APP_UART)'. MCCONF offset 16 gives l_in_current_max 50.0.

v1-legacy/firmware/config/vesc_snapshot_meta.txt: fw_major=6, fw_minor=6, hw_name=410. v1-legacy/scripts/vesc_provision.py: EXPECTED_FW=(6,6); installs scripts/vesc_lisp_push_iq.lisp ('auto-starts on every VESC boot').

VESC default APPCONF_TIMEOUT_MSEC is 1000 (appconf_default.h:28).

**Suggested fix.** 1. Add v2/tools/provision_a1.py (host or mpremote). It should read the FW tuple, erase and stop LispBM, set and verify every D6 field (including timeout, timeout brake current and the motor-direction setting from F01), and save a snapshot.
2. Have INIT read APPCONF and refuse RUN, with a visible reason, if the timeout or limits differ from D6.
3. Update D5/D6 and the test fixtures for the actual FW 6.6 controller.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F17: LIMP recovery has no hold-off, hysteresis or latch, and exit re-checks only the entry reason. VESC faults auto-clear after 500 ms, so the command that caused the fault is re-applied at about 1-2 Hz

- **Severity:** medium
- **Area:** safety
- **Location:** `v2/firmware/control.py:142`
- **Status:** confirmed
- **Reference:** RGX-2-003 §3 States ('auto-recover where the cause clears'), D7; VESC bldc 5.02 mc_interface.c:2116-2123

**Claim.** R_VESC_FAULT exits on the first telemetry frame with fault == 0 (control.py:141-143). VESC clears m_fault_now automatically when m_ignore_iterations expires (m_fault_stop_time_ms, default 500 ms), whether or not the cause persists.

Conditions that the command itself creates therefore oscillate: overvoltage from regen I*R near a full bank (F09), undervoltage from boost sag on a low bank (F19), and overcurrent. There is no fault counter, latch or back-off.

More generally, every LIMP exit tests only the reason that caused entry (control.py:132-143), not all RUN preconditions. For example, LIMP(R_VESC_FAULT) can exit with thr_failed set, and LIMP(R_THROTTLE) can exit with the link stale. One RUN tick then executes the strategy on stale or invalid inputs.

**Failure scenario.** Full-throttle boost on a 16 V bank sags the A1 terminal below 8 V. A1 raises UNDER_VOLTAGE and the firmware enters LIMP. The bank recovers and the fault auto-clears after 500 ms, the firmware exits LIMP, the slew re-ramps, and the fault repeats. Assist surges at about 1.5 Hz. The regen-near-full case has the same shape, pulsing the rear brake.

**Evidence.** Code: control.py:141-143 `elif self.reason == R_VESC_FAULT: if not v.fault and not link_stale: self._exit_limp()`.

VESC: bldc mc_interface.c:2118-2122 (auto-clear after m_ignore_iterations); mcconf_default.h:400 `MCCONF_M_FAULT_STOP_TIME 500`.

Scratch results: demo_limp_paths.py #4, alternating fault frames give a RUN/LIMP state every tick ([1, 2, 1, 2, ...]).

docs/V2_PLAN.md §2.2 item 4 'No debouncing'.

**Suggested fix.** 1. Require a clean interval before exit (for example 1 s with no fault).
2. Use exponential back-off, and latch after N faults within a time window.
3. On any LIMP exit, re-evaluate all RUN preconditions (link fresh, no fault, throttle valid and armed), not only the entry reason.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F18: Assist is never re-armed from idle, and throttle-fault recovery has no hysteresis. A held or stuck throttle gives assist immediately at boot and immediately after a fault clears, and a chattering fault never latches

- **Severity:** medium
- **Area:** safety
- **Location:** `v2/firmware/control.py:139`
- **Status:** confirmed
- **Reference:** RGX-2-003 §3 States (INIT self-check); RGX-2-001 §10.3; RGX-2-100 Rev D sheet 3 note 6; docs/V2_PLAN.md §2.2 item 4; research/hardware-design.md §9b

**Claim.** INIT exits on the FW reply plus v_in (control.py:119-120), and LIMP exits via _exit_limp (control.py:139-140, 151-153). Neither checks that the throttle has returned to idle, so the strategy runs on the first RUN tick with whatever the throttle reads.

Once failed has latched, a single in-window median sample sets _bad_since = -1 and failed = False (sensors.py:108-111), and the loop leaves LIMP in the same tick. The persistence timer also resets on any one good sample, so a fault that alternates in and out of the window never latches, and the in-window samples keep passing their value through. No power-up idle check exists either.

**Failure scenario.** - Boot with the throttle held at 60 %: the first RUN tick commands 2 A, and it reaches 24 A after about 120 ms. A mechanically stuck throttle launches the bike as soon as A1 answers the handshake.
- The rider holds full throttle and the connector intermits for 70 ms: assist drops, failed = True, LIMP. The first good sample 10 ms later returns 0.909 with failed = False, and assist ramps 0 -> 36 A with no rider action.
- Repeated intermittency gives surging assist. Combined with F03, it also toggles the rear brake.

**Evidence.** Code: control.py:119-120 `if v.fw_major and v.v_in > 1.0: self.state = RUN`; control.py:139-140 `if not thr_failed: self._exit_limp()`; sensors.py:108-109 `self._bad_since = -1; self.failed = False`.

Scratch results:
- demo_limp_paths.py #3: 'first ticks after boot: [2.0, 4.0, 6.0] ... after 250 ms: 24.0' and 'single good sample -> state 1 assist resumes: [2.0, 4.0, ...]'.
- throttle_demo.py (intermittent short, then release): '(80, (0.0, False)) (90, (0.0, True)) (100, (0.909, False)) (110, (0.909, False))'.

**Suggested fix.** 1. Add an assist-armed flag, cleared on entry to INIT or LIMP and on any throttle fault. Set it only after the throttle has read inside the idle deadband (F05) for about 200 ms. The envelope zeroes cmd > 0 while disarmed.
2. Latch the throttle fault until the signal has been continuously in-window for at least 200 ms and has passed through idle.
3. Make the persistence counter leaky (decrement on good samples) so chatter still latches.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F20: The slew limiter scales its step by measured dt, so after any core-0 stall (a flash-flush erase, for example) one command can step the torque by up to the full cap

- **Severity:** medium
- **Area:** safety
- **Location:** `v2/firmware/control.py:90`
- **Status:** confirmed
- **Reference:** RGX-2-003 D8, D13; config.py:53 comment

**Claim.** max_step = SLEW_A_PER_S * dt, where dt is the wall-clock gap since the previous tick (control.py:157-158). During a stall A1 holds the last command, so the whole accumulated allowance arrives as a single step on the first tick afterwards.

That breaks config.py:53's intent ('no strategy bug may step the torque') and D8's '10 ms command granularity for the slew limiter'. Stall sources exist by design: a standstill flash flush stalls core 0 about 45 ms per sector erase (D13, F12/F23), and it runs exactly when the rider is about to pull away. Any stall up to the 250 ms LIMP threshold is possible.

**Failure scenario.** The bike is at standstill and the log flush has started. The rider twists the throttle during an erase. A 45 ms stall gives a first step from 0 to 9.0 A; a 150 ms stall gives 0 to 30.0 A in one frame, against a nominal maximum step of 2.0 A. From standstill, with the clutch grounding the carrier, the bike lurches.

**Evidence.** Code: control.py:157-158 `dt = ((now_ms - self._last_ms) if self._last_ms >= 0 else config.TICK_MS) / 1000.0`; control.py:90 `max_step = config.SLEW_A_PER_S * dt`.

Scratch results (demo_time.py): 'stall 45 ms: first command step 0 -> 9.0 A', 'stall 150 ms: ... 30.0 A'.

**Suggested fix.** Clamp the dt used by the slew limiter to one frame, e.g. dt_slew = min(dt, config.TICK_MS/1000), so the limit applies per command frame. Optionally give the strategy its own clamped dt as well.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F21: Timestamps from ticks_ms (which wrap at 2^30 ms, about 12.4 days) are subtracted directly. At the wrap the envelope outputs about -2.1e8 A, which MicroPython's struct silently truncates into a rising, uncommanded current; the timeouts also go blind

- **Severity:** medium
- **Area:** platform
- **Location:** `v2/firmware/control.py:157`
- **Status:** confirmed
- **Reference:** MicroPython time.ticks_ms/ticks_diff docs; RGX-2-003 D8, §3 safety envelope; RGX-2-001 §10.4

**Claim.** On rp2, time.ticks_ms() returns mp_hal_ticks_ms() & (2^30 - 1) (extmod/modtime.c:151-152, py/mpconfig.h MICROPY_PY_TIME_TICKS_PERIOD = MP_SMALL_INT_POSITIVE_MASK + 1). The MicroPython docs forbid direct subtraction of these values. main.py uses ticks_diff for its own scheduling but passes raw ticks_ms into loop.tick.

Raw subtractions:
- control.py:157 (dt)
- vesc.py:316 (age_ms)
- vesc.py:277 (rtt)
- sensors.py:79 (wheel zero timeout)
- sensors.py:105 (throttle fail timer)
- ui.py:215 (standstill)

At the wrap, dt is about -1,073,741 s. The envelope applies slew after the caps, so it returns last_cmd - 2.147e8. CPython raises struct.error. MicroPython packs out-of-range ints with no error, using two's-complement truncation (binary.c:340-343, mpz.c:1592-1620). Right after the wrap, age_ms is about -1.07e9, so link-loss detection is blind, and the wheel and throttle timeouts never fire.

**Failure scenario.** After 12.43 days of continuous uptime (bench supply, soak rig, or S1 closed while charging through J4; unlikely on BT1's ~4 h keep-alive), the wrap tick sets i_cmd to about -2.147e8 A. A1 receives truncated values that step by +2 A per tick, from +2 A upward, in the assist direction with the throttle at zero, clamped only by A1's own limits. This persists until some LIMP resets i_cmd.

The two reviewers' reproductions differ in the exact on-wire sequence ([2, 4, 6, 8 ...] A versus min/max 0..1400 A), but both show uncommanded, rising current. Host tests cannot see this because CPython's struct raises.

**Evidence.** Scratch results:
- rt/wrap_mp.py on MicroPython v1.29: 'state 1 i_cmd -214746964.8 last SET_CURRENT on wire (A): 1400.0 min/max sent: 0.0 1400.0'.
- MicroPython struct check: 'no error; packed -> 2.0 A' for -214748362800 mA.
- demo_time.py: 'CPython tick at now=0 raised struct.error ... i_cmd=-2.14748e+08 A'; truncated commands [2.0, 4.0, 6.0, 8.0, ...].

References: MicroPython v1.22.2 extmod/modtime.c:152; py/mpconfig.h:1555/1925; docs/library/time.rst ('directly using subtraction on them will produce incorrect result').

**Suggested fix.** 1. Use time.ticks_diff for every timestamp difference, injecting a diff function so the host tests still run.
2. Clamp dt to [0, 5*TICK_MS].
3. Re-apply the absolute caps after the slew step.
4. Range-check and clamp amps in pack_set_current.
5. Add a host test with now_ms wrapping at 2^30.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F22: INIT (and any other non-LIMP state) shows the normal RIDE page, so the rider cannot tell that regen and assist are disabled

- **Severity:** medium
- **Area:** safety
- **Location:** `v2/firmware/ui.py:152`
- **Status:** confirmed
- **Reference:** RGX-2-003 D14, D6, §3 States; RGX-2-001 §1

**Claim.** The page choice is `if st == LIMP or sn[SN_FAULT]: SYSTEM else RIDE`, and the RIDE page never shows the state. In INIT, control.py forces i_cmd = 0, but telemetry requests continue, so the RIDE page shows live speed and bank voltage and looks healthy. INIT can persist for a whole ride (F04). With no motor torque, the carrier brake transmits nothing.

**Failure scenario.** A1's UART app is not yet listening when the Pico sends its single FW request, so the state stays INIT. The rider sees '22.0 km/h / 31.4 V / 0.0 A AST', squeezes the rear lever expecting regen, and gets no rear braking at all, with no warning on screen.

**Evidence.** Scratch results (demo_pages.py): 'INIT (stuck, telemetry live): [' 22.0 km/h', ' 31.4 V', '  0.0 A AST', 'rtt  0 e0']'.

Code: control.py:186-187 `if self.state != RUN: self.i_cmd = 0.0`; ui.py:152 page selection.

**Suggested fix.** 1. Show SYSTEM, or a prominent 'INIT / NO FW' banner, for every state other than RUN.
2. Put a permanent state glyph on the RIDE page.
3. Fix the root cause with the FW retry in F04.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F23: A flush is one unbounded, non-abortable write of up to 96 KB. A rider who sets off mid-flush rides through 0.7-1.3 s of repeated core-0 lockouts, each up to 424 ms, which exceeds A1's 200 ms timeout

- **Severity:** medium
- **Area:** safety
- **Location:** `v2/firmware/ui.py:221`
- **Status:** confirmed
- **Reference:** RGX-2-001 §10.9; RGX-2-003 D13, D7 (200 ms A1 timeout)

**Claim.** _flush writes every ring record in one loop, one 24 B f.write per record (up to 4096 records = 96 KB = about 25 sector erases), and never re-checks motion. With the n//2 gate, every flush is at least 48 KB.

Measured on a Pico-sized littlefs:
- 2048 records: 13 erases and 195 pages.
- 4096 records: 25 erases and 387 pages.
- The longest back-to-back stall within a single littlefs call is 1 erase plus 8 pages: 48 ms typical, 424 ms maximum (W25Q16JV).

Each record slice `mv[off:off+REC_SIZE]` also allocates.

**Failure scenario.** The rider waits 3 s at a light with the ring at least half full, and the flush starts. The light changes and the rider pulls away during the next ~0.7-1.3 s (typical; 5.8-11 s at datasheet maximum). Core 0 freezes for up to ~48 ms at a time, so throttle input is ignored and the command is held stale.

A maximum-case erase (up to 424 ms) exceeds A1's 200 ms UART timeout, and A1 releases the motor while the bike is moving. That drops assist, or regen, which is the rear brake. This is exactly the 'flash writes while moving' that D13 and spec §10.9 prohibit.

**Evidence.** Code: ui.py:217-225 (all records in one pass, then ring.clear()); ui.py:248-249.

MicroPython: ports/rp2/rp2_flash.c:169-176 (multicore lockout plus IRQs disabled).

Scratch results:
- rt/flushcost.py: 'flush 2048 rec: 13 erases, 51 prog calls (195 pages) -> both-core stall typ 0.66 s, max 5.8 s'; 'flush 4096 rec: 25 erases ... typ 1.28 s, max 11.2 s'.
- rt/percall.py: 'longest single-call ... flash stall ... write typ 48.2 ms, max 424 ms'.
- demo_flush.py scenario D: a single 72,720 B flush (18 sectors).

D13 cites '~45 ms per 4 KB sector erase'.

**Suggested fix.** 1. Flush in chunks of at most one 4 KB sector per core-1 iteration, written as contiguous ring segments (at most 2 writes, no per-record slicing).
2. Re-check the multi-signal standstill predicate (F12) before every chunk and abort on motion or throttle, keeping a watermark so the remainder goes out at the next standstill.
3. Document the per-operation stall against the 200 ms A1 timeout in D13.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F25: Core 1 has no exception guard and no liveness monitoring: any exception silently freezes the display and stops logging

- **Severity:** medium
- **Area:** platform
- **Location:** `v2/firmware/ui.py:227`
- **Status:** confirmed
- **Reference:** RGX-2-003 D2 (dual-core hardened), D13, D14

**Claim.** Core1.run() is a bare while-True loop. On rp2, an uncaught exception in a _thread only prints 'Unhandled exception in thread started by ...' to USB CDC, which is not connected on the bike. The thread then returns and core 1 idles in WFI.

The WDT is fed only by core 0 (main.py:48-49), and core 0 has no core-1 heartbeat, so the death goes undetected. Possible triggers: OSError from _flush (ENOSPC, EIO, a corrupt littlefs; see F13), MemoryError (auto-GC is disabled globally, F24), or an I2C OSError outside Panel.render's try.

**Failure scenario.** Core 1 dies from any of these triggers. The SSD1306 keeps its last image, so the rider sees plausible but frozen speed and bank voltage for the rest of the ride. A later LIMP (link loss, VESC fault) is never shown, and the RAM ring stops recording.

**Evidence.** Code: ui.py:227-250 has no try/except around the loop body. main.py:36 `_thread.start_new_thread(core1.run, ())`. main.py:48-49 feeds the WDT from core 0 only.

MicroPython: py/modthread.c:192 `mp_printf(MICROPY_ERROR_PRINTER, "Unhandled exception in thread started by ");`, then :202 `mp_thread_finish();`. ports/rp2/mpthreadport.c:116-117 ('returning from here will loop the core forever (WFI)').

Scratch results (demo_enospc.py): the display still shows the last rendered frame after the thread dies.

**Suggested fix.** 1. Wrap each section of the Core1.run body (render, log, flush) in try/except Exception, with counters and back-off, so core 1 never exits.
2. Publish a core-1 heartbeat counter that core 0 checks. If it stops, raise a visible fault or reset, or feed the WDT only while the heartbeat advances.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F26: The parser does not cap LEN: a false start commits it to swallowing up to 258 B (about 10 replies, about 206 ms) of real telemetry

- **Severity:** medium
- **Area:** protocol
- **Location:** `v2/firmware/vesc.py:152`
- **Status:** confirmed
- **Reference:** RGX-2-003 D11, D7 (250 ms silence -> LIMP); FrameParser docstring (<= 78 B)

**Claim.** The _LEN state accepts any value from 1 to 255 (vesc.py:154 `_PAYLOAD if 0 < b else _HUNT`). The class docstring itself says 'nothing our command set can receive exceeds 78 B'. On FW 5.02, GET_VALUES full is 73 B, SELECTIVE is 20/22 B and FW_VERSION is about 23 B.

Any noise 0x02 followed by a large byte, or a flip in the high bits of a real LEN byte, blinds the link for up to LEN+3 bytes. At 25 B per 20 ms that comes close to LINK_TIMEOUT_MS = 250 on its own, before any additional loss or the backtracking problem in F06.

**Failure scenario.** '0x02 0xFF' noise before a reply swallows the next 258 B, about 10 replies or 206 ms. Any additional loss crosses 250 ms and drops to LIMP, which zeroes motor torque and so the rear brake.

A LEN of 20 with bit 7 flipped reads as 148 and loses about 6-7 replies (140 ms). In simulation, one [0x02, 0xFF] glitch during regen caused LIMP in 3 of 300 random telemetry streams.

**Evidence.** Scratch results:
- lencap.py (1500 trials, assist telemetry), stray 0x02 plus a random byte: as shipped, mean 8.15 frames lost, max 60, P(LIMP) = 13.8 %. With LEN > 80 rejected: mean 0.80, max 14, P = 0.1 %.
- lencap.py, single-bit flip: 1.33 / 47 / 0.5 % as shipped vs 1.09 / 11 / 0.0 % with the cap.
- test_parser_len_corruption_recovery_cost: '1 bit flip in LEN of frame 3: 7 of 20 frames lost (= 140 ms)'.
- sim_glitch.py: '[0x02,0xFF]: LIMP (regen->0 A) in 3/300 seeds'.
- test_len_msb_flip_costs_more_than_one_frame: 0 frames delivered after the flip.

**Suggested fix.** In _LEN, reject b > _MAX_EXPECTED (for example 80, derived from the largest reply the firmware requests) and count the rejection. Keep the cap even after adding backtracking (F06), because it bounds worst-case latency.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F27: The 200 A/s slew limit takes 200 ms to reach full regen, which is missing from D9's lever-to-torque budget and cancels most of the latency gain the lever sensor is meant to deliver

- **Severity:** medium
- **Area:** design-conformance
- **Location:** `v2/firmware/config.py:53`
- **Status:** confirmed
- **Reference:** RGX-2-003 D8, D9 (latency budget), §3 safety envelope (slew limit)

**Claim.** D9's latency table budgets 'Command wire + VESC ramp ~5 ms' and totals about 35 ms lever-to-felt-torque with a lever sensor (about 100-160 ms with slip-only sensing). It justifies the Rev E lever-sensor hardware change by the resulting 4x reduction.

The envelope's slew limit, SLEW_A_PER_S = 200 A/s (2 A per 10 ms tick, control.py:90-95), applies to regen as well. The Placeholder's lever path requests -40*(1-s) immediately, so the slew becomes the dominant term:
- 2 A at <= 10 ms.
- About 6 A (15 %) at the 35 ms point.
- 20 A at about 100 ms.
- 40 A at about 200 ms.

The slip-only path gets the same 200 ms added on top of its 100-160 ms. D8 cites '10 ms command granularity for the slew limiter', but no document budgets the ramp.

**Failure scenario.** The rider grabs the rear lever at 20 km/h with BRAKE_FITTED set. Rear braking reaches half strength about 100 ms after the lever, roughly three times the D9 figure, and full strength about 200 ms after it. The lever sensor that D9 recommends delivers far less of the promised improvement, and the owner decides on a hardware change using a budget that leaves out the largest term.

**Evidence.** config.py:53 `SLEW_A_PER_S = 200.0`; control.py:90-95; strategy.py:32-33 (lever path requests the full regen command at once); RGX-2-003 D9 table (lines 190-198: 'Command wire + VESC ramp | ~5 ms', total '~35 ms'); D8 (line 184). Arithmetic: 40 A / 200 A/s = 200 ms; 20 A / 200 A/s = 100 ms.

**Suggested fix.** 1. Add the slew ramp to the D9 budget explicitly and choose the rate from it. For example, use an asymmetric limiter: a fast regen-direction slew (e.g. 1000 A/s, 40 A in 40 ms) and a slower assist-direction slew. Alternatively allow one bounded step on a lever edge.
2. Confirm the choice at FW-5 with a bench measurement of the VESC current-loop response and of the rider-felt onset.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F30: SPD_MIN_PERIOD_US = 5000 µs rejects only speeds above 252 km/h, not the 60 km/h its comment says (60 km/h is a 21,000 µs period)

- **Severity:** medium
- **Area:** correctness
- **Location:** `v2/firmware/config.py:44`
- **Status:** confirmed
- **Reference:** RGX-2-001 §4.9; RGX-2-003 Rev B (no §7.1 exists)

**Claim.** The noise-rejection floor is 4.2 times too permissive. With SPD_PPR = 6 and WHEEL_CIRC_M = 2.10, a 5 ms period is 60e6/(5000*6) = 2000 rpm, i.e. 252 km/h. 60 km/h is 476 rpm, a 21.0 ms period. Any spurious pair between 5 and 21 ms (60-252 km/h) passes as a real speed, and this constant is the only glitch filter in the sensor path.

The constant also has no authoritative source: the '§7.1' citations at config.py:43 and sensors.py:21 point to a section that does not exist in RGX-2-003 Rev B.

**Failure scenario.** A glitch or start-up pair of 8-20 ms is accepted as 63-158 km/h. Downstream:
- envelope() applies the crossover guard (speed > 28 km/h zeroes regen when the bank is above 39 V).
- Slip reads near 1 during braking.
- KCrossCheck ingests a bogus ratio.
All of these act on a physically impossible speed that the stated design intent would have discarded.

**Evidence.** Code: config.py:44 `SPD_MIN_PERIOD_US = const(5000)  # <5 ms period = >60 km/h = noise, reject`; sensors.py:63.

Scratch results:
- `kinematics.kmh(kinematics.wheel_rpm_from_period_us(5000))` = 252.0 km/h; period_us_for_kmh(60) = 21000 µs (scenarios.py 'A').
- test_sensor_review.py::test_min_period_constant_is_252_kmh_not_60 passes.

**Suggested fix.** 1. Derive the constant instead of hard-coding it: SPD_MIN_PERIOD_US = 60e6 / (SPD_PPR * rpm(V_MAX_KMH)) with V_MAX_KMH = 60, giving about 21,000 µs.
2. Add a per-phase minimum (F31).
3. Fix the dangling §7.1 references.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F31: Wheel-period capture does not reject glitches or bounce: one spurious transition splits a phase and is accepted as up to about 4x the true speed, and FIFO overflow is silent

- **Severity:** medium
- **Area:** correctness
- **Location:** `v2/firmware/sensors.py:71`
- **Status:** confirmed
- **Reference:** RGX-2-001 §10.1, §9 (SPD conditioning R6/C6); RGX-2-100 Rev D sheet 3 note 4; RGX-2-002 BOM W1

**Claim.** The PIO program exits a phase loop on the first sample of the opposite level (jmp pin polled every 1 µs), with no minimum-width check. The Python side sums whichever two words arrive next. One short spurious transition inside a phase therefore produces two words: the partial phase plus ~1 µs, and the remainder plus the next phase. Both sums pass SPD_MIN_PERIOD_US and are published as speeds. There is no plausibility or rate-of-change check.

FIFO overflow is also silent: push(noblock) drops the newest word, FDEBUG.RXSTALL is never read, and the 4-word FIFO is not joined. After an overflow, the next pair straddles the gap.

The SPD conductor runs in the same Higo Z910 jacket as the three phase leads (BOM W1), on a 10 kOhm pull-up node.

**Failure scenario.** Emulated at 10 km/h (126 ms period): one 2 µs low glitch 30 ms into a high phase makes the reported speed go 10.0 -> 42.0 km/h for 90 ms -> 13.1 -> 10.0. During carrier-held braking that gives s_est = 0.76, and Placeholder regen falls from about 40 A toward 9.5 A. With the bank above 39 V, the 42 km/h reading also exceeds CROSSOVER_KMH and zeroes regen. Correcting SPD_MIN_PERIOD_US (F30) alone would not reject this.

**Evidence.** Code: sensors.py:26-40 (exit on the first opposite sample; push(noblock)); sensors.py:71-78 (blind pairing); the only filter is sensors.py:63.

RP2040 PUSH noblock semantics: on a full RX FIFO the newest data is lost and FDEBUG_RXSTALL is set. MicroPython asm_pio defaults to fifo_join JOIN_NONE.

Scratch results:
- scenarios.py 'B': 't=670 ms kmh=42.0; t=760 ms kmh=13.1; t=890 ms kmh=10.0'.
- test_sensor_review.py::test_glitch_split_phase_accepted_as_4x_speed passes.

**Suggested fix.** 1. In PIO, qualify each edge: after a level change, re-sample for 20-50 µs and fall back into the counting loop on a bounce.
2. In Python, reject phases below a minimum half-period and periods that change by more than a physically bounded ratio (about ±30 % per period at <= 1 g).
3. Use fifo_join = PIO.JOIN_RX, and check RXSTALL through machine.mem32 so _pending can be reset after a drop.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F32: The 'allocation-free' claims for core 0 and core 1 are false. On rp2 every float is a heap object; the link path, snapshot copies and core-1 rendering allocate on every tick, which removes D2's mitigation for the unresolved cross-core GC register race

- **Severity:** medium
- **Area:** platform
- **Location:** `v2/firmware/vesc.py:8`
- **Status:** confirmed
- **Reference:** RGX-2-003 D2, D10 ('Pure, no alloc'), D11, D12, §3 ('allocation-free'), gate FW-0/FW-2

**Claim.** rp2 uses the default MICROPY_OBJ_REPR_A, so every float result and every array('f') element read creates a new 16 B heap object. Function frames larger than 44 B are also heap-allocated per call.

Core 0: the link RX/TX path allocates on every tick, contrary to vesc.py:8 and D11:
- memoryview(buf)[:n] on every write (vesc.py:283, 289, 297)
- self._rxmv[:take] on every read (vesc.py:307)
- data[i:i+take] in the payload state (vesc.py:161)
- '>' + fmt string concatenation per field (vesc.py:222)
- struct.unpack_from tuples (vesc.py:215, 222)
- a boxed float per field (vesc.py:223)
- float EMAs (vesc.py:278) and amps*1000 (vesc.py:93)

Core 1: snapshot.read into array('f') boxes every element; pack_record does float math and round(); rendering builds formatted strings; each flush slices a memoryview per record; and with the display absent, _try_init builds a new SSD1306 with a fresh 1 KB buffer every 200 ms.

D2 accepts GIL-less dual-core only on the condition that core 1 makes no steady-state allocation ('cross-core allocator contention ≈ 0'). The rp2 GC scans core 1's stack array but not its CPU registers when core 0 collects, a gap the maintainer explicitly left open (PR #8310). The seqlock protocol itself is sound, but D12's 'no lock core 0 can ever block on' is false, because every boxed float takes the GC mutex. D10's 'Pure, no alloc' strategy contract cannot be met by any law that computes a float.

**Failure scenario.** Core 0 runs gc.collect() every 100 ms (and once at main.py:42, after core 1 has started). If that happens while core 1 is between gc_alloc returning a new float or string and storing it on its VM stack, the object is referenced only from a core-1 register. It is swept and reallocated, and two live objects share memory: corrupted values, possibly in Values or i_cmd on core 0, or a HardFault. This is the #7124 class that FW-0 is meant to retire, made far more likely by an allocation rate D2 assumed to be about zero.

Anyone enforcing the rule with micropython.heap_lock() gets a MemoryError on the first link tick.

**Evidence.** MicroPython v1.29 (gc.mem_alloc deltas, GC disabled):
- link.tick with no RX: 128.0 B/call.
- link.tick with one 25 B reply: 784.5 B/call.
- parse_selective: 512.0 B/call.
- parser.feed of one frame: 576.0 B/call.

Breakdown in blocks per call (rt/breakdown.py): Snapshot.write 24, Snapshot.read 24, parse_selective 13, VescLink.tick 4, kinematics 14, envelope 9. rt/alloc_rate.py: 'CORE1 run(): 31.5 blocks/iteration'. mp_alloc_ui.py: snapshot.read 768 B/call, ring.append 640 B/call, RIDE page text 512 B/frame, flush slicing 2176 B per 64 records.

MicroPython source: py/objarray.c array_subscr (m_new_obj for memoryview slices); py/objfloat.c:186; py/objfun.c:202, 273-274; ports/rp2/mpthreadport.c:93-97 (only core1_stack is scanned); PR micropython#8310 (dpgeorge: 'both core0's and core1's registers need be collected ... so far doesn't seem to be needed').

**Suggested fix.** 1. Either make core 1 genuinely allocation-free, or accept the risk explicitly in D2 and design the FW-0 soak around the real allocation profile. Means: copy the snapshot as raw words with a viper ptr32 loop, pack log records from integers, render from pre-scaled integers into fixed bytearrays, and reuse one SSD1306 object.
2. On core 0, pre-build fixed memoryviews in __init__, copy payload bytes with an index loop or viper, precompute format strings or decode bytes manually into a preallocated array, and keep RTT and age as integers. Implement the parser and decoder in viper or native code, as D1/D11 decided (F58).
3. Correct the vesc.py:8 comment and the D10/D11/D12/§3 wording.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F33: Every scheduled gc.collect() scans the whole 96 KB log ring word by word, adding an unbudgeted 2-3 ms to one core-0 tick in ten

- **Severity:** medium
- **Area:** platform
- **Location:** `v2/firmware/config.py:64`
- **Status:** confirmed
- **Reference:** RGX-2-003 D1 (overturned by a GC pause > 5 ms), D13 (96 KB ring), §3 per-tick budget ≤ 4 ms

**Claim.** MicroPython's GC is conservative and scans every word of every reachable block. The ring bytearray is 98,304 B (24,576 words) on the GC heap, reachable from the Core1 object, so every collection walks it with VERIFY_PTR. At roughly 10-15 cycles per word that is about 250-370k cycles, 2-3 ms at 125 MHz, for the ring alone; sweeping the rest of the heap comes on top.

This runs inside every tenth core-0 tick (main.py:56-58). While it runs, core 1's allocations block on the GC mutex. Neither D1's '≤ 5 ms GC pause' budget nor main.py's 9 ms deadline threshold allows for it.

**Failure scenario.** On FW-2 hardware, the GC tick costs about 3-5 ms of collection plus the tick's own interpreted work. That lands near or over the 9 ms miss threshold, so deadline_miss climbs by about 10 per second, and D1's overturn criterion (GC pause > 5 ms) may trip because of the logger rather than the control code.

**Evidence.** config.py:64 `LOG_RING_RECORDS = const(4096)   # x 24 B = 96 KB`; ui.py:63 `self.buf = bytearray(self.n * REC_SIZE)`. py/gc.c gc_mark_subtree (~526-590) scans every word of each block. MicroPython has no no-scan allocation flag. main.py:56-61 times the collection into gc_max_ms. The cycle figure is an estimate, to be confirmed at FW-2.

**Suggested fix.** 1. Measure at FW-2 with and without the ring.
2. If it matters, shrink the ring (for example 1024 records, flushed more often at standstill), or keep log data off the scanned heap (for example a firmware build that reserves a static RAM region accessed through uctypes.bytearray_at).
3. Budget the result explicitly in D1/D13.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F34: The test fixtures hand-pick ERPM and i_motor values that are physically inconsistent: the regen test asserts the VESC-motoring sign and the assist test uses impossible kinematics. A green suite therefore certifies F01/F02 and blocks the correct fix

- **Severity:** medium
- **Area:** testing
- **Location:** `v2/tests/test_control.py:179`
- **Status:** confirmed
- **Reference:** RGX-2-003 §4 Testing, D10

**Claim.** test_regen_ramps_when_carrier_dragged (lines 174-185) models carrier-held counter-rotation as negative ERPM with DIR_SIGN = +1 and asserts a negative command. By VESC semantics, negative current at negative ERPM is motoring: the runaway case in F01.

test_assist_flows_and_snapshot_publishes (line 164) feeds erpm = +1000 at 100 wheel rpm, i.e. the motor at +100 rpm while the carrier is supposedly grounded. The physical value is -500 rpm, and the test value is what keeps s at 1 and hides F02.

The fixtures also report i_motor with the command's sign, whereas the VESC reports it motoring-positive. Both tests fail if DIR_SIGN is set to the value the '+A = assist' convention forces (-1). Loop-level safety paths are not exercised either (see F37).

**Failure scenario.** A developer corrects DIR_SIGN to -1 after B-2. Two tests fail, so the change is reverted, or the tests are edited to match the Placeholder, and the physically wrong behaviour stays locked in.

**Evidence.** test_control.py:179 `erpm = int(-config.K_RATIO * 150.0 * config.POLE_PAIRS * 0.9)` (DIR_SIGN not applied); :185 `assert loop.i_cmd < -1.0`; :164 `erpm=1000` with sensors.rpm = 100.

Scratch run with DIR_SIGN = -1 (run_tests_dirsign_neg.py): 'FAILED ...test_assist_flows_and_snapshot_publishes', 'FAILED ...test_regen_ramps_when_carrier_dragged', '2 failed, 41 passed'.

**Suggested fix.** 1. Generate telemetry from a small Willis/clutch plant parameterised by A1's orientation, with i_motor reported as the VESC does.
2. Assert on VESC-side semantics: sign(I) against sign(ERPM) must be motoring during assist and generating during regen.
3. Replace every hand-picked ERPM literal with the plant helper.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F35: The parser corruption property test asserts a false property and passes only by RNG luck. LEN-byte flips, stray 0x02 bytes, mid-frame entry and reordering are untested, so the 'no stuck states' requirement is unverified

- **Severity:** medium
- **Area:** testing
- **Location:** `v2/tests/test_vesc.py:86`
- **Status:** confirmed
- **Reference:** RGX-2-003 §4 ('Parser property tests: random corruption/truncation/reordering must yield no false-accepts and no stuck states'), D11; v2/research/decisions.md 2026-08-02

**Claim.** test_parser_never_false_accepts_corruption flips 2 random bits per byte position and asserts that 'recovery must cost at most one following good frame'. It passes only because seed 99 never flips LEN bits 5-7, and because its payload (erpm=1234, v_in=30, zero currents) contains no 0x02 byte.

Tried exhaustively, flips of LEN bits 5-7 violate the test's own assertion. test_parser_recovers_from_garbage_flood (seed 5) fails for 79 of 300 seeds. The decision log's 'recovery costs ≤ 1 following frame ... pass by construction' is false.

RGX-2-003 §4 also requires reordering and random-truncation properties. The suite has no reordering test and only one fixed truncation case. The 'no stuck states' property is violated by F06 and F26.

**Failure scenario.** CI stays green with a parser that locks up for seconds on one stray byte (F06), or goes blind for about 200 ms on one LEN flip (F26). The defect would first show up on the road.

**Evidence.** Scratch results:
- exhaustive_bitflip.py: 'violating the committed assertion: [(1, 5), (1, 6), (1, 7)]'.
- test_len_msb_flip_costs_more_than_one_frame: the same payload with frame[1] ^= 0x80 plus two good frames gives got == [], so under the committed code got[-1] would raise IndexError.
- test_one_stray_byte_blinds_parser_indefinitely: 0 of 500 frames delivered.
- test_parser_garbage_flood_is_seed_lucky: 'fails for 79/300 seeds'.
- prop_reorder.py: 0 false accepts, but up to 9 of 12 following clean frames lost.

**Suggested fix.** 1. Make corruption tests exhaustive: every byte position times every bit, over multi-frame streams.
2. Add stray-byte, mid-frame-entry, 0x02-bearing-payload, reordering and truncation cases.
3. Replace single seeds with a seed sweep, and assert that delivery resumes within a bounded number of good frames.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F37: Mapping RGX-2-003's requirements to tests shows most §3/§4/D-item requirements are untested, including every loop-level safety path

- **Severity:** medium
- **Area:** testing
- **Location:** `v2/tests/test_control.py:1`
- **Status:** confirmed
- **Reference:** RGX-2-003 §3, §4, §5 gates FW-0..FW-7, C-0

**Claim.** Tested today:
- 40 V taper at the midpoint and at 40 V; crossover guard; caps; slew; brake-over-assist at envelope level.
- INIT -> RUN happy path; link-silence LIMP and recovery; latched strategy-exception LIMP; VESC-fault LIMP and recovery.
- Zero on the wire in LIMP (only the TX length is checked, not the 0 A value).
- 100 Hz / 50 Hz schedule; Willis/slip algebra; CRC vector; sampled single-bit corruption; chunking; one truncation case; record codec and ring; single-threaded seqlock.

Untested:
- Throttle-window LIMP and recovery through the loop (control.py:127, 139-140); bank above 40 V (control.py:77).
- Lost INIT reply, INIT self-check, FW-tuple logging.
- TEMP_DIV 1 Hz schedule; parse_full GET_VALUES fallback (vesc.py:231-237, 269).
- k cross-check in the loop and the SENSOR flag; seqlock under a concurrent reader and writer.
- The no-allocation rule (a heap_lock test); no print/sleep/file I/O on core 0.
- Per-tick ≤ 4 ms, GC ≤ 5 ms, WDT.
- Parser reordering, random truncation, multi-byte corruption, bounded 'no stuck states'.
- Replay; hal.py; PIO capture and the throttle window (§10.1-10.3).
- Logger flush and standstill policy; display pages (LINK is missing); C-0 observer; D2 fallback; D6 provisioning.

**Failure scenario.** Every untested item can regress, or is already broken (F03, F04, F06, F14 and others), while the suite reports 43 of 43 green.

**Evidence.** `python3 -m pytest -q` gives 43 passed. pytest-cov control.py missing lines 56, 77, 127, 139-140, 182; vesc.py missing 214, 231-237, 269, 309, 311. Scratch test_review.py: 11 of 12 checks fail on the current firmware.

**Suggested fix.** Add a requirements-traceability table (requirement ID -> test ID) to v2/tests/README or to RGX-2-003 §4, and close the untested rows, starting with the safety ones: throttle fault in the loop, INIT retry, wire current sign, observer mode.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F38: The RTT metric cannot measure RTT: it is quantised to the 10 ms tick, pairs replies with the wrong request, and spikes after lost replies, and the RTT test never checks RTT

- **Severity:** medium
- **Area:** protocol
- **Location:** `v2/firmware/vesc.py:277`
- **Status:** confirmed
- **Reference:** RGX-2-001 §11 item 11; RGX-2-003 gate FW-3, D5, D14 (LINK page RTT)

**Claim.** rtt = self._now - self._req_ms, where both are tick timestamps. The poll runs once per tick immediately after TX, so a reply can never be seen in the tick that requested it. Every steady-state sample is exactly 10 ms for any true latency from 0 to 10 ms, and 20 ms above that.

_req_ms is set only when it is < 0 (vesc.py:298-299) and cleared only by a good frame (including the FW_VERSION reply). A lost reply therefore leaves it pointing at the stale request: the next reply records 30 ms, and an outage of T ms records T. test_link_rtt_and_fw (test_vesc.py:173) checks only the FW tuple.

**Failure scenario.** Bench gate FW-3 and spec §11 item 11 read a constant 10.0 ms (about 10.8 ms with occasional lost replies) whether A1 answers in 3 ms or 9 ms. D5's push-upgrade decision ('adopt only if RTT/freshness data ... shows the request hop matters') is then made on a number that overstates the hop about 3x. After a 5 s unplug and replug, the RTT reads about 1000 ms and decays over dozens of samples.

**Evidence.** Scratch results:
- sim.py with VESC processing of 0.3/2.0/5.0/6.0 ms (true wire RTT about 3.3-9.0 ms): link.rtt_ms = 10.00 in every case.
- test_rtt_after_single_lost_reply_is_attributed_to_stale_request passes: steady 10.0 ms, and one lost reply raises the EMA to 14.0 ms.
- sim_rtt.py (3 ms replies, 1 in 10 lost): 'link.rtt_ms = 10.8 ms'.

**Suggested fix.** 1. Timestamp request TX and frame completion with time.ticks_us() (pass the timestamp into poll/feed).
2. Allow one outstanding request, tagged with its mask. If the next request is due while one is outstanding, count a lost reply and do not pair it.
3. Exclude non-telemetry frames from RTT.
4. Add a test with injected latency.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F39: Tick-timing instrumentation cannot support FW-2, D1 or D13: it measures only the tick body against a 9 ms threshold, keeps no worst-case tick, does not count skipped ticks, and does not separate excused standstill/flush misses

- **Severity:** medium
- **Area:** tooling
- **Location:** `v2/firmware/main.py:64`
- **Status:** confirmed
- **Reference:** RGX-2-003 D1 ('Overturned by'), D13, D14, §4 ('every budget number ... enforced by a counter'), gates FW-2, FW-7

**Claim.** main.py counts a miss only when t0 to the end of loop.tick plus GC exceeds (TICK_MS - 1) = 9 ms (main.py:64-65). It never records the maximum tick duration, so D1's overturn criterion ('worst-case tick > 6 ms') and the 4 ms per-tick budget cannot be evaluated.

Core 0 spends most of each period in the sleep loop (main.py:66-67). A flash-lockout stall there (up to 48 ms typical, 424 ms maximum, imposed asynchronously by core 1) is invisible, and the realign at main.py:70-71 skips the missed ticks without counting them.

D13 says 'deadline misses at standstill are counted and excused', but there is one monotonic counter, ui.py gives core 0 no flush-in-progress signal, and a telemetry request pending across a lockout inflates the RTT EMA.

**Failure scenario.** - On target, ticks run at 7-8.9 ms, above the 6 ms overturn threshold, while deadline_miss stays 0. FW-2 passes and D1's escape hatch is never triggered.
- During a flush, 4-40 ticks of keepalive are skipped with deadline_miss unchanged.
- After a ride with 3 flushes, whatever misses were counted cannot be attributed to riding or to parked flushes, so 'LINK page green' (FW-7) is never meaningful.

**Evidence.** main.py:50 `t0 = time.ticks_us()` (after the sleep). main.py:64-65 `if time.ticks_diff(time.ticks_us(), t0) > (config.TICK_MS - 1) * 1000: loop.deadline_miss += 1`. main.py:70-71 realign with no counter. The snapshot layout has no max-tick field (control.py SN_*). ui.py:249 comment `# both cores stall briefly: D13` with no signalling.

**Suggested fix.** 1. Track tick_max_us plus counts above 4 ms and above 6 ms.
2. Measure lateness as ticks_diff(tick_start, scheduled) and count every skipped period on realign.
3. Have core 1 publish a flush-in-progress word or sequence counter. Keep separate 'miss while moving' and 'excused miss' counters, and freeze RTT updates across a flush.
4. Publish all of these in the snapshot and on the LINK page, and add tools/fw2_report.py.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F40: The D14 LINK page does not exist: link-health counters, I2C errors, ring drops and the k/SENSOR status are never displayed, and gate FW-7 has nothing to judge

- **Severity:** medium
- **Area:** design-conformance
- **Location:** `v2/firmware/ui.py:161`
- **Status:** confirmed
- **Reference:** RGX-2-003 D10, D14, §4, gates FW-2, FW-7

**Claim.** D14 requires three pages: RIDE, LINK and SYSTEM. ui.py has only _page_ride and _page_system, chosen by state, and there is no page-selection mechanism.

SN_FRAMES, SN_OVERRUN, SN_KEST, the resync, RTT and deadline-miss counters, Panel.errors (D14 'I²C errors: count') and ring.dropped are never shown together. SN_FRAMES, SN_OVERRUN, SN_KEST, Panel.errors and ring.dropped are not shown at all. D10's 'SENSOR flag on the LINK page' therefore has no home. §4 says every budget number is 'enforced by a counter visible on the LINK page', and gate FW-7 is 'LINK page green'.

**Failure scenario.** The shell sensor starts double-counting and k_est drifts to 6.5 (30 % off). The RIDE page is unchanged and no flag appears anywhere. With the display unplugged, Panel.errors rises by 5 per second and nobody can see it. FW-7 cannot be evaluated.

**Evidence.** Scratch results (demo_pages.py): 'RUN, k cross-check 30% off (SN_KEST=6.5): [' 22.0 km/h', ' 31.4 V', '  0.0 A AST', 'rtt  0 e0']'; 'display absent: Panel.errors after 5 s of renders = 25 (never shown/logged)'.

grep shows no reference to SN_KEST, SN_FRAMES, SN_OVERRUN or `healthy` in ui.py.

**Suggested fix.** 1. Add a LINK page showing frames, CRC fails, resyncs, overruns, RTT, deadline misses, max tick, GC max, I2C errors, ring drops and SENSOR.
2. Add page rotation (timed cycling at standstill, or a GPIO button in drawing Rev E).
3. Force LINK or SYSTEM to display whenever SENSOR is set or a counter goes bad.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F42: Log files have no header: the FW tuple required by D6, the record-format version and the bench-set kinematic constants are never recorded

- **Severity:** medium
- **Area:** design-conformance
- **Location:** `v2/firmware/ui.py:220`
- **Status:** confirmed
- **Reference:** RGX-2-003 D6, D13

**Claim.** D6 requires 'record the exact FW tuple at every INIT handshake into the log header'. Ride files contain only raw 24 B records. The snapshot has no FW fields (control.py:193-221), so ui.py could not write them even if it tried, and vesc.py:270-273 keeps only major and minor, dropping the hardware name, UUID and test-build number.

There is also no record-format version, no REC_FMT string, no MicroPython version and no config snapshot. K_RATIO, DIR_SIGN, POLE_PAIRS, WHEEL_CIRC_M and SPD_PPR are all [BENCH] placeholders that are expected to change, and the logged slip is computed on target from them.

**Failure scenario.** After B-2 flips DIR_SIGN and the teardown sets K_RATIO = 4.7, old logs (slip computed with k = 5 and the opposite sign) and new logs decode identically and silently corrupt the scoring corpus. A future REC_FMT change misdecodes every existing file with no error. After an A1 re-flash, nothing records which controller firmware produced which ride.

**Evidence.** ui.py:220-224 writes records only. control.py SN_* layout (lines 26-30) has no FW slot. vesc.py:271-272 `self.values.fw_major = payload[1]; self.values.fw_minor = payload[2]`. RGX-2-003 D6.

**Suggested fix.** When a boot's file is created, write a header record: magic, format version, REC_FMT, boot id, full FW tuple (major, minor, hardware name, test build), MicroPython version, K_RATIO, DIR_SIGN, POLE_PAIRS, WHEEL_CIRC_M, SPD_PPR and TICK_MS. Publish the FW fields through the snapshot or a separate write-once slot.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F43: The logged fields cannot support the scoring-sim replay or D3's CAN decision: there is no brake state, LIMP reason, temperature, k estimate or link-health counter, and 10 Hz point samples alias the regen ramps

- **Severity:** medium
- **Area:** design-conformance
- **Location:** `v2/firmware/ui.py:24`
- **Status:** confirmed
- **Reference:** RGX-2-003 D3, D10, D13, D15, §4 Testing (Replay); research/decisions.md Rev A

**Claim.** REC_FMT stores ms, wheel_rpm, erpm, v_bank, i_in, i_motor, i_cmd, slip, throttle, vsys, state and fault. It omits fields the design depends on:
1. SN_BRAKE, the lever state. The scoring metrics integrate over brake windows and brake_demand (v1-legacy/sim/scoring.py `_capture_increments`). In C-0 corpora, with no regen, the lever bit is the only brake-intent signal.
2. SN_REASON, so link, throttle, VESC-fault and strategy LIMPs cannot be told apart.
3. SN_TFET, the only thermal datum.
4. SN_KEST. D10 says every assist episode calibrates k, but the result is lost at power-off.
5. The link-health counters (CRC, resync, frames, RTT, deadline miss, overrun). The Rev A decision says 'link-health counters in every snapshot and log record', and D3's CAN-escape trigger ('CRC-error bursts correlated with phase current') needs them time-aligned with i_motor.

Each 10 Hz record is also a single point sample of a 100 Hz loop, with no min/max, so 200 A/s ramps alias. The timestamp is core 1's read time, and the snapshot version is not stored, so stale snapshots cannot be detected.

**Failure scenario.** C-0 observer rides meant to feed the scoring sim produce logs with no brake windows, so capture and fidelity cannot be computed. A ride with repeated CRC bursts under 40 A regen cannot be correlated with current, so D3's UART-versus-CAN decision still has no data. The k cross-check result is gone after every ride.

**Evidence.** ui.py:24 `REC_FMT = "<IHhHhhhHHHBB"`; pack_record packs only those 12 fields. control.py:218 publishes SN_BRAKE, :220 SN_REASON, :208 SN_TFET, :217 SN_KEST and :211-216 the counters; ui.py logs none of them. research/decisions.md Rev A: 'link-health counters in every snapshot and log record'. RGX-2-003 D3, D15, §4 Replay.

**Suggested fix.** 1. Pack state (2 bits), reason (3 bits), brake (1 bit) and sensor health (1 bit) into the state byte.
2. Add temp_fet, k_est, and per-interval deltas of crc_fail, resync and deadline_miss, growing the record to 28-32 B if needed, with the format version from F42.
3. Consider per-interval min/max for i_motor and i_cmd, and store the snapshot version.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### C02: Every envelope clamp runs before the slew limiter, so each promised 'zero' (40 V bank-full, crossover guard, brake-wins, throttle-failed) takes up to 200 ms to arrive. The unit tests hide this by passing dt=10 s

- **Severity:** low
- **Area:** correctness
- **Location:** `v2/firmware/control.py:89`
- **Status:** confirmed
- **Reference:** Spec §6 BANK FULL and §10.8 (regen tapers to zero toward 40 V); RGX-2-003 §3 Safety envelope (crossover guard 'regen 0', 'regen wins'); D9 latency table; config.py:47,53

**Claim.** envelope() computes the clamped command (throttle-failed at L67, brake-wins at L70, taper and crossover guard at L72-83), then applies the 200 A/s slew against last_cmd (L89-95). The slew is meant to stop strategy bugs from stepping the torque (config.py:53), but it also rate-limits the safety clamps. At a realistic dt=0.01 s the output is still -38 A at 40.5 V bank, with the crossover guard active, and +38 A of assist with brake=True. Reaching zero takes 200 ms in every case. Every envelope test that asserts one of these zeros uses big_dt()=10 s precisely to 'disable the slew term' (test_control.py:33-80), so the suite certifies behaviour that never happens at 100 Hz. This is distinct from F09 (taper endpoint and I·R), F20 (dt scaling) and F27 (onset latency): here the clamps themselves are not enforced.

**Failure scenario.** (a) Lever sensor fitted, rider on full assist (+40 A) grabs the brake. The envelope keeps assisting at +20 A 100 ms after the lever edge, only reaches 0 at 200 ms, and full regen arrives at 400 ms. D9 budgets about 35 ms for lever to torque. (b) The reported bank voltage steps above 40 V (I·R step, a fresh telemetry frame after a gap, or LIMP recovery with a high v_in). Regen continues at -38 → 0 A over 200 ms, adding roughly 0.4-0.6 V to a 6.67 F bank, so A1's 40 V OV trip, not the firmware taper, is what stops regen. (c) The crossover guard trips at 39 V above 28 km/h and still commands -38 A on that tick.

**Evidence.** Scratch demos slew_clamp.py and lever_edge.py: "bank 40.5 V (above V_BANK_MAX), last=-40 A -> -38.0", "bank 39.5 V & 30 km/h (crossover guard), last=-40 -> -38.0", "brake=True while strategy asks +40, last=+40 -> 38.0", "throttle_failed ... -> 38.0", "ms until bank-full 'zero' is reached from -40 A: 200". Through ControlLoop with v_in=40.4 V reported: i_cmd = [-38.0, -36.0, ..., -2.0, -0.0] (20 ticks). Lever edge: "assist removed after 200 ms; i_cmd at +100 ms: 20.0 A (still assisting with brake=True)", "full regen after 400 ms". config.py:47 says `V_BANK_MAX = 40.0  # regen zero here (spec §10.8)`.

**Suggested fix.** Apply the slew limit only to the strategy's requested change. Apply safety reductions after it, as a magnitude clamp that may always move toward zero immediately. For example: slew the raw strategy command first, then clamp regen by f(v_bank) and the guard, zero assist on brake or throttle-failed, and apply the absolute caps. Alternatively, allow unlimited slew toward zero. Rewrite the envelope tests with dt=TICK_MS and a nonzero last_cmd so the enforced zero is actually tested.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### C03: The VSYS log cannot characterise A1 BEC sag as drawing Note 7 requires: it is one ADC conversion per tick, logged as a point sample every 100 ms (1 s at standstill), with no minimum-hold

- **Severity:** low
- **Area:** design-conformance
- **Location:** `v2/firmware/control.py:207`
- **Status:** confirmed
- **Reference:** RGX-2-100 Rev D Sheet 3 NOTE 7; RGX-2-001 §10.6; §4.8 VSYS hold-up

**Claim.** RGX-2-100 Sheet 3 Note 7 says 'U2 SHALL LOG VSYS VIA ADC3 TO CHARACTERISE A1 BEC SAG', and spec §10.6 says 'VSYS logged'. The implementation takes a single ~2 µs ADC conversion per 10 ms tick (control.py:207 → sensors.py:135-136). Core 1 then logs whichever snapshot is current every ≥100 ms while riding, or every 1 s at standstill (ui.py:236-239). No per-interval minimum is kept. The sags that matter are set by C4's 5.5-6.8 ms hold-up and by boost-time bank collapse (F19). A sag shorter than one tick is almost never sampled, a sag shorter than 100 ms reaches the log with probability of roughly duration/100 ms, and its depth is never recorded. This is separate from F29 (the reading is halved) and F43 (missing fields and regen-ramp aliasing): here the specific characterisation purpose of the requirement cannot be met.

**Failure scenario.** During bench or C-0 boosts, the BEC dips to 3.6 V for 25 ms as the bank sags under 40 A. The log shows either nothing (about 75 % chance) or one arbitrary point in the dip. The minimum is never captured, so the brownout margin that F19's reset chain depends on cannot be measured from logs. A sag deep enough to reset the Pico also wipes the RAM ring (F14), so the most important events are never logged at all.

**Evidence.** control.py:207 `sc[SN_VSYS] = self.sensors.vsys()` is a single sample per tick. sensors.py:136 `return self._vsys.read_u16() * 3.0 * 3.3 / 65535.0`. ui.py:236-239 log at LOG_RIDE_HZ=10 or LOG_IDLE_HZ=1 from the latest snapshot. Schematic RGX-2-100 line 608: "U2 SHALL LOG VSYS VIA ADC3 TO CHARACTERISE A1 BEC SAG". Spec :459 gives C4 hold-up as 6.8 ms nominal and 5.5 ms at -20 %.

**Suggested fix.** On core 0, keep a running min (and max) of VSYS, oversampled several times per tick. Publish min/max in the snapshot and reset them when core 1 logs a record. Log the min in the record. Optionally latch a 'VSYS below X' counter and the worst value into a small noinit or retained RAM area that survives a watchdog or brownout reset, and dump it at the next boot.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### C04: The 'final' strategy contract has no reset or enter hook and passes the per-tick dt, so a stateful control law resumes with stale state and a wrong dt after every LIMP or INIT period

- **Severity:** low
- **Area:** design-conformance
- **Location:** `v2/firmware/control.py:173`
- **Status:** confirmed
- **Reference:** RGX-2-003 D10 (Strategy contract final), §3 States (auto-recovering LIMP); research/system-design.md §6.1

**Claim.** The strategy is called only in RUN (control.py:173-176) and receives dt = time since the last *tick* (L157-158), not time since its own last call. Strategy (strategy.py:10-14; RGX-2-003 D10 'Strategy contract (final)') has no reset(), enter() or state-change notification. The deferred law is expected to be a slip regulator with an integrator (research/system-design.md §6.1 `i_regen -= Kp·e + Ki·∫e dt`). After LIMP (link, throttle or VESC fault, which auto-recover) or after INIT, such a law resumes with the integrator and filter state it had before the outage. It is told dt=0.01 s although 300-400+ ms have passed, and nothing tells it the rider's intent may have changed in between.

**Failure scenario.** The rider brakes hard and the slip integrator winds to a large regen value. The telemetry link drops for 300 ms (LIMP R_LINK) and the rider releases the lever. On recovery the law immediately re-issues the stale regen command, slewed from 0 at 200 A/s, while the rider is coasting or on the throttle. A derivative or filter term computes with dt=0.01 over a 400 ms gap and spikes. With the carrier free, regen torque has no reaction path and spins the rotor up unloaded.

**Evidence.** Scratch demo contract_state.py (toy integrating law): "first strategy call after recovery got dt = 0.01 (time since its previous call: ~400 ms)", "strategy integrator on resume: -11.9 -> i_cmd -11.9 while coasting with carrier free", "Strategy has reset/enter hook: False". control.py:173 `if self.state == RUN:` gates the only call site. RGX-2-003-FW.md:222 declares the contract final.

**Suggested fix.** Extend the contract before any law is written. Add a reset() (or on_enter(state)) that ControlLoop calls on every transition into RUN. Pass dt as the time since the strategy's previous call and clamp it, or pass a 'first call after gap' flag. Add a loop test in which a stateful stub asserts it was reset after LIMP → RUN.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### C05: The logged and published bank current i_in uses VESC's sign (positive = discharging), the opposite of the project convention 'positive current into the bank is charging', and nothing records the sign

- **Severity:** low
- **Area:** correctness
- **Location:** `v2/firmware/control.py:203`
- **Status:** confirmed
- **Reference:** v2/README.md Conventions (current sign); RGX-2-003 D13 (log as scoring-sim corpus), §4 Replay

**Claim.** v2/README.md:66 fixes the convention for every document: 'Positive current into the bank is charging.' The firmware copies VESC's avg input current (SELECTIVE bit 3, vesc.py:28) straight into SN_IIN (control.py:203) and into the 24 B log record (ui.py:35), unchanged. In that field, regen (bank charging) is negative. The repo's own test fixture encodes this: test_ui.py:20 builds a braking record (ERPM -7940, ICMD -12.3) with SN_IIN=-3.51. unpack_record labels the field only 'i_in', and the log has no header (F42), so the sign is never recorded. SN_ERPM and SN_IMOTOR are likewise stored in VESC-native sign, not the positive-forward convention.

**Failure scenario.** C-0 and ride corpora are decoded against the README convention for the scoring sim or energy accounting (harvested vs spent Wh, closed-loop invariant §2). Every regen episode then counts as discharge and every boost as charge. The result shows the bank being drained by braking and invalidates harvest scoring and any η estimate built from i_in·v_bank.

**Evidence.** README.md:66 "Positive current into the bank is charging." control.py:203 `sc[SN_IIN] = v.i_in`; ui.py:35 packs it unchanged; tests/test_ui.py:20 `SN_IIN=-3.51` alongside `SN_ICMD=-12.3` in a regen record.

**Suggested fix.** Either negate i_in at ingestion (i_bank = -v.i_in) and name it i_bank_chg, or keep the VESC sign but record it explicitly in a log header or record-format version together with DIR_SIGN. Document the sign of every logged current in the record codec, and add a test that asserts the convention.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F12: 'Verified standstill' uses only the wheel sensor and the throttle, sampled once. A lost SPD signal, or any speed below about 3 km/h, lets a flash flush lock out core 0 while the bike moves, including during regen braking

- **Severity:** low
- **Area:** safety
- **Location:** `v2/firmware/ui.py:207`
- **Status:** confirmed
- **Reference:** RGX-2-001 §10.9 ('No synchronous flash writes while moving'), §1; RGX-2-003 D13, D6/D7 (200 ms A1 timeout)

**Claim.** _standstill treats wheel_rpm <= 0.5 and throttle <= 0.02 held for 3 s as verified standstill, evaluated once before a flush starts. It ignores independent motion evidence that is already in the snapshot: SN_ERPM, SN_IMOTOR, SN_ICMD, SN_BRAKE and SN_STATE.

wheel_rpm reads 0 whenever SPD pulses stop: connector off, R6 open, a sensor fault (its output structure is still unverified, spec §11), or rolling below about 25 rpm / 3.1 km/h because SPD_ZERO_MS = 400 ms at 6 PPR.

Every littlefs erase or program on rp2 runs multicore_lockout_start_blocking() plus save_and_disable_interrupts(), which freezes core 0. That is 45 ms typical and 400 ms maximum per 4 KB erase (W25Q16JV). Anything over 200 ms trips A1's UART timeout, which releases motor current; anything over 250 ms also sends the loop into LIMP(R_LINK).

**Failure scenario.** On a descent the rider holds the carrier brake with the throttle released, and the SPD wire comes loose. wheel_rpm goes to 0 while ERPM is -7500 and i_cmd is -25 A. Three seconds later, 'still' is True with about 3000 records in the ring, and a 72 KB flush (18 sector erases) starts. Core 0 loses the CPU in 45-400 ms blocks during active regen. Any erase over 200 ms makes A1 release current, and the rear brake goes slack.

The same happens when a rider creeps or walks the bike below 3 km/h.

**Evidence.** Code: ui.py:207-215; config.py:43 `SPD_ZERO_MS = const(400)`.

Scratch results:
- demo_flush.py scenario D: 'flush at t=303.0 s, 72720 B (~18 x 4 KB sectors) while ERPM=-7500.0 i_cmd=-25.0 brake=1.0'.
- percall.py: 'longest single-call (back-to-back) flash stall ... typ 48.2 ms, max 424 ms'.

MicroPython and pico-sdk:
- ports/rp2/rp2_flash.c:169-176 `multicore_lockout_start_blocking(); ... save_and_disable_interrupts();`
- ports/rp2/mpthreadport.c: multicore_lockout_victim_init() on core 0.
- pico-sdk multicore_lockout_handler disables IRQs on the victim core.

**Suggested fix.** 1. Require all of the following for standstill: wheel_rpm == 0, |ERPM| below a small threshold, |i_motor| and |i_cmd| below about 0.5 A, brake released, and a longer dwell.
2. Treat any disagreement between wheel speed and motor speed as 'moving' and raise a SENSOR flag.
3. Re-check the predicate before every flush chunk (F23).

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F19: Nothing enforces the spec's V_lo boost floor: 40 A of assist is allowed at any bank voltage, and the A1 checklist omits the battery-cut and minimum-input settings. The chain is A1 brownout, then a Pico reset, then the INIT stall

- **Severity:** low
- **Area:** safety
- **Location:** `v2/firmware/control.py:85`
- **Status:** confirmed
- **Reference:** RGX-2-001 §3 (V_lo, V_margin), §4.2, §4.5, §9 'A1 brown-out'; RGX-2-003 D6

**Claim.** Spec §3 defines V_lo = V_startup + V_margin + I_boost*R_total precisely so that boost never pulls A1's terminal below its start-up threshold plus 1 V. §4.2 gives V_lo = 16.5-23.7 V at 40 A.

The envelope caps assist at I_ASSIST_MAX_A = 40 A regardless of v_in (control.py:85-86, config.py:51). The D6 A1 checklist omits l_battery_cut_start/end and l_min_vin. VESC defaults (cut 10 V to 8 V, minimum 8 V) put the running floor at V_startup with zero margin.

The research state machine inhibited assist below V_LO (research/system-design.md:282); that was dropped along with the PRECHARGE state, but the sag concern it addressed is independent of precharge. Bank rest voltage is 12.69 V (spec §4.5), so every ride starts below V_lo.

**Failure scenario.** Bank OC is 16 V and the rider applies full throttle at about 8 km/h. Battery current is about 30 A, and the drop across 0.277 Ohm puts the A1 terminal near 7.7 V. Two outcomes are possible:
- UV-fault cycling (F17).
- A1 brownout. A1's BEC collapses (C4 gives 6.8 ms of hold-up, 'decoupling only'), the Pico resets, both reboot together, and the single FW request is lost during A1's DC calibration (F04). The system stays in INIT with no rear brake for the rest of the ride.

**Evidence.** Code: control.py:85-86 `if cmd > config.I_ASSIST_MAX_A: cmd = config.I_ASSIST_MAX_A` is the only assist limit and has no v_bank term.

Spec §3 l.74-78 ('without V_margin the design operates at the brownout boundary by construction'). RGX-2-003 D6 checklist.

VESC mcconf_default.h:54 `MCCONF_L_MIN_VOLTAGE 8.0`, :60 `MCCONF_L_BATTERY_CUT_START 10.0`, :63 `..._END 8.0`.

**Suggested fix.** 1. In the envelope, limit assist so the estimated terminal voltage stays at or above V_startup + V_margin, e.g. I_max = (v_oc_est - (V_startup + V_margin))/R_total using v_in and i_in, with a taper.
2. Add l_battery_cut_start/end (for example 11/9.5 V) and l_min_vin to the D6 checklist and to the provisioning tool (F16).

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F28: RX is drained after the control decision, so the strategy acts on telemetry 18-28 ms old, over D9's <= 20 ms budget

- **Severity:** low
- **Area:** design-conformance
- **Location:** `v2/firmware/control.py:189`
- **Status:** confirmed
- **Reference:** RGX-2-003 D9 table ('Telemetry age (ERPM) <= 20 ms'); RGX-2-001 §11 'A1 telemetry ERPM lag'

**Claim.** ControlLoop.tick reads link.values (control.py:164-176), makes its decision, and only then calls link.tick(), which sends and then polls (vesc.py:300). A reply that arrived during the previous 10 ms sits in the ring until after the next decision, so every decision uses data one tick older than necessary. link.age_ms() is also measured from poll time, not arrival time.

**Failure scenario.** During braking, the slip regulator and the envelope act on ERPM, i_motor and v_in sampled up to 28 ms earlier (mean 23 ms), not the <= 20 ms D9 assumes. The i_motor field is additionally a read-and-reset average over the preceding 20 ms, so its centroid is about 10 ms older still.

**Evidence.** Scratch results: sim_age.py, a byte-timed VESC model (115200 wire time, 0.3 ms processing) measuring data age at strategy.update. As shipped: min/max/mean = 18.0 / 28.0 / 23.0 ms. With link.poll(now) moved before loop.tick: 8.0 / 18.0 / 13.0 ms.

**Suggested fix.** Split VescLink into poll() and send(). Call link.poll(now) at the top of ControlLoop.tick, before reading values and evaluating link staleness, and keep the TX at the end of the tick.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F29: Constructing ADC(0)/ADC(3) by channel number leaves the RP2040 pad pull-down enabled, so VSYS reads about half its true value and the throttle reads about 2 % low

- **Severity:** low
- **Area:** platform
- **Location:** `v2/firmware/sensors.py:120`
- **Status:** confirmed
- **Reference:** RGX-2-001 §10.6, §7 U2 'ADC3 = VSYS/3'; RGX-2-100 Rev D sheet 3 note 7; RGX-2-003 D14

**Claim.** MicroPython's rp2 ADC constructor calls adc_gpio_init() (pulls off, input buffer off) only when it is given a Pin. With an integer channel from 0 to 3, the pad keeps its reset state, which for GPIO26 and GPIO29 is pull-down enabled (PADS_BANK0 reset 0x56).

The Pico's VSYS divider is 200k/100k (66.7 kOhm Thevenin), so the 50-80 kOhm internal pull-down loads it heavily, and the firmware's fixed x3 scaling under-reads. On GP26 the pull-down forms a divider with R7 plus the throttle source, so readings come out about 1.4-2.2 % below v1's calibration, which was taken with ADC(Pin(26)) and pulls disabled.

**Failure scenario.** With VSYS = 5.0 V from A1's BEC, the pin sees 0.143-0.182 x VSYS instead of 0.333, so vsys() reports about 2.1-2.7 V. The SYSTEM page and logs show a brownout on a healthy rail, and a real BEC sag is scaled by an unknown nonlinear factor, which defeats the spec §10.6 goal. The throttle idle count shifts from 1073 to about 1050-1058, which invalidates reuse of the v1 calibration.

**Evidence.** Code: config.py:20-21 `ADC_THROTTLE = const(0)`, `ADC_VSYS = const(3)`; sensors.py:90, 120.

MicroPython ports/rp2/machine_adc.c: the integer path never calls adc_gpio_init (same in v1.19.1 and v1.22.2; in v1.22.2 even the integer 26 skips it).

pico-sdk pads_bank0.h: PADS_BANK0_GPIO26_RESET 0x56, GPIO26/29_PDE_RESET 0x1.

Known issues: micropython/micropython#10947 and raspberrypi/pico-feedback#318. RP2040 datasheet §5.2.3.4 (pull-down 50-80 kOhm). v1-legacy/firmware/drivers/throttle.py:22 used ADC(Pin(26)).

**Suggested fix.** Build both channels from Pin objects, ADC(Pin(26)) and ADC(Pin(29)), which runs adc_gpio_init on every MicroPython release, or store pin numbers rather than channel numbers in config. Then re-take the throttle calibration.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F36: There is no hal.py: sensors.py and main.py have 0 % host coverage, the viper CRC (vesc_fast) never runs, and the target half of ui.py is untested

- **Severity:** low
- **Area:** testing
- **Location:** `v2/firmware/sensors.py:88`
- **Status:** confirmed
- **Reference:** RGX-2-003 §4 Testing (host-first, hal.py), gates FW-1, FW-4; RGX-2-001 §10.1-10.3

**Claim.** RGX-2-003 §4 puts the core logic 'CPython-clean behind a 30-line hal.py'. None exists. WheelSpeed and Throttle raise RuntimeError off-target (sensors.py:51-52, 88-89) and accept no injected state machine or ADC, and conftest.FakeSensors replaces SensorBank wholesale.

As a result, none of the 43 tests executes the pure logic in sensors.py: PIO period pairing, SPD_MIN_PERIOD rejection, the 400 ms zero timeout, the §10.3 window with 50 ms fail persistence, the median, and the active-low brake. vesc_fast.py is never imported by the host suite, so FW-1's byte-identical claim for the viper CRC has never been checked on any interpreter.

**Failure scenario.** The throttle phantom assist (F05), the 252 km/h threshold (F30), the glitch split (F31), the start-up sample (F54) and stale speed (F10) all pass CI. A viper-CRC mismatch would reject every frame on target and put the loop into permanent LIMP, with no signal on the host.

**Evidence.** pytest-cov over the v2 suite: main.py 0 %, sensors.py 0 %, ui.py 22 % (lines 90-250 missing), vesc_fast.py 5 %, control.py 96 %, vesc.py 95 %. grep finds no import of sensors in v2/tests. conftest.py:35-53 FakeSensors.

Scratch test_sensor_review.py shows five sensor defects become one-line assertions once the classes accept fakes. All 9 modules compile with mpy-cross 1.22.2 -march=armv6m.

**Suggested fix.** 1. Add hal.py (Pin, ADC, StateMachine, UART, I2C, ticks) with host fakes, and let WheelSpeed(sm=None) and Throttle(adc=None) accept injected collaborators.
2. Add host tests for: idle -> 0, full -> 1, deadband, window/persistence/re-arm, pairing with glitch/drop/start-up words, the min-period constant, and decay while decelerating (pio_sim.py can serve as a fixture).
3. Run the CRC tests against vesc_fast under the MicroPython unix port in CI.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F41: The boot id is len(os.listdir('/logs')), so it collides after any file is deleted, and every later ride then appends into one old file

- **Severity:** low
- **Area:** correctness
- **Location:** `v2/firmware/ui.py:205`
- **Status:** confirmed
- **Reference:** RGX-2-003 D13, D6

**Claim.** _next_boot_id returns the number of entries in /logs. After the owner prunes old logs (the only remedy for F13), or any other file lands in /logs, the count is at or below the highest existing index. The generated path then names an existing file, which is opened 'ab' and appended to.

The collision creates no new file, so the count never rises again, and every later boot appends to that same file. If listdir raises, the id is 0 on every boot. Files have no header or boot marker, and ms restarts at each boot, so merged rides cannot be separated reliably.

**Failure scenario.** /logs holds ride0000 to ride0004. The owner deletes ride0000-0002 to free space. The next boot writes a new ride0002. Every boot after that gets id 3 and appends to the old ride0003.bin, so months of rides pile into one file with overlapping ms values.

**Evidence.** ui.py:196-205 `return len(names)`; ui.py:221 `with open(path, "ab")`.

Scratch results:
- demo_flush.py scenario E: '[(2, 'new'), (3, 'APPENDS TO EXISTING FILE'), (3, 'APPENDS TO EXISTING FILE')]'.
- demo_flush.py scenario F: 'listdir OSError -> boot id 0 (every boot)'.

**Suggested fix.** Use max(parsed index) + 1 over files matching ride%04d.bin, or keep a persistent counter file. Create each file exclusively (check it does not exist, then open 'wb') and write a header record carrying the boot id (F42).

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F44: USB bench-stream mode cannot be switched on, would still write flash if it were, and streams too few fields for C-0 corpora

- **Severity:** low
- **Area:** design-conformance
- **Location:** `v2/firmware/ui.py:194`
- **Status:** confirmed
- **Reference:** RGX-2-003 D13, D15, gates FW-0..FW-3, C-0

**Claim.** `self.bench_stream = False` is set in __init__ and nothing ever sets it: no config flag, no USB detection, no pin, and no REPL access while main.py loops.

The flush gate (ui.py:248) ignores bench_stream, so even if it were enabled, bench mode would still write flash. That contradicts D13 ('USB bench mode streams instead and never touches flash').

The streamed line (ui.py:241-244) carries kmh, erpm, vbank, icmd, vsys and slip with no header. It lacks i_in, i_motor, throttle, brake, state and fault, so it cannot provide D15's 'telemetry + wheel speed' corpora; p_elec cannot even be computed without i_in.

**Failure scenario.** On the FW-0 to FW-3 bench and at C-0 there is no way to get the stream. If someone patches bench_stream = True, the Pico still erases flash at standstill, stalling both cores during bench timing measurements, and the captured CSV cannot be scored.

**Evidence.** grep: `bench_stream` appears only at ui.py:194 (set to False) and ui.py:240 (read).

Scratch results (demo_pages.py with bench_stream=True): 'flash bytes written = 960 ; sample line: LOG,100,0.0,0,0.00,0.00,0.00,0.000'.

**Suggested fix.** 1. Add config.BENCH_STREAM, or detect USB VBUS through GP24, to enable streaming.
2. Skip _flush while streaming.
3. Print a header line and the same field set as the binary record, or hex-dump the packed record. Document it in the deploy tooling.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F45: There is no host-side log decoder, fetch or replay tooling, and no tests of Core1's flush, standstill, boot-id or page policy

- **Severity:** low
- **Area:** tooling
- **Location:** `v2/tests/test_ui.py:14`
- **Status:** confirmed
- **Reference:** RGX-2-003 §4 Testing (Replay), D3, D13, D15

**Claim.** No replay harness exists for v1 ride logs or C-0 corpora, and no test reads v1-legacy/data/*.csv or logs/*.csv, although RGX-2-003 §4 lists replay of those as regression inputs.

The only decoder is ui.unpack_record, for a single record, inside the firmware module. Nothing copies /logs off the Pico, splits files by boot, handles a partial trailing record, exports CSV, or adapts logs to the scoring sim. The v1 scorer derives dt from t[1] - t[0] (scoring.py `_dt_from_log`), but v2 logs mix 10 Hz and 1 Hz sampling with loop jitter, so a naive adapter gets the energy integrals wrong.

test_ui.py covers only the codec and the Ring. The Core1 policy is host-testable (the review harness runs it under CPython with fakes), yet the flush, standstill, boot-id and page-choice defects (F12-F14, F22, F41) are all untested.

**Failure scenario.** The first C-0 corpora come off the bike as headerless .bin files with no tool to read or resample them. The first person to feed them to the scorer with a uniform-dt assumption gets wrong capture scores for strategy selection.

**Evidence.** `grep -rn unpack_record` finds only ui.py and tests/test_ui.py. v2 has no scripts/ or tools/ directory. test_ui.py has 5 tests, all on the codec and Ring. v1 data format exists (v1-legacy/data/ride_trace.csv header 't_ms,sys_state,req_mode,whl_rpm,...,mot_rpm,...'). Harness: scratchpad/review/ui_dim/harness.py.

**Suggested fix.** 1. Add v2/tools/pull_logs.sh and tools/decode_log.py: mpremote fetch, header parse, per-boot split, CSV/NPZ export, and resampling to a uniform dt.
2. Add a replay test that drives ControlLoop from v1 traces and decoded C-0 logs (converted for the k and pole-pair differences) through the existing fakes.
3. Add host tests for Core1 with fake machine, framebuf, time and os modules: flush at every standstill, no flush while ERPM ≠ 0, boot-id uniqueness after deletion, SYSTEM shown for INIT, surviving ENOSPC.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F46: There is no v2 deploy script, and the unstoppable WDT plus an uncaught KeyboardInterrupt turn every redeploy over mpremote into a race

- **Severity:** low
- **Area:** tooling
- **Location:** `v2/firmware/main.py:41`
- **Status:** confirmed
- **Reference:** RGX-2-003 §3 (WDT 2 s); v1 tooling parity

**Claim.** v1 had scripts/deploy_to_flash.sh; v2 has nothing equivalent. v1's script cannot be reused: it copies v1's package layout and by default runs v1 VESC provisioning (50 A / 43 V plus the Lisp install).

main.py arms machine.WDT(2000) at boot, and MicroPython documents that a running WDT cannot be stopped. mpremote's Ctrl-C raises KeyboardInterrupt, which main.py's `except Exception` (main.py:74) does not catch, so main.py exits to the REPL with the WDT still armed, and the board resets within 2 s in the middle of the copy. There is no boot-time escape (a boot.py flag or GPIO) to skip main.py.

**Failure scenario.** After the first deploy, every later `mpremote cp` of the 9 modules is cut off by a WDT reset within 2 s. main.py restarts and re-arms the WDT, so the developer ends up reflashing the UF2 or disabling WDT_ENABLE in the shipped config.

**Evidence.** main.py:41 `wdt = WDT(timeout=config.WDT_MS) if config.WDT_ENABLE else None`; config.py:60 `WDT_ENABLE = True`. MicroPython v1.22.2 docs/library/machine.WDT.rst:26 ('the WDT cannot be stopped either'). v1-legacy/scripts/deploy_to_flash.sh:52-55 runs vesc_provision.py by default. `find v2 -name '*.sh' -o -name 'deploy*'` returns nothing.

**Suggested fix.** 1. Add v2/tools/deploy.sh (mpremote) that copies the flat modules and does no provisioning.
2. Add a boot.py escape (for example a GPIO held low, or a /nomain flag) that skips main.py and the WDT.
3. Document the procedure.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F48: The D10 k cross-check has no effect: the healthy flag is never consumed or published, the loop's feed gate is never met under consistent kinematics, abs() hides sign errors, and wheel-sensor loss is not detected

- **Severity:** low
- **Area:** design-conformance
- **Location:** `v2/firmware/kinematics.py:79`
- **Status:** confirmed
- **Reference:** RGX-2-003 D10 ('Free cross-check'); research/system-design.md §10 'Shell sensor lost'

**Claim.** KCrossCheck.healthy (kinematics.py:79) is never read by control.py, never published (only k_est goes to SN_KEST), and never displayed. D10 requires 'drift outside ±5 % raises a SENSOR flag on the LINK page'.

The loop feeds the check only when i_cmd > 2 and i_motor > 2 (control.py:181-182). That line is never executed by the suite, and with consistent kinematics the Placeholder never holds assist (F02), so it does not run in practice either.

Other gaps:
- No plausibility check exists for wheel_rpm = 0 while |ERPM| is large, which is how a dead shell sensor would appear. The research failure table says 'Shell sensor lost: Inhibit regen, flag. Do not guess'.
- The crossover guard's speed depends solely on this sensor.
- The check uses abs(), so it could not detect the F01 sign error, and test_crosscheck_converges_and_flags feeds a positive m_rpm = +k*w (the opposite of the physical sign), which abs() hides.

**Failure scenario.** - The shell-sensor magnet shifts and the wheel reads 2x. Carrier = (m + k*w_meas)/(1+k) appears to spin when the carrier is held, giving spurious s > 0 during braking and reduced regen.
- With the sensor dead, speed stays at 0 and the crossover guard is never evaluated.
In both cases k_est drifts far from 5 and nothing latches or flags it.

**Evidence.** kinematics.py:79 `self.healthy = err <= self._tol` has no consumer (grep 'healthy' finds only kinematics.py:69 and :79). control.py:181-182 feeds the check; control.py:217 publishes only `self.kx.k_est`. Coverage: control.py line 182 is missing. test_kinematics.py:49.

**Suggested fix.** 1. Publish kx.healthy (or a SENSOR bit) in the snapshot and show it on the LINK page (F40).
2. Add a plausibility check (wheel_rpm near 0 with |motor_rpm| above a threshold, or k error above 5 %) that inhibits regen and raises the flag.
3. Add a signed-ratio check: m_rpm*w must be negative during assist.
4. Add loop tests with consistent assist telemetry asserting samples > 0, and with 10 % k drift asserting the flag.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F49: Lost replies leave stale v_in and ERPM driving the envelope for up to about 278 ms, with no per-request loss tracking

- **Severity:** low
- **Area:** correctness
- **Location:** `v2/firmware/control.py:184`
- **Status:** confirmed
- **Reference:** RGX-2-001 §10.8; RGX-2-003 §3 safety envelope, D7

**Claim.** Values persist unchanged when replies are lost, and nothing in the link tracks unanswered requests. The envelope's bank taper and the 39 V crossover guard use v.v_in, which can be up to LINK_TIMEOUT_MS (250 ms) plus the 28 ms decision age (F28) old before LIMP. F06 and F26 make such windows much more frequent than wire noise alone would.

**Failure scenario.** The last good v_in reads 37.9 V (f = 1, full regen allowed), then replies stop. The 6.67 F bank charges at up to about 6 V/s for about 0.278 s, reaching about 39.5 V, while the firmware still believes 37.9 V. The crossover guard never fires in that window, and slip is computed from frozen ERPM against a live wheel speed.

**Evidence.** vesc.py:274-279 updates values only on good frames. control.py:184 `self.i_cmd = envelope(cmd, v.v_in, speed_kmh, ...)`. control.py:122 judges staleness only against LINK_TIMEOUT_MS = 250 (config.py:61). The age figure comes from sim_age.py.

**Suggested fix.** Count consecutive unanswered telemetry requests in VescLink. In the envelope, zero or halve regen when telemetry age exceeds about 2 request periods while v_in is within about 2 V of V_TAPER_START. Keep the 250 ms LIMP for everything else.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F50: A NaN command crashes the loop inside link.tick (int(nan)) instead of entering LIMP

- **Severity:** low
- **Area:** correctness
- **Location:** `v2/firmware/vesc.py:93`
- **Status:** confirmed
- **Reference:** RGX-2-003 §3 ('a strategy exception -> zero current + LIMP, never a crash')

**Claim.** envelope() cannot clamp NaN because every comparison with NaN is False, so a NaN strategy output reaches pack_set_current. There, int(amps * 1000) raises ValueError from link.tick (control.py:189), which is outside the strategy try/except (control.py:175-179). The exception propagates to main.py's last-resort handler, which calls machine.reset().

**Failure scenario.** A future control law returns NaN (for example 0/0 on a guarded path, or NaN from a float sensor). The Pico reboots mid-ride instead of taking LIMP(R_STRATEGY). A1's 200 ms timeout releases the motor, and after the reboot the INIT handshake has to succeed again (F04).

**Evidence.** Scratch test_review_nan.py passes: pytest.raises(ValueError) on loop.tick while state is RUN. MicroPython v1.29: `int(float("nan")*1000)` raises 'ValueError: can't convert NaN to int'.

**Suggested fix.** Sanitise at the protocol boundary: if amps != amps, set amps = 0.0, and clamp to ±I_MAX before int(). In ControlLoop, treat a non-finite cmd as a strategy fault (LIMP R_STRATEGY).

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F51: main.py's imports run outside the boot guard and before the WDT, the viper CRC fallback catches only ImportError, and a deterministic boot exception becomes a silent reset loop

- **Severity:** low
- **Area:** platform
- **Location:** `v2/firmware/main.py:7`
- **Status:** confirmed
- **Reference:** RGX-2-003 D11, gate FW-1, D1 (frozen-module escape); vesc_fast.py header; main.py boot philosophy

**Claim.** vesc_fast.py:3 promises that 'absent/failed import falls back to the pure-Python table CRC', but vesc.py:73-76 catches only ImportError. A viper compile failure raises ViperTypeError (a TypeError subclass), and a fragmented heap during on-device compilation raises MemoryError. Either propagates out of `import vesc` at main.py:19, which is module level: before `try: run()` (main.py:74) and before the WDT is armed (main.py:41).

There is no known-answer check that the viper CRC matches the reference (gate FW-1). Conversely, a deterministic exception inside run() (for example in build() or the 96 KB Ring allocation) resets immediately, re-runs and fails again, with the traceback printed only to USB.

**Failure scenario.** A pinned MicroPython release hits a viper compile error or MemoryError in vesc_fast. main.py dies at import and drops to the REPL, with no machine.reset() and no WDT. The Pico never sends a frame, A1 times out (safe), and the bike stays dead with a blank display until a power cycle. A deterministic error in build() instead cycles the Pico about every 1 s indefinitely.

**Evidence.** Scratch mp/fb_test.py on MicroPython v1.29: a failing viper function in vesc_fast is 'NOT caught by except ImportError: ViperTypeError base (<class 'TypeError'>,)'. The real vesc_fast compiles and matches the reference CRC on v1.29 ('0x31c3', 'viper mismatches: 0') and under mpy-cross -march=armv6m.

Code: main.py:7-19 (imports), main.py:74-82 (guard); vesc.py:73-76.

**Suggested fix.** 1. Use `except Exception` around the vesc_fast import, and accept the viper version only if crc16(b"123456789", 0, 9) == 0x31C3.
2. Move the application imports inside the guarded region, or arm the WDT in a tiny boot stub first.
3. Count consecutive boot failures in a file or RTC scratch register and stop resetting after N, showing the error on the OLED.
4. Ship .mpy or frozen modules.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F52: The brake input (GP14) is read raw, with no debounce and no open-wire detection, and it is not on drawing Rev D

- **Severity:** low
- **Area:** design-conformance
- **Location:** `v2/firmware/sensors.py:133`
- **Status:** confirmed
- **Reference:** RGX-2-003 D9; RGX-2-100 Rev D sheet 3 note 2; RGX-2-001 §8

**Claim.** brake() returns Pin.value() == 0 once per tick, with only the internal 50-80 kOhm pull-up and no persistence or debounce. With active-low and a pull-up, an open or broken lever wire reads 'not braking', indistinguishable from a healthy released lever.

D9 promotes this input to the regen fast path, but GP14 does not appear on drawing Rev D (spec §8 lists nine pins), so it has no series resistor as sheet 3 note 2 requires for 5 V-domain lever sensors.

**Failure scenario.** With BRAKE_FITTED = True, a severed lever cable silently removes the ~35 ms lever path, and the rider falls back to 100-160 ms slip sensing with no warning. EMI or bounce on the long lever cable makes brake() true for single ticks. Each event zeroes the positive command in the envelope and makes the Placeholder request regen, a -2 A slew step per event during assist.

**Evidence.** sensors.py:121-124, 132-133; config.py:18-19; RGX-2-001 §8 pin table (nine pins, no GP14); RGX-2-003 D9 ('queued into drawing Rev E').

**Suggested fix.** 1. Debounce (require 2 consecutive ticks to assert).
2. Prefer a normally-closed or two-state sensor so an open wire can be detected, or at least add a boot-time plausibility check.
3. Add GP14 with a series resistor and TVS to drawing Rev E before setting BRAKE_FITTED.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F53: Multi-byte corruption can produce CRC-valid false accepts (about 1 in 65k). There are no plausibility checks, no multi-byte test, and a bad frame can partially update Values

- **Severity:** low
- **Area:** testing
- **Location:** `v2/firmware/vesc.py:211`
- **Status:** confirmed
- **Reference:** RGX-2-003 §4 parser property tests, D11

**Claim.** The only corruption test flips single bits, which CRC16 always detects. For random 2-4 byte bursts, CRC16 plus the END byte accepts about 1 in 65k. parse_selective trusts the echoed mask, ignores surplus length, and writes fields progressively before failing, so a bad frame can partially update Values. §4 demands no false accepts, which needs checks beyond the CRC.

**Failure scenario.** During EMI bursts, a false accept can set v_in = 46.9 V (regen tapered to 0 for a frame), raise a spurious fault (LIMP, regen lost), or produce a wild ERPM (wrong slip for one or more ticks).

**Evidence.** Scratch test_parser_false_accept_multibyte_bursts: '3 false accepts in 300000 corrupted frames; samples (erpm, v_in, fault) = [(-6000.0, 36.0, 0), (-6000.0, 46.9, 0), ...]'.

**Suggested fix.** 1. Accept a SELECTIVE reply only if its mask equals the one requested and its length is exactly the expected one.
2. Parse into a scratch Values object and commit atomically.
3. Range-check v_in, erpm and fault.
4. Add a multi-byte burst fuzz test.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F54: The PIO program does not sync to an edge at start, so the first wheel-speed sample after boot or reset is a partial phase and can read up to 2.2x too fast

- **Severity:** low
- **Area:** correctness
- **Location:** `v2/firmware/sensors.py:24`
- **Status:** confirmed
- **Reference:** RGX-2-001 §6 'MCU RESET', §10.1

**Claim.** The program starts counting 'high' immediately. If the pin is mid-high, the first word is a partial phase; if it is low, the first word is 0 and the second a partial low. Python pairs words from the first one, so the first published period is short by an arbitrary amount. in_base is passed (sensors.py:55) but no `wait pin` instruction uses it, which suggests the intended sync was left out.

**Failure scenario.** A WDT or exception reset (main.py:82 machine.reset()) or a brownout while riding at 30 km/h, followed by a fast INIT -> RUN. Across start phases 0.05, 0.25, 0.45 and 0.55, the first reported speed is 31.6, 40.0, 54.6 and 66.7 km/h respectively, held for up to one period. Slip, the crossover guard and the strategy act on it for the first ticks of RUN.

**Evidence.** sensors.py:24-28 (no wait/sync before the first count); sensors.py:54-57 (SM activated, _pending = -1).

Scratch results: scenarios.py 'C': 'start phase 0.55: first reported 66.7 km/h at 20 ms (true 30.0)'; test_sensor_review.py::test_boot_mid_phase_first_sample_wrong passes.

**Suggested fix.** Prefix the program with `wait(1, pin, 0)` then `wait(0, pin, 0)` (in_base is already set) so the first count starts on an edge, or discard the first two words in Python.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F55: KCrossCheck compares current ERPM with a wheel speed up to a full period old, so normal launch boosts bias k_est by +6 to 12 % and mark a perfect sensor unhealthy

- **Severity:** low
- **Area:** correctness
- **Location:** `v2/firmware/kinematics.py:71`
- **Status:** confirmed
- **Reference:** RGX-2-003 D10 ('drift outside ±5 % raises a SENSOR flag')

**Claim.** During assist, feed() takes |m|/w, with m from ERPM about 20 ms old and w from the last completed period (mean age about one period, 126 ms at 10 km/h). Under acceleration this ratio is biased high. The 30 rpm floor (3.8 km/h) does not exclude the low-speed range where the lag is largest, and nothing compensates for it. The ±5 % health test therefore fails on ordinary launches, and the logged or displayed k_est is biased.

**Failure scenario.** Emulated with the real WheelSpeed and KCrossCheck, carrier grounded, 20 ms ERPM lag:
- 4.5 -> 12 km/h in 1.5 s ends with k_est = 5.33 (+6.6 %), healthy = False.
- 4.5 -> 12 km/h in 1.0 s gives +10.4 %.
- 4.5 -> 10 km/h in 0.8 s gives +12.2 %.
Once F48 wires the flag up, every hard launch would flag a healthy shell sensor.

**Evidence.** kinematics.py:71-79; control.py:181-182.

Scratch results (kx_demo.py): '4.5 12 1.5 a=1.39 k_est=5.329 err +6.6% healthy=False n=150'.

**Suggested fix.** Feed only samples where the wheel period is fresh (age below a fraction of the period), or compare against a lag-matched motor speed (the ERPM average over the same window as the wheel period). Raise the speed floor, or scale the tolerance with acceleration.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F56: rx_overrun is the wrong instrument: it counts 'readinto filled the 256 B chunk', which never happens in normal polling and happens without any loss after a stall, while real UART overruns and framing errors stay invisible

- **Severity:** low
- **Area:** tooling
- **Location:** `v2/firmware/vesc.py:310`
- **Status:** confirmed
- **Reference:** RGX-2-003 D3 (link-health counters), D11, D14 LINK page; RGX-2-001 §10.9

**Claim.** rx_overrun increments only when one readinto returns exactly 256 B. In steady polling at most about 2 replies (54 B or less) are pending, so the counter is structurally 0. After a long core-0 stall (a standstill flash erase, for example) more than 256 B can be pending, and it increments with zero data loss.

Genuine loss happens when the 32-byte hardware FIFO overruns or the 1 KB ring fills. MicroPython's rp2 driver discards the OE flag and delivers framing-error bytes as data, so neither loss nor noise is visible. The counter reaches the snapshot as SN_OVERRUN, one of D3's link-health instruments.

**Failure scenario.** - Bytes are dropped (an IRQ-latency stall) or noise causes framing errors. SN_OVERRUN stays 0 and the damage appears only as crc_fail: the 'CRC-error bursts' signature D3 would attribute to EMI to justify the CAN migration, repeating v1's misdiagnosis in the other direction.
- After one slow tick with about 300 B pending, rx_overrun increments with no loss, and the owner reads it as evidence of RX overflow.

**Evidence.** Scratch results:
- test_rx_overrun_never_counts_in_poll_mode: dropping 3 bytes of every reply for 2 s gives rx_overrun == 0 and frames_ok == 0.
- test_rx_overrun_not_counted_without_loss: '[rx_overrun] frames_ok=12 crc_fail=0 rx_overrun=1'.

MicroPython rp2 machine_uart.c uart_drain_rx_fifo (v1.29 lines 162-190, v1.22.2 lines 148-175): the OE and FE branches are empty, followed by ringbuf_put.

**Suggested fix.** 1. Replace it with counters that can be observed: telemetry requests sent versus replies parsed (lost replies), bytes discarded in HUNT, LEN rejects.
2. Document that hardware OE and FE are not exposed by the MicroPython rp2 UART, or read UARTRSR through machine.mem32 if they are needed.
3. Rename or remove rx_overrun and add a test.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F57: Parser health counters mix error classes: an END mismatch counts as a CRC fail, resync always equals crc_fail, and long_seen counts CRC failures

- **Severity:** low
- **Area:** tooling
- **Location:** `v2/firmware/vesc.py:181`
- **Status:** confirmed
- **Reference:** RGX-2-003 D3, D11 ('a counter increments'), D14

**Claim.** In _END, `b == FRAME_END and crc16(...) == self._crc` short-circuits, so a framing error (wrong END byte) increments crc_fail without any CRC check. resync is incremented at the same place only (vesc.py:182), so it always equals crc_fail. When the CRC fails but END is correct, the unconsumed 0x03 re-enters HUNT and bumps long_seen (vesc.py:148-149), although no long frame exists. LEN = 0 rejects and junk bytes in HUNT are not counted at all.

**Failure scenario.** Parser desyncs from stray bytes (F06) show up as 'CRC errors'. D3's decision rule ('if bench/ride data shows CRC-error bursts correlated with phase current ... migrate to CAN') cannot separate bit errors from framing or resync effects, and the long_seen diagnostic reports phantom long frames.

**Evidence.** Scratch tests:
- test_end_byte_mismatch_counted_as_crc_fail: CRC valid, END = 0x00 gives crc_fail == 1 and resync == 1.
- test_crc_fail_with_correct_end_bumps_long_seen: long_seen == 1.
- test_one_stray_byte_blinds_parser_indefinitely asserts resync == crc_fail.

**Suggested fix.** 1. Count separately: crc_fail (CRC mismatch only), end_fail, len_reject, hunt_junk.
2. Make resync count HUNT re-entries.
3. Consume the 0x03 on a CRC failure with a good END, or skip long_seen in that case.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F58: The parser and decoder are interpreted Python rather than viper as D1/D11 decided; the per-frame cost probably misses FW-1's < 200 µs

- **Severity:** low
- **Area:** design-conformance
- **Location:** `v2/firmware/vesc.py:211`
- **Status:** confirmed
- **Reference:** RGX-2-003 D1, D11, gate FW-1

**Claim.** D1 says 'Viper for CRC + parser inner loop', but only crc16 is viper. FrameParser.feed runs a per-byte state machine in Python, and parse_selective loops over all 16 table entries per reply, building a format string, calling struct and boxing a float for each set bit.

**Failure scenario.** Gate FW-1 (< 200 µs per frame) and D1's premise that a ≤ 70 B parse is cheap probably fail on target. This is an estimate, not a measurement on RP2040: relative to a trivial loop iteration, feed plus handler costs about 870 iterations, which at D1's own 100-300k simple ops/s projects to milliseconds per frame, most of the ≤ 4 ms tick budget.

**Evidence.** Scratch mp/mp_time.py on MicroPython v1.29 unix x64: crc16 (viper, 20 B) 0.44 µs; parse_selective 13.58 µs; parser.feed(25 B frame) + handler 20.21 µs; reference `while i<1000: i+=1` 0.023 µs per iteration (ratio about 870). Relative figures only; on-target confirmation is needed at FW-1.

**Suggested fix.** Move the byte state machine and the SELECTIVE decode into a viper function that writes into a preallocated array('f') indexed by a precomputed field map. At minimum, precompute the format strings and iterate only over set bits.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F59: The PIO X counter runs out after 2^32 µs (71.6 min) at a standstill and pushes a 0 that pairs with a stale pending phase, producing a phantom speed; counts above 2^30 are heap-allocated

- **Severity:** low
- **Area:** correctness
- **Location:** `v2/firmware/sensors.py:37`
- **Status:** confirmed
- **Reference:** RGX-2-003 Rev B §3 (no allocation on core 0)

**Claim.** jmp(x_dec) falls through when X reaches 0, so after 2^32 iterations in one phase the program pushes ~X = 0 and moves to the other phase loop without an edge. If the wheel stopped in a low phase, Python already holds the preceding high phase in _pending, pairs it with this 0, and accepts the sum if it is ≥ 5 ms, producing a phantom speed while parked. sm.get() also returns values ≥ 2^30 (17.9 min) as heap-allocated big ints on core 0.

**Failure scenario.** The bike is walked slowly into a rack and left with S1 on for 72 minutes. At 71.6 min the pair (H_last + 0) = 420 ms is accepted, and 3.0 km/h is reported for 400 ms while stationary. That resets the ui standstill timer and logs a false movement. Harmless to torque with the Placeholder, but it corrupts logs and standstill logic.

**Evidence.** sensors.py:35-40 (low loop). MicroPython rp2.rst: 'x_dec: true if register is non-zero, and do post decrement'. rp2_pio.c StateMachine.get -> mp_obj_new_int_from_uint(value).

Scratch pio_sim.py with X preset near exhaustion: 'fifo [0, 0]', 'reported while parked: rpm 23.8 kmh 3.00'.

**Suggested fix.** On counter exhaustion, jump to a distinct timeout path that pushes a sentinel (for example 0xFFFFFFFF), and have Python discard the sentinel and reset _pending. Alternatively saturate by reloading X without pushing.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F62: The SYSTEM page has no temperatures, FW tuple or last-fault latch and is visible only during LIMP, and the RIDE page shows commanded current instead of power

- **Severity:** low
- **Area:** design-conformance
- **Location:** `v2/firmware/ui.py:173`
- **Status:** confirmed
- **Reference:** RGX-2-003 D14; RGX-2-001 §9 ('Annunciation ... None fitted')

**Claim.** D14 specifies SYSTEM as 'VSYS, temps, state, last fault, FW tuple' and RIDE as 'speed, bank V, power, slip bar'.

_page_system shows no SN_TFET and no FW tuple. It shows the live SN_FAULT rather than a latched last fault, and it can be seen only while LIMP or a fault is active. _page_ride shows |i_cmd| instead of power (v_bank*i_in).

R_VESC_FAULT leaves LIMP on the tick the fault clears, so a fault seen in a single 20 ms telemetry frame is gone from the display by the next render, and the 10 Hz logger catches it only about 20 % of the time. No event record exists anywhere.

**Failure scenario.** During a boost the bank sags and A1 reports an undervoltage fault for one telemetry frame. The SYSTEM page flashes for at most one render or not at all, the fault is probably missing from the log, and afterwards neither the display nor the log shows it happened. During a long climb the FET temperature is never visible.

**Evidence.** Scratch results (demo_pages.py): 'LIMP R_VESC_FAULT: ['STATE 2 RSN 3', 'FAULT 2', ...]', then on the next frame 'fault cleared next frame: [' 22.0 km/h', ' 31.4 V', '  0.0 A AST', 'rtt  0 e0']'.

Code: control.py:141-143 exits LIMP as soon as `not v.fault`; ui.py:164 prints `abs(sn[SN_ICMD])`.

**Suggested fix.** 1. Latch the last fault and last LIMP reason, with a timestamp, and show them on SYSTEM.
2. Add temp_fet and the FW tuple to SYSTEM, and allow SYSTEM to be viewed in RUN.
3. Show power (W) on RIDE.
4. Log fault and LIMP transitions as event records so 10 Hz sampling cannot miss them.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F63: The D2 single-core fallback cannot be built from ui.py: Core1 has no bounded step() API, the display sends whole frames over I2C, and the flush is monolithic

- **Severity:** low
- **Area:** design-conformance
- **Location:** `v2/firmware/ui.py:227`
- **Status:** confirmed
- **Reference:** RGX-2-003 D2, D14

**Claim.** D2 calls its fallback 'fully specified now': the same ui.py driven by the core-0 scheduler, with the display at 1 Hz sent as 8 x 128 B I2C chunks (about 3.3 ms each) in the loop slack, and a rate-limited bench stream.

ui.py offers only Core1.run(), an infinite loop with sleep_ms(20). SSD1306.show() sends the whole 1025 B frame in one writevto (about 23 ms at 400 kHz), and _flush is monolithic. None of it can be time-sliced.

**Failure scenario.** FW-0 fails on two pinned releases, so under D2 the fallback is 'chosen automatically', but no fallback exists. Moving ui onto core 0 as it stands would put 23 ms display writes and second-long flushes into the 10 ms control loop, which is v1's failure mode.

**Evidence.** ui.py:120-123 show() sends the full buffer; ui.py:227-250 run() has no bounded per-call step. RGX-2-003 D2 'Fallback, fully specified now'.

**Suggested fix.** Split Core1 into a bounded step(now) that does one unit of work (one 128 B display page, one log sample, one flush chunk) plus a thin thread wrapper that calls it in a loop. Add show_page(i) to the SSD1306 driver.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …


---

## Refuted (kept for the record)

### F07: When the bank is full, regen tapers to zero and so does the rear brake. There is no fallback, no rider warning, and it is not listed as an accepted risk; a 6.67 F bank fills after a few seconds of hard braking

- **Severity:** —
- **Area:** safety
- **Location:** `v2/firmware/control.py:72`
- **Status:** refuted
- **Reference:** RGX-2-001 §1, §6 BANK FULL, §10.8, §12; v2/README.md consequence 2; RGX-2-003 §3 safety envelope, D14

**Claim.** Spec §6 defines BANK FULL as 'Regen tapers to zero. No dump, no phase short'. envelope() implements this: regen tapers linearly from 38 V to zero at 40 V, and is zeroed outright above 39 V when speed exceeds 28 km/h (control.py:72-83). Because T_sun = 0 implies all torques are zero, the carrier friction brake transmits nothing once regen is tapered. The rear brake therefore disappears whenever the bank is full.

The bank is small. At C = 20 F / 3 = 6.67 F:
- 30 V to 38 V is about 1.8 kJ.
- 20 V to 38 V is about 3.5 kJ.
Against roughly 1.4 kW of maximum regen (40 A at about 35 V), that is about 1.5-3 s of hard braking. One stop from 30 km/h (100 kg, about 3.5 kJ of kinetic energy) can fill it.

What is missing:
- No sink or alternative brake mode.
- No rider indication when the taper begins. The RIDE page shows only bank V and |i_cmd|.
- The loss of rear braking is not in spec §12 'Accepted without mitigation'. That list covers only the absence of annunciation for protective operations.

The design thus silently turns the rear lever into a brake that fades within seconds on any descent.

**Failure scenario.** A descent at 25 km/h, with the rider on the rear lever and the front brake only lightly applied, starting from a bank at 30 V. After about 2 s of -40 A regen, v_in passes 38 V and regen tapers to 0 at 40 V, or immediately at 39 V above 28 km/h. With motor torque at 0, the carrier brake stops decelerating the wheel. The rider, who has learned that the rear lever brakes, gets no rear braking and no warning, and has only the front brake for the rest of the descent. The rear brake returns only after the bank is drawn down by assist or self-discharge.

**Evidence.** Spec and docs:
- RGX-2-001 §6: 'BANK FULL | V_bank -> 40 V | Regen tapers to zero. No dump, no phase short'.
- RGX-2-001 §1: 'T_sun = 0 => all torques zero'.
- v2/README.md consequence 2 (the rear brake does nothing unless the motor produces torque).
- RGX-2-001 §12 'Accepted without mitigation' has no entry for loss of rear braking at full bank.

Code:
- control.py:72-83 (taper and crossover guard).
- config.py:47-50 (V_BANK_MAX 40, V_TAPER_START 38, V_CROSSOVER_GUARD 39, CROSSOVER_KMH 28).
- ui.py:161-166: the RIDE page shows no taper or regen-available indication.

Arithmetic: 1/2*6.67*(38^2-30^2) = 1.81 kJ; 1/2*6.67*(38^2-20^2) = 3.48 kJ; 40 A x 35 V = 1.4 kW.

**Suggested fix.** 1. Make this an explicit spec decision. Either record 'no rear braking at bank full' in §12 with its rider-facing consequence, or add a sink: a dump resistor, or a winding-dissipation brake mode such as a phase short or handbrake-style command, evaluated on A1 FW 5.x.
2. In firmware, compute and publish the regen-available fraction (the taper factor f), and show a 'REGEN LIMITED / NO REAR BRAKE' indication on the RIDE page from 38 V onwards. Log the taper events.
3. Include the bank-full scenario in the rider briefing and in the gate-2 test.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F24: gc.disable() turns heap exhaustion into MemoryError on both cores. A MemoryError inside the strategy latches LIMP (no regen, so no rear brake) until power cycle

- **Severity:** —
- **Area:** platform
- **Location:** `v2/firmware/main.py:43`
- **Status:** refuted
- **Reference:** RGX-2-003 D1 (scheduled GC on a pre-allocated heap), D2, §3 ('a strategy exception -> zero current + LIMP, never a crash')

**Claim.** The comment 'collections are scheduled, not random' assumes allocation still succeeds with the GC disabled. In MicroPython, gc_alloc with auto-collect disabled starts with collected = 1 and returns NULL when no free run exists, so m_malloc raises MemoryError even if the whole heap is garbage. gc_auto_collect_enabled is global, so core 1 is affected too.

Steady-state garbage between scheduled collections is large (F32), and headroom is neither measured nor published. Where the MemoryError lands decides the outcome:
- Inside strategy.update: the `except Exception` at control.py:177 latches R_STRATEGY LIMP until power cycle, misattributed as a strategy fault.
- Elsewhere on core 0: main.py:76-82 calls machine.reset().
- On core 1: the thread dies silently (F25).

**Failure scenario.** Measured garbage, estimated for rp2:
- Core 0: about 1.1 KB per tick, about 11 KB per GC_DIV = 10 window.
- Core 1: about 2 KB per window.
- A flush: up to 64 KB.

The Pico GC heap is about 210 KB. The 96 KB ring, the firmware compiled from source and the 4 KB core-1 stack leave an estimated 55-75 KB. The nominal margin is only a few windows, and it can be closed by a strategy swap that allocates more, a long core-0 stall, or fragmentation. Exhaustion in the Placeholder's float math means permanent LIMP mid-ride, so the carrier brake does nothing.

**Evidence.** MicroPython source:
- py/gc.c:913 `int collected = !MP_STATE_MEM(gc_auto_collect_enabled);`
- py/gc.c:961-968 returns NULL when collected (the collect path at :970-971 runs only when auto-collect is enabled).
- py/modgc.c:46; py/malloc.c:88 `m_malloc_fail`.

Scratch results:
- rt/churn.py (MicroPython v1.29, 200 KB heap): 'gc.disable(): MemoryError after 6265 garbage floats = 200480 B; heap was 200512 B free'.
- rt/test_rt_review.py::test_transient_MemoryError_in_strategy_latches_LIMP_until_power_cycle passes (LIMP, R_STRATEGY, i_cmd 0 after 20,000 ticks).
- rt/alloc_rate.py: 'CORE0 loop.tick: 70.5 GC blocks/tick -> ~1128 B/tick on rp2'; 'FLUSH of 4096 records: 4115 blocks -> ~64.3 KB'.

Issue #12557: 209,936 B free at start.

**Suggested fix.** 1. Keep gc.disable() only together with a measured headroom guard: publish the minimum gc.mem_free() since boot, and run gc.collect() early when free memory drops below a threshold. Or use gc.threshold() instead of disable.
2. In control.py, re-raise MemoryError before `except Exception`, or treat it as a non-latching reason.
3. Make _flush allocation-free.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F47: The exact MicroPython version pin required by D1 and the FW-0 soak harness do not exist, and nothing checks the runtime version on target

- **Severity:** —
- **Area:** tooling
- **Location:** `v2/firmware/main.py:7`
- **Status:** refuted
- **Reference:** RGX-2-003 D1, D2, gate FW-0

**Claim.** D1 and FW-0 require an exact, tested MicroPython release pinned in the repo, because dual-core stability regresses between releases (for example v1.23 versus v1.22.x). The repo has no version file, no UF2 reference or hash, and no runtime check of sys.implementation.version or os.uname(). There is also no soak harness (loop skeleton plus display churn plus ring writes plus standstill flush) with which to run FW-0.

**Failure scenario.** The firmware is flashed onto whatever MicroPython is current, possibly a release with the reported _thread regressions, and runs with no indication. The FW-0 gate cannot be run reproducibly.

**Evidence.** grep under v2/ and .github/ for a version pin, .uf2, mpremote or sys.implementation finds only the prose in RGX-2-003-FW.md:55. There is no v2/tools or scripts directory.

**Suggested fix.** 1. Add v2/firmware/MICROPYTHON_VERSION (release plus UF2 SHA256) and a boot-time check that refuses RUN on a mismatch, with a visible reason.
2. Add a CI unix-port job pinned to the same tag.
3. Add tools/soak.py for FW-0, including a forced standstill flush.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F60: VSYS on ADC3/GP29 is valid only on a plain Pico. On a Pico W, GP29 is the CYW43 SPI clock and the divider is gated by GP25, and the firmware does not check which board it is on

- **Severity:** —
- **Area:** platform
- **Location:** `v2/firmware/sensors.py:136`
- **Status:** refuted
- **Reference:** RGX-2-001 §7 U2 'Raspberry Pi Pico ... ADC3 = VSYS/3', §10.6

**Claim.** vsys() assumes GP29 = VSYS/3. On a Pico W (and Pico 2 W), GP29 is WL_CLK, and VSYS/3 appears there only while GP25 (WL_CS) is high and no SPI transfer is in progress. MicroPython Pico W builds bring up the CYW43 at boot, so ADC(3) returns a meaningless value. Switching to ADC(Pin(29)) there (the F29 fix) would take GP29 away from the CYW43 and break the radio and on-board LED. The BOM allows substituting a board ('On hand; else PiShop.ca').

**Failure scenario.** The owner fits a Pico W (common, same footprint). VSYS logs and the SYSTEM page show noise near 0 V or the clock level, so spec §10.6 is not met and any BEC-sag investigation is misled.

**Evidence.** sensors.py:120, 136; config.py:21. References: https://github.com/orgs/micropython/discussions/10421 and https://github.com/raspberrypi/pico-sdk/issues/1222 (Pico W: GPIO25 high enables the GPIO29 VSYS reading). MicroPython ports/rp2/main.c:183 runs cyw43_init at startup.

**Suggested fix.** Detect the board (os.uname().machine contains 'Pico W'). Either do the GP25-high / GP29-input read with the CYW43 idle, or report VSYS as unavailable. Record the plain-Pico requirement in config and the README.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

### F61: The core-1 thread runs littlefs, framebuf and string formatting on the default 4 KB heap-allocated stack with no guard, and the flush path is not part of the FW-0 soak

- **Severity:** —
- **Area:** platform
- **Location:** `v2/firmware/main.py:36`
- **Status:** refuted
- **Reference:** RGX-2-003 D2, gate FW-0

**Claim.** _thread.start_new_thread() is called without _thread.stack_size(), so rp2 allocates a 4096 B stack from the GC heap (the Python-level recursion limit is set 512 B lower). C-level stack depth is not checked: the littlefs commit/compaction path under f.write/close, plus the VM frames of run -> _flush -> with. A C-stack overflow on core 1 silently writes into the adjacent GC heap block, because MicroPython arms no MPU guard for core 1. D2's FW-0 soak lists 'loop skeleton + display churn + ring writes', without saying whether it includes the littlefs flush.

**Failure scenario.** On a flush that triggers littlefs metadata compaction, core-1 stack use exceeds 4 KB and corrupts whatever heap object precedes core1_stack, possibly one core 0 uses. Symptoms appear later and far from the cause. This is unmeasured and flagged as a test gap.

**Evidence.** MicroPython ports/rp2/mpthreadport.c:133 `*stack_size = 4096; // default stack size`, :144 `core1_stack = m_new(uint32_t, core1_stack_num_words);`, then `*stack_size -= 512;`. main.py:36 `_thread.start_new_thread(core1.run, ())`.

**Suggested fix.** 1. Set _thread.stack_size() explicitly (for example 8-12 KB).
2. At FW-0, measure the stack high-water mark with a painted stack during a forced flush with compaction.
3. Include 'standstill flush every N s' in the FW-0 soak explicitly.

**Verifier 1: confirmed, low.** I tried to refute this and could not. The code does what the finding says, and the VESC sign claim holds in the bldc source.

How the value flows:
1. vesc.py:28 stores SELECTIVE bit 3 as `i_in`.
2. control.py:203 copies it into the snapshot unchanged: `sc[SN_IIN] = v.i_in`.
3. ui.py:35 packs it unchanged.
4. ui.py:51 decodes it as "i_in".
5. `Core1._flush` (ui.py ~223-231) writes bare 24 B records to rideNNNN.bin. There is no header, format version, DIR_SIGN or sign field.

What VESC sends: in bldc release_5_03, commands.c:376-377 sends `mc_interface_read_reset_avg_input_current()` for bit 3. That is the average of `current_in_filtered` (mc_interface.c:1824), which is `m_motor_state.i_bus` (mcpwm_foc.c:1186-1189). `i_bus` is modulation times phase current (mcpwm_foc.c:3913). It is positive when drawing power from the DC link and negative when regenerating. The regen battery limit confirms this: `MCCONF_L_IN_CURRENT_MIN = -60.0` ("Lower" input current limit). So when the bank is charging, the logged `i_in` is negative. The repo's own fixtures say the same: test_ui.py:20 (`SN_IIN=-3.51` with `SN_ICMD=-12.3`) and test_vesc.py:135.

Scratch test: I fed a regen SELECTIVE frame (i_in=-3.5, i_motor=-12.25, erpm=-7940) through the real `ControlLoop`, the snapshot, `pack_record` and `unpack_record`. The decoded record has i_in = -3.5, and its keys contain no sign metadata. Integrating v_bank·i_in under the README convention gives -1.75 Wh for a 60 s, 3.5 A charge at 30 V, so the charge reads as a discharge.

Nothing in the design accepts the VESC sign. RGX-2-003 D13/D15 only says the log feeds the scoring sim, and C-0 corpora are named as replay inputs (§4). The README says its conventions are "used in every document". The record also renames `v_in` to `v_bank` but keeps `i_in` …

