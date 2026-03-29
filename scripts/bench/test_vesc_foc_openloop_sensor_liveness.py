# scripts/bench/test_vesc_foc_openloop_sensor_liveness.py
#
# Sensor liveness test: send foc_openloop at a tiny current to force PWM active,
# then sample foc_state repeatedly to see if i_abs rises from 0.
# If sensors are alive, i_abs should be non-zero while openloop runs.
# Sends foc_openloop 0 0 at the end to ensure motor stops.

from time import sleep_ms

from scripts.lib.vesc_terminal import run_terminal_cmd
from scripts.lib.vesc_uart_template import VescUartTemplate

vesc = VescUartTemplate(rxbuf=4096)


def parse_foc_state(lines):
    out = {}
    for line in lines:
        s = line.strip()
        if not s or ":" not in s:
            continue
        k, v = s.split(":", 1)
        k = k.strip().lower()
        v = v.strip()
        try:
            out[k] = float(v)
        except Exception:
            pass
    return out


print()
print("=" * 62)
print("  FOC OPENLOOP SENSOR LIVENESS TEST")
print("=" * 62)
print()
print("WARNING: Motor may twitch/rotate briefly at 0.5A. Keep wheel free.")
print()

# --- Baseline: sample foc_state BEFORE openloop ---
print("Phase 1: Baseline foc_state (no openloop)...")
baseline_lines = run_terminal_cmd(vesc, "foc_state", timeout_ms=1400, settle_ms=15)
baseline = parse_foc_state(baseline_lines)
print("  Raw lines:", baseline_lines[:6])
print("  Parsed:", baseline)

sleep_ms(500)

# --- Send foc_openloop 0.5A at 100 ERPM ---
# Low enough to not move the wheel much, high enough to force ADC sampling
OPENLOOP_CURRENT = 0.5   # amps
OPENLOOP_ERPM = 100      # very slow

print()
print("Phase 2: Sending foc_openloop %.1f %.0f ..." % (OPENLOOP_CURRENT, OPENLOOP_ERPM))
ol_start_lines = run_terminal_cmd(
    vesc,
    "foc_openloop %.1f %.0f" % (OPENLOOP_CURRENT, OPENLOOP_ERPM),
    settle_ms=15,
    timeout_ms=1400
)
print("  foc_openloop response:", ol_start_lines[:4])

sleep_ms(300)  # let it spin up

# --- Sample foc_state 6 times across ~3 seconds while openloop is running ---
print()
print("Phase 3: Sampling foc_state during openloop (6 samples, 500ms apart)...")
samples = []
iabs_max = None

for i in range(6):
    lines = run_terminal_cmd(vesc, "foc_state", timeout_ms=1200, settle_ms=15)
    vals = parse_foc_state(lines)
    iabs = vals.get("i_abs", None)
    iq   = vals.get("iq", None)
    id_  = vals.get("id", None)
    phase = vals.get("phase", None)
    samples.append(vals)
    if iabs is not None:
        if iabs_max is None or iabs > iabs_max:
            iabs_max = iabs
    print("  [%d] i_abs=%.3f  iq=%.3f  id=%.3f  phase=%.3f" % (
        i + 1,
        iabs  if iabs  is not None else -999,
        iq    if iq    is not None else -999,
        id_   if id_   is not None else -999,
        phase if phase is not None else -999,
    ))
    sleep_ms(500)

# --- STOP: always stop openloop regardless of results ---
print()
print("Phase 4: Stopping foc_openloop...")
stop_lines = run_terminal_cmd(vesc, "foc_openloop 0 0", timeout_ms=1400, settle_ms=15)
print("  Stop response:", stop_lines[:4])

sleep_ms(300)

# --- Post-stop sample ---
print()
print("Phase 5: Post-stop foc_state sample...")
post_lines = run_terminal_cmd(vesc, "foc_state", timeout_ms=1400, settle_ms=15)
post = parse_foc_state(post_lines)
print("  Parsed:", post)

# --- Verdict ---
print()
print("=" * 62)
print("  VERDICT")
print("=" * 62)

if iabs_max is None:
    print("  INCONCLUSIVE: No i_abs values parsed from foc_state.")
    print("  Check raw lines in Phase 3 output above.")
elif iabs_max < 0.05:
    print("  FAIL: i_abs stayed at %.4fA during openloop." % iabs_max)
    print("  Current sense path is NOT responding to commanded current.")
    print("  => Hardware damage likely on channels 0 and 1.")
    print("  => Firmware upgrade or board-level repair needed.")
else:
    print("  PASS: i_abs reached %.4fA during openloop." % iabs_max)
    print("  Current sense path IS alive — firmware was suppressing readings.")
    print("  => Proceed with LispBM offset override (path 2).")

print("=" * 62)
print()
