"""Train the neural teacher via Evolution Strategies (ES).

Stage 1 of the distillation pipeline.  Produces a saved ``theta.npz``
parameter file which :mod:`scripts.pysr_distill_teacher` can load for
Stage 2.

Why ES and not backprop
-----------------------
Our JAX physics loop uses ``lax.fori_loop`` (see
``sim/jax/physics_strategy.py``).  That loop is fine for forward
evaluation + ``vmap`` + ``jit``, but **reverse-mode AD through
fori_loop is not supported in JAX without restructuring the loop to
``lax.scan``**, which is a large change to battle-tested sim code.

ES sidesteps this entirely:
  * Only uses the *forward* sim â€” no AD.
  * Naturally handles non-smooth objectives (CVaR-20, clamps).
  * Parallelises trivially across the population axis.
  * One jit compile for the lifetime of training (the MLP's ``theta``
    is a traced input; every new candidate reuses the cached graph).

Tradeoff: ES gradient estimates have higher variance than true grads.
Mitigated with antithetic sampling + rank-based fitness shaping +
Adam momentum.

Fitness design
--------------
Each ES step evaluates a candidate by rolling the JAX sim over
B = rides Ã— perts trajectories.  We convert the batched (capture,
fidelity) array into:

    fitness = mean(composite_B) + cvar_weight * cvar20(composite_B)

Using *mean* as the backbone keeps the gradient estimate dense and
well-conditioned.  Adding a CVaR-20 term (with user-tunable weight,
default 0.5) steers the search toward policies with good worst-case
performance.  Pure CVaR-20 alone is too noisy to optimise with ES at
modest population sizes.

Usage
-----
CPU shakedown (~5 min):
    python scripts/research/train_neural_teacher.py ^
      --rides-per-profile 1 --perts 3 --pop 16 --steps 20

Default CPU run (~45 min):
    python scripts/research/train_neural_teacher.py

WSL / GPU overnight (~2 h):
    wsl bash scripts/wsl_run.sh python scripts/research/train_neural_teacher.py ^
      --rides-per-profile 10 --perts 20 --pop 64 --steps 300
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Ensure the repo root is importable when run as a plain script.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Force UTF-8 stdout/stderr so logging through PowerShell pipelines
# doesn't blow up on the cp1252 charmap.  PYTHONIOENCODING is also
# respected by subprocesses we might spawn later.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# Tell XLA's CPU backend to use every logical core we have.  Default
# is cautious; population vmap over 16 candidates Ã— 12 trajs is
# plenty of work to saturate them.
_n_cpu = os.cpu_count() or 4
os.environ.setdefault(
    "XLA_FLAGS",
    f"--xla_cpu_multi_thread_eigen=true "
    f"--xla_force_host_platform_device_count=1 "
    f"intra_op_parallelism_threads={_n_cpu} "
    f"inter_op_parallelism_threads={_n_cpu}",
)

import numpy as np
import jax
import jax.numpy as jnp

from sim.jax.env import DEFAULT_FLOAT
from sim.jax.physics_strategy import simulate_ride_strategy_jax
from sim.jax.pysr_driver import build_batch
from sim.jax.scoring import score_rides_jax, profile_weighted_composite
from sim.ride_generator import generate_ride_set, PROFILES
from sim.scoring import _sample_perturbations
from sim.scoreboard import append_scoreboard

from scripts.research.neural_teacher import (
    MLPShape, init_theta, policy_k,
)
from scripts.research import neural_teacher_gru as _gru_mod


# =====================================================================
#  Fitness function: theta -> scalar (higher is better)
# =====================================================================

def _cvar20(x: np.ndarray) -> float:
    """CVaR-20 = mean of the worst 20%.  Same convention as
    ``sim.jax.scoring.cvar20``; kept local to avoid the import.
    """
    if x.size == 0:
        return 0.0
    k = max(1, int(np.ceil(0.20 * x.size)))
    return float(np.mean(np.sort(x)[:k]))


def build_sim_fn(rides, perts, seed_base: int = 0xB6B6, *,
                 arch: str = "mlp", jitter_deadband: float = 0.0):
    """Return ``(score_single, score_population, profile_names)``.

    ``score_single(theta)``       -> ``(eff[B], fidelity[B])``
    ``score_population(thetas)``  -> ``(eff[P, B], fidelity[P, B])``

    Both compile once and reuse the cached graph for every subsequent
    call.  ``score_population`` is the one the ES loop should use â€” a
    single JAX dispatch over ``P*B`` trajectories saturates CPU cores
    far better than a Python for-loop over ``score_single``.

    ``arch`` selects the policy family:
        "mlp"  -> stateless 13â†’32â†’32â†’1 MLP (original teacher).
        "gru"  -> stateful GRU(H=32) cell; hidden state carried per tick.
    """
    static, batched, profile_names, n_valid, brake_mask = build_batch(
        rides, perts, seed_base=seed_base)
    static_stripped = {k: v for k, v in static.items() if k != "strategy_fn"}

    # fidelity baseline is the idealized band-brake-at-wheel channel
    # integrated in parallel by the on-sim (logs["speed_baseline"]).
    # No separate motor-off sim needed.
    if arch == "mlp":
        shape = MLPShape()

        def _sim_with_theta(theta, kw):
            def strat(*feats):
                feats_vec = jnp.stack(feats)
                return policy_k(theta, feats_vec, shape)
            return simulate_ride_strategy_jax(
                strategy_fn=strat, **static_stripped, **kw)
    elif arch == "gru":
        gru_shape = _gru_mod.GRUShape()
        h0 = _gru_mod.initial_state(gru_shape)

        def _sim_with_theta(theta, kw):
            # GRU carries its hidden state across ticks via strategy_step_fn.
            step = _gru_mod.make_strategy_step_fn(theta, gru_shape)
            return simulate_ride_strategy_jax(
                strategy_step_fn=step,
                strategy_state0=h0,
                **static_stripped, **kw,
            )
    else:
        raise ValueError(f"build_sim_fn: unknown arch {arch!r}")

    # â”€â”€ Single-theta path (for baseline + monitor-theta evals) â”€â”€â”€â”€â”€â”€
    sim_batched = jax.jit(jax.vmap(_sim_with_theta, in_axes=(None, 0)))

    def _jitter_per_traj(current_BT, brake_mask_BT, n_valid_B):
        """RMS over-deadband |Î”current| per trajectory, in amps.

        The GRU's "chatter" shows up as tick-to-tick jumps in commanded
        current.  Small corrections (below ``jitter_deadband`` amps) are
        free â€” mimicking a real slew-rate tolerance.  Excess is squared
        and averaged so large chatter spikes dominate the penalty.
        Setting ``jitter_deadband=0`` recovers the plain RMS metric.
        """
        di = jnp.diff(current_BT, axis=-1, prepend=current_BT[..., :1])
        # mask both ticks of the pair (mask[t] & mask[t-1]) to avoid
        # punishing the brake-onset step.
        m_prev = jnp.concatenate(
            [brake_mask_BT[..., :1], brake_mask_BT[..., :-1]], axis=-1)
        m = brake_mask_BT & m_prev
        idx = jnp.arange(current_BT.shape[-1])
        m = m & (idx < n_valid_B[..., None])
        excess = jnp.maximum(jnp.abs(di) - jitter_deadband, 0.0)
        sq = jnp.where(m, excess * excess, 0.0)
        count = jnp.maximum(jnp.sum(m.astype(jnp.float32), axis=-1), 1.0)
        return jnp.sqrt(jnp.sum(sq, axis=-1) / count)

    def _score_single(theta):
        logs = sim_batched(theta, batched)
        logs["speed"].block_until_ready()
        eff_B, feel_B, _ = score_rides_jax(
            t=logs["t"], speed_on=logs["speed"],
            p_elec=logs["p_elec"],
            p_copper=logs["p_copper"],
            p_brake=logs["p_brake"],
            brake_demand=logs["brake_demand"],
            speed_base=logs["speed_baseline"],
            brake_mask=brake_mask, n_valid=n_valid,
        )
        jitter_B = _jitter_per_traj(logs["current"], brake_mask, n_valid)
        return np.asarray(eff_B), np.asarray(feel_B), np.asarray(jitter_B)

    # â”€â”€ Population path: one big vmap dispatch â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    #
    # Outer vmap over theta-axis (P candidates), inner over batch axis
    # (B trajectories).  Produces logs with leading shape [P, B, T].
    # We then flatten to [P*B, T], score with the existing B-vmapped
    # scorer, and reshape the per-traj metrics back to [P, B].
    def _sim_pop_one_theta(theta):
        return jax.vmap(_sim_with_theta, in_axes=(None, 0))(theta, batched)

    sim_pop = jax.jit(jax.vmap(_sim_pop_one_theta))

    # Pre-tile the auxiliary scoring tensors across the population axis
    # at score time (cheap: just [P*B] int/bool vectors).
    def _score_population(thetas_PxN):
        P = thetas_PxN.shape[0]
        logs = sim_pop(thetas_PxN)
        logs["speed"].block_until_ready()

        # Flatten [P, B, T] -> [P*B, T] for the batched scorer.
        def _flat(x):
            return x.reshape((-1,) + x.shape[2:])

        flat = {k: _flat(v) for k, v in logs.items()}
        # Tile scoring tensors PÃ— along batch axis.
        n_valid_PB = jnp.tile(n_valid, P)
        brake_PB = jnp.tile(brake_mask, (P, 1))

        eff_PB, feel_PB, _ = score_rides_jax(
            t=flat["t"], speed_on=flat["speed"],
            p_elec=flat["p_elec"],
            p_copper=flat["p_copper"],
            p_brake=flat["p_brake"],
            brake_demand=flat["brake_demand"],
            speed_base=flat["speed_baseline"],
            brake_mask=brake_PB, n_valid=n_valid_PB,
        )
        jitter_PB = _jitter_per_traj(flat["current"], brake_PB, n_valid_PB)
        B = brake_mask.shape[0]
        eff_np = np.asarray(eff_PB).reshape(P, B)
        feel_np = np.asarray(feel_PB).reshape(P, B)
        jitter_np = np.asarray(jitter_PB).reshape(P, B)
        return eff_np, feel_np, jitter_np

    return _score_single, _score_population, profile_names


def composites_by_pert(eff_B, feel_B, profile_names, n_pert: int) -> np.ndarray:
    """Aggregate per-traj (eff, fidelity) â†’ one composite per perturbation.

    We need this because "across perturbations" is the axis CVaR-20
    lives on â€” ride-set diversity is baked into the profile-weighted
    composite.
    """
    n_rides = len(profile_names) // n_pert
    weights = {n: PROFILES[n].weight for n in PROFILES}
    out = np.zeros(n_pert, dtype=np.float64)
    for pi in range(n_pert):
        idxs = [ri * n_pert + pi for ri in range(n_rides)]
        prof_list = [profile_names[i] for i in idxs]
        _, _, c_w = profile_weighted_composite(
            eff_B[idxs], feel_B[idxs], prof_list, weights)
        out[pi] = c_w
    return out


# =====================================================================
#  Evolution Strategies outer loop
# =====================================================================

def rank_transform(f: np.ndarray) -> np.ndarray:
    """Rank-based centered linear transform.

    Returns values in roughly [-0.5, 0.5] where the best candidate
    gets +0.5.  Standard in modern ES (Wierstra 2014, Salimans 2017).
    """
    ranks = np.empty_like(f, dtype=np.float64)
    order = np.argsort(f)           # ascending
    ranks[order] = np.arange(len(f))
    return ranks / max(1, len(f) - 1) - 0.5


def es_train(
    *,
    rides_per_profile: int,
    n_perts: int,
    pop: int,
    steps: int,
    sigma: float,
    lr: float,
    cvar_weight: float,
    mean_weight: float,
    jitter_weight: float,
    jitter_deadband: float,
    jitter_warmup_steps: int,
    jitter_ramp_steps: int,
    seed: int,
    eval_seed_base: int,
    output_path: Path,
    resample_every: int,
    arch: str = "mlp",
):
    """Run ES on the neural teacher.  Saves ``theta.npz`` on exit."""
    if arch == "mlp":
        shape = MLPShape()
        theta = np.asarray(init_theta(shape, seed=seed))
    elif arch == "gru":
        shape = _gru_mod.GRUShape()
        theta = np.asarray(_gru_mod.init_theta(shape, seed=seed))
    else:
        raise ValueError(f"es_train: unknown arch {arch!r}")
    n_params = shape.n_params

    # Adam state
    m = np.zeros_like(theta)
    v = np.zeros_like(theta)
    beta1, beta2, eps = 0.9, 0.999, 1e-8

    rng = np.random.default_rng(seed)

    # Build the fixture.  We can optionally resample rides/perts every
    # ``resample_every`` steps to regularise against overfit.
    def _make_fixture(rng_ride):
        rides = generate_ride_set(
            seeds_per_profile=rides_per_profile,
            base_seed=int(rng_ride.integers(0, 2**31 - 1)),
        )
        perts = _sample_perturbations(rng_ride, n_perts)
        return rides, perts

    rides, perts = _make_fixture(rng)
    score_single, score_population, profile_names = build_sim_fn(
        rides, perts, seed_base=eval_seed_base, arch=arch,
        jitter_deadband=jitter_deadband)

    # Fixed held-out fixture for deterministic checkpoint selection.
    # Separate RNG stream + seed_base so there is no overlap with the
    # (possibly resampling) training fixture.  This means ``best_fit``
    # is always comparable across steps, regardless of --resample-every.
    eval_rng = np.random.default_rng(seed ^ 0xA5A5A5)
    eval_rides, eval_perts = _make_fixture(eval_rng)
    score_eval_single, _, eval_profile_names = build_sim_fn(
        eval_rides, eval_perts, seed_base=eval_seed_base + 0x10_000, arch=arch,
        jitter_deadband=jitter_deadband)

    def _theta_fitness(theta_arr) -> float:
        eff_B, feel_B, jitter_B = score_single(
            jnp.asarray(theta_arr, dtype=DEFAULT_FLOAT))
        comps = composites_by_pert(eff_B, feel_B, profile_names, len(perts))
        return (mean_weight * float(np.mean(comps))
                + cvar_weight * _cvar20(comps)
                - jitter_weight * float(np.mean(jitter_B)))

    def _population_fitness(thetas_PxN) -> np.ndarray:
        """Score P candidates in one jit dispatch -> float array [P]."""
        eff_PB, feel_PB, jitter_PB = score_population(
            jnp.asarray(thetas_PxN, dtype=DEFAULT_FLOAT))
        out = np.zeros(eff_PB.shape[0], dtype=np.float64)
        for p in range(eff_PB.shape[0]):
            comps = composites_by_pert(
                eff_PB[p], feel_PB[p], profile_names, len(perts))
            out[p] = (mean_weight * np.mean(comps)
                      + cvar_weight * _cvar20(comps)
                      - jitter_weight * float(np.mean(jitter_PB[p])))
        return out

    def _eval_fitness(theta_arr) -> float:
        """Score theta on the FIXED held-out fixture (never resampled)."""
        eff_B, feel_B, jitter_B = score_eval_single(
            jnp.asarray(theta_arr, dtype=DEFAULT_FLOAT))
        comps = composites_by_pert(
            eff_B, feel_B, eval_profile_names, len(eval_perts))
        return (mean_weight * float(np.mean(comps))
                + cvar_weight * _cvar20(comps)
                - jitter_weight * float(np.mean(jitter_B)))

    def _eval_stats(theta_arr) -> tuple[float, float, float]:
        """Return (composite_mean, cvar20, mean_jitter_A) on held-out.

        Independent of the fitness weights so the scoreboard always
        records comparable raw metrics.
        """
        eff_B, feel_B, jitter_B = score_eval_single(
            jnp.asarray(theta_arr, dtype=DEFAULT_FLOAT))
        comps = composites_by_pert(
            eff_B, feel_B, eval_profile_names, len(eval_perts))
        return (float(np.mean(comps)), float(_cvar20(comps)),
                float(np.mean(jitter_B)))

    best_fit = -np.inf
    best_theta = theta.copy()

    t_start = time.time()
    jitter_weight_max = float(jitter_weight)
    # Curriculum: keep jitter_weight=0 for the first ``jitter_warmup_steps``
    # steps so the policy finds useful (possibly wiggly) control behavior,
    # then linearly ramp to ``jitter_weight_max`` over ``jitter_ramp_steps``.
    # Reassigning the ``jitter_weight`` name updates all closures since
    # they read it from this enclosing scope.
    jitter_weight = 0.0 if jitter_warmup_steps > 0 else jitter_weight_max
    print(f"[ES] n_params={n_params}  pop={pop}  steps={steps}  "
          f"sigma={sigma}  lr={lr}  "
          f"mean_w={mean_weight}  cvar_w={cvar_weight}  "
          f"jitter_w_max={jitter_weight_max}  "
          f"jitter_deadband={jitter_deadband}A  "
          f"jitter_warmup={jitter_warmup_steps}  "
          f"jitter_ramp={jitter_ramp_steps}  "
          f"rides_per_profile={rides_per_profile}  perts={n_perts}")
    print(f"[ES] fixture: {len(rides)} rides x {len(perts)} perts "
          f"= {len(rides) * len(perts)} trajectories")
    print(f"[ES] eval fixture (held-out): "
          f"{len(eval_rides)} rides x {len(eval_perts)} perts "
          f"= {len(eval_rides) * len(eval_perts)} trajectories")

    # Warm compile (both paths)
    t0 = time.time()
    base_fit = _theta_fitness(theta)
    print(f"[ES] compile + baseline fitness: {time.time() - t0:.1f} s  "
          f"baseline = {base_fit:.3f}")
    # Warm up the population path too (first call compiles).
    t0 = time.time()
    _ = _population_fitness(np.tile(theta[None, :], (pop, 1)))
    print(f"[ES] population-path compile: {time.time() - t0:.1f} s")
    # Warm up the eval path + seed the best-so-far on the held-out set.
    t0 = time.time()
    base_eval = _eval_fitness(theta)
    best_fit = base_eval
    best_theta = theta.copy()
    print(f"[ES] eval-path compile: {time.time() - t0:.1f} s  "
          f"baseline_eval = {base_eval:.3f}")

    last_step_completed = 0
    try:
        for step in range(1, steps + 1):
            # Curriculum-anneal jitter penalty: 0 during warmup, then
            # linearly ramp to max over ``jitter_ramp_steps``.
            if step <= jitter_warmup_steps:
                jitter_weight = 0.0
            elif jitter_ramp_steps > 0:
                frac = min(1.0,
                           (step - jitter_warmup_steps) / jitter_ramp_steps)
                jitter_weight = jitter_weight_max * frac
            else:
                jitter_weight = jitter_weight_max

            # Antithetic noise: half the population, then mirror.
            half = pop // 2
            eps_half = rng.standard_normal((half, n_params)).astype(np.float32)
            eps_full = np.concatenate([eps_half, -eps_half], axis=0)

            # One big vmapped dispatch: P*B trajectories in a single
            # jit call.  MUCH better core utilisation than the per-theta
            # Python loop.
            t_pop = time.time()
            cand = theta[None, :] + sigma * eps_full      # [P, n_params]
            fits = _population_fitness(cand)
            t_pop = time.time() - t_pop

            # Rank transform -> gradient estimate
            u = rank_transform(fits)
            grad = (eps_full.T @ u) / (pop * sigma)

            # Adam update (gradient ascent -> sign as-is since u is
            # already "higher-fitness = larger").
            m = beta1 * m + (1 - beta1) * grad
            v = beta2 * v + (1 - beta2) * (grad * grad)
            m_hat = m / (1 - beta1 ** step)
            v_hat = v / (1 - beta2 ** step)
            theta = theta + lr * m_hat / (np.sqrt(v_hat) + eps)

            # Log metrics.  Selection uses the FIXED held-out fixture so
            # ``best_fit`` is comparable across fixture resamples.  The
            # training-fixture fitness ``train_fit`` is shown for context.
            train_fit = _theta_fitness(theta)
            eval_fit = _eval_fitness(theta)
            if eval_fit > best_fit:
                best_fit = eval_fit
                best_theta = theta.copy()

            elapsed = time.time() - t_start
            print(f"[ES] step {step:3d}/{steps}  "
                  f"pop_fit min/mean/max = {fits.min():.2f} / "
                  f"{fits.mean():.2f} / {fits.max():.2f}  "
                  f"train_fit = {train_fit:.2f}  "
                  f"eval_fit = {eval_fit:.2f}  best_eval = {best_fit:.2f}  "
                  f"Î» = {jitter_weight:.3f}  "
                  f"t_pop = {t_pop:.1f}s  elapsed = {elapsed/60:.1f}m",
                  flush=True)
            last_step_completed = step

            # Optional fixture resample for regularisation
            if resample_every > 0 and step % resample_every == 0:
                rides, perts = _make_fixture(rng)
                score_single, score_population, profile_names = build_sim_fn(
                    rides, perts, seed_base=eval_seed_base + step, arch=arch,
                    jitter_deadband=jitter_deadband)

                # Rebuild the closures so they see the new fixture's tensors.
                def _theta_fitness(theta_arr, _ss=score_single,
                                   _pn=profile_names, _np=len(perts)):
                    eff_B, feel_B, jitter_B = _ss(
                        jnp.asarray(theta_arr, dtype=DEFAULT_FLOAT))
                    comps = composites_by_pert(eff_B, feel_B, _pn, _np)
                    return (mean_weight * float(np.mean(comps))
                            + cvar_weight * _cvar20(comps)
                            - jitter_weight * float(np.mean(jitter_B)))

                def _population_fitness(thetas_PxN, _sp=score_population,
                                        _pn=profile_names, _np=len(perts)):
                    eff_PB, feel_PB, jitter_PB = _sp(
                        jnp.asarray(thetas_PxN, dtype=DEFAULT_FLOAT))
                    out = np.zeros(eff_PB.shape[0], dtype=np.float64)
                    for p in range(eff_PB.shape[0]):
                        comps = composites_by_pert(
                            eff_PB[p], feel_PB[p], _pn, _np)
                        out[p] = (mean_weight * np.mean(comps)
                                  + cvar_weight * _cvar20(comps)
                                  - jitter_weight * float(np.mean(jitter_PB[p])))
                    return out

                print(f"[ES] resampled fixture @ step {step}", flush=True)
    except KeyboardInterrupt:
        print(f"\n[ES] KeyboardInterrupt at step {last_step_completed}; "
              f"saving best-so-far theta...", flush=True)
    finally:
        # Compute raw mean/cvar on best_theta so the scoreboard row is
        # comparable across runs regardless of --mean-weight/--cvar-weight.
        try:
            best_mean, best_cvar, best_jitter = _eval_stats(best_theta)
        except Exception as e:  # sim shapes may be stale on interrupt
            print(f"[ES] warning: could not re-score best_theta: {e}")
            best_mean, best_cvar, best_jitter = (float("nan"),) * 3

        output_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(output_path,
                 theta=best_theta,
                 theta_final=theta,
                 best_fit=best_fit,
                 best_mean=best_mean,
                 best_cvar20=best_cvar,
                 best_jitter_A=best_jitter,
                 n_params=n_params,
                 steps_completed=last_step_completed)
        print(f"[ES] saved best theta (fit={best_fit:.2f}  "
              f"mean={best_mean:.2f}  cvar20={best_cvar:.2f}  "
              f"jitter={best_jitter:.2f}A  "
              f"steps={last_step_completed}/{steps}) -> {output_path}")

        fixture_tag = (
            f"heldout_{rides_per_profile}x{n_perts}"
            f"_B{len(eval_rides) * len(eval_perts)}"
        )
        notes = (
            f"pop={pop} sigma={sigma} lr={lr} "
            f"mean_w={mean_weight} cvar_w={cvar_weight} "
            f"jitter_w_max={jitter_weight_max} "
            f"jitter_deadband={jitter_deadband}A "
            f"jitter_warmup={jitter_warmup_steps} "
            f"jitter_ramp={jitter_ramp_steps} "
            f"jitter_A={best_jitter:.2f} "
            f"steps={last_step_completed}/{steps}"
        )
        append_scoreboard(
            source="neural_teacher",
            run_id=output_path.stem,
            cvar20=best_cvar,
            composite_mean=best_mean,
            n_features=13,
            fixture=fixture_tag,
            notes=notes,
            artifact=str(output_path),
        )


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rides-per-profile", type=int, default=3)
    p.add_argument("--perts", type=int, default=8,
                   help="Number of perturbations (includes the nominal).")
    p.add_argument("--pop", type=int, default=32,
                   help="Population size (must be even for antithetic).")
    p.add_argument("--steps", type=int, default=100)
    p.add_argument("--sigma", type=float, default=0.05,
                   help="ES perturbation std.")
    p.add_argument("--lr", type=float, default=0.02,
                   help="Adam learning rate on the ES gradient.")
    p.add_argument("--cvar-weight", type=float, default=0.5,
                   help="Weight on CVaR-20 inside the fitness function. "
                        "0 = pure mean composite; 1 â‰ˆ equal weight.")
    p.add_argument("--mean-weight", type=float, default=1.0,
                   help="Weight on mean composite in the fitness. "
                        "Set 0 with --cvar-weight 1 for pure CVaR-20.")
    p.add_argument("--jitter-weight", type=float, default=0.0,
                   help="Fitness penalty per RMS-amp of tick-to-tick "
                        "command jitter on brake windows. 0 disables. "
                        "0.3â€“0.5 keeps the GRU smooth without "
                        "killing responsiveness.")
    p.add_argument("--jitter-deadband", type=float, default=0.0,
                   help="Free |Î”current| threshold in amps â€” corrections "
                        "below this are not penalised. Only the excess "
                        "above the deadband contributes to the RMS. "
                        "0 = pure RMS (old behavior); 0.15 A is a "
                        "reasonable 'no-chatter' tolerance.")
    p.add_argument("--jitter-warmup-steps", type=int, default=0,
                   help="Curriculum: hold jitter_weight=0 for this many "
                        "steps so the policy learns useful control "
                        "behavior before being penalised for chatter. "
                        "Recommend ~40%% of --steps.")
    p.add_argument("--jitter-ramp-steps", type=int, default=0,
                   help="Curriculum: linearly ramp jitter_weight from 0 "
                        "to --jitter-weight over this many steps after "
                        "the warmup ends. 0 = step change. Recommend "
                        "~30%% of --steps.")
    p.add_argument("--seed", type=int, default=0,
                   help="Seed for theta init and ES noise.")
    p.add_argument("--eval-seed-base", type=int, default=0xB6B6,
                   help="Noise seed base for the sim trajectories.")
    p.add_argument("--resample-every", type=int, default=0,
                   help="Resample fixture every N steps (0 = never).")
    p.add_argument("--arch", choices=("mlp", "gru"), default="mlp",
                   help="Policy architecture: 'mlp' (stateless, default) "
                        "or 'gru' (stateful, 13â†’GRU(32)â†’1).")
    p.add_argument("--output", type=Path,
                   default=Path("sim/output/neural_teacher/theta.npz"))
    args = p.parse_args()
    if args.pop % 2 != 0:
        raise ValueError("--pop must be even (antithetic sampling)")
    return args


if __name__ == "__main__":
    args = parse_args()
    es_train(
        rides_per_profile=args.rides_per_profile,
        n_perts=args.perts,
        pop=args.pop,
        steps=args.steps,
        sigma=args.sigma,
        lr=args.lr,
        cvar_weight=args.cvar_weight,
        mean_weight=args.mean_weight,
        jitter_weight=args.jitter_weight,
        jitter_deadband=args.jitter_deadband,
        jitter_warmup_steps=args.jitter_warmup_steps,
        jitter_ramp_steps=args.jitter_ramp_steps,
        seed=args.seed,
        eval_seed_base=args.eval_seed_base,
        output_path=args.output,
        resample_every=args.resample_every,
        arch=args.arch,
    )
