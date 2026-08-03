"""sim.plotting -- Canvas-based efficiency gallery HTML generation."""

import base64
import json
import os
from string import Template

import numpy as np

from config.settings import (
    FLUX_LINKAGE_WB as FLUX_LINKAGE,
    MOTOR_PHASE_RESISTANCE_OHM as R_PHASE,
    REGEN_CURRENT_MAX_A as I_MAX,
    VESC_MOTOR_POLE_PAIRS as POLE_PAIRS,
    VESC_WATT_MAX,
    WHEEL_RADIUS_M as R_WHEEL,
)
from .physics import ETA_GEAR, GEAR_N, KT, MU_K, MU_S, T_DRAG_COEFF


# High-contrast, color-blind-friendlier palette.  Each strategy gets a
# unique hue so overlays stay readable when 3+ strategies are plotted.
STRATEGY_COLORS = {
    "fixed_ff":     "#e76f51",  # warm coral
    "pi_controller": "#d1495b",  # crimson
    "aimd_ff":      "#00798c",  # teal
}

_DEFAULT_COLORS = [
    "#e76f51",
    "#d1495b",
    "#00798c",
    "#6a4c93",
    "#edae49",
    "#30638e",
    "#2a9d8f",
]


def _strategy_colors(labels):
    colors = {}
    for idx, label in enumerate(labels):
        colors[label] = STRATEGY_COLORS.get(label, _DEFAULT_COLORS[idx % len(_DEFAULT_COLORS)])
    return colors


def _to_u8_b64(values):
    clipped = np.clip(np.rint(values), 0, 255).astype(np.uint8)
    return base64.b64encode(clipped.tobytes()).decode("ascii")


def _build_motor_map(cols=200, rows=60):
    rpm = np.linspace(0.0, 2000.0, cols)
    tq_scale = (1.0 + GEAR_N) * KT * ETA_GEAR
    tq_abs_max = tq_scale * I_MAX
    # Generator-only: regen-braking region (negative carrier torque).
    tq = np.linspace(-tq_abs_max, 0.0, rows)

    rpm_grid, tq_grid = np.meshgrid(rpm, tq)
    current = np.abs(tq_grid) / max(tq_scale, 1e-9)
    omega_m = rpm_grid * (2.0 * np.pi / 60.0)
    omega_e = omega_m * POLE_PAIRS
    p_emf = FLUX_LINKAGE * omega_e * current
    p_copper = 1.5 * R_PHASE * current * current
    p_drag = T_DRAG_COEFF * omega_m
    p_cap = np.maximum(0.0, np.minimum(VESC_WATT_MAX, p_emf - p_copper - p_drag))
    # Device-level efficiency: include planetary-gear loss so the peak
    # is honest (a ~95% motor through a ~97% gear still caps near 92%).
    # p_mech_carrier = p_motor_shaft / ETA_GEAR, so divide the captured
    # energy by the carrier-side mechanical input.
    p_mech_motor = p_cap + p_copper + p_drag
    p_mech_carrier = p_mech_motor / max(ETA_GEAR, 1e-6)
    eta = np.divide(p_cap, p_mech_carrier, out=np.zeros_like(p_cap), where=p_mech_carrier > 1e-9)

    mmap_max = float(np.max(eta)) if eta.size else 1.0
    mmap_u8 = 255.0 * eta / max(mmap_max, 1e-9)
    wheel_speed = rpm / GEAR_N * (2.0 * np.pi / 60.0) * R_WHEEL * 3.6

    return {
        "b64": _to_u8_b64(mmap_u8),
        "rpm": np.round(rpm, 1).tolist(),
        "tq": np.round(tq, 4).tolist(),
        "wheel_speed": np.round(wheel_speed, 1).tolist(),
        "rows": int(rows),
        "cols": int(cols),
        "max": float(mmap_max),
        "tq_min": float(tq[0]),
        "tq_max": float(tq[-1]),
        "tq_scale": float(tq_scale),
    }


def _round_list(values, digits=3):
    return np.round(np.asarray(values, dtype=float), digits).tolist()


def _quantize_to_int16(values, vmin, vmax):
    """Quantize float array to int16 range, return [b64_encoded, scale, offset]."""
    if len(values) == 0:
        return ("", 1.0, 0.0)
    v = np.asarray(values, dtype=float)
    scale = float((vmax - vmin) / 65535.0) if vmax > vmin else 1.0
    offset = float(vmin)
    # Quantize: (v - offset) / scale, clamped to [0, 65535]
    quantized = np.clip(np.rint((v - offset) / scale), 0, 65535).astype(np.uint16)
    b64 = base64.b64encode(quantized.tobytes()).decode("ascii")
    return (b64, scale, offset)


def _serialize_scores(score_dict):
    if not score_dict:
        return None
    return {
        "capture": float(score_dict["capture"]),
        "fidelity": float(score_dict["fidelity"]),
        "composite": float(score_dict["composite"]),
        "energy_J":   float(score_dict.get("energy_J", 0.0)),
        "peak_decel": float(score_dict.get("peak_decel", 0.0)),
        "peak_jerk":  float(score_dict.get("peak_jerk", 0.0)),
    }


def _basket_summary(strategy_names, traj_data):
    """Mean aggregate across the full (brake x mass x speed) basket.

    Powers the ranked summary + energy bar chart.  Keeping it Python-side
    avoids reconstructing arrays in JS and keeps the payload small.
    """
    summary = {}
    for label in strategy_names:
        vals = {"capture": [], "fidelity": [], "composite": [],
                "energy_J": [], "peak_decel": [], "peak_jerk": []}
        for _, data in traj_data[label].items():
            for sc in data["scores"]:
                if sc is None:
                    continue
                for k in vals:
                    vals[k].append(float(sc.get(k, 0.0)))
        summary[label] = {
            k: (float(np.mean(v)) if v else 0.0) for k, v in vals.items()
        }
    return summary


def _sample_indices(n_points, target=240):
    if n_points <= target:
        return np.arange(n_points, dtype=int)
    return np.linspace(0, n_points - 1, target, dtype=int)


def _serialize_traj_payload(strategy_names, traj_data, speeds, sample_idx):
    """Serialize trajectory data with quantization for all arrays."""
    payload = {}
    
    for label in strategy_names:
        label_cases = {}
        for (bi, mi), data in traj_data[label].items():
            key = f"{bi}_{mi}"
            traces = []
            for si, v0 in enumerate(speeds):
                # Only serialize what the JS actually draws:
                #   spd, spd_base -> Speed Decay
                #   rpm, cur      -> Operating-Point map
                # brake_demand / p_elec / eta per-sample arrays were dead
                # weight (never read in JS); scores carry aggregate metrics.
                spd_sample = data["spd"][si][sample_idx]
                spd_base_sample = data["spd_base"][si][sample_idx]
                rpm_sample = data["rpm"][si][sample_idx]
                cur_sample = data["cur"][si][sample_idx]

                spd_min, spd_max = float(np.min(spd_sample)), float(np.max(spd_sample))
                sbs_min, sbs_max = float(np.min(spd_base_sample)), float(np.max(spd_base_sample))
                rpm_min, rpm_max = float(np.min(rpm_sample)), float(np.max(rpm_sample))
                cur_min, cur_max = float(np.min(cur_sample)), float(np.max(cur_sample))

                spd_b64, spd_scale, spd_off = _quantize_to_int16(spd_sample, spd_min, spd_max)
                sbs_b64, sbs_scale, sbs_off = _quantize_to_int16(spd_base_sample, sbs_min, sbs_max)
                rpm_b64, rpm_scale, rpm_off = _quantize_to_int16(rpm_sample, rpm_min, rpm_max)
                cur_b64, cur_scale, cur_off = _quantize_to_int16(cur_sample, cur_min, cur_max)

                trace = {
                    "v0": float(v0),
                    "spd_q": spd_b64, "spd_s": spd_scale, "spd_o": spd_off,
                    "sbs_q": sbs_b64, "sbs_s": sbs_scale, "sbs_o": sbs_off,
                    "rpm_q": rpm_b64, "rpm_s": rpm_scale, "rpm_o": rpm_off,
                    "cur_q": cur_b64, "cur_s": cur_scale, "cur_o": cur_off,
                    "score": _serialize_scores(data["scores"][si]),
                }
                traces.append(trace)
            label_cases[key] = traces
        payload[label] = {"traj": label_cases}
    
    return payload


