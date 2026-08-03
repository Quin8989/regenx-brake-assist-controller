"""Population-level JAX scorer for PySR / evolutionary loops.

A PySR generation evaluates hundreds of candidate expressions against
the same ride/perturbation fixture.  This module wraps
:class:`sim.jax.pysr_driver.CandidateEvaluator` with a batch API:

    evaluator = PopulationEvaluator(rides, perts)
    scores    = evaluator.evaluate_population(expressions)

Each call compiles once per *new* expression (cached thereafter), then
launches one jit(vmap(simulate)) over all B = len(rides) * len(perts)
trajectories.  Sequential per-candidate launches let us avoid having
to fuse heterogeneous symbolic trees into one XLA graph — this is the
right trade for PySR where generations introduce 50–200 genuinely new
structures per round.

Batch-width throughput (B) is the dial that decides whether GPU beats
CPU: at B=140 the RTX 3070 underfills; at B ≳ 2000 it pulls ahead.
Increase rides × perts to widen B.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from sim.jax.pysr_driver import CandidateEvaluator


@dataclass
class PopulationTiming:
    """Per-generation throughput profile."""
    n_candidates: int
    n_new_compiled: int
    n_cache_hits: int
    t_compile_s: float = 0.0     # includes first-call XLA compile
    t_hot_s: float = 0.0         # sum of hot eval wall-clock
    t_score_s: float = 0.0       # python-side scoring / reshape
    best_cvar20: float = float("-inf")
    best_expression: str | None = None
    all_cvar20: list[float] = field(default_factory=list)

    @property
    def ms_per_candidate(self) -> float:
        total = self.t_compile_s + self.t_hot_s + self.t_score_s
        return 1000.0 * total / max(1, self.n_candidates)

    def summary(self) -> str:
        total = self.t_compile_s + self.t_hot_s + self.t_score_s
        return (
            f"{self.n_candidates} candidates  "
            f"({self.n_new_compiled} compiled, "
            f"{self.n_cache_hits} cached)\n"
            f"  compile: {self.t_compile_s*1000:7.0f} ms  "
            f"hot: {self.t_hot_s*1000:7.0f} ms  "
            f"score: {self.t_score_s*1000:6.0f} ms\n"
            f"  total:   {total*1000:7.0f} ms  "
            f"({self.ms_per_candidate:.1f} ms/candidate)\n"
            f"  best cvar20: {self.best_cvar20:.2f}  "
            f"expr: {self.best_expression!r}"
        )


class PopulationEvaluator(CandidateEvaluator):
    """CandidateEvaluator extended with a vectorised population API."""

    def evaluate_population(
        self,
        expressions: Sequence[str],
    ) -> tuple[list[dict], PopulationTiming]:
        """Score every expression; return per-candidate results + timing.

        All launches share the same pre-built batched kwargs, so the
        host→device transfer cost is paid once per instance lifetime.
        """
        results: list[dict] = []
        timing = PopulationTiming(
            n_candidates=len(expressions),
            n_new_compiled=0,
            n_cache_hits=0,
        )

        for expr in expressions:
            was_cached = expr in self._cache
            t0 = time.perf_counter()
            res = self.evaluate(expr)
            t = time.perf_counter() - t0

            if was_cached:
                timing.n_cache_hits += 1
                timing.t_hot_s += res["t_sim_s"]
                timing.t_score_s += res["t_score_s"]
            else:
                timing.n_new_compiled += 1
                # First call mixes compile + exec; attribute the delta
                # above the sim-only timing to "compile".
                timing.t_compile_s += max(0.0, t - res["t_sim_s"]
                                          - res["t_score_s"])
                timing.t_hot_s += res["t_sim_s"]
                timing.t_score_s += res["t_score_s"]

            results.append(res)
            if res["cvar20"] > timing.best_cvar20:
                timing.best_cvar20 = float(res["cvar20"])
                timing.best_expression = expr
            timing.all_cvar20.append(float(res["cvar20"]))

        return results, timing

    @property
    def batch_width(self) -> int:
        """Number of trajectories per jit call.  Widen this to feed GPUs."""
        return self._B
