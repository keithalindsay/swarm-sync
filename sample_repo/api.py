"""Public surface of sample_repo.

Built in Unit U13. Imports both `calc` (module-level, attribute calls) and
`formats` (named imports), giving `calc.add` additional cross-module fan-in on
top of `formats.py`'s -- together with the test suite's own direct calls, this
is what pushes `calc.py::add`'s blast_radius past FREEZE_THRESHOLD (3) and
makes it sample_repo's frozen-contract candidate (DESIGN §3 step 5).
"""
from __future__ import annotations

import calc
import formats


def summarize(a, b):
    """Add two numbers and format the result as money."""
    total = calc.add(a, b)
    return formats.money(total)


def apply_discount(price, discount_pct):
    """price minus discount_pct% of price, formatted as money."""
    discount_amount = calc.mul(price, discount_pct / 100)
    new_price = calc.sub(price, discount_amount)
    return formats.money(new_price)


def report(before, after):
    """Describe the change from `before` to `after` as an amount + percentage."""
    change = calc.sub(after, before)
    return f"{formats.money(change)} ({formats.percent(change, before)})"
