"""Covers formats.py -- a dependent of calc.add/calc.mul/calc.div, and (via
total_with_tax) the call site test case #3 exercises when calc.add's frozen
signature changes."""
from __future__ import annotations

from formats import money, percent, total_with_tax


def test_money():
    assert money(12.5) == "$12.50"


def test_money_custom_currency():
    assert money(3, currency="EUR ") == "EUR 3.00"


def test_percent():
    assert percent(1, 4) == "25.0%"


def test_percent_zero_total():
    assert percent(5, 0) == "0.0%"


def test_total_with_tax():
    assert total_with_tax(100, 0.1) == 110.0
