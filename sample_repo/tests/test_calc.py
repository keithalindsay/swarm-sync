"""Covers calc.py -- the merge gate for money-shot #1 (two agents concurrently
editing `add`/`sub` in this same file) and for money-shot #5 (a deliberately
test-breaking edit here must fail this suite so the integrator rejects it)."""
from __future__ import annotations

import pytest

from calc import add, div, mul, sub


def test_add():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-1, 1) == 0


def test_sub():
    assert sub(5, 3) == 2


def test_mul():
    assert mul(4, 3) == 12


def test_div():
    assert div(10, 2) == 5


def test_div_by_zero_raises():
    with pytest.raises(ZeroDivisionError):
        div(1, 0)
