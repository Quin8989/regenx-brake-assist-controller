"""Fast smoke test: can Julia call a Python function and convert its
return value to Float64 the way our PySR loss snippet needs?

This isolates the Python-Julia callback pattern from the (slow)
PySR/SymbolicRegression compile cascade so we can iterate on syntax
without paying minutes per attempt.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", _SCRIPT_DIR)]


def py_scorer(expr: str) -> float:
    print(f"  [py] received: {expr!r}")
    return 3.14 if expr == "k_prev" else 99.9


def main() -> int:
    print("importing juliacall...")
    from juliacall import Main as jl
    print("  ok")

    # Put the Python callable in Julia global scope.
    jl.regenx_score = py_scorer

    # Attempt 1 — unqualified pyconvert (likely to fail if PythonCall
    # isn't `using`'d into Main).
    print("\nAttempt 1: unqualified pyconvert")
    try:
        jl.seval("""
        function test_a(s::String)
            loss_py = Main.regenx_score(s)
            return pyconvert(Float64, loss_py)
        end
        """)
        r = jl.test_a("k_prev")
        print(f"  returned: {r!r}  type={type(r).__name__}")
    except Exception as exc:
        print(f"  FAIL: {exc!r}")

    # Attempt 2 — fully qualified PythonCall.pyconvert.
    print("\nAttempt 2: PythonCall.pyconvert")
    try:
        jl.seval("""
        function test_b(s::String)
            loss_py = Main.regenx_score(s)
            return PythonCall.pyconvert(Float64, loss_py)
        end
        """)
        r = jl.test_b("k_prev")
        print(f"  returned: {r!r}  type={type(r).__name__}")
    except Exception as exc:
        print(f"  FAIL: {exc!r}")

    # Attempt 3 — explicit `using PythonCall` then pyconvert.
    print("\nAttempt 3: `using PythonCall`, pyconvert")
    try:
        jl.seval("""
        using PythonCall
        function test_c(s::String)
            loss_py = Main.regenx_score(s)
            return pyconvert(Float64, loss_py)
        end
        """)
        r = jl.test_c("k_prev")
        print(f"  returned: {r!r}  type={type(r).__name__}")
    except Exception as exc:
        print(f"  FAIL: {exc!r}")

    # Attempt 4 — return a Julia Float64 directly via pyconvert at
    # call time, so the loss body is trivial.
    print("\nAttempt 4: have Python return float; Julia Float64(...)")
    try:
        jl.seval("""
        function test_d(s::String)
            loss_py = Main.regenx_score(s)
            return Float64(loss_py)
        end
        """)
        r = jl.test_d("k_prev")
        print(f"  returned: {r!r}  type={type(r).__name__}")
    except Exception as exc:
        print(f"  FAIL: {exc!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
