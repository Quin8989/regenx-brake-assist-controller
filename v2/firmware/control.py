# control.py — 100 Hz loop logic: state machine, safety envelope, snapshot.
#
# Pure logic, host-testable: no machine/time imports; now_ms is injected and
# collaborators are duck-typed (see tests/conftest.py fakes). The safety
# envelope clamps the strategy's output — never the other way round.
# RGX-2-003 §3, D8-D10, D12.

from array import array

import config
import kinematics

# --- states ---
INIT = 0
RUN = 1
LIMP = 2

# --- LIMP reasons ---
R_NONE = 0
R_LINK = 1        # auto-recovers
R_THROTTLE = 2    # auto-recovers
R_VESC_FAULT = 3  # auto-recovers when fault clears
R_STRATEGY = 4    # latched until power cycle

# --- snapshot layout (array('f') indices) ---
SN_WHEEL_RPM, SN_KMH, SN_ERPM, SN_MOTOR_RPM, SN_SLIP, SN_VBANK, SN_IIN, \
    SN_IMOTOR, SN_ICMD, SN_THROTTLE, SN_VSYS, SN_TFET, SN_STATE, SN_FAULT, \
    SN_FRAMES, SN_CRCFAIL, SN_RESYNC, SN_RTT, SN_DLMISS, SN_GCMAX, SN_KEST, \
    SN_BRAKE, SN_OVERRUN, SN_REASON = range(24)
SN_LEN = 24


class Snapshot:
    """Seqlock, single writer (core 0) / single reader (core 1). D12."""

    def __init__(self):
        self._a = array("f", [0.0] * SN_LEN)
        self._b = array("f", [0.0] * SN_LEN)
        self._active = self._a          # buffer readers should use
        self._spare = self._b
        self.version = 0                # even = stable

    def write(self, src):
        self.version += 1               # odd: write in progress
        sp = self._spare
        for i in range(SN_LEN):
            sp[i] = src[i]
        self._spare = self._active
        self._active = sp
        self.version += 1               # even: stable

    def read(self, dst):
        while True:
            v1 = self.version
            if v1 & 1:
                continue
            act = self._active
            for i in range(SN_LEN):
                dst[i] = act[i]
            if self.version == v1:
                return


def envelope(cmd, v_bank, kmh_val, throttle_failed, brake, last_cmd, dt):
    """Clamp the strategy output. Spec §9/§10; BOM §5.3. Pure."""
    # throttle sensor failed -> no assist
    if throttle_failed and cmd > 0.0:
        cmd = 0.0
    # brake wins over throttle
    if brake and cmd > 0.0:
        cmd = 0.0
    if cmd < 0.0:
        # bank-full taper, linear V_TAPER_START -> V_BANK_MAX
        span = config.V_BANK_MAX - config.V_TAPER_START
        f = (config.V_BANK_MAX - v_bank) / span
        if f < 0.0:
            f = 0.0
        elif f > 1.0:
            f = 1.0
        # back-EMF crossover guard (BOM §5.3)
        if v_bank > config.V_CROSSOVER_GUARD and kmh_val > config.CROSSOVER_KMH:
            f = 0.0
        cmd *= f
    # absolute caps
    if cmd > config.I_ASSIST_MAX_A:
        cmd = config.I_ASSIST_MAX_A
    elif cmd < -config.I_REGEN_MAX_A:
        cmd = -config.I_REGEN_MAX_A
    # slew limit: no strategy bug may step the torque
    max_step = config.SLEW_A_PER_S * dt
    d = cmd - last_cmd
    if d > max_step:
        cmd = last_cmd + max_step
    elif d < -max_step:
        cmd = last_cmd - max_step
    return cmd


