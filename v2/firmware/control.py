# control.py — the 100 Hz tick: sense, decide, clamp, send, publish.
#
# Pure logic, host-testable: collaborators are duck-typed (tests/conftest.py)
# and nothing here reads a clock. RGX-2-003 §3, D8-D10 (as amended in Rev D).
#
# States fall out of the link instead of being tracked:
#   RUN    link.ok >= LINK_RECOVER_FRAMES (clean telemetry, no VESC fault)
#   LIMP   otherwise: boot, silence, VESC fault. 0 A, recovers by itself.

from array import array

import config as C

# snapshot layout: one array('f') the display on core 1 reads. Core 0 is the
# only writer; the display may see a mix of two consecutive ticks, which is
# harmless, so no lock is needed. SN_LATE is written by main.py.
(SN_WHEEL, SN_VIN, SN_ICMD, SN_TFET, SN_STATE, SN_FAULT, SN_BAD,
 SN_LATE) = range(8)
SN_LEN = 8

RUN, LIMP_LINK, LIMP_FAULT = 0, 1, 2


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


def request(e, de, throttle, i_last):
    """The rider's current request: the throttle gives assist and ends regen;
    otherwise PI on the slip error e = SLIP_SET - s toward the allowed slip.

    Velocity form: it adjusts the current actually sent last tick, so nothing
    winds up while the envelope clamps it. While the rider holds the carrier
    (s < SLIP_SET) regen grows until the brake just slips, so the braking
    torque is whatever the squeeze can hold; when they let go the carrier
    freewheels (s -> 1) and regen falls to 0. Regenerating with nobody
    braking is harmless: with the carrier free the motor has nothing to push
    against and only slows its own rotor.
    """
    if throttle > 0.0:
        return C.I_ASSIST_MAX * throttle
    r = (-i_last if i_last < 0.0 else 0.0) + C.SLIP_KP * de + C.SLIP_KI * e * C.DT
    return -r if r > 0.0 else 0.0


def envelope(req, last, v_in, i_in, w_rpm):
    """Slew the request, then clamp it. Pure.

    The slew only limits how fast torque builds (SLEW_STEP_A per tick away
    from zero); any reduction, including a reversal through zero, is taken
    at once. The clamps only ever shrink |i|, so every limit and a released
    throttle act in the same tick. Both voltage limits use the
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
        cap = min(C.I_ASSIST_MAX, (v_oc - C.V_TERM_MIN) / C.R_BANK)
        return i if i < cap else (cap if cap > 0.0 else 0.0)
    cap = 0.0 if w_rpm < C.W_MIN_RPM else min(C.I_REGEN_MAX, (C.V_TERM_MAX - v_oc) / C.R_BANK)
    return i if i > -cap else (-cap if cap > 0.0 else 0.0)


class Control:
    def __init__(self, link, sensors):
        self.link = link
        self.sensors = sensors
        self.sn = array("f", [0.0] * SN_LEN)
        self.i = 0.0
        self._e = C.SLIP_SET - 1.0      # slip error, kept current every tick

    def tick(self):
        """One 10 ms tick. Returns the current sent to A1 (A, + assist)."""
        L = self.link
        S = self.sensors
        L.poll()
        w = S.wheel.read()
        thr = S.throttle.read()
        e = C.SLIP_SET - slip(L.erpm, w)
        de = e - self._e
        self._e = e
        run = L.ok >= C.LINK_RECOVER_FRAMES
        i = envelope(request(e, de, thr, self.i), self.i, L.v_in, L.i_in, w) if run else 0.0
        self.i = i
        L.send(i)

        sn = self.sn
        sn[SN_WHEEL] = w
        sn[SN_VIN] = L.v_in
        sn[SN_ICMD] = i
        sn[SN_TFET] = L.temp_fet
        sn[SN_STATE] = RUN if run else (LIMP_FAULT if L.fault else LIMP_LINK)
        sn[SN_FAULT] = L.fault
        sn[SN_BAD] = L.bad
        return i
