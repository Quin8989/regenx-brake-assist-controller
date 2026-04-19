"""sim — RegenX regen brake simulation suite.

Adds firmware/ to sys.path so sim tools can import config, core, etc.

Modules:
    physics           Physical constants, Numba-JIT inner loop, simulation engine.
    strategy_context  StrategyContext — hardware-enforced signal contract.
    strategies        Control strategy classes (pi_controller, aimd_ff).
    scoring           Scoring framework + Monte Carlo robustness analysis.
    plotting          Plotting functions and HTML gallery generation.

Runners:
    run_tune     Tune strategies via DE.  python -m sim.run_tune --strategies pi_controller
    run_gallery  Interactive HTML gallery. python -m sim.run_gallery --strategies aimd_ff
"""
import sys as _sys
import pathlib as _pathlib
_fw = str(_pathlib.Path(__file__).parent.parent / "firmware")
if _fw not in _sys.path:
    _sys.path.insert(0, _fw)
del _sys, _pathlib, _fw
