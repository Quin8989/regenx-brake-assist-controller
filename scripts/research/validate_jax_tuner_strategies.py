"""Parity test: numpy vs JAX composite score for the 3 tunable regen
strategies.

The ride simulator has small known architectural differences between
backends (post-iq_kp clip timing, FOC step ordering, taper re-compute
after noise path) — validated in scripts/research/validate_jax_ride
strategy.py within absolute channel tolerances tuned for PySR-sized
currents (k≈0.1).  For tuner-sized currents (k≈0.2–0.3), those abs
tols would be tight but the *relative* error is the same.

The tuner's Optuna objective consumes the composite
``0.40·efficiency + 0.60·feel``.  We compare that final score between
backends within a few points.

Tolerance budget: 3.0 composite points (same scale as
scripts/research/validate_jax_cvar20.py's CVaR-20 test).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import jax
import jax.numpy as jnp
import numpy as np

jax.config.update("jax_enable_x64", True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "firmware"))

from sim.physics import CTRL_PERIOD, DT
from sim.ride_generator import generate_ride, PROFILES
from sim.scoring import score_rides
from sim.jax.physics_strategy import simulate_ride_strategy_jax
from sim.jax.tuner_strategies import build_step_fn
from sim.jax.scoring import score_rides_jax

from scripts.research.validate_jax_ride_strategy import build_jax_kwargs

# Silence numpy telemetry noise.
import sim.physics as _sp
_orig_init_state = _sp._init_state


def _init_state_nonoise(*a, **kw):
    s = _orig_init_state(*a, **kw)
    s["noise_rng"] = None
    return s


_sp._init_state = _init_state_nonoise


from regen.strategies import (
    FixedFfRegenStrategy, PiSlipRegenStrategy, AimdFfRegenStrategy,
)

CASES = [
    ("fixed_ff",
     FixedFfRegenStrategy, dict(k=0.25)),
    ("pi_controller",
     PiSlipRegenStrategy, dict(k_ff=0.30, ki=0.40, alpha=0.50)),
    ("aimd_ff",
     AimdFfRegenStrategy, dict(k=0.15, beta_md=0.08,
                                unlock_thresh=1500.0, k_ai=0.05)),
]

COMPOSITE_TOL = 3.0   # points


def np_composite(np_cls, params, rides):
    factory = lambda: np_cls(**params)  # noqa: E731
    result = score_rides(
        factory, rides,
        sim_kwargs=dict(rpm_noise_sigma=0.0, iq_noise_sigma=0.0,
                        iq_bias=0.0, vcap_noise_sigma=0.0),
    )
    return result.composite, result.efficiency, result.feel


def _noop(*_):
    return jnp.asarray(0.0)


def jax_composite(strategy_key, params, rides, jit_fn):
    ctrl_steps = max(1, int(CTRL_PERIOD / DT))
    n_ticks_padded = max(r.n // ctrl_steps for r in rides)

    step_fn, state0, iq_kp = build_step_fn(
        strategy_key, params, dt_ctrl=CTRL_PERIOD)

    per_ride_kwargs = []
    n_valid = []
    brake_masks = []
    for r in rides:
        kw, n_ride = build_jax_kwargs(r, n_ticks_padded=n_ticks_padded,
                                       strategy_fn=_noop)
        # Strip stateless placeholder; we're using stateful path.
        kw.pop("strategy_fn", None)
        kw["strategy_fn"] = None
        kw["strategy_step_fn"] = step_fn
        kw["strategy_state0"] = state0
        kw["iq_kp"] = iq_kp
        per_ride_kwargs.append(kw)
        n_valid.append(n_ride)
        brake_masks.append(np.asarray(kw["brake_ticks"]) > 0.0)

    # Motor-off baseline: zero-k fixed_ff.
    off_step, off_s0, _ = build_step_fn(
        "fixed_ff", dict(k=0.0), dt_ctrl=CTRL_PERIOD)
    off_kw_list = []
    for kw in per_ride_kwargs:
        okw = dict(kw)
        okw["strategy_step_fn"] = off_step
        okw["strategy_state0"] = off_s0
        okw["iq_kp"] = 0.0
        off_kw_list.append(okw)

    on_logs_list = [jit_fn(**kw) for kw in per_ride_kwargs]
    off_logs_list = [jit_fn(**kw) for kw in off_kw_list]

    def _stack(key, lst):
        return jnp.stack([l[key] for l in lst])

    t = _stack("t", on_logs_list)
    speed_on = _stack("speed", on_logs_list)
    speed_base = _stack("speed_baseline", on_logs_list)
    p_elec = _stack("p_elec", on_logs_list)
    p_copper = _stack("p_copper", on_logs_list)
    p_brake = _stack("p_brake", on_logs_list)
    brake_demand = _stack("brake_demand", on_logs_list)
    brake_mask = jnp.asarray(np.stack(brake_masks, axis=0), dtype=jnp.bool_)
    n_valid_j = jnp.asarray(n_valid, dtype=jnp.int32)

    eff_B, feel_B, comp_B = score_rides_jax(
        t=t, speed_on=speed_on, speed_base=speed_base,
        p_elec=p_elec, p_copper=p_copper, p_brake=p_brake,
        brake_demand=brake_demand,
        brake_mask=brake_mask, n_valid=n_valid_j,
    )
    return (float(np.mean(np.asarray(comp_B))),
            float(np.mean(np.asarray(eff_B))),
            float(np.mean(np.asarray(feel_B))))


def main():
    prof_list = list(PROFILES.items())
    rides = [generate_ride(prof, seed=100 * i + 1, duration=60.0)
             for i, (_n, prof) in enumerate(prof_list)]

    jit_fn = jax.jit(
        simulate_ride_strategy_jax,
        static_argnames=("strategy_fn", "strategy_step_fn",
                         "delay_slots", "n_ticks", "ctrl_steps", "dt"),
    )

    n_pass = 0
    print(f"{'strategy':<16} {'np_comp':>8} {'jx_comp':>8} "
          f"{'Δ':>6}  {'np_eff':>7} {'jx_eff':>7}  "
          f"{'np_feel':>8} {'jx_feel':>8}  result")
    print("─" * 95)

    for strategy_key, np_cls, params in CASES:
        t0 = time.perf_counter()
        comp_np, eff_np, feel_np = np_composite(np_cls, params, rides)
        t_np = time.perf_counter() - t0

        t0 = time.perf_counter()
        comp_jx, eff_jx, feel_jx = jax_composite(
            strategy_key, params, rides, jit_fn)
        t_jx = time.perf_counter() - t0

        diff = abs(comp_np - comp_jx)
        ok = diff <= COMPOSITE_TOL
        flag = "PASS" if ok else "FAIL"
        print(f"{strategy_key:<16} {comp_np:8.2f} {comp_jx:8.2f} "
              f"{diff:6.2f}  {eff_np:7.2f} {eff_jx:7.2f}  "
              f"{feel_np:8.2f} {feel_jx:8.2f}  [{flag}]  "
              f"(np={t_np*1e3:.0f}ms jx={t_jx*1e3:.0f}ms)")
        if ok:
            n_pass += 1

    print(f"\n{n_pass}/{len(CASES)} strategies within {COMPOSITE_TOL} pts")
    return 0 if n_pass == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
