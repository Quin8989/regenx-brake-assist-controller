import array

import config
import control
import strategy
import vesc
from conftest import FakeUART, FakeSensors, make_frame, selective_payload, \
    fw_payload


def make_loop(strat=None):
    uart = FakeUART()
    link = vesc.VescLink(uart, config)
    sensors = FakeSensors()
    loop = control.ControlLoop(link, sensors, strat or strategy.Placeholder())
    return loop, uart, link, sensors


def feed_telem(uart, link, now, **kw):
    uart.inject(make_frame(selective_payload(config.TELEM_MASK, **kw)))
    link.poll(now)


def goto_run(loop, uart, link, now=0):
    uart.inject(make_frame(fw_payload()))
    feed_telem(uart, link, now, v_in=20.0)
    loop.tick(now)
    assert loop.state == control.RUN
    return now


# --- envelope ----------------------------------------------------------------
def big_dt():
    return 10.0  # seconds: disables the slew term in envelope unit tests


def test_envelope_taper_midpoint():
    # at 39 V the 38->40 taper halves regen
    out = control.envelope(-20.0, 39.0, 10.0, False, True, -20.0, big_dt())
    assert abs(out + 10.0) < 1e-6


def test_envelope_full_bank_zeroes_regen():
    out = control.envelope(-20.0, 40.0, 10.0, False, True, -20.0, big_dt())
    assert out == 0.0


def test_envelope_crossover_guard():
    # bank nearly full + above crossover speed -> regen forced to zero
    out = control.envelope(-20.0, 39.5, 29.0, False, True, -20.0, big_dt())
    assert out == 0.0
    # same bank voltage, slow -> only tapered, not zeroed
    out2 = control.envelope(-20.0, 39.5, 20.0, False, True, -20.0, big_dt())
    assert out2 < 0.0


def test_envelope_caps():
    assert control.envelope(500.0, 20.0, 10.0, False, False, 500.0,
                            big_dt()) == config.I_ASSIST_MAX_A
    assert control.envelope(-500.0, 20.0, 10.0, False, True, -500.0,
                            big_dt()) == -config.I_REGEN_MAX_A


def test_envelope_slew_limit():
    dt = 0.01
    out = control.envelope(40.0, 20.0, 10.0, False, False, 0.0, dt)
    assert abs(out - config.SLEW_A_PER_S * dt) < 1e-9


def test_envelope_brake_wins_over_assist():
    assert control.envelope(30.0, 20.0, 10.0, False, True, 30.0,
                            big_dt()) == 0.0


def test_envelope_throttle_failed_no_assist():
    assert control.envelope(30.0, 20.0, 10.0, True, False, 30.0,
                            big_dt()) == 0.0
    # regen unaffected by throttle failure
    assert control.envelope(-10.0, 20.0, 10.0, True, True, -10.0,
                            big_dt()) < 0.0


# --- state machine -----------------------------------------------------------
def test_init_to_run_requires_fw_and_telemetry():
    loop, uart, link, sensors = make_loop()
    loop.tick(0)
    assert loop.state == control.INIT
    goto_run(loop, uart, link, 10)


def test_link_timeout_limps_and_recovers():
    loop, uart, link, sensors = make_loop()
    goto_run(loop, uart, link, 0)
    t = config.LINK_TIMEOUT_MS + 50
    loop.tick(t)
    assert loop.state == control.LIMP and loop.reason == control.R_LINK
    assert loop.i_cmd == 0.0
    # recovery: enough fresh frames
    for i in range(config.LINK_RECOVER_FRAMES + 1):
        t += 10
        feed_telem(uart, link, t, v_in=20.0)
        loop.tick(t)
    assert loop.state == control.RUN


def test_strategy_exception_latches_limp():
    class Bomb(strategy.Strategy):
        """Behaves on the first call (so INIT->RUN survives), then raises."""
        def __init__(self):
            self.calls = 0

        def update(self, *a):
            self.calls += 1
            if self.calls > 1:
                raise ValueError("boom")
            return 0.0

    loop, uart, link, sensors = make_loop(Bomb())
    goto_run(loop, uart, link, 0)
    loop.tick(10)
    assert loop.state == control.LIMP
    assert loop.reason == control.R_STRATEGY
    # stays latched even with a healthy link
    for i in range(50):
        feed_telem(uart, link, 20 + i * 10, v_in=20.0)
        loop.tick(20 + i * 10)
    assert loop.state == control.LIMP


def test_vesc_fault_limps_until_clear():
    loop, uart, link, sensors = make_loop()
    goto_run(loop, uart, link, 0)
    feed_telem(uart, link, 10, v_in=20.0, fault=3)
    loop.tick(10)
    assert loop.state == control.LIMP
    assert loop.reason == control.R_VESC_FAULT
    feed_telem(uart, link, 20, v_in=20.0, fault=0)
    loop.tick(20)
    assert loop.state == control.RUN


def test_limp_commands_zero_on_wire():
    loop, uart, link, sensors = make_loop()
    goto_run(loop, uart, link, 0)
    sensors.thr = 0.5
    loop.tick(10)
    assert loop.i_cmd > 0.0
    loop.tick(config.LINK_TIMEOUT_MS + 100)      # LIMP via silence
    assert loop.i_cmd == 0.0
    n = len(uart.tx)
    loop.tick(config.LINK_TIMEOUT_MS + 110)      # keepalive continues at 0 A
    assert len(uart.tx) > n


# --- integration: placeholder strategy through the loop ----------------------
def test_assist_flows_and_snapshot_publishes():
    loop, uart, link, sensors = make_loop()
    goto_run(loop, uart, link, 0)
    sensors.thr = 0.5
    sensors.rpm = 100.0
    t = 0
    for i in range(30):
        t += 10
        feed_telem(uart, link, t, v_in=20.0, erpm=1000)
        loop.tick(t)
    assert loop.i_cmd > 5.0
    dst = array.array("f", [0.0] * control.SN_LEN)
    loop.snapshot.read(dst)
    assert abs(dst[control.SN_ICMD] - loop.i_cmd) < 1e-6
    assert dst[control.SN_STATE] == control.RUN
    assert dst[control.SN_VBANK] == 20.0


def test_regen_ramps_when_carrier_dragged():
    loop, uart, link, sensors = make_loop()
    goto_run(loop, uart, link, 0)
    sensors.rpm = 150.0                     # riding
    # carrier being held: motor counter-rotating near lock
    erpm = int(-config.K_RATIO * 150.0 * config.POLE_PAIRS * 0.9)
    t = 0
    for i in range(30):
        t += 10
        feed_telem(uart, link, t, v_in=20.0, erpm=erpm)
        loop.tick(t)
    assert loop.i_cmd < -1.0                # regen commanded


def test_snapshot_seqlock_versioning():
    sn = control.Snapshot()
    src = array.array("f", [float(i) for i in range(control.SN_LEN)])
    sn.write(src)
    assert sn.version % 2 == 0
    dst = array.array("f", [0.0] * control.SN_LEN)
    sn.read(dst)
    assert list(dst) == [float(i) for i in range(control.SN_LEN)]
    src[0] = 99.0
    sn.write(src)
    sn.read(dst)
    assert dst[0] == 99.0
    assert sn.version == 4
