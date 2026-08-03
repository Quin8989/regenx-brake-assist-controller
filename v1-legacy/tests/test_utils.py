# tests/test_utils.py — clamp, linear_map

import pytest
from utils import clamp, linear_map


@pytest.mark.parametrize("val,lo,hi,expected", [
    (5, 0, 10, 5),
    (-1, 0, 10, 0),
    (15, 0, 10, 10),
    (0, 0, 10, 0),
    (10, 0, 10, 10),
    (0.5, 0.0, 1.0, 0.5),
    (-5, -10, -1, -5),
    (-15, -10, -1, -10),
])
def test_clamp(val, lo, hi, expected):
    assert clamp(val, lo, hi) == expected


@pytest.mark.parametrize("val,in_lo,in_hi,out_lo,out_hi,expected", [
    (5, 0, 10, 0, 100, 50.0),
    (0, 0, 10, 100, 200, 100.0),
    (10, 0, 10, 100, 200, 200.0),
    (5, 0, 10, 100, 0, 50.0),
    (5, 5, 5, 100, 200, 100.0),
    (15, 0, 10, 0, 100, 150.0),
])
def test_linear_map(val, in_lo, in_hi, out_lo, out_hi, expected):
    assert linear_map(val, in_lo, in_hi, out_lo, out_hi) == expected

