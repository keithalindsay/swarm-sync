"""Covers api.py -- the public surface, importing both calc (module-level) and
formats (named), which is what gives calc.add its cross-module fan-in on top
of formats.py's own."""
from __future__ import annotations

from api import apply_discount, report, summarize


def test_summarize():
    assert summarize(2, 3) == "$5.00"


def test_apply_discount():
    assert apply_discount(200, 10) == "$180.00"


def test_report():
    result = report(100, 150)
    assert result.startswith("$50.00")
    assert "50.0%" in result
