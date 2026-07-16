"""Human-readable formatting built on top of calc.py's arithmetic core.

Built in Unit U13. Depends on `calc.add`/`calc.mul`/`calc.div`, so this module is
part of `calc.add`'s cross-module fan-in (DESIGN §3 step 5) and a natural
dependent for money-shot #3: if `calc.add`'s signature ever changes,
`total_with_tax` below is the call site that needs re-planning.
"""
from __future__ import annotations

from calc import add, div, mul


def money(amount, currency="$"):
    """Format `amount` as e.g. "$12.50"."""
    return f"{currency}{amount:.2f}"


def percent(part, total):
    """`part`/`total` as a percentage string, e.g. "25.0%". Zero `total` is
    reported as "0.0%" rather than propagating calc.div's ZeroDivisionError --
    a formatting concern, not an arithmetic one."""
    if total == 0:
        return "0.0%"
    return f"{div(part, total) * 100:.1f}%"


def total_with_tax(amount, tax_rate):
    """amount + amount*tax_rate, via calc.add/calc.mul."""
    return add(amount, mul(amount, tax_rate))
