import pytest

import config as C
import control
from conftest import Rig, last_current, rotor_erpm
from control import envelope, request, slip

W = 150.0          # wheel rpm, ~19 km/h


# --- slip --------------------------------------------------------------------
def test_slip_follows_the_plant():
    assert slip(rotor_erpm(W, 1.0), W) == 1.0     # coasting: rotor parked
    assert slip(rotor_erpm(W, 0.0), W) == 0.0     # carrier held
    assert slip(rotor_erpm(W, 0.4), W) == pytest.approx(0.4)
    assert slip(rotor_erpm(W, 0.0), C.W_MIN_RPM - 1) == 1.0   # too slow to tell


# --- envelope ------------------------------------------------------------------
def test_slew_limits_only_the_build_up():
    assert envelope(40.0, 0.0, 25.0, 0.0) == C.SLEW_STEP_A
    assert envelope(-40.0, 0.0, 25.0, 0.0) == -C.SLEW_STEP_A
    assert envelope(10.0, 20.0, 25.0, 0.0) == 10.0      # release: at once
    assert envelope(0.0, 20.0, 25.0, 0.0) == 0.0
    assert envelope(-36.0, 20.0, 25.0, 0.0) == -C.SLEW_STEP_A   # reversal
    assert envelope(-10.0, -30.0, 25.0, 0.0) == -10.0


def test_clamps_act_in_the_same_tick():
    # bank at the terminal limit: regen goes to 0 at once, not at the slew rate
    assert envelope(-30.0, -30.0, C.V_TERM_MAX, 0.0) == 0.0


def test_regen_cap_uses_open_circuit_voltage():
    # 10 A of regen lifts the terminal by I*R; the cap must not mistake that
    # for a full bank (v_oc = 38.5 - 3.67 = 34.8 V -> cap 11.4 A)
    assert envelope(-10.0, -10.0, 38.5, -10.0) == -10.0


def test_assist_floor_protects_a1_supply():
    cap = (10.0 - C.V_TERM_MIN) / C.R_BANK
    assert envelope(30.0, 30.0, 10.0, 0.0) == pytest.approx(cap)
    assert envelope(30.0, 30.0, C.V_TERM_MIN, 0.0) == 0.0


def test_caps_never_flip_the_sign():
    assert envelope(-30.0, -30.0, 45.0, 0.0) == 0.0
    assert envelope(30.0, 30.0, 5.0, 0.0) == 0.0


@pytest.mark.parametrize("r_true", [0.187, 0.27, 0.367])
def test_full_bank_settles_below_the_ov_trip(r_true):
    # closed loop against a bank with unknown ESR and one tick of telemetry
    # delay: the terminal must settle at or under V_TERM_MAX without cycling
    v_oc, i, seen = 38.0, 0.0, (38.0, 0.0)
    trace = []
    for _ in range(300):
        i = envelope(-40.0, i, seen[0], seen[1])
        v_in = v_oc - i * r_true                  # regen (i < 0) lifts the terminal
        seen = (v_in, i)                          # A1 reports i_in = i here
        v_oc += -i * 0.01 / 6.67                  # bank charges
        trace.append(v_in)
    assert max(trace) <= C.V_TERM_MAX + C.SLEW_STEP_A * r_true + 1e-6
    tail = trace[-50:]
    assert max(tail) - min(tail) < 0.05


# --- the loop, driven through the plant ------------------------------------------
def test_boot_commands_nothing_until_the_link_is_clean():
    r = Rig()
    for _ in range(C.LINK_RECOVER_FRAMES - 1):
        assert r.step(wheel=W, s=0.0, thr=0.5) == 0.0
        assert not r.running
    assert r.step(wheel=W, s=0.0, thr=0.5) > 0.0     # no FW handshake needed
    assert r.running


def test_assist_stays_assist_with_the_carrier_held(rig):
    # review F02: s = 0 during assist too; the throttle must win
    rig.boot()
    for _ in range(40):
        i = rig.step(wheel=W, s=0.0, thr=0.5)
    assert i == pytest.approx(C.I_ASSIST_MAX * 0.5)
    assert last_current(rig.uart.tx) > 0 and rig.link.erpm > 0    # motoring


def test_braking_regenerates(rig):
    # review F01: with the carrier held the rotor turns forward (+ERPM) and
    # regen is negative current, so the VESC generates instead of motoring
    rig.boot()
    for _ in range(100):
        i = rig.step(wheel=W, s=0.0)
    assert i < -10.0
    assert last_current(rig.uart.tx) < 0 and rig.link.erpm > 0     # generating


def test_coasting_commands_nothing(rig):
    rig.boot()
    for _ in range(20):
        assert rig.step(wheel=W, s=1.0) == 0.0


def test_releasing_the_throttle_cuts_assist_at_once(rig):
    rig.boot()
    for _ in range(30):
        rig.step(wheel=W, s=0.0, thr=1.0)
    assert rig.step(wheel=W, s=0.0, thr=0.0) <= 0.0


