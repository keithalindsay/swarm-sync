"""Arithmetic core for sample_repo.

Built in Unit U13 as the fixture repo swarm-sync's agents edit concurrently in the
demo (DESIGN.md §7). Four independent top-level functions, none of which call each
other -- `add`/`sub` (or `mul`/`div`) is what test case #1 targets: two agents
editing two different functions in the SAME file at the same time, so symbol-mode
leasing has something real to prove disjoint on.

`add` also ends up as sample_repo's highest-fan-in symbol once `formats.py`,
`api.py`, and their own test suites call it across a module boundary -- that
makes it a frozen-contract candidate (blast_radius >= FREEZE_THRESHOLD=3,
DESIGN §3 step 5), which is what test case #3 (U15) changes under an
exclusive lease.
"""
from __future__ import annotations


def add(a, b):
    """Return a + b."""
    return a + b


def sub(a, b):
    """Return a - b."""
    return a - b


def mul(a, b):
    """Return a * b."""
    return a * b


def div(a, b):
    """Return a / b. Raises ZeroDivisionError if b == 0 -- deliberately not
    caught here; callers decide how to handle it (see formats.percent)."""
    return a / b