class ControlLoop:
    def __init__(self, link, sensors, strat):
        self.link = link
        self.sensors = sensors
        self.strategy = strat
        self.snapshot = Snapshot()
        self.state = INIT
        self.reason = R_NONE
        self.i_cmd = 0.0
        self.kx = kinematics.KCrossCheck()
        self._scratch = array("f", [0.0] * SN_LEN)
        self._last_ms = -1
        self._limp_frames_base = 0
        self.deadline_miss = 0
        self.gc_max_ms = 0.0

    # -- state transitions -----------------------------------------------
    def _update_state(self, now, thr_failed):
        v = self.link.values
        if self.state == INIT:
            if v.fw_major and v.v_in > 1.0:
                self.state = RUN
            return
        link_stale = self.link.age_ms(now) > config.LINK_TIMEOUT_MS
        if self.state == RUN:
            if link_stale:
                self._enter_limp(R_LINK)
            elif thr_failed:
                self._enter_limp(R_THROTTLE)
            elif v.fault:
                self._enter_limp(R_VESC_FAULT)
            return
        # LIMP
        if self.reason == R_STRATEGY:
            return  # latched
        if self.reason == R_LINK:
            fresh = self.link.parser.frames_ok - self._limp_frames_base
            if not link_stale and fresh >= config.LINK_RECOVER_FRAMES:
                self._exit_limp()
        elif self.reason == R_THROTTLE:
            if not thr_failed:
                self._exit_limp()
        elif self.reason == R_VESC_FAULT:
            if not v.fault and not link_stale:
                self._exit_limp()

    def _enter_limp(self, reason):
        self.state = LIMP
        self.reason = reason
        self._limp_frames_base = self.link.parser.frames_ok
        self.i_cmd = 0.0

    def _exit_limp(self):
        self.state = RUN
        self.reason = R_NONE

    # -- the tick ---------------------------------------------------------
    def tick(self, now_ms):
        dt = ((now_ms - self._last_ms) if self._last_ms >= 0
              else config.TICK_MS) / 1000.0
        self._last_ms = now_ms

        wheel_rpm = self.sensors.wheel_rpm(now_ms)
        throttle, thr_failed = self.sensors.throttle(now_ms)
        brake = self.sensors.brake()
        v = self.link.values

        m_rpm = kinematics.motor_rpm(v.erpm)
        s = kinematics.slip(m_rpm, wheel_rpm)
        speed_kmh = kinematics.kmh(wheel_rpm)

        self._update_state(now_ms, thr_failed)

        cmd = 0.0
        if self.state == RUN:
            try:
                cmd = self.strategy.update(s, wheel_rpm, v.v_in, throttle,
                                           brake, v.i_motor, dt)
            except Exception:
                self._enter_limp(R_STRATEGY)
                cmd = 0.0
            # live k cross-check during genuine assist (D10)
            if self.i_cmd > 2.0 and v.i_motor > 2.0:
                self.kx.feed(m_rpm, wheel_rpm)

        self.i_cmd = envelope(cmd, v.v_in, speed_kmh, thr_failed, brake,
                              self.i_cmd, dt)
        if self.state != RUN:
            self.i_cmd = 0.0

        self.link.tick(now_ms, self.i_cmd)
        self._publish(wheel_rpm, speed_kmh, m_rpm, s, throttle, brake)
        return self.i_cmd

    def _publish(self, wheel_rpm, speed_kmh, m_rpm, s, throttle, brake):
        sc = self._scratch
        v = self.link.values
        p = self.link.parser
        sc[SN_WHEEL_RPM] = wheel_rpm
        sc[SN_KMH] = speed_kmh
        sc[SN_ERPM] = v.erpm
        sc[SN_MOTOR_RPM] = m_rpm
        sc[SN_SLIP] = s
        sc[SN_VBANK] = v.v_in
        sc[SN_IIN] = v.i_in
        sc[SN_IMOTOR] = v.i_motor
        sc[SN_ICMD] = self.i_cmd
        sc[SN_THROTTLE] = throttle
        sc[SN_VSYS] = self.sensors.vsys()
        sc[SN_TFET] = v.temp_fet
        sc[SN_STATE] = self.state
        sc[SN_FAULT] = v.fault
        sc[SN_FRAMES] = p.frames_ok
        sc[SN_CRCFAIL] = p.crc_fail
        sc[SN_RESYNC] = p.resync
        sc[SN_RTT] = self.link.rtt_ms
        sc[SN_DLMISS] = self.deadline_miss
        sc[SN_GCMAX] = self.gc_max_ms
        sc[SN_KEST] = self.kx.k_est
        sc[SN_BRAKE] = 1.0 if brake else 0.0
        sc[SN_OVERRUN] = self.link.rx_overrun
        sc[SN_REASON] = self.reason
        self.snapshot.write(sc)
