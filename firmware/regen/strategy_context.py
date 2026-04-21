"""Hardware-enforced signal contract for regen strategies.

``StrategyContext`` is the only data bundle passed to ``strategy.update(ctx)``.
All fields correspond to signals measurable on the physical Pico + VESC
hardware. ``__slots__`` prevents adding arbitrary attributes — any attempt
to sneak sim-only data (e.g. ``locked`` from physics.py) raises
AttributeError at runtime.

Signal sources
==============
VESC selective telemetry (100 Hz):  rpm, vcap, iq_actual, duty_cycle, input_current
VESC LispBM push (100 Hz, aggregated over a 10 ms / 1 kHz-sampled window):
    rpm_fast, iq_mean, drpm_mean, drpm_peak_neg
Pico local:  dt_ctrl

The LispBM script (scripts/vesc_lisp_push_iq.lisp) samples at 1 kHz and
emits, every 10 ms, the mean d(rpm)/dt (telescoping sum / window) and the
most-negative per-sample d(rpm)/dt in the window.  These let the Pico
strategies see both trend deceleration (drpm_mean) and sub-poll slip
spikes (drpm_peak_neg) without aliasing the ~2-5 ms unlock transients
that 100 Hz Pico sampling would miss.

Strategies should read ``preferred_rpm`` / ``preferred_iq`` which return the
low-latency push value when fresh and fall back to averaged telemetry
otherwise — keeps the fallback policy in one place.  ``drpm_mean`` /
``drpm_peak_neg`` are 0.0 when the push path is stale; callers that treat
those as slip signals must therefore ignore them in that case.

Fields removed in 2026-04 cleanup: temp_fet, temp_motor, vd, vq.  None of
the shipped strategies read them and selective telemetry does not refresh
vd/vq, so they were a source of stale/zero reads.
"""


class StrategyContext:
    __slots__ = (
        'rpm',               # Mechanical RPM, averaged (ERPM / pole_pairs)
        'vcap',              # Bus / supercap voltage (V)
        'iq_actual',         # Averaged FOC q-axis current (A)
        'duty_cycle',        # Duty cycle (−1..1)
        'input_current',     # Input / bus current (A)
        'rpm_fast',          # Less-filtered mechanical RPM from LispBM push (or None)
        'iq_mean',           # Mean iq over LispBM push window, A (or None)
        'drpm_mean',         # Mean d(rpm)/dt over push window, rpm/s
        'drpm_peak_neg',     # Most-negative per-sample d(rpm)/dt, rpm/s
        'dt_ctrl',           # Control loop period (s)
    )

    def __init__(self, rpm=0.0, vcap=0.0, dt_ctrl=0.01,
                 iq_actual=0.0, duty_cycle=0.0, input_current=0.0,
                 rpm_fast=None, iq_mean=None,
                 drpm_mean=0.0, drpm_peak_neg=0.0):
        self.rpm = rpm
        self.vcap = vcap
        self.dt_ctrl = dt_ctrl
        self.iq_actual = iq_actual
        self.duty_cycle = duty_cycle
        self.input_current = input_current
        self.rpm_fast = rpm_fast
        self.iq_mean = iq_mean
        self.drpm_mean = drpm_mean
        self.drpm_peak_neg = drpm_peak_neg

    @property
    def preferred_rpm(self):
        """Lower-latency LispBM RPM when available, else averaged telemetry."""
        return self.rpm if self.rpm_fast is None else self.rpm_fast

    @property
    def preferred_iq(self):
        """Lower-latency LispBM iq (window mean) when available, else averaged."""
        return self.iq_actual if self.iq_mean is None else self.iq_mean