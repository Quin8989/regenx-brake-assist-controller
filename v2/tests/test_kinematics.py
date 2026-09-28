import config
import kinematics as km


def test_coasting_slip_is_one():
    # rotor parked, wheel turning -> carrier at free speed
    assert km.slip(0.0, 150.0) == 1.0


def test_held_carrier_slip_is_zero():
    # carrier held: omega_m = -k * omega_w
    w = 150.0
    m = -config.K_RATIO * w
    assert abs(km.carrier_rpm(m, w)) < 1e-9
    assert km.slip(m, w) == 0.0


def test_slip_midpoint():
    w = 100.0
    free = km.carrier_free_rpm(w)
    # carrier at half free speed: omega_m from Willis
    m = (1 + config.K_RATIO) * (free / 2) - config.K_RATIO * w
    assert abs(km.slip(m, w) - 0.5) < 1e-9


def test_slip_clamped_beyond_free():
    # motor spinning forward (assist direction reversed) can push s>1 raw
    assert km.slip(500.0, 100.0) == 1.0
    assert km.slip(-9999.0, 100.0) == 0.0


def test_zero_wheel_speed_reports_freewheel():
    assert km.slip(0.0, 0.0) == 1.0


def test_conversions():
    # 20 km/h, 2.1 m circumference -> 158.7 rpm; 6 PPR -> 63 ms period
    rpm = 20.0 / 3.6 / config.WHEEL_CIRC_M * 60.0
    period_us = 60_000_000.0 / (rpm * config.SPD_PPR)
    assert abs(km.wheel_rpm_from_period_us(period_us) - rpm) < 1e-6
    assert abs(km.kmh(rpm) - 20.0) < 1e-9
    assert km.wheel_rpm_from_period_us(0) == 0.0


def test_motor_rpm_sign_and_scale():
    assert km.motor_rpm(10_000) == 10_000 * config.DIR_SIGN / config.POLE_PAIRS


def test_crosscheck_converges_and_flags():
    kx = km.KCrossCheck(alpha=0.2)
    for _ in range(100):
        kx.feed(config.K_RATIO * 100.0, 100.0)   # perfect assist relation
    assert kx.healthy
    assert abs(kx.k_est - config.K_RATIO) < 0.05
    kx2 = km.KCrossCheck(alpha=0.2)
    for _ in range(100):
        kx2.feed(config.K_RATIO * 130.0, 100.0)  # 30% off -> unhealthy
    assert not kx2.healthy


def test_crosscheck_ignores_slow_samples():
    kx = km.KCrossCheck()
    kx.feed(1000.0, 10.0)   # wheel too slow: ignored
    assert kx.samples == 0
