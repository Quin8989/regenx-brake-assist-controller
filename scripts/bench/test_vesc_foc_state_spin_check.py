# scripts/bench/test_vesc_foc_state_spin_check.py
#
# Read-only test: sample foc_state repeatedly while manually spinning wheel.
# No torque commands are sent.

from time import sleep_ms, ticks_ms, ticks_diff

from scripts.lib.vesc_terminal import run_terminal_cmd
from scripts.lib.vesc_uart_template import VescUartTemplate

SAMPLE_MS = 300
IDLE_MS = 6000
SPIN_MS = 14000

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


def collect(label, duration_ms, prompt=None):
    print("\n--- %s (%0.1fs) ---" % (label, duration_ms / 1000.0))
    if prompt:
        print(prompt)

    start = ticks_ms()
    samples = 0
    iabs_min = None
    iabs_max = None
    iq_min = None
    iq_max = None
    phase_min = None
    phase_max = None

    while ticks_diff(ticks_ms(), start) < duration_ms:
        lines = run_terminal_cmd(vesc, "foc_state", timeout_ms=1200, settle_ms=15)
        vals = parse_foc_state(lines)

        if "i_abs" in vals:
            x = vals["i_abs"]
            iabs_min = x if iabs_min is None or x < iabs_min else iabs_min
            iabs_max = x if iabs_max is None or x > iabs_max else iabs_max
        if "iq" in vals:
            x = vals["iq"]
            iq_min = x if iq_min is None or x < iq_min else iq_min
            iq_max = x if iq_max is None or x > iq_max else iq_max
        if "phase" in vals:
            x = vals["phase"]
            phase_min = x if phase_min is None or x < phase_min else phase_min
            phase_max = x if phase_max is None or x > phase_max else phase_max

        samples += 1
        sleep_ms(SAMPLE_MS)

    def span(a, b):
        if a is None or b is None:
            return None
        return b - a

    return {
        "label": label,
        "samples": samples,
        "iabs_min": iabs_min,
        "iabs_max": iabs_max,
        "iabs_span": span(iabs_min, iabs_max),
        "iq_min": iq_min,
        "iq_max": iq_max,
        "iq_span": span(iq_min, iq_max),
        "phase_min": phase_min,
        "phase_max": phase_max,
        "phase_span": span(phase_min, phase_max),
    }


print()
print("=" * 62)
print("  VESC FOC State Spin Check (Read-Only)")
print("=" * 62)

idle = collect("Phase 1: Idle", IDLE_MS)
spin = collect("Phase 2: Spin", SPIN_MS, prompt="Spin wheel by hand repeatedly now.")

print("\n" + "=" * 62)
print("Summary")
print("=" * 62)

for r in (idle, spin):
    print("%s: samples=%d" % (r["label"], r["samples"]))
    print("  i_abs span: %s" % ("%.3f" % r["iabs_span"] if r["iabs_span"] is not None else "N/A"))
    print("  iq span:    %s" % ("%.3f" % r["iq_span"] if r["iq_span"] is not None else "N/A"))
    print("  phase span: %s" % ("%.3f" % r["phase_span"] if r["phase_span"] is not None else "N/A"))

print("\nInterpretation:")
if spin["phase_span"] is not None and spin["phase_span"] > 0.2:
    if (spin["iabs_span"] or 0.0) < 0.05 and (spin["iq_span"] or 0.0) < 0.05:
        print("  Phase changed but i_abs/iq stayed near zero -> current sensing path appears non-responsive under faulted operation.")
    else:
        print("  Phase and current terms changed together -> current estimation appears responsive.")
else:
    print("  No clear phase movement observed; rerun while spinning wheel more aggressively.")

print("\nDone")
