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


STRATEGY_COLORS = {
    "pi_controller": "#d1495b",
    "aimd_ff": "#00798c",
}

_DEFAULT_COLORS = [
    "#d1495b",
    "#00798c",
    "#edae49",
    "#30638e",
    "#6a4c93",
]


def _strategy_colors(labels):
    colors = {}
    for idx, label in enumerate(labels):
        colors[label] = STRATEGY_COLORS.get(label, _DEFAULT_COLORS[idx % len(_DEFAULT_COLORS)])
    return colors


def _to_u8_b64(values):
    clipped = np.clip(np.rint(values), 0, 255).astype(np.uint8)
    return base64.b64encode(clipped.tobytes()).decode("ascii")


def _build_motor_map(cols=200, rows=120):
    rpm = np.linspace(0.0, 2000.0, cols)
    tq_scale = (1.0 + GEAR_N) * KT * ETA_GEAR
    tq_abs_max = tq_scale * I_MAX
    tq = np.linspace(-tq_abs_max, tq_abs_max, rows)

    rpm_grid, tq_grid = np.meshgrid(rpm, tq)
    current = np.abs(tq_grid) / max(tq_scale, 1e-9)
    omega_m = rpm_grid * (2.0 * np.pi / 60.0)
    omega_e = omega_m * POLE_PAIRS
    p_emf = FLUX_LINKAGE * omega_e * current
    p_copper = 1.5 * R_PHASE * current * current
    p_drag = T_DRAG_COEFF * omega_m
    p_cap = np.maximum(0.0, np.minimum(VESC_WATT_MAX, p_emf - p_copper - p_drag))
    denom = p_cap + p_copper + p_drag
    eta = np.divide(p_cap, denom, out=np.zeros_like(p_cap), where=denom > 1e-9)

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
        "energy": float(score_dict["energy"]),
        "tracking": float(score_dict["tracking"]),
        "smoothness": float(score_dict["smoothness"]),
        "composite": float(score_dict["composite"]),
    }


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
                spd_sample = data["spd"][si][sample_idx]
                rpm_sample = data["rpm"][si][sample_idx]
                cur_sample = data["cur"][si][sample_idx]
                brake_sample = data["brake_demand"][si][sample_idx]
                eff_brake_sample = data["eff_brake"][si][sample_idx]
                p_elec_sample = data["p_elec"][si][sample_idx]
                
                # Quantize all arrays to int16 for size reduction
                spd_min, spd_max = float(np.min(spd_sample)), float(np.max(spd_sample))
                rpm_min, rpm_max = float(np.min(rpm_sample)), float(np.max(rpm_sample))
                cur_min, cur_max = float(np.min(cur_sample)), float(np.max(cur_sample))
                brk_min, brk_max = float(np.min(brake_sample)), float(np.max(brake_sample))
                efb_min, efb_max = float(np.min(eff_brake_sample)), float(np.max(eff_brake_sample))
                pel_min, pel_max = float(np.min(p_elec_sample)), float(np.max(p_elec_sample))
                
                spd_b64, spd_scale, spd_off = _quantize_to_int16(spd_sample, spd_min, spd_max)
                rpm_b64, rpm_scale, rpm_off = _quantize_to_int16(rpm_sample, rpm_min, rpm_max)
                cur_b64, cur_scale, cur_off = _quantize_to_int16(cur_sample, cur_min, cur_max)
                brk_b64, brk_scale, brk_off = _quantize_to_int16(brake_sample, brk_min, brk_max)
                efb_b64, efb_scale, efb_off = _quantize_to_int16(eff_brake_sample, efb_min, efb_max)
                pel_b64, pel_scale, pel_off = _quantize_to_int16(p_elec_sample, pel_min, pel_max)
                
                trace = {
                    "v0": float(v0),
                    "spd_q": spd_b64, "spd_s": spd_scale, "spd_o": spd_off,
                    "rpm_q": rpm_b64, "rpm_s": rpm_scale, "rpm_o": rpm_off,
                    "cur_q": cur_b64, "cur_s": cur_scale, "cur_o": cur_off,
                    "brk_q": brk_b64, "brk_s": brk_scale, "brk_o": brk_off,
                    "efb_q": efb_b64, "efb_s": efb_scale, "efb_o": efb_off,
                    "pel_q": pel_b64, "pel_s": pel_scale, "pel_o": pel_off,
                    "eta": _round_list(data["eta"][si][sample_idx], digits=3),
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
    font-size: 1rem;
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
    font-size: 0.97rem;
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
    font-size: 0.9rem;
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
    font-size: 0.76rem;
    color: #52606d;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-weight: 800;
  }
  .score-chip .v {
    display: block;
    font-size: 1rem;
    font-weight: 800;
    color: #102a43;
    margin-top: 4px;
    line-height: 1.4;
  }
  .score-note {
    padding: 0 16px 12px;
    font-size: 0.85rem;
    color: #616e7c;
  }
  .mfrac {
    display: inline-block;
    text-align: center;
    vertical-align: middle;
    line-height: 1.2;
    font-size: 0.88em;
    margin: 0 2px;
  }
  .mfrac > span { display: block; }
  .mfrac > span:first-child { border-bottom: 1px solid #243b53; padding: 0 3px 2px; }
  .mfrac > span:last-child  { padding: 2px 3px 0; }
  .score-formula-table {
    width: 100%;
    border-collapse: collapse;
    margin: 10px 0 4px;
    font-size: 0.88rem;
  }
  .score-formula-table th {
    text-align: left;
    font-size: 0.76rem;
    color: #52606d;
    border-bottom: 2px solid #d9e2ec;
    padding: 3px 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .score-formula-table td {
    padding: 6px 10px;
    vertical-align: middle;
    border-bottom: 1px solid #e5e7eb;
  }
  .score-formula-table td:first-child { white-space: nowrap; }
  .score-formula-table td:nth-child(2) { white-space: nowrap; color: #1f5f8b; font-weight: 700; }
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
  Shared overlays for direct comparison. The lower operating-point map auto-zooms to the active traces.
</p>

<h2>Motor / Generator Efficiency Reference Map</h2>
<div class="note">
  Instantaneous regen efficiency of the BLDC generator through the planetary gear. This is a reference map, not the scoring energy metric.
  The scoring energy dimension is harvested electrical energy divided by demanded braking energy over time.
  <br><br>
  <b>X-axis:</b> motor RPM. The top axis shows equivalent wheel speed with the carrier locked.
  <br>
  <b>Y-axis:</b> carrier torque from the motor. Negative torque is regen braking support.
</div>
<div id="sec-motor-map"></div>

<h2>How Scores Are Calculated</h2>
<div class="note">
  Each strategy is evaluated on <b>10 scenarios</b> (everyday city &amp; road braking, 10&ndash;40 km/h, 0.15&ndash;1.1 m/s²), scored on three independent dimensions (0&ndash;100).
  Emergency scenarios (decel &ge; 1 m/s²) drop the energy weight to 0 and raise tracking to 80&thinsp;%.
  <table class="score-formula-table">
    <thead><tr><th>Dimension</th><th>Normal&thinsp;/&thinsp;Emergency weight</th><th>Formula</th><th>Notes</th></tr></thead>
    <tbody>
    <tr>
      <td><b>Energy</b></td>
      <td>40&thinsp;% &thinsp;/&thinsp; 0&thinsp;%</td>
      <td>S<sub>E</sub> = 100 &sdot; <span class="mfrac"><span>&int; P<sub>elec</sub> dt</span><span>&int; F<sub>brake</sub> &sdot; v&thinsp;dt</span></span></td>
      <td>Harvested electrical energy &divide; demanded braking energy</td>
    </tr>
    <tr>
      <td><b>Tracking</b></td>
      <td>40&thinsp;% &thinsp;/&thinsp; 80&thinsp;%</td>
      <td>S<sub>T</sub> = 100 &sdot; clamp<span style="font-size:0.9em">&thinsp;(1 &minus; <span class="mfrac"><span>&Vert;&tau;<sub>motor</sub> &minus; &tau;<sub>demand</sub>&Vert;<sub>RMS</sub></span><span>&Vert;&tau;<sub>demand</sub>&Vert;<sub>RMS</sub></span></span>,&thinsp;0,&thinsp;1)</span></td>
      <td>Timesteps with v &le; 5&thinsp;km/h are excluded</td>
    </tr>
    <tr>
      <td><b>Smoothness</b></td>
      <td>20&thinsp;% &thinsp;/&thinsp; 20&thinsp;%</td>
      <td>S<sub>J</sub> = 100 &sdot; max<span style="font-size:0.9em">&thinsp;(0,&thinsp;1 &minus; <span class="mfrac"><span>J<sub>RMS</sub></span><span>J<sub>ref</sub></span></span>)&emsp;J<sub>ref</sub> = 20&thinsp;m/s&sup3;</span></td>
      <td>RMS jerk; J = d&sup2;v/dt&sup2;</td>
    </tr>
    <tr>
      <td><b>Composite</b></td>
      <td>&mdash;</td>
      <td>S<sub>C</sub> = w<sub>E</sub>&sdot;S<sub>E</sub> + w<sub>T</sub>&sdot;S<sub>T</sub> + w<sub>J</sub>&sdot;S<sub>J</sub></td>
      <td>Displayed score is the <em>single-case</em> result for the current slider selection</td>
    </tr>
    </tbody>
  </table>
</div>

<h2>Trajectory: Speed Decay &amp; Operating-Point Overlays</h2>
<div class="note">
  The two active strategies are drawn on the same charts so you can compare decay shape, braking aggressiveness, and operating-point path directly.
  The lower map uses the same reference efficiency background but zooms to the displayed trajectories.
  Hover the operating-point map to read off RPM, torque and instantaneous efficiency.
</div>
<div id="sec-trajectory"></div>


<script>
const LABELS = $labels;
const NAMES = $names;
const COLORS = $colors;
const DATA = $traj_payload;
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

const MG = {top: 44, right: 124, bottom: 72, left: 88};
const CBAR_W = 16, CBAR_GAP = 10;

function buildMotorMap() {
  const cont = document.getElementById('sec-motor-map');
  const card = document.createElement('div');
  card.className = 'strat-card';
  const hdr = document.createElement('div');
  hdr.className = 'strat-header';
  hdr.style.color = '#37474F';
  hdr.textContent = 'BLDC Generator - Instantaneous Efficiency (Reference)';
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

    ctx.font = 'bold 17px sans-serif';
    ctx.fillStyle = '#102a43';
    ctx.textAlign = 'center';
    ctx.fillText('Motor Efficiency Reference - η(RPM, Torque)', W / 2, 24);

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
  cont.appendChild(sliderWrap);

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
    value.textContent = 'Energy --  Tracking --  Smooth --  Composite --';
    row.appendChild(value);
    scoreStrip.appendChild(row);
    scoreRows[lab] = value;
  }
  card.appendChild(scoreStrip);

  const scoreNote = document.createElement('div');
  scoreNote.className = 'score-note';
  scoreNote.textContent = 'Selected-case nominal score for the current brake, mass, and initial-speed setting.';
  card.appendChild(scoreNote);

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
  addLegendLine('Static brake threshold', 'rgba(255,60,60,0.85)', true);
  addLegendLine('Kinetic brake threshold', 'rgba(255,140,0,0.85)', true);
  card.appendChild(legend);
  cont.appendChild(card);

  const mmapFull = decodeU8(MMAP_B64, MMAP_ROWS * MMAP_COLS);
  const regenRows = [];
  for (let r = 0; r < MMAP_ROWS; r++) if (MMAP_TQ[r] <= 0) regenRows.push(r);
  const MG_T = {top: 46, right: 28, bottom: 62, left: 82};
  const MG_M = {top: 52, right: 150, bottom: 80, left: 94};

  function baselineSpeed(v0Kmh, brakeNm, massKg, t) {
    const v0 = v0Kmh / 3.6;
    const a = brakeNm / (massKg * TRAJ_R_WHEEL);
    return Math.max(0, v0 - a * t) * 3.6;
  }

  function getSelected() {
    const bi = +brk.inp.value;
    const mi = +mass.inp.value;
    const si = +spd.inp.value;
    const key = bi + '_' + mi;
    const selected = [];
    for (const lab of LABELS) {
      const traces = DATA[lab].traj[key];
      if (!traces || !traces[si]) continue;
      const tr = traces[si];
      const npts = T_TRAJ.length;
      // Dequantize all quantized arrays on load
      tr.spd = dequantize(decodeU16(tr.spd_q, npts), tr.spd_s, tr.spd_o);
      tr.rpm = dequantize(decodeU16(tr.rpm_q, npts), tr.rpm_s, tr.rpm_o);
      tr.cur = dequantize(decodeU16(tr.cur_q, npts), tr.cur_s, tr.cur_o);
      tr.brake_demand = dequantize(decodeU16(tr.brk_q, npts), tr.brk_s, tr.brk_o);
      tr.eff_brake = dequantize(decodeU16(tr.efb_q, npts), tr.efb_s, tr.efb_o);
      tr.p_elec = dequantize(decodeU16(tr.pel_q, npts), tr.pel_s, tr.pel_o);
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
      const y = mg.top + plotH * (1 - baselineSpeed(state.spdKmh, state.brakeNm, state.massKg, T_TRAJ[j]) / maxSpd);
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
    const baseAccel = -(state.brakeNm / (state.massKg * TRAJ_R_WHEEL));
    let minAcc = baseAccel;
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
    const yBase = mg.top + plotH * (1 - (baseAccel - yMin) / yRange);
    ctx.beginPath(); ctx.moveTo(mg.left, yBase); ctx.lineTo(mg.left + plotW, yBase); ctx.stroke();
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
    const H = Math.round(W * 0.34);
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
    ctx.strokeStyle = 'rgba(255,60,60,0.85)';
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(mg.left, yStatic); ctx.lineTo(mg.left + plotW, yStatic); ctx.stroke();
    ctx.restore();
    ctx.font = '12px sans-serif';
    ctx.fillStyle = 'rgba(255,60,60,0.92)';
    ctx.textAlign = 'left';
    ctx.fillText('Static brake ' + state.brakeNm.toFixed(0) + ' Nm', mg.left + 8, Math.max(mg.top + 14, yStatic - 8));
    ctx.save();
    ctx.setLineDash([5, 5]);
    ctx.strokeStyle = 'rgba(255,140,0,0.85)';
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(mg.left, yKinetic); ctx.lineTo(mg.left + plotW, yKinetic); ctx.stroke();
    ctx.restore();
    ctx.fillStyle = 'rgba(255,140,0,0.95)';
    ctx.fillText('Kinetic brake ' + (state.brakeNm * MU_K_OVER_MU_S).toFixed(0) + ' Nm', mg.left + 8, Math.max(mg.top + 30, yKinetic - 8));

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
    ctx.font = '12px sans-serif';
    ctx.fillStyle = '#6b7780';
    const rpmTicksTop = niceTicks(rpmMin, rpmMax, 6);
    for (const rpm of rpmTicksTop) {
      const x = mg.left + ((rpm - rpmMin) / rpmSpan) * plotW;
      const kmh = rpm / TRAJ_GEAR_N * 2 * Math.PI / 60 * TRAJ_R_WHEEL * 3.6;
      ctx.fillText(kmh.toFixed(0) + ' km/h', x, mg.top - 12);
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
    ctx.fillText('Carrier Torque (Nm)', 0, 0);
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
    ctx.fillText('Operating-Point Overlay (Auto-Zoomed)', mg.left + plotW / 2, 26);

    cvs._mapGeom = {left: mg.left, top: mg.top, plotW, plotH, rpmMin: state._rpmMin, rpmSpan: state._rpmSpan, tqMin: state._tqMin, tqSpan: state._tqSpan};
  }

  function updateAll() {
    const state = getSelected();
    brk.val.textContent = state.brakeNm.toFixed(0) + ' Nm';
    mass.val.textContent = state.massKg + ' kg';
    spd.val.textContent = state.spdKmh + ' km/h';
    for (const lab of LABELS) {
      const item = state.selected.find(function(x) { return x.lab === lab; });
      if (!item || !item.tr.score) {
        scoreRows[lab].textContent = 'Energy --  Tracking --  Smooth --  Composite --';
        continue;
      }
      const sc = item.tr.score;
      scoreRows[lab].textContent = 'Energy ' + sc.energy.toFixed(1) + '  Tracking ' + sc.tracking.toFixed(1) + '  Smooth ' + sc.smoothness.toFixed(1) + '  Composite ' + sc.composite.toFixed(1);
    }
    drawSpeed(state);
    drawAccel(state);
    drawJerk(state);
    drawMap(state);
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
