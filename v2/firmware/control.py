# control.py — the 100 Hz tick: sense, decide, clamp, send, publish.
#
# Pure logic, host-testable: collaborators are duck-typed (tests/conftest.py)
# and nothing here reads a clock. RGX-2-003 §3, D8-D10 (as amended in Rev C).
#
# States fall out of the link instead of being tracked:
#   RUN    link.ok >= LINK_RECOVER_FRAMES (clean telemetry, no VESC fault)
#   LIMP   otherwise: boot, silence, VESC fault. 0 A, recovers by itself.
#   DEAD   the strategy raised or returned garbage. 0 A until power cycle.

from array import array

import config as C

# snapshot layout: one array('f') shared with core 1. Core 0 is the only
# writer; a reader may see a mix of two consecutive ticks, which is harmless
# for display and logging, so no lock or seqlock is needed.
(SN_WHEEL, SN_ERPM, SN_VIN, SN_IIN, SN_IMOTOR, SN_ICMD, SN_THR, SN_BRAKE,
 SN_VSYS, SN_TFET, SN_STATE, SN_FAULT, SN_FW, SN_FRAMES, SN_BAD, SN_MISS,
 SN_TMAX) = range(17)
SN_LEN = 17

RUN, LIMP_LINK, LIMP_FAULT, DEAD = 0, 1, 2, 3


def slip(erpm, w_rpm):
    """Carrier slip, 1 = freewheeling, 0 = held (spec §1, sign per config).

    With the carrier held the rotor turns at k x wheel, so s = 1 - rotor/(k
    wheel). s is also 0 during assist (the clutch holds the carrier): it means
    "carrier held", and the throttle says why.
    """
    if w_rpm < C.W_MIN_RPM:
        return 1.0
    s = 1.0 - erpm / (C.POLE_PAIRS * C.K_RATIO * w_rpm)
    return 0.0 if s < 0.0 else (1.0 if s > 1.0 else s)


def envelope(req, last, v_in, i_in, w_rpm, brake):
    """Slew the strategy's request, then clamp it. Pure.

    The slew only limits how fast torque builds (SLEW_STEP_A per tick away
    from zero); any reduction, including a reversal through zero, is taken
    at once. The clamps only ever shrink |i|, so every limit, a released
    throttle and a pulled lever act in the same tick. Both voltage limits use the
    open-circuit estimate v_oc = v_in + i_in*R_BANK (VESC i_in > 0 = drawing
    from the bank). Regen current is capped so A1's terminal stays below
    V_TERM_MAX and assist so it stays above V_TERM_MIN. That single formula is
    the bank-full taper and the brownout floor, and it cannot limit-cycle on
    the bank's I*R step.
    """
    s = C.SLEW_STEP_A
    if req > 0.0:
        i = min(req, (last if last > 0.0 else 0.0) + s)
    else:
        i = max(req, (last if last < 0.0 else 0.0) - s)
    v_oc = v_in + i_in * C.R_BANK
    if i > 0.0:
        cap = 0.0 if brake else min(C.I_ASSIST_MAX, (v_oc - C.V_TERM_MIN) / C.R_BANK)
        return i if i < cap else (cap if cap > 0.0 else 0.0)
    cap = 0.0 if w_rpm < C.W_MIN_RPM else min(C.I_REGEN_MAX, (C.V_TERM_MAX - v_oc) / C.R_BANK)
    return i if i > -cap else (-cap if cap > 0.0 else 0.0)


class Control:
    def __init__(self, link, sensors, strategy):
        self.link = link
        self.sensors = sensors
        self.strategy = strategy
        self.sn = array("f", [0.0] * SN_LEN)
        self.i = 0.0
        self.dead = False
        self._ran = False

    def tick(self):
        """One 10 ms tick. Returns the current sent to A1 (A, + assist)."""
        L = self.link
        S = self.sensors
        L.poll()
        w = S.wheel.read()
        thr = S.throttle.read()
        brake = S.brake()
        run = L.ok >= C.LINK_RECOVER_FRAMES and not self.dead
        i = 0.0
        if run:
            try:
                if not self._ran:
                    self.strategy.reset()
                req = float(self.strategy.update(slip(L.erpm, w), w, L.v_in,
                                                 thr, brake, L.i_motor, C.DT))
                if not -1000.0 < req < 1000.0:   # also catches NaN and inf
                    raise ValueError(req)
                i = envelope(req, self.i, L.v_in, L.i_in, w, brake)
            except Exception:
                self.dead = True
                run = False
                i = 0.0
        self._ran = run
        self.i = i
        L.send(i)

        sn = self.sn
        sn[SN_WHEEL] = w
        sn[SN_ERPM] = L.erpm
        sn[SN_VIN] = L.v_in
        sn[SN_IIN] = L.i_in
        sn[SN_IMOTOR] = L.i_motor
        sn[SN_ICMD] = i
        sn[SN_THR] = thr
        sn[SN_BRAKE] = brake
        sn[SN_VSYS] = S.vsys()
        sn[SN_TFET] = L.temp_fet
        sn[SN_STATE] = (DEAD if self.dead else RUN if run
                        else LIMP_FAULT if L.fault else LIMP_LINK)
        sn[SN_FAULT] = L.fault
        sn[SN_FW] = L.fw
        sn[SN_FRAMES] = L.frames
        sn[SN_BAD] = L.bad
        return i