def test_throttle_loss_keeps_regen(rig):
    # review F03: a dead throttle reads 0 (sensors.Throttle); that only stops
    # assist, the link stays up and braking still regenerates
    rig.boot()
    for _ in range(20):
        rig.step(wheel=W, s=0.0, thr=0.5)
    for _ in range(100):
        i = rig.step(wheel=W, s=0.0, thr=0.0)
    assert rig.running and i < -10.0


def test_link_silence_zeroes_at_once_and_recovers(rig):
    rig.boot()
    for _ in range(20):
        rig.step(wheel=W, s=0.0, thr=0.5)
    for _ in range(C.LINK_TIMEOUT_TICKS):
        assert rig.step(wheel=W, s=0.0, thr=0.5, reply=False) > 0.0
    assert rig.step(wheel=W, s=0.0, thr=0.5, reply=False) == 0.0
    assert not rig.running and rig.link.fault == 0
    n = len(rig.uart.tx)
    rig.step(reply=False)
    assert len(rig.uart.tx) > n                        # keepalive continues at 0 A
    assert last_current(rig.uart.tx) == 0.0
    for _ in range(C.LINK_RECOVER_FRAMES):
        i = rig.step(wheel=W, s=0.0, thr=0.5)
    assert rig.running and i == C.SLEW_STEP_A    # ramps from 0


def test_vesc_fault_limps_until_ten_clean_frames(rig):
    rig.boot()
    rig.step(wheel=W, s=0.0, thr=0.5, fault=5)
    assert not rig.running and rig.link.fault == 5 and rig.loop.i == 0.0
    for _ in range(C.LINK_RECOVER_FRAMES - 1):
        rig.step(wheel=W, s=0.0, thr=0.5)
        assert not rig.running
    rig.step(wheel=W, s=0.0, thr=0.5)
    assert rig.running


def test_snapshot_publishes_the_tick(rig):
    rig.boot()
    for _ in range(30):
        rig.step(wheel=W, s=0.0, thr=0.5, v_in=31.5, i_motor=19.5, temp=40.0)
    sn = rig.loop.sn
    assert sn[control.SN_IMOTOR] == pytest.approx(19.5)
    assert sn[control.SN_VIN] == pytest.approx(31.5)
    assert sn[control.SN_WHEEL] == W
    assert sn[control.SN_TFET] == pytest.approx(40.0)
    assert sn[control.SN_RUN] == 1.0


# --- the slip PI, closed loop through the whole tick --------------------------------
def band_plant(grip, seconds, delay_ticks=6, thr_at=None):
    """Carrier held by a friction band that grips up to `grip` amps worth of
    motor current: slip grows while regen exceeds the grip and shrinks while
    it is below (fixed carrier inertia, an integrating plant). The loop sees
    slip delay_ticks late, as 6-pulse wheel sensing gives it."""
    r = Rig()
    r.boot(wheel=W)
    s, seen, trace = 0.0, [1.0] * delay_ticks, []
    for n in range(int(seconds / C.DT)):
        thr = 0.5 if thr_at is not None and n * C.DT >= thr_at else 0.0
        i = r.step(wheel=W, s=seen[-delay_ticks], thr=thr)
        s = min(1.0, max(0.0, s + 0.2 * (-i - grip) * C.DT))
        seen.append(s)
        trace.append((s, i))
    return trace


@pytest.mark.parametrize("grip", [8.0, 25.0, 32.0])
def test_regen_settles_where_the_rider_squeezes(grip):
    tail = band_plant(grip, 6.0)[-200:]
    assert all(abs(s - C.SLIP_SET) < 0.01 for s, _ in tail)       # at the allowed slip
    assert all(abs(-i - grip) < 0.5 for _, i in tail)             # torque = the squeeze


def test_a_grip_beyond_the_envelope_just_holds_the_carrier():
    # the bank cap (38 A at 25 V) binds first: full allowed regen, carrier held
    tail = band_plant(60.0, 6.0)[-50:]
    cap = (C.V_TERM_MAX - 25.0) / C.R_BANK
    assert all(s == 0.0 and abs(-i - cap) < 1e-6 for s, i in tail)


def test_the_throttle_ends_regen_at_once():
    trace = band_plant(25.0, 4.0, thr_at=3.0)
    n = int(3.0 / C.DT)
    assert trace[n - 1][1] < -20.0 and trace[n][1] > 0.0


def test_no_regen_near_a_standstill(rig):
    # slip reads 1 below W_MIN_RPM, so even a held carrier gets no regen
    # (it would back the wheel up); the request alone guarantees it
    rig.boot()
    for _ in range(50):
        i = rig.step(wheel=W, s=0.0)
    assert i < -10.0
    assert rig.step(wheel=C.W_MIN_RPM - 1, s=0.0) == 0.0
    for _ in range(100):
        assert rig.step(wheel=C.W_MIN_RPM - 1, s=0.0) == 0.0


def test_request_caps_regen():
    assert request(1.0, 1.0, 0.0, -C.I_REGEN_MAX) == -C.I_REGEN_MAX


def test_request_never_regenerates_while_coasting():
    i = 0.0
    for _ in range(100):
        e = C.SLIP_SET - 1.0
        i = envelope(request(e, 0.0, 0.0, i), i, 25.0, 0.0)
    assert i == 0.0
