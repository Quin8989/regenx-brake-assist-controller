"""sim.strategy_context — Hardware-enforced signal contract for strategies.

StrategyContext is the ONLY data bundle passed to strategy .update(ctx).
All fields correspond to signals measurable on the physical Pico + VESC
hardware. __slots__ prevents adding arbitrary attributes — any attempt
to sneak sim-only data (like the ``locked`` boolean from physics.py)
raises AttributeError at runtime.

Signal sources on real hardware
================================

VESC standard telemetry (COMM_GET_VALUES / SELECTIVE, 100 Hz):
    rpm, vcap, iq_actual, duty_cycle, input_current,
    temp_fet, temp_motor, vd, vq

VESC LispBM push (COMM_CUSTOM_APP_DATA, 100 Hz, lower latency):
    rpm_fast, iq_instantaneous

The naming here reflects the existing public API in the project, but the
important distinction is latency, not raw-ADC immediacy:
    rpm               Averaged / normal telemetry path
    rpm_fast          Less-filtered RPM estimate from LispBM push
    iq_actual         Averaged telemetry iq
    iq_instantaneous  Lower-latency filtered iq from LispBM push

When the lower-latency LispBM push is unavailable or stale, strategies
should use preferred_rpm / preferred_iq instead of deciding ad hoc which
field to read. That keeps the fallback policy in one place.

Pico-local:
    dt_ctrl

Motor / system constants accessible via config/settings.py:
    FLUX_LINKAGE_WB, MOTOR_PHASE_RESISTANCE_OHM, VESC_MOTOR_POLE_PAIRS,
    REGEN_CURRENT_MAX_A, VCAP_REGEN_TAPER_START_V, VCAP_REGEN_TAPER_END_V,
    WHEEL_RADIUS_M, CAPACITANCE_F
"""


class StrategyContext:
    """Bundle of signals available to regen strategies on real hardware.

    Every field corresponds to a signal the Pico can actually read from
    the VESC via UART telemetry, LispBM push, or local measurement.
    __slots__ prevents adding arbitrary attributes — any attempt to sneak
    sim-only data (like the ``locked`` boolean from physics.py) will
    raise AttributeError at runtime.
    """
    __slots__ = (
        'rpm',               # Mechanical RPM (averaged, ERPM / pole_pairs)
        'vcap',              # Bus / supercap voltage (V)
        'iq_actual',         # Averaged FOC q-axis current (A)
        'duty_cycle',        # Duty cycle (−1 to 1)
        'input_current',     # Input / bus current (A)
        'temp_fet',          # MOSFET temperature (°C)
        'temp_motor',        # Motor temperature (°C)
        'vd',                # FOC d-axis voltage (V)
        'vq',                # FOC q-axis voltage (V)
        'rpm_fast',          # Optional less-filtered RPM from LispBM push
        'iq_instantaneous',  # Optional lower-latency filtered iq from LispBM push
        'dt_ctrl',           # Control loop period (s)
    )

    def __init__(self, rpm=0.0, vcap=0.0, dt_ctrl=0.01,
                 iq_actual=0.0, duty_cycle=0.0, input_current=0.0,
                 temp_fet=25.0, temp_motor=25.0, vd=0.0, vq=0.0,
                 rpm_fast=None, iq_instantaneous=None):
        self.rpm = rpm
        self.vcap = vcap
        self.dt_ctrl = dt_ctrl
        self.iq_actual = iq_actual
        self.duty_cycle = duty_cycle
        self.input_current = input_current
        self.temp_fet = temp_fet
        self.temp_motor = temp_motor
        self.vd = vd
        self.vq = vq
        self.rpm_fast = rpm_fast
        self.iq_instantaneous = iq_instantaneous

    @property
    def preferred_rpm(self):
        """Preferred RPM signal for control decisions.

        Uses the lower-latency LispBM RPM when available, otherwise falls
        back to the averaged telemetry RPM.
        """
        if self.rpm_fast is not None:
            return self.rpm_fast
        return self.rpm

    @property
    def preferred_iq(self):
        """Preferred iq signal for control decisions.

        Uses the lower-latency LispBM iq when available, otherwise falls
        back to the averaged telemetry iq.
        """
        if self.iq_instantaneous is not None:
            return self.iq_instantaneous
        return self.iq_actual