def generate_efficiency_gallery_html(
    strategy_names,
    names,
    *,
    traj_data,
    t_traj,
    traj_brakes,
    traj_masses,
    speeds,
    output_dir,
    tag,
):
    colors = _strategy_colors(strategy_names)
    motor_map = _build_motor_map()
    sample_idx = _sample_indices(len(t_traj), target=120)
    t_traj_sampled = np.asarray(t_traj)[sample_idx]
    traj_payload = _serialize_traj_payload(strategy_names, traj_data, speeds, sample_idx)
    basket_summary = _basket_summary(strategy_names, traj_data)

    html = Template(
        r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>RegenX - Strategy Gallery (Scoring-Aligned)</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: "Segoe UI", Tahoma, sans-serif;
    background: linear-gradient(180deg, #f4f6f8 0%, #eef2f4 100%);
    color: #1f2933;
    padding: 24px;
    max-width: 1480px;
    margin: 0 auto;
  }
  h1 {
    text-align: center;
    padding: 12px 0 6px;
    font-size: 2rem;
    color: #102a43;
  }
  .subtitle {
    text-align: center;
    color: #52606d;
    margin-bottom: 20px;
    font-size: 1.05rem;
    line-height: 1.6;
  }
  h2 {
    margin: 28px 0 12px;
    padding-bottom: 8px;
    border-bottom: 3px solid #486581;
    color: #102a43;
    font-size: 1.3rem;
  }
  .note {
    background: #ffffff;
    padding: 14px 18px;
    border-left: 5px solid #3e7cb1;
    margin: 12px 0;
    font-size: 1.02rem;
    line-height: 1.65;
    box-shadow: 0 1px 4px rgba(16, 42, 67, 0.08);
  }
  .slider-box {
    background: #f8fafc;
    padding: 12px 18px;
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .slider-box label {
    font-weight: 700;
    font-size: 0.95rem;
    min-width: 145px;
  }
  .slider-box input[type=range] {
    flex: 1;
    height: 6px;
    accent-color: #3e7cb1;
    cursor: pointer;
  }
  .slider-val {
    font-size: 1rem;
    font-weight: 700;
    color: #1f5f8b;
    min-width: 90px;
    text-align: right;
  }
  .strat-card {
    background: #ffffff;
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 2px 10px rgba(16, 42, 67, 0.1);
    margin-bottom: 20px;
  }
  .strat-header {
    padding: 12px 16px;
    font-weight: 800;
    font-size: 1.08rem;
    border-bottom: 1px solid #d9e2ec;
  }
  .chart-wrap {
    position: relative;
    min-width: 0;
  }
  canvas.heatmap {
    width: 100%;
    display: block;
  }
  .tooltip {
    position: absolute;
    pointer-events: none;
    background: rgba(15, 23, 42, 0.9);
    color: #fff;
    padding: 6px 10px;
    border-radius: 4px;
    font-size: 0.8rem;
    display: none;
    white-space: nowrap;
    z-index: 10;
  }
  .legend-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    padding: 8px 16px 14px;
    font-size: 0.98rem;
    color: #52606d;
  }
  .score-strip {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 10px;
    padding: 12px 16px;
    border-bottom: 1px solid #e5e7eb;
    background: #f8fafc;
  }
  .score-chip {
    min-width: 0;
    padding: 10px 12px;
    border-radius: 8px;
    background: #ffffff;
    box-shadow: inset 0 0 0 1px #d9e2ec;
  }
  .score-chip .k {
    display: block;
    font-size: 0.92rem;
    color: #52606d;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-weight: 800;
  }
  .score-chip .v {
    display: block;
    font-size: 1.02rem;
    font-weight: 800;
    color: #102a43;
    margin-top: 4px;
    line-height: 1.4;
  }
  .score-chip .v .chip-line {
    display: block;
  }
  .score-chip .v .chip-sub {
    display: block;
    font-size: 0.94rem;
    font-weight: 600;
    color: #52606d;
    margin-top: 4px;
    letter-spacing: 0.1px;
  }
  .score-note {
    padding: 0 16px 12px;
    font-size: 0.95rem;
    color: #616e7c;
  }
  .mfrac {
    display: inline-block;
    text-align: center;
    vertical-align: middle;
    line-height: 1.25;
    font-size: 1em;
    margin: 0 4px;
    min-width: 160px;
  }
  .mfrac > span { display: block; white-space: nowrap; }
  .mfrac > span:first-child { border-bottom: 1px solid #243b53; padding: 0 6px 3px; }
  .mfrac > span:last-child  { padding: 3px 6px 0; }
  .score-formula-table {
    width: 100%;
    border-collapse: collapse;
    margin: 10px 0 4px;
    font-size: 0.99rem;
    table-layout: auto;
  }
  .score-formula-table th {
    text-align: left;
    font-size: 0.9rem;
    color: #52606d;
    border-bottom: 2px solid #d9e2ec;
    padding: 3px 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .score-formula-table td {
    padding: 10px 12px;
    vertical-align: middle;
    border-bottom: 1px solid #e5e7eb;
    line-height: 1.6;
  }
  .score-formula-table td:first-child { white-space: nowrap; width: 110px; }
  .score-formula-table td:nth-child(2) { white-space: nowrap; color: #1f5f8b; font-weight: 700; width: 70px; }
  .score-formula-table td:nth-child(3) { min-width: 320px; white-space: nowrap; }
  .strategy-table {
    width: 100%;
    border-collapse: collapse;
    margin: 10px 0 4px;
    font-size: 0.97rem;
    table-layout: auto;
  }
  .strategy-table th {
    text-align: left;
    font-size: 0.9rem;
    color: #52606d;
    border-bottom: 2px solid #d9e2ec;
    padding: 3px 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .strategy-table td {
    padding: 12px;
    vertical-align: top;
    border-bottom: 1px solid #e5e7eb;
    line-height: 1.55;
  }
  .strategy-table td:first-child { white-space: nowrap; width: 150px; font-weight: 700; color: #1f5f8b; }
  .strategy-table td:first-child small { display: block; font-weight: 400; color: #829ab1; font-size: 0.82rem; margin-top: 2px; }
  .strategy-table td:nth-child(2) { min-width: 260px; font-family: "Cambria Math", Cambria, serif; font-size: 1.0rem; color: #243b53; }
  .strategy-table td:nth-child(2) small { color: #627d98; font-size: 0.82rem; display: block; margin-top: 3px; font-family: inherit; }
  .strategy-table ul.term-list { margin: 6px 0 0; padding-left: 18px; }
  .strategy-table ul.term-list li { margin: 2px 0; }
  .lock-swatch {
    width: 18px;
    height: 18px;
    border: 2px solid rgba(200, 30, 30, 0.9);
    background: transparent;
  }
  .plot-row {
    display: flex;
    gap: 0;
    flex-wrap: wrap;
  }
  .plot-row > .chart-wrap {
    flex: 1 1 480px;
  }
</style>
</head>
<body>
<h1>RegenX - Strategy Gallery (Scoring-Aligned)</h1>
<p class="subtitle">
  Shared overlays for direct comparison.
  Per-trace scores are single-scenario (free decel at the selected brake, mass, and initial speed).
  The tune's composite is a weighted average over the full ride basket; numbers here will not match the tune summary.
</p>

<h2>Generator Efficiency Reference Map</h2>
<div class="note">
  <b>X-axis:</b> motor RPM (top axis shows equivalent wheel speed).<br>
  <b>Y-axis:</b> carrier-side regen torque commanded by the motor (negative = generator).<br>
  <b>Colour:</b> instantaneous device efficiency &eta; &mdash; fraction of mechanical power at the carrier that becomes battery current, at steady-state for this one operating point. Includes copper, rotor drag, and planetary-gear loss; excludes band-slip heat, wire / connector, and VESC switching losses.
</div>
<div id="sec-motor-map"></div>

<h2>How Scores Are Calculated</h2>
<div class="note">
  <table class="score-formula-table">
    <thead><tr><th>Dimension</th><th>Weight</th><th>Formula</th><th>Symbols</th></tr></thead>
    <tbody>
    <tr>
      <td><b>Capture</b> S<sub>C</sub></td>
      <td>40&thinsp;%</td>
      <td>S<sub>C</sub> = 100 &sdot; clamp<span class="mfrac"><span>&int; P<sub>elec</sub> dt</span><span>&int; (&tau;<sub>brake</sub> &sdot; &omega;<sub>ours</sub>) dt</span></span></td>
      <td>
        <ul class="term-list">
          <li><b>P<sub>elec</sub></b> &mdash; net electrical power into the cap (post-copper, post-ESR).</li>
          <li><b>&tau;<sub>brake</sub></b> &mdash; rider's commanded brake torque.</li>
          <li><b>&omega;<sub>ours</sub></b> &mdash; wheel speed <em>under this strategy</em>.</li>
          <li><b>Denominator</b> &mdash; brake energy an ideal friction brake would remove right now.</li>
        </ul>
      </td>
    </tr>
    <tr>
      <td><b>Fidelity</b> S<sub>F</sub></td>
      <td>60&thinsp;%</td>
      <td>S<sub>F</sub> = 100 &sdot; clamp(1 &minus; <span class="mfrac"><span>&int; |P<sub>regen</sub> &minus; P<sub>base</sub>| dt</span><span>&int; P<sub>base</sub> dt</span></span>,&thinsp;0,&thinsp;1)</td>
      <td>
        <ul class="term-list">
          <li><b>P<sub>regen</sub></b> = P<sub>elec</sub> + P<sub>copper</sub> + P<sub>band</sub> &mdash; total power pulled from the wheel.</li>
          <li><b>P<sub>base</sub></b> = &tau;<sub>brake</sub> &sdot; &omega;<sub>ours</sub> &mdash; ideal-friction-brake target.</li>
          <li>L1 tracking error, symmetric: over- and under-engagement cost the same.</li>
        </ul>
      </td>
    </tr>
    <tr>
      <td><b>Composite</b></td>
      <td>&mdash;</td>
      <td>0.40&sdot;S<sub>C</sub> + 0.60&sdot;S<sub>F</sub></td>
      <td>Fidelity-weighted fitness: chasing joules can&rsquo;t beat tracking the rider.</td>
    </tr>
    </tbody>
  </table>
</div>

<h2>Strategies in This Gallery</h2>
<div class="note">
  <table class="strategy-table">
    <thead><tr><th>Strategy</th><th>Controller law</th><th>What each term does</th></tr></thead>
    <tbody>
    <tr>
      <td><b>fixed_ff</b><br/><small>feed-forward only</small></td>
      <td>i<sub>q</sub> = k &sdot; &tau;<sub>brake</sub></td>
      <td>
        Pure proportional feed-forward: commanded regen current scales linearly with rider brake demand. Single tuned parameter.
        <ul class="term-list">
          <li><b>k</b> &mdash; feed-forward gain (A per N&middot;m of brake demand). Higher k = more aggressive capture but easier to overshoot P<sub>base</sub> and lose fidelity.</li>
        </ul>
        <em>No feedback, no state.</em> Can't adapt to wheel speed, slip, or motor loading &mdash; the baseline.
      </td>
    </tr>
    <tr>
      <td><b>pi_controller</b><br/><small>decel PI</small></td>
      <td>i<sub>q</sub> = k<sub>ff</sub>&sdot;(1 + k<sub>i</sub> &sdot; &int;&thinsp;e dt) &sdot; &lambda;&omega;<sub>e</sub>/R,&emsp;e = (|&Delta;rpm|/rpm) &minus; &alpha;&sdot;(i<sub>q</sub>/rpm)</td>
      <td>
        PI on the rider's <i>external</i> decel proxy: observed speed-normalised decel minus the portion attributable to motor regen. When the rider pulls harder than motor torque alone can explain, the integrator grows and gain climbs. Observation-only &mdash; uses signals the firmware's StrategyContext already carries.
        <ul class="term-list">
          <li><b>k<sub>ff</sub></b> &mdash; feed-forward gain (same role as fixed_ff's k).</li>
          <li><b>k<sub>i</sub></b> &mdash; integral gain on the external-decel residual. Positive polarity: more residual &rarr; more regen.</li>
          <li><b>&alpha;</b> &mdash; couples q-axis current to expected motor-induced decel. Large &alpha; makes the PI attribute more observed decel to the motor (conservative); small &alpha; attributes more to the rider (aggressive).</li>
        </ul>
      </td>
    </tr>
    <tr>
      <td><b>aimd_ff</b><br/><small>additive-increase / multiplicative-decrease w/ feed-forward</small></td>
      <td>i<sub>q</sub> = k &sdot; &tau;<sub>brake</sub> &sdot; g(slip)<br/><small>g ramps up by k<sub>ai</sub>, collapses by &beta;<sub>md</sub> on slip</small></td>
      <td>
        Feed-forward with a TCP-style slip-avoidance multiplier. When the motor isn't slipping, gain slowly grows (additive increase) to capture more energy; when slip is detected, gain collapses (multiplicative decrease) to hand control back to the band brake. Ships as the runtime default.
        <ul class="term-list">
          <li><b>k</b> &mdash; base feed-forward gain (A per N&middot;m).</li>
          <li><b>k<sub>ai</sub></b> &mdash; additive-increase rate: how fast the gain multiplier climbs when the motor is well-behaved.</li>
          <li><b>&beta;<sub>md</sub></b> &mdash; multiplicative-decrease fraction: how hard the gain collapses when slip is detected (e.g. 0.13 = cut gain to 87% per slip event).</li>
          <li><b>unlock_thresh</b> &mdash; slip detection threshold (RPM units). Below this, no slip penalty; above, MD fires.</li>
        </ul>
        <em>Stateful and adaptive.</em> Wins the composite because it can push capture hard when safe and back off cleanly when the band brake needs to dominate.
      </td>
    </tr>
    <tr>
      <td><b>neural_teacher_gru</b><br/><small>learned policy (research)</small></td>
      <td>i<sub>q</sub> = i<sub>q</sub><sup>prev</sup> + &Delta;<sub>max</sub> &sdot; tanh(GRU(features))</td>
      <td>
        A 32-unit GRU consuming 13 hand-crafted features (RPM, &Delta;RPM, jerk, slip, duty cycle, etc.) and emitting a bounded per-tick delta on the previous current command. Trained offline with evolutionary strategies on the same simulator, selected by held-out capture+fidelity.
        <ul class="term-list">
          <li><b>&Delta;<sub>max</sub></b> &mdash; per-tick current slew cap. Prevents the GRU from issuing a step change bigger than the motor can physically follow.</li>
          <li><b>features</b> &mdash; RPM, drpm, iq, duty, vcap, prev-k, jerk, slip, decel fraction, d_iq, mechanical power.</li>
          <li><b>hidden state</b> &mdash; 32-dim GRU memory; lets the policy integrate history (braking-onset spike detection, warm-up, etc.) without hand-coded state machines.</li>
        </ul>
        Research-only &mdash; not shipped to firmware. Used as a symbolic-regression teacher for analytic distillation.
      </td>
    </tr>
    </tbody>
  </table>
</div>

<h2>Energy Recovered This Scenario</h2>
<div id="sec-energy-bar"></div>

<h2>Trajectory: Speed Decay &amp; Operating-Point Overlays</h2>
<div id="sec-trajectory"></div>


<script>
const LABELS = $labels;
const NAMES = $names;
const COLORS = $colors;
const DATA = $traj_payload;
const BASKET = $basket_summary;
const T_TRAJ = $t_traj;
const TRAJ_BRAKES = $traj_brakes;
const TRAJ_MASSES = $traj_masses;
const TRAJ_SPEEDS = $traj_speeds;
const TRAJ_R_WHEEL = $traj_r_wheel;
const TRAJ_GEAR_N = $traj_gear_n;

const MMAP_B64 = '$mmap_b64';
const MMAP_RPM = $mmap_rpm;
const MMAP_TQ = $mmap_tq;
const MMAP_WSPD = $mmap_wspd;
const MMAP_ROWS = $mmap_rows;
const MMAP_COLS = $mmap_cols;
const MMAP_MAX = $mmap_max;
const MMAP_TQ_MIN = $mmap_tq_min;
const MMAP_TQ_MAX = $mmap_tq_max;
const MMAP_TQ_SCALE = $mmap_tq_scale;
const MU_K_OVER_MU_S = $mu_k_over_mu_s;

function decodeU8(b64, len) {
  const bin = atob(b64);
  const a = new Uint8Array(len);
  for (let i = 0; i < len; i++) a[i] = bin.charCodeAt(i);
  return a;
}

function decodeU16(b64, len) {
  const bin = atob(b64);
  const a = new Uint16Array(len);
  const u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  // Copy bytes in little-endian order
  for (let i = 0; i < len; i++) {
    a[i] = u8[2*i] | (u8[2*i+1] << 8);
  }
  return a;
}

function dequantize(u16array, scale, offset) {
  const result = new Float32Array(u16array.length);
  for (let i = 0; i < u16array.length; i++) {
    result[i] = u16array[i] * scale + offset;
  }
  return result;
}

function plasmaColor(t) {
  t = Math.max(0, Math.min(1, t));
  let r, g, b;
  if (t < 0.25) {
    const s = t / 0.25;
    r = 13 + s * 74 | 0; g = 8 + s * 8 | 0; b = 135 + s * 17 | 0;
  } else if (t < 0.5) {
    const s = (t - 0.25) / 0.25;
    r = 87 + s * 105 | 0; g = 16 + s * 14 | 0; b = 152 - s * 62 | 0;
  } else if (t < 0.75) {
    const s = (t - 0.5) / 0.25;
    r = 192 + s * 51 | 0; g = 30 + s * 90 | 0; b = 90 - s * 65 | 0;
  } else {
    const s = (t - 0.75) / 0.25;
    r = 243 - s * 3 | 0; g = 120 + s * 129 | 0; b = 25 + s * 8 | 0;
  }
  return [r, g, b];
}

function niceTicks(min, max, count) {
  const span = Math.max(max - min, 1e-6);
  const raw = span / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  let step = mag;
  if (norm > 5) step = 10 * mag;
  else if (norm > 2) step = 5 * mag;
  else if (norm > 1) step = 2 * mag;
  const ticks = [];
  const start = Math.ceil(min / step) * step;
  for (let v = start; v <= max + step * 0.5; v += step) ticks.push(v);
  return ticks;
}

function shortName(s) {
  return s.replace(/\b(\d+\.\d{4,})/g, function(_, n) { return parseFloat(n).toFixed(3); });
}

function interpLinear(xArr, yArr, xi) {
  if (xi <= xArr[0]) return yArr[0];
  if (xi >= xArr[xArr.length - 1]) return yArr[yArr.length - 1];
  let lo = 0, hi = xArr.length - 1;
  while (lo < hi - 1) { const mid = (lo + hi) >> 1; if (xArr[mid] <= xi) lo = mid; else hi = mid; }
  const dt = xArr[hi] - xArr[lo];
  const a = dt > 1e-9 ? (xi - xArr[lo]) / dt : 0;
  return yArr[lo] * (1 - a) + yArr[hi] * a;
}

const MG = {top: 44, right: 150, bottom: 72, left: 88};
const CBAR_W = 16, CBAR_GAP = 10;

function buildMotorMap() {
  const cont = document.getElementById('sec-motor-map');
  const card = document.createElement('div');
  card.className = 'strat-card';
  const hdr = document.createElement('div');
  hdr.className = 'strat-header';
  hdr.style.color = '#37474F';
  hdr.textContent = 'Generator Efficiency (Reference)';
  card.appendChild(hdr);

  const wrap = document.createElement('div');
  wrap.className = 'chart-wrap';
  const cvs = document.createElement('canvas');
  cvs.className = 'heatmap';
  wrap.appendChild(cvs);
  const tip = document.createElement('div');
  tip.className = 'tooltip';
  wrap.appendChild(tip);
  card.appendChild(wrap);
  cont.appendChild(card);

  const data = decodeU8(MMAP_B64, MMAP_ROWS * MMAP_COLS);

  function render() {
    const dpr = window.devicePixelRatio || 1;
    const W = cvs.parentElement.clientWidth;
    const H = Math.round(W * 0.58);
    cvs.style.width = W + 'px';
    cvs.style.height = H + 'px';
    cvs.width = W * dpr;
    cvs.height = H * dpr;
    const ctx = cvs.getContext('2d');
    ctx.scale(dpr, dpr);

    const plotW = W - MG.left - MG.right - CBAR_W - CBAR_GAP;
    const plotH = H - MG.top - MG.bottom;
    const cellW = plotW / MMAP_COLS;
    const cellH = plotH / MMAP_ROWS;

    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, W, H);

    for (let r = 0; r < MMAP_ROWS; r++) {
      for (let c = 0; c < MMAP_COLS; c++) {
        const v = data[r * MMAP_COLS + c] / 255.0;
        const rgb = plasmaColor(v);
        ctx.fillStyle = 'rgb(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ')';
        const x0 = Math.floor(MG.left + c * cellW);
        const y0 = Math.floor(MG.top + (MMAP_ROWS - 1 - r) * cellH);
        const x1 = Math.ceil(MG.left + (c + 1) * cellW);
        const y1 = Math.ceil(MG.top + (MMAP_ROWS - r) * cellH);
        ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
      }
    }

    const zeroFrac = (0 - MMAP_TQ_MIN) / (MMAP_TQ_MAX - MMAP_TQ_MIN);
    const zeroY = MG.top + (1.0 - zeroFrac) * plotH;
    ctx.save();
    ctx.strokeStyle = 'rgba(100,100,100,0.9)';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(MG.left, zeroY);
    ctx.lineTo(MG.left + plotW, zeroY);
    ctx.stroke();
    ctx.restore();
    ctx.font = '12px sans-serif';
    ctx.fillStyle = '#52606d';
    ctx.textAlign = 'left';
    ctx.fillText('0 Nm', MG.left + plotW + 6, zeroY + 4);

    ctx.font = '12px sans-serif';
    ctx.fillStyle = '#243b53';
    ctx.textAlign = 'center';
    const xStep = Math.ceil(MMAP_COLS / 8);
    for (let c = 0; c < MMAP_COLS; c += xStep) {
      const x = MG.left + (c + 0.5) * cellW;
      ctx.fillText(MMAP_RPM[c].toFixed(0), x, MG.top + plotH + 30);
    }
    ctx.font = '15px sans-serif';
    ctx.fillText('Motor RPM', MG.left + plotW / 2, MG.top + plotH + 56);

    ctx.font = '12px sans-serif';
    ctx.fillStyle = '#6b7780';
    for (let c = 0; c < MMAP_COLS; c += xStep) {
      const x = MG.left + (c + 0.5) * cellW;
      ctx.fillText(MMAP_WSPD[c].toFixed(0) + ' km/h', x, MG.top - 12);
    }

    ctx.textAlign = 'right';
    ctx.fillStyle = '#243b53';
    ctx.font = '12px sans-serif';
    const yStep = Math.ceil(MMAP_ROWS / 8);
    for (let r = 0; r < MMAP_ROWS; r += yStep) {
      const y = MG.top + (MMAP_ROWS - 1 - r + 0.5) * cellH + 4;
      ctx.fillText(MMAP_TQ[r].toFixed(1), MG.left - 14, y);
    }
    ctx.save();
    ctx.font = '15px sans-serif';
    ctx.translate(30, MG.top + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.fillText('Carrier Torque from Motor (Nm)', 0, 0);
    ctx.restore();

    const cbX = MG.left + plotW + CBAR_GAP;
    for (let i = 0; i < plotH; i++) {
      const rgb = plasmaColor(1.0 - i / plotH);
      ctx.fillStyle = 'rgb(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ')';
      ctx.fillRect(cbX, MG.top + i, CBAR_W, 1);
    }
    ctx.strokeStyle = '#243b53';
    ctx.strokeRect(cbX, MG.top, CBAR_W, plotH);
    ctx.font = '12px sans-serif';
    ctx.fillStyle = '#243b53';
    ctx.textAlign = 'left';
    const cbTickX = cbX + CBAR_W + 8;
    ctx.fillText((MMAP_MAX * 100).toFixed(0) + '%', cbTickX, MG.top + 10);
    ctx.fillText((MMAP_MAX * 50).toFixed(0) + '%', cbTickX, MG.top + plotH / 2 + 4);
    ctx.fillText('0%', cbTickX, MG.top + plotH - 4);
    ctx.save();
    ctx.font = '15px sans-serif';
    ctx.translate(cbTickX + 46, MG.top + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.fillText('Regen Efficiency', 0, 0);
    ctx.restore();

    cvs._plotGeom = {left: MG.left, top: MG.top, cellW, cellH, rows: MMAP_ROWS, cols: MMAP_COLS};
  }

  render();

  cvs.addEventListener('mousemove', function(e) {
    const g = cvs._plotGeom;
    if (!g) return;
    const rect = cvs.getBoundingClientRect();
    const mx = (e.clientX - rect.left) / rect.width * cvs.width / (window.devicePixelRatio || 1);
    const my = (e.clientY - rect.top) / rect.height * cvs.height / (window.devicePixelRatio || 1);
    const col = Math.floor((mx - g.left) / g.cellW);
    const row = g.rows - 1 - Math.floor((my - g.top) / g.cellH);
    if (col < 0 || col >= g.cols || row < 0 || row >= g.rows) {
      tip.style.display = 'none';
      return;
    }
    const v = data[row * g.cols + col] / 255.0 * MMAP_MAX * 100;
    const tq = MMAP_TQ[row];
    const cur = Math.abs(tq) / MMAP_TQ_SCALE;
    tip.textContent = MMAP_RPM[col].toFixed(0) + ' RPM (' + MMAP_WSPD[col].toFixed(0) + ' km/h), ' + tq.toFixed(1) + ' Nm (' + cur.toFixed(1) + ' A): η=' + v.toFixed(1) + '%';
    tip.style.display = 'block';
    const tipW = tip.offsetWidth || 220;
    const localX = e.clientX - rect.left;
    if (localX + tipW + 20 > rect.width) tip.style.left = (localX - tipW - 8) + 'px';
    else tip.style.left = (localX + 12) + 'px';
    tip.style.top = (e.clientY - rect.top - 20) + 'px';
  });
  cvs.addEventListener('mouseleave', function() { tip.style.display = 'none'; });

  let resizeTimer;
  window.addEventListener('resize', function() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(render, 100);
  });
}

function buildTrajectory() {
  const cont = document.getElementById('sec-trajectory');
  const defBrk = Math.floor(TRAJ_BRAKES.length / 2);
  const defMass = TRAJ_MASSES.indexOf(100) >= 0 ? TRAJ_MASSES.indexOf(100) : Math.floor(TRAJ_MASSES.length / 2);
  const defSpd = Math.floor(TRAJ_SPEEDS.length / 2);
  const BASE_COLOR = '#888';

  const sliderWrap = document.createElement('div');
  sliderWrap.className = 'strat-card';
  sliderWrap.style.marginBottom = '12px';
  const shdr = document.createElement('div');
  shdr.className = 'strat-header';
  shdr.style.color = '#37474F';
  shdr.textContent = 'Shared Controls';
  sliderWrap.appendChild(shdr);

  function addSlider(label, min, max, defVal) {
    const box = document.createElement('div');
    box.className = 'slider-box';
    const lbl = document.createElement('label');
    lbl.textContent = label;
    box.appendChild(lbl);
    const inp = document.createElement('input');
    inp.type = 'range';
    inp.min = min;
    inp.max = max;
    inp.value = defVal;
    box.appendChild(inp);
    const val = document.createElement('span');
    val.className = 'slider-val';
    box.appendChild(val);
    sliderWrap.appendChild(box);
    return {inp, val};
  }

  const brk = addSlider('Band Brake Torque', 0, TRAJ_BRAKES.length - 1, defBrk);
  const mass = addSlider('Rider Mass', 0, TRAJ_MASSES.length - 1, defMass);
  const spd = addSlider('Initial Speed', 0, TRAJ_SPEEDS.length - 1, defSpd);

  // ── Strategy toggles ─────────────────────────────────────────────
  const enabled = {};
  for (const lab of LABELS) enabled[lab] = true;
  const toggleBox = document.createElement('div');
  toggleBox.className = 'slider-box';
  toggleBox.style.flexWrap = 'wrap';
  const toggleLbl = document.createElement('label');
  toggleLbl.textContent = 'Active Strategies';
  toggleBox.appendChild(toggleLbl);
  const toggleWrap = document.createElement('div');
  toggleWrap.style.display = 'flex';
  toggleWrap.style.flexWrap = 'wrap';
  toggleWrap.style.gap = '8px';
  toggleWrap.style.flex = '1';
  const toggleBtns = {};
  for (const lab of LABELS) {
    const btn = document.createElement('button');
    btn.type = 'button';
    const col = COLORS[lab] || '#607D8B';
    btn.dataset.lab = lab;
    btn.style.cursor = 'pointer';
    btn.style.padding = '6px 12px';
    btn.style.borderRadius = '16px';
    btn.style.fontSize = '0.88rem';
    btn.style.fontWeight = '700';
    btn.style.border = '2px solid ' + col;
    btn.textContent = shortName(NAMES[lab]);
    function paint() {
      if (enabled[lab]) {
        btn.style.background = col;
        btn.style.color = '#fff';
        btn.style.opacity = '1';
      } else {
        btn.style.background = '#fff';
        btn.style.color = col;
        btn.style.opacity = '0.55';
      }
    }
    btn.addEventListener('click', function() {
      // Always keep at least one active.
      const activeCount = LABELS.filter(function(l) { return enabled[l]; }).length;
      if (enabled[lab] && activeCount === 1) return;
      enabled[lab] = !enabled[lab];
      paint();
      updateAll();
    });
    paint();
    toggleBtns[lab] = btn;
    toggleWrap.appendChild(btn);
  }
  toggleBox.appendChild(toggleWrap);
  sliderWrap.appendChild(toggleBox);

  const card = document.createElement('div');
  card.className = 'strat-card';
  const hdr = document.createElement('div');
  hdr.className = 'strat-header';
  hdr.style.color = '#37474F';
  hdr.textContent = 'Speed & Deceleration Overlay';
  card.appendChild(hdr);

  const scoreStrip = document.createElement('div');
  scoreStrip.className = 'score-strip';
  const scoreRows = {};
  for (const lab of LABELS) {
    const row = document.createElement('div');
    row.className = 'score-chip';
    const title = document.createElement('div');
    title.className = 'k';
    title.style.color = COLORS[lab] || '#333';
    title.textContent = shortName(NAMES[lab]);
    row.appendChild(title);
    const value = document.createElement('div');
    value.className = 'v';
    value.textContent = 'Capture --  Fidelity --  Composite --';
    row.appendChild(value);
    scoreStrip.appendChild(row);
    scoreRows[lab] = value;
  }
  card.appendChild(scoreStrip);

  function makeWrap() {
    const wrap = document.createElement('div');
    wrap.className = 'chart-wrap';
    const cvs = document.createElement('canvas');
    cvs.className = 'heatmap';
    wrap.appendChild(cvs);
    return {wrap, cvs};
  }

  const row1 = document.createElement('div');
  row1.className = 'plot-row';
  const pSpd = makeWrap();
  const pAcc = makeWrap();
  const pJrk = makeWrap();
  row1.appendChild(pSpd.wrap);
  row1.appendChild(pAcc.wrap);
  row1.appendChild(pJrk.wrap);
  const row2 = document.createElement('div');
  row2.className = 'plot-row';
  const pMap = makeWrap();
  row2.appendChild(pMap.wrap);
  card.appendChild(sliderWrap);
  card.appendChild(row2);
  card.appendChild(row1);

  const legend = document.createElement('div');
  legend.className = 'legend-bar';
  function addLegendLine(label, color, dashed) {
    const sp = document.createElement('span');
    sp.style.display = 'inline-flex';
    sp.style.alignItems = 'center';
    sp.style.gap = '6px';
    const sw = document.createElement('span');
    sw.style.width = '24px';
    sw.style.height = '0';
    sw.style.display = 'inline-block';
    sw.style.borderTop = (dashed ? '3px dashed ' : '4px solid ') + color;
    sp.appendChild(sw);
    sp.appendChild(document.createTextNode(label));
    legend.appendChild(sp);
  }
  for (const lab of LABELS) addLegendLine(shortName(NAMES[lab]), COLORS[lab] || '#607D8B', false);
  addLegendLine('Direct wheel brake', BASE_COLOR, true);
  addLegendLine('Static brake threshold', '#c81e1e', true);
  addLegendLine('Dynamic brake threshold', '#1565c0', true);
  card.appendChild(legend);
  cont.appendChild(card);

  const mmapFull = decodeU8(MMAP_B64, MMAP_ROWS * MMAP_COLS);
  const regenRows = [];
  for (let r = 0; r < MMAP_ROWS; r++) if (MMAP_TQ[r] <= 0) regenRows.push(r);
  const MG_T = {top: 46, right: 28, bottom: 62, left: 82};
  const MG_M = {top: 62, right: 150, bottom: 80, left: 94};

  function baselineSpeedFromTrace(selected, j) {
    // Per-trace baseline wheel speed from the sim's parallel-integrated
    // traditional-brake bike (same mass, C_rr, grade; wheel torque is
    // kinetic-level brake_val·µ_k/µ_s).  All selected strategies run
    // against the same baseline for given (mass, brake, v0).
    if (!selected.length) return 0;
    return selected[0].tr.spd_base[j];
  }

  function getSelected() {
    const bi = +brk.inp.value;
    const mi = +mass.inp.value;
    const si = +spd.inp.value;
    const key = bi + '_' + mi;
    const selected = [];
    for (const lab of LABELS) {
      if (!enabled[lab]) continue;
      const traces = DATA[lab].traj[key];
      if (!traces || !traces[si]) continue;
      const tr = traces[si];
      const npts = T_TRAJ.length;
      // Dequantize all quantized arrays on load
      tr.spd = dequantize(decodeU16(tr.spd_q, npts), tr.spd_s, tr.spd_o);
      tr.spd_base = dequantize(decodeU16(tr.sbs_q, npts), tr.sbs_s, tr.sbs_o);
      tr.rpm = dequantize(decodeU16(tr.rpm_q, npts), tr.rpm_s, tr.rpm_o);
      tr.cur = dequantize(decodeU16(tr.cur_q, npts), tr.cur_s, tr.cur_o);
      selected.push({lab: lab, tr: tr, color: COLORS[lab] || '#607D8B'});
    }
    return {
      brakeNm: TRAJ_BRAKES[bi],
      massKg: TRAJ_MASSES[mi],
      spdKmh: TRAJ_SPEEDS[si],
      selected: selected,
    };
  }

  function drawAxisFrame(ctx, mg, plotW, plotH, xTicks, xMap, yTicks, yMap) {
    ctx.strokeStyle = '#e6edf3';
    ctx.lineWidth = 1;
    for (const xTick of xTicks) {
      const x = xMap(xTick);
      ctx.beginPath(); ctx.moveTo(x, mg.top); ctx.lineTo(x, mg.top + plotH); ctx.stroke();
    }
    for (const yTick of yTicks) {
      const y = yMap(yTick);
      ctx.beginPath(); ctx.moveTo(mg.left, y); ctx.lineTo(mg.left + plotW, y); ctx.stroke();
    }
    ctx.strokeStyle = '#c5ced6';
    ctx.strokeRect(mg.left, mg.top, plotW, plotH);
  }

  function drawSpeed(state) {
    const cvs = pSpd.cvs;
    const dpr = window.devicePixelRatio || 1;
    const W = cvs.parentElement.clientWidth;
    const H = Math.round(W * 0.50);
    cvs.style.width = W + 'px';
    cvs.style.height = H + 'px';
    cvs.width = W * dpr;
    cvs.height = H * dpr;
    const ctx = cvs.getContext('2d');
    ctx.scale(dpr, dpr);

    const mg = MG_T;
    const plotW = W - mg.left - mg.right;
    const plotH = H - mg.top - mg.bottom;
    const tMax = T_TRAJ[T_TRAJ.length - 1];
    const maxSpd = Math.max(state.spdKmh + 2, 8);

    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, W, H);
    const xTicks = niceTicks(0, tMax, 12);
    const yTicks = niceTicks(0, maxSpd, 6);
    drawAxisFrame(ctx, mg, plotW, plotH, xTicks, function(t) { return mg.left + plotW * t / tMax; }, yTicks, function(s) { return mg.top + plotH * (1 - s / maxSpd); });

    ctx.save();
    ctx.setLineDash([7, 5]);
    ctx.strokeStyle = BASE_COLOR;
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    for (let j = 0; j < T_TRAJ.length; j++) {
      const x = mg.left + plotW * T_TRAJ[j] / tMax;
      const y = mg.top + plotH * (1 - baselineSpeedFromTrace(state.selected, j) / maxSpd);
      if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.restore();

    // Inline legend: direct wheel brake (top-right inside plot area)
    ctx.save();
    ctx.setLineDash([7, 5]);
    ctx.strokeStyle = BASE_COLOR;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(mg.left + plotW - 132, mg.top + 14);
    ctx.lineTo(mg.left + plotW - 104, mg.top + 14);
    ctx.stroke();
    ctx.restore();
    ctx.font = '11px sans-serif';
    ctx.fillStyle = '#777';
    ctx.textAlign = 'left';
    ctx.fillText('Direct brake', mg.left + plotW - 100, mg.top + 18);

    for (const item of state.selected) {
      ctx.strokeStyle = item.color;
      ctx.lineWidth = 3;
      ctx.beginPath();
      for (let j = 0; j < item.tr.spd.length; j++) {
        const x = mg.left + plotW * T_TRAJ[j] / tMax;
        const y = mg.top + plotH * (1 - item.tr.spd[j] / maxSpd);
        if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }

    ctx.fillStyle = '#243b53';
    ctx.font = '12px sans-serif';
    ctx.textAlign = 'center';
    for (const t of xTicks) {
      if (t === 0) continue;
      ctx.fillText(t.toFixed(0), mg.left + plotW * t / tMax, mg.top + plotH + 22);
    }
    ctx.font = '15px sans-serif';
    ctx.fillText('Time (s)', mg.left + plotW / 2, mg.top + plotH + 48);
    ctx.textAlign = 'right';
    ctx.font = '12px sans-serif';
    for (const s of yTicks) ctx.fillText(s.toFixed(0), mg.left - 14, mg.top + plotH * (1 - s / maxSpd) + 4);
    ctx.save();
    ctx.font = '15px sans-serif';
    ctx.translate(28, mg.top + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.fillText('Wheel Speed (km/h)', 0, 0);
    ctx.restore();
    ctx.font = 'bold 16px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('Speed Decay Overlay (' + state.spdKmh + ' km/h)', mg.left + plotW / 2, 24);
  }

  function drawAccel(state) {
    const cvs = pAcc.cvs;
    const dpr = window.devicePixelRatio || 1;
    const W = cvs.parentElement.clientWidth;
    const H = Math.round(W * 0.50);
    cvs.style.width = W + 'px';
    cvs.style.height = H + 'px';
    cvs.width = W * dpr;
    cvs.height = H * dpr;
    const ctx = cvs.getContext('2d');
    ctx.scale(dpr, dpr);

    const mg = MG_T;
    const plotW = W - mg.left - mg.right;
    const plotH = H - mg.top - mg.bottom;
    const tMax = T_TRAJ[T_TRAJ.length - 1];
    // Traditional-brake decel is the per-timestep derivative of the
    // sim's parallel-integrated baseline wheel speed (spd_base), which
    // already includes rolling resistance + grade + kinetic brake
    // torque.  Same source as the Speed Decay "Direct brake" line.
    const baseAcc = [];
    if (state.selected.length) {
      const sb = state.selected[0].tr.spd_base;
      for (let j = 1; j < sb.length; j++) {
        const dt = T_TRAJ[j] - T_TRAJ[j - 1];
        baseAcc.push(dt > 0 ? (sb[j] - sb[j - 1]) / 3.6 / dt : 0);
      }
    }
    let minAcc = 0;
    for (const a of baseAcc) if (a < minAcc) minAcc = a;
    for (const item of state.selected) {
      for (let j = 1; j < item.tr.spd.length; j++) {
        const dt = T_TRAJ[j] - T_TRAJ[j - 1];
        if (dt > 0) minAcc = Math.min(minAcc, (item.tr.spd[j] - item.tr.spd[j - 1]) / 3.6 / dt);
      }
    }
    const absMax = Math.max(Math.ceil(Math.abs(minAcc) * 2) / 2, 0.5);
    const yMin = -absMax;
    const yMax = 0.0;
    const yRange = yMax - yMin;

    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, W, H);
    const xTicks = niceTicks(0, tMax, 12);
    const yTicks = niceTicks(yMin, yMax, 6);
    drawAxisFrame(ctx, mg, plotW, plotH, xTicks, function(t) { return mg.left + plotW * t / tMax; }, yTicks, function(a) { return mg.top + plotH * (1 - (a - yMin) / yRange); });

    ctx.save();
    ctx.setLineDash([7, 5]);
    ctx.strokeStyle = BASE_COLOR;
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    let baseStarted = false;
    for (let j = 0; j < baseAcc.length; j++) {
      const t = T_TRAJ[j + 1];
      const a = Math.max(yMin, Math.min(yMax, baseAcc[j]));
      const x = mg.left + plotW * t / tMax;
      const y = mg.top + plotH * (1 - (a - yMin) / yRange);
      if (!baseStarted) { ctx.moveTo(x, y); baseStarted = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.restore();

    // Inline legend: direct wheel brake (top-right inside plot area)
    ctx.save();
    ctx.setLineDash([7, 5]);
    ctx.strokeStyle = BASE_COLOR;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(mg.left + plotW - 132, mg.top + 14);
    ctx.lineTo(mg.left + plotW - 104, mg.top + 14);
    ctx.stroke();
    ctx.restore();
    ctx.font = '11px sans-serif';
    ctx.fillStyle = '#777';
    ctx.textAlign = 'left';
    ctx.fillText('Direct brake', mg.left + plotW - 100, mg.top + 18);

    for (const item of state.selected) {
      ctx.strokeStyle = item.color;
      ctx.lineWidth = 3;
      ctx.beginPath();
      let started = false;
      for (let j = 1; j < item.tr.spd.length; j++) {
        const dt = T_TRAJ[j] - T_TRAJ[j - 1];
        const acc = dt > 0 ? (item.tr.spd[j] - item.tr.spd[j - 1]) / 3.6 / dt : 0;
        const x = mg.left + plotW * T_TRAJ[j] / tMax;
        const y = mg.top + plotH * (1 - (Math.max(yMin, Math.min(yMax, acc)) - yMin) / yRange);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }

    ctx.fillStyle = '#243b53';
    ctx.font = '12px sans-serif';
    ctx.textAlign = 'center';
    for (const t of xTicks) {
      if (t === 0) continue;
      ctx.fillText(t.toFixed(0), mg.left + plotW * t / tMax, mg.top + plotH + 22);
    }
    ctx.font = '15px sans-serif';
    ctx.fillText('Time (s)', mg.left + plotW / 2, mg.top + plotH + 48);
    ctx.textAlign = 'right';
    ctx.font = '12px sans-serif';
    for (const a of yTicks) ctx.fillText(a.toFixed(1), mg.left - 14, mg.top + plotH * (1 - (a - yMin) / yRange) + 4);
    ctx.save();
    ctx.font = '15px sans-serif';
    ctx.translate(28, mg.top + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.fillText('Acceleration (m/s²)', 0, 0);
    ctx.restore();
    ctx.font = 'bold 16px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('Acceleration Overlay', mg.left + plotW / 2, 24);
  }

  function drawJerk(state) {
    const cvs = pJrk.cvs;
    const dpr = window.devicePixelRatio || 1;
    const W = cvs.parentElement.clientWidth;
    const H = Math.round(W * 0.34);
    cvs.style.width = W + 'px';
    cvs.style.height = H + 'px';
    cvs.width = W * dpr;
    cvs.height = H * dpr;
    const ctx = cvs.getContext('2d');
    ctx.scale(dpr, dpr);

    const mg = MG_T;
    const plotW = W - mg.left - mg.right;
    const plotH = H - mg.top - mg.bottom;
    const tMax = T_TRAJ[T_TRAJ.length - 1];

    // Compute jerk via central finite-difference of speed
    const J_REF = 20;
    let absMaxJ = 0;
    const jerkData = [];
    for (const item of state.selected) {
      const J = [];
      for (let j = 1; j < item.tr.spd.length - 1; j++) {
        const dt1 = T_TRAJ[j]     - T_TRAJ[j - 1];
        const dt2 = T_TRAJ[j + 1] - T_TRAJ[j];
        if (dt1 <= 0 || dt2 <= 0) { J.push(0); continue; }
        const a1 = (item.tr.spd[j]     - item.tr.spd[j - 1]) / 3.6 / dt1;
        const a2 = (item.tr.spd[j + 1] - item.tr.spd[j])     / 3.6 / dt2;
        const jrk = (a2 - a1) / ((dt1 + dt2) * 0.5);
        J.push(jrk);
        absMaxJ = Math.max(absMaxJ, Math.abs(jrk));
      }
      jerkData.push({color: item.color, J});
    }
    const ceil5 = Math.max(2, Math.ceil(absMaxJ / 2) * 2);
    const yMax = ceil5;
    const yMin = -ceil5;
    const yRange = yMax - yMin;

    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, W, H);
    const xTicks = niceTicks(0, tMax, 12);
    const yTicks = niceTicks(yMin, yMax, 6);
    drawAxisFrame(ctx, mg, plotW, plotH, xTicks, function(t) { return mg.left + plotW * t / tMax; }, yTicks, function(a) { return mg.top + plotH * (1 - (a - yMin) / yRange); });

    // ±J_ref reference lines
    ctx.save();
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = 'rgba(90, 90, 200, 0.55)';
    ctx.lineWidth = 1.5;
    for (const jref of [J_REF, -J_REF]) {
      if (jref < yMin || jref > yMax) continue;
      const yr = mg.top + plotH * (1 - (jref - yMin) / yRange);
      ctx.beginPath(); ctx.moveTo(mg.left, yr); ctx.lineTo(mg.left + plotW, yr); ctx.stroke();
    }
    ctx.restore();
    if (J_REF >= yMin && J_REF <= yMax) {
      ctx.font = '11px sans-serif';
      ctx.fillStyle = 'rgba(90, 90, 200, 0.8)';
      ctx.textAlign = 'left';
      ctx.fillText('\u00b1Jref = \u00b120 m/s\u00b3', mg.left + 6, mg.top + plotH * (1 - (J_REF - yMin) / yRange) - 5);
    }

    // Zero line
    ctx.save();
    ctx.strokeStyle = '#aab4be';
    ctx.lineWidth = 1;
    const y0 = mg.top + plotH * (1 - (0 - yMin) / yRange);
    ctx.beginPath(); ctx.moveTo(mg.left, y0); ctx.lineTo(mg.left + plotW, y0); ctx.stroke();
    ctx.restore();

    for (const {color, J} of jerkData) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      for (let j = 0; j < J.length; j++) {
        const t = T_TRAJ[j + 1];
        const x = mg.left + plotW * t / tMax;
        const y = mg.top + plotH * (1 - (Math.max(yMin, Math.min(yMax, J[j])) - yMin) / yRange);
        if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }

    ctx.fillStyle = '#243b53';
    ctx.font = '12px sans-serif';
    ctx.textAlign = 'center';
    for (const t of xTicks) {
      if (t === 0) continue;
      ctx.fillText(t.toFixed(0), mg.left + plotW * t / tMax, mg.top + plotH + 22);
    }
    ctx.font = '15px sans-serif';
    ctx.fillText('Time (s)', mg.left + plotW / 2, mg.top + plotH + 48);
    ctx.textAlign = 'right';
    ctx.font = '12px sans-serif';
    for (const j of yTicks) ctx.fillText(j.toFixed(0), mg.left - 14, mg.top + plotH * (1 - (j - yMin) / yRange) + 4);
    ctx.save();
    ctx.font = '15px sans-serif';
    ctx.translate(28, mg.top + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.fillText('Jerk (m/s\u00b3)', 0, 0);
    ctx.restore();
    ctx.font = 'bold 16px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('Jerk Overlay', mg.left + plotW / 2, 24);
  }

  function drawMap(state) {
    const cvs = pMap.cvs;
    const dpr = window.devicePixelRatio || 1;
    const W = cvs.parentElement.clientWidth;
    const H = Math.round(W * 0.55);
    cvs.style.width = W + 'px';
    cvs.style.height = H + 'px';
    cvs.width = W * dpr;
    cvs.height = H * dpr;
    const ctx = cvs.getContext('2d');
    ctx.scale(dpr, dpr);

    const mg = MG_M;
    const plotW = W - mg.left - mg.right - CBAR_W - CBAR_GAP;
    const plotH = H - mg.top - mg.bottom;
  const tMax = T_TRAJ[T_TRAJ.length - 1];
    const tqStatic = -state.brakeNm;
    const tqKinetic = -state.brakeNm * MU_K_OVER_MU_S;

    let rpmMin = Infinity, rpmMax = 0, tqMin = 0, tqMax = 0;
    const traces = [];
    for (const item of state.selected) {
      const pts = [];
      for (let j = 0; j < item.tr.rpm.length; j++) {
        const rpm = item.tr.rpm[j];
        const tq = -MMAP_TQ_SCALE * item.tr.cur[j];
        if (rpm <= 0) continue;
        pts.push([rpm, tq, j]);
        rpmMin = Math.min(rpmMin, rpm);
        rpmMax = Math.max(rpmMax, rpm);
        tqMin = Math.min(tqMin, tq);
        tqMax = Math.max(tqMax, tq);
      }
      traces.push({lab: item.lab, color: item.color, pts: pts});
    }
    if (!isFinite(rpmMin)) { rpmMin = 0; rpmMax = 500; tqMin = -5; tqMax = 0; }
    tqMin = Math.min(tqMin, tqStatic, tqKinetic);
    tqMax = Math.max(tqMax, 0);
    const rpmPad = Math.max(50, (rpmMax - rpmMin) * 0.12);
    const tqPadLow = Math.max(1.5, Math.abs(tqMin) * 0.12);
    const tqPadHigh = Math.max(0.8, Math.abs(tqMax - tqMin) * 0.08);
    rpmMin = Math.max(0, rpmMin - rpmPad);
    rpmMax = Math.min(MMAP_RPM[MMAP_RPM.length - 1], rpmMax + rpmPad);
    tqMin = Math.max(MMAP_TQ[regenRows[0]], tqMin - tqPadLow);
    tqMax = Math.min(0, tqMax + tqPadHigh);
    const rpmSpan = Math.max(1, rpmMax - rpmMin);
    const tqSpan = Math.max(1e-6, tqMax - tqMin);
    state._rpmMin = rpmMin; state._rpmSpan = rpmSpan;
    state._tqMin  = tqMin;  state._tqSpan  = tqSpan;

    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, W, H);

    const rpmStep = MMAP_RPM.length > 1 ? (MMAP_RPM[1] - MMAP_RPM[0]) : 1;
    const tqStep = Math.abs(MMAP_TQ[1] - MMAP_TQ[0]);
    for (const r of regenRows) {
      const tq = MMAP_TQ[r];
      if (tq + tqStep * 0.5 < tqMin || tq - tqStep * 0.5 > tqMax) continue;
      for (let c = 0; c < MMAP_COLS; c++) {
        const rpm = MMAP_RPM[c];
        if (rpm + rpmStep * 0.5 < rpmMin || rpm - rpmStep * 0.5 > rpmMax) continue;
        const v = mmapFull[r * MMAP_COLS + c] / 255.0;
        const rgb = plasmaColor(v);
        const dr = Math.round(rgb[0] * 0.35 + 255 * 0.65);
        const dg = Math.round(rgb[1] * 0.35 + 255 * 0.65);
        const db = Math.round(rgb[2] * 0.35 + 255 * 0.65);
        ctx.fillStyle = 'rgb(' + dr + ',' + dg + ',' + db + ')';
        const x0 = mg.left + ((rpm - rpmStep * 0.5 - rpmMin) / rpmSpan) * plotW;
        const x1 = mg.left + ((rpm + rpmStep * 0.5 - rpmMin) / rpmSpan) * plotW;
        const y1 = mg.top + (1 - ((tq - tqStep * 0.5) - tqMin) / tqSpan) * plotH;
        const y0 = mg.top + (1 - ((tq + tqStep * 0.5) - tqMin) / tqSpan) * plotH;
        const rx0 = Math.floor(x0), ry0 = Math.floor(y0);
        ctx.fillRect(rx0, ry0, Math.max(1, Math.ceil(x1) - rx0), Math.max(1, Math.ceil(y1) - ry0));
      }
    }

    ctx.strokeStyle = '#c5ced6';
    ctx.strokeRect(mg.left, mg.top, plotW, plotH);

    function tqToY(tq) { return mg.top + (1 - (tq - tqMin) / tqSpan) * plotH; }
    const yStatic = tqToY(tqStatic);
    const yKinetic = tqToY(tqKinetic);
    ctx.save();
    ctx.setLineDash([8, 4]);
    ctx.strokeStyle = '#c81e1e';
    ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.moveTo(mg.left, yStatic); ctx.lineTo(mg.left + plotW, yStatic); ctx.stroke();
    ctx.restore();
    ctx.font = 'bold 13px sans-serif';
    ctx.fillStyle = '#b71c1c';
    ctx.textAlign = 'left';
    ctx.fillText('Static brake ' + state.brakeNm.toFixed(0) + ' Nm', mg.left + 8, Math.max(mg.top + 14, yStatic - 8));
    ctx.save();
    ctx.setLineDash([5, 5]);
    ctx.strokeStyle = '#1565c0';
    ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.moveTo(mg.left, yKinetic); ctx.lineTo(mg.left + plotW, yKinetic); ctx.stroke();
    ctx.restore();
    ctx.fillStyle = '#0d47a1';
    ctx.fillText('Dynamic brake ' + (state.brakeNm * MU_K_OVER_MU_S).toFixed(0) + ' Nm', mg.left + 8, Math.max(mg.top + 30, yKinetic - 8));

    for (const item of traces) {
      if (item.pts.length < 2) continue;
      ctx.strokeStyle = item.color;
      ctx.lineWidth = 3.5;
      ctx.beginPath();
      for (let p = 0; p < item.pts.length; p++) {
        const x = mg.left + ((item.pts[p][0] - rpmMin) / rpmSpan) * plotW;
        const y = mg.top + (1 - (item.pts[p][1] - tqMin) / tqSpan) * plotH;
        if (p === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
      const sp = item.pts[0];
      const ep = item.pts[item.pts.length - 1];
      const sx = mg.left + ((sp[0] - rpmMin) / rpmSpan) * plotW;
      const sy = mg.top + (1 - (sp[1] - tqMin) / tqSpan) * plotH;
      const ex = mg.left + ((ep[0] - rpmMin) / rpmSpan) * plotW;
      const ey = mg.top + (1 - (ep[1] - tqMin) / tqSpan) * plotH;
      ctx.fillStyle = item.color;
      ctx.beginPath(); ctx.arc(sx, sy, 5, 0, 2 * Math.PI); ctx.fill();
      ctx.fillRect(ex - 4, ey - 4, 8, 8);
    }

    // Time tick markers: 0.1 s spacing for the first second, then 0.5 s spacing.
    const markerTimes = [0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0];
    for (let t = 1.5; t <= tMax + 1e-9; t += 0.5) markerTimes.push(+t.toFixed(1));
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'left';
    for (const item of traces) {
      if (item.pts.length < 2) continue;
      let markerIndex = 0;
      for (const pt of item.pts) {
        const t = T_TRAJ[pt[2]];
        while (markerIndex < markerTimes.length && t + 1e-9 >= markerTimes[markerIndex]) {
          const x = mg.left + ((pt[0] - rpmMin) / rpmSpan) * plotW;
          const y = mg.top  + (1 - (pt[1] - tqMin) / tqSpan) * plotH;
          const mt = markerTimes[markerIndex];
          const isWholeSecond = Math.abs(mt - Math.round(mt)) < 1e-9;
          ctx.beginPath(); ctx.arc(x, y, isWholeSecond ? 4.0 : 2.7, 0, 2 * Math.PI);
          ctx.fillStyle = '#fff'; ctx.fill();
          ctx.strokeStyle = item.color; ctx.lineWidth = isWholeSecond ? 1.6 : 1.2; ctx.stroke();
          if (isWholeSecond) {
            ctx.fillStyle = '#243b53';
            ctx.fillText(mt.toFixed(0) + 's', x + 5, y - 3);
          }
          markerIndex += 1;
        }
      }
    }

    ctx.fillStyle = '#243b53';
    ctx.font = '12px sans-serif';
    ctx.textAlign = 'center';
    const rpmTicks = niceTicks(rpmMin, rpmMax, 7);
    for (const rpm of rpmTicks) {
      const x = mg.left + ((rpm - rpmMin) / rpmSpan) * plotW;
      ctx.fillText(rpm.toFixed(0), x, mg.top + plotH + 30);
    }
    ctx.font = '15px sans-serif';
    ctx.fillText('Motor RPM', mg.left + plotW / 2, mg.top + plotH + 60);
    ctx.font = '14px sans-serif';
    ctx.fillStyle = '#243b53';
    const rpmTicksTop = niceTicks(rpmMin, rpmMax, 6);
    for (const rpm of rpmTicksTop) {
      const x = mg.left + ((rpm - rpmMin) / rpmSpan) * plotW;
      const kmh = rpm / TRAJ_GEAR_N * 2 * Math.PI / 60 * TRAJ_R_WHEEL * 3.6;
      ctx.fillText(kmh.toFixed(0) + ' km/h', x, mg.top - 14);
    }
    ctx.textAlign = 'right';
    ctx.fillStyle = '#243b53';
    ctx.font = '12px sans-serif';
    const tqTicks = niceTicks(tqMin, tqMax, 6);
    for (const tq of tqTicks) {
      const y = mg.top + (1 - (tq - tqMin) / tqSpan) * plotH;
      ctx.fillText(tq.toFixed(1), mg.left - 14, y + 4);
    }
    ctx.save();
    ctx.font = '15px sans-serif';
    ctx.translate(32, mg.top + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.fillText('Motor-Commanded Carrier Torque (Nm)', 0, 0);
    ctx.restore();

    const cbX = mg.left + plotW + CBAR_GAP;
    for (let i = 0; i < plotH; i++) {
      const rgb = plasmaColor(1.0 - i / plotH);
      const dr = Math.round(rgb[0] * 0.35 + 255 * 0.65);
      const dg = Math.round(rgb[1] * 0.35 + 255 * 0.65);
      const db = Math.round(rgb[2] * 0.35 + 255 * 0.65);
      ctx.fillStyle = 'rgb(' + dr + ',' + dg + ',' + db + ')';
      ctx.fillRect(cbX, mg.top + i, CBAR_W, 1);
    }
    ctx.strokeStyle = '#243b53';
    ctx.strokeRect(cbX, mg.top, CBAR_W, plotH);
    const cbTickX = cbX + CBAR_W + 8;
    ctx.textAlign = 'left';
    ctx.font = '12px sans-serif';
    ctx.fillStyle = '#243b53';
    ctx.fillText((MMAP_MAX * 100).toFixed(0) + '%', cbTickX, mg.top + 10);
    ctx.fillText((MMAP_MAX * 50).toFixed(0) + '%', cbTickX, mg.top + plotH / 2 + 4);
    ctx.fillText('0%', cbTickX, mg.top + plotH - 4);
    ctx.save();
    ctx.font = '15px sans-serif';
    ctx.translate(cbTickX + 48, mg.top + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.fillText('Regen Efficiency', 0, 0);
    ctx.restore();

    ctx.font = 'bold 16px sans-serif';
    ctx.fillStyle = '#102a43';
    ctx.textAlign = 'center';
    ctx.fillText('Operating-Point Overlay', mg.left + plotW / 2, 26);

    cvs._mapGeom = {left: mg.left, top: mg.top, plotW, plotH, rpmMin: state._rpmMin, rpmSpan: state._rpmSpan, tqMin: state._tqMin, tqSpan: state._tqSpan};
  }

  function updateAll() {
    const state = getSelected();
    brk.val.textContent = state.brakeNm.toFixed(0) + ' Nm';
    mass.val.textContent = state.massKg + ' kg';
    spd.val.textContent = state.spdKmh + ' km/h';
    for (const lab of LABELS) {
      const row = scoreRows[lab];
      const chip = row.parentElement;
      chip.style.display = enabled[lab] ? '' : 'none';
      const item = state.selected.find(function(x) { return x.lab === lab; });
      if (!item || !item.tr.score) {
        row.innerHTML = '<span class="chip-line">Capture --  Fidelity --  Composite --</span>';
        continue;
      }
      const sc = item.tr.score;
      const e  = sc.energy_J || 0;
      const pd = sc.peak_decel || 0;
      const pj = sc.peak_jerk || 0;
      row.innerHTML =
        '<span class="chip-line">Capture ' + sc.capture.toFixed(1) +
          '  Fidelity ' + sc.fidelity.toFixed(1) +
          '  Composite ' + sc.composite.toFixed(1) + '</span>' +
        '<span class="chip-sub">Energy ' + e.toFixed(0) + ' J' +
          '    Peak decel ' + pd.toFixed(2) + ' m/s\u00b2' +
          '    Peak jerk ' + pj.toFixed(1) + ' m/s\u00b3</span>';
    }
    drawSpeed(state);
    drawAccel(state);
    drawJerk(state);
    drawMap(state);
    if (window.__updateEnergyBar) window.__updateEnergyBar(state);
  }

  brk.inp.addEventListener('input', updateAll);
  mass.inp.addEventListener('input', updateAll);
  spd.inp.addEventListener('input', updateAll);

  // ── Operating-point map hover tooltip ───────────────────────────
  const mapTip = document.createElement('div');
  mapTip.className = 'tooltip';
  pMap.wrap.appendChild(mapTip);
  pMap.cvs.addEventListener('mousemove', function(e) {
    const g = pMap.cvs._mapGeom;
    if (!g) return;
    const rect = pMap.cvs.getBoundingClientRect();
    const dpr  = window.devicePixelRatio || 1;
    const mx = (e.clientX - rect.left) / rect.width  * pMap.cvs.width  / dpr;
    const my = (e.clientY - rect.top)  / rect.height * pMap.cvs.height / dpr;
    if (mx < g.left || mx > g.left + g.plotW || my < g.top || my > g.top + g.plotH) {
      mapTip.style.display = 'none'; return;
    }
    const rpm = g.rpmMin + (mx - g.left) / g.plotW * g.rpmSpan;
    const tq  = g.tqMin  + (1 - (my - g.top)  / g.plotH) * g.tqSpan;
    const kmh = rpm / TRAJ_GEAR_N * 2 * Math.PI / 60 * TRAJ_R_WHEEL * 3.6;
    // Interpolate efficiency from mmap
    const cFrac = (rpm - MMAP_RPM[0]) / (MMAP_RPM[MMAP_RPM.length-1] - MMAP_RPM[0]);
    const rFrac = (tq  - MMAP_TQ[0])  / (MMAP_TQ[MMAP_TQ.length-1]  - MMAP_TQ[0]);
    const ci = Math.min(MMAP_COLS-1, Math.max(0, Math.round(cFrac * (MMAP_COLS-1))));
    const ri = Math.min(MMAP_ROWS-1, Math.max(0, Math.round(rFrac * (MMAP_ROWS-1))));
    const eff = mmapFull[ri * MMAP_COLS + ci] / 255.0 * MMAP_MAX * 100;
    mapTip.textContent = rpm.toFixed(0) + ' RPM (' + kmh.toFixed(1) + ' km/h)  ' + tq.toFixed(1) + ' Nm  η ' + eff.toFixed(1) + '%';
    mapTip.style.display = 'block';
    const tipW   = mapTip.offsetWidth || 260;
    const localX = e.clientX - rect.left;
    mapTip.style.left = (localX + tipW + 20 > rect.width ? localX - tipW - 8 : localX + 12) + 'px';
    mapTip.style.top  = (e.clientY - rect.top - 20) + 'px';
  });
  pMap.cvs.addEventListener('mouseleave', function() { mapTip.style.display = 'none'; });
  requestAnimationFrame(updateAll);
  let resizeTimer;
  window.addEventListener('resize', function() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(updateAll, 100);
  });
}

function buildEnergyBar() {
  const cont = document.getElementById('sec-energy-bar');
  if (!cont) return;
  const card = document.createElement('div');
  card.className = 'strat-card';
  const hdr = document.createElement('div');
  hdr.className = 'strat-header';
  hdr.style.color = '#37474F';
  hdr.textContent = 'Energy Recovered -- Selected Scenario';
  card.appendChild(hdr);
  const wrap = document.createElement('div');
  wrap.className = 'chart-wrap';
  wrap.style.padding = '12px 20px 20px';
  const cvs = document.createElement('canvas');
  cvs.className = 'heatmap';
  wrap.appendChild(cvs);
  card.appendChild(wrap);
  cont.appendChild(card);

  let latestState = null;

  function render() {
    if (!latestState) return;
    const rows = [];
    for (const item of latestState.selected) {
      const e = (item.tr && item.tr.score) ? (item.tr.score.energy_J || 0.0) : 0.0;
      rows.push({lab: item.lab, label: shortName(NAMES[item.lab]),
                 energy: e, color: item.color || COLORS[item.lab] || '#607D8B'});
    }
    if (!rows.length) return;
    rows.sort(function(a, b) { return b.energy - a.energy; });

    const dpr = window.devicePixelRatio || 1;
    const W = wrap.clientWidth;
    const barH = 34, gap = 14, topPad = 12, botPad = 40;
    const H = topPad + botPad + rows.length * (barH + gap);
    cvs.style.width = W + 'px';
    cvs.style.height = H + 'px';
    cvs.width = W * dpr;
    cvs.height = H * dpr;
    const ctx = cvs.getContext('2d');
    ctx.scale(dpr, dpr);

    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, W, H);

    // Measure label widths with shortName and cap to a reasonable left gutter.
    ctx.font = '700 14px "Segoe UI", sans-serif';
    let maxLabelW = 160;
    for (const r of rows) {
      const w = ctx.measureText(r.label).width;
      if (w > maxLabelW) maxLabelW = w;
    }
    const labelPx = Math.min(360, Math.ceil(maxLabelW) + 18);
    const valuePx = 110;
    const leftPad = 18;
    const plotX = leftPad + labelPx;
    const plotW = Math.max(80, W - plotX - valuePx - 12);
    const maxE = Math.max(1e-6, ...rows.map(function(r) { return r.energy; }));

    ctx.textBaseline = 'middle';
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      const y = topPad + i * (barH + gap);
      const yC = y + barH / 2;

      ctx.fillStyle = '#243b53';
      ctx.textAlign = 'right';
      ctx.font = '700 14px "Segoe UI", sans-serif';
      ctx.fillText(r.label, plotX - 12, yC);

      ctx.fillStyle = '#eef2f4';
      ctx.fillRect(plotX, y, plotW, barH);

      const w = plotW * r.energy / maxE;
      const grad = ctx.createLinearGradient(plotX, y, plotX + Math.max(1, w), y);
      grad.addColorStop(0, r.color);
      grad.addColorStop(1, shadeHex(r.color, 0.25));
      ctx.fillStyle = grad;
      ctx.fillRect(plotX, y, w, barH);

      ctx.fillStyle = '#102a43';
      ctx.textAlign = 'left';
      ctx.font = '800 14px "Segoe UI", sans-serif';
      ctx.fillText(r.energy.toFixed(0) + ' J', plotX + plotW + 10, yC);
    }

    ctx.strokeStyle = '#c5ced6';
    ctx.beginPath();
    ctx.moveTo(plotX, topPad + rows.length * (barH + gap) - gap + 4);
    ctx.lineTo(plotX + plotW, topPad + rows.length * (barH + gap) - gap + 4);
    ctx.stroke();
    ctx.fillStyle = '#52606d';
    ctx.textAlign = 'center';
    ctx.font = '12px "Segoe UI", sans-serif';
    const ticks = niceTicks(0, maxE, 5);
    for (const t of ticks) {
      const x = plotX + plotW * t / maxE;
      ctx.fillText(t.toFixed(0), x, H - 20);
    }
    ctx.font = '700 13px "Segoe UI", sans-serif';
    ctx.fillStyle = '#243b53';
    ctx.fillText('Joules recovered in this scenario (higher is better)',
                 plotX + plotW / 2, H - 6);
  }

  function shadeHex(hex, amt) {
    if (!/^#[0-9a-f]{6}$$/i.test(hex)) return hex;
    const r = parseInt(hex.slice(1,3),16);
    const g = parseInt(hex.slice(3,5),16);
    const b = parseInt(hex.slice(5,7),16);
    const mix = function(c) { return Math.round(c + (255 - c) * amt); };
    return 'rgb(' + mix(r) + ',' + mix(g) + ',' + mix(b) + ')';
  }

  window.__updateEnergyBar = function(state) {
    latestState = state;
    render();
  };

  let resizeTimer;
  window.addEventListener('resize', function() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(render, 100);
  });
}

buildEnergyBar();
buildMotorMap();
buildTrajectory();

</script>
</body>
</html>
'''
    ).substitute(
        labels=json.dumps(strategy_names, separators=(",", ":")),
        names=json.dumps(names, separators=(",", ":")),
        colors=json.dumps(colors, separators=(",", ":")),
        traj_payload=json.dumps(traj_payload, separators=(",", ":")),
        basket_summary=json.dumps(basket_summary, separators=(",", ":")),
        t_traj=json.dumps(_round_list(t_traj_sampled, digits=3), separators=(",", ":")),
        traj_brakes=json.dumps(_round_list(traj_brakes), separators=(",", ":")),
        traj_masses=json.dumps(_round_list(traj_masses), separators=(",", ":")),
        traj_speeds=json.dumps(_round_list(speeds), separators=(",", ":")),
        traj_r_wheel=json.dumps(float(R_WHEEL)),
        traj_gear_n=json.dumps(float(GEAR_N)),
        mmap_b64=motor_map["b64"],
        mmap_rpm=json.dumps(motor_map["rpm"], separators=(",", ":")),
        mmap_tq=json.dumps(motor_map["tq"], separators=(",", ":")),
        mmap_wspd=json.dumps(motor_map["wheel_speed"], separators=(",", ":")),
        mmap_rows=json.dumps(motor_map["rows"]),
        mmap_cols=json.dumps(motor_map["cols"]),
        mmap_max=json.dumps(motor_map["max"]),
        mmap_tq_min=json.dumps(motor_map["tq_min"]),
        mmap_tq_max=json.dumps(motor_map["tq_max"]),
        mmap_tq_scale=json.dumps(motor_map["tq_scale"]),
        mu_k_over_mu_s=json.dumps(float(MU_K / MU_S)),
    )

    path = os.path.join(output_dir, f"{tag}gallery.html")
    os.makedirs(output_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    size_mb = os.path.getsize(path) / (1024 * 1024)
    print(f"    saved {path} ({size_mb:.1f} MB)")
    return path
