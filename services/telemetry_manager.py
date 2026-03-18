# services/telemetry_manager.py — Interpret and organize latest VESC telemetry
#
# Accepts decoded telemetry from vesc_comm, detects stale data or
# unreasonable jumps, and optionally computes derived values.


class TelemetryManager:
    def __init__(self, shared_state):
        self._state = shared_state

    def update(self):
        """Process latest telemetry values in shared_state.

        Called each cycle to run plausibility checks and compute derived values.
        """
        # TODO: Detect stale telemetry or unreasonable jumps
        # TODO: Optionally filter noisy values
        # TODO: Compute derived values (e.g. wheel speed from rpm + wheel geometry)
        pass

    # TODO: Decide authoritative source for speed estimation
    # TODO: Decide which telemetry values are display-only vs control-relevant
    # TODO: Define bounds / plausibility checks for telemetry
