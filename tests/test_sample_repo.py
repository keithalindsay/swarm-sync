"""U13 — sample_repo/ + its own pytest suite. DESIGN.md §7 (the demo's fixture repo).

Done when (BUILD_PLAN.md): sample_repo has >=3 modules with a real import/call graph,
>=1 file with two independent functions, >=1 high-fan-in symbol (frozen-contract
candidate), and `pytest sample_repo/tests` is green.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from swarmsync.classifier.graph import (
    FREEZE_THRESHOLD,
    blast_radius,
    build_graph,
    co_schedulable,
    extract_contracts,
)
from swarmsync.classifier.indexer import index_repo

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_REPO = REPO_ROOT / "sample_repo"


@pytest.fixture(scope="module")
def classified():
    parcels = index_repo(SAMPLE_REPO)
    graph = build_graph(parcels, SAMPLE_REPO)
    blast = blast_radius(graph)
    contracts = extract_contracts(parcels, graph, blast)
    return parcels, graph, blast, contracts


def test_at_least_three_top_level_modules(classified):
    parcels, graph, blast, contracts = classified
    top_level_files = {
        p.path for p in parcels if p.path.endswith(".py") and "/" not in p.path
    }
    assert len(top_level_files) >= 3, top_level_files


def test_real_cross_file_import_or_call_edge_exists(classified):
    parcels, graph, blast, contracts = classified
    assert graph.edges, "expected at least one import/call edge in sample_repo's graph"
    cross_file = any(
        graph.parcels_by_id[dependency].path != graph.parcels_by_id[dependent].path
        for dependent, deps in graph.edges.items()
        for dependency in deps
        if dependent in graph.parcels_by_id and dependency in graph.parcels_by_id
    )
    assert cross_file, "expected an edge that actually crosses a file boundary"


def test_calc_has_two_symbol_parcels_with_disjoint_spans(classified):
    """The indexer emits >=2 function parcels in ONE file with disjoint byte spans.

    This used to be phrased as money-shot #1's precondition ("so symbol-mode leasing can
    let two agents edit them at once") and asserted via `co_schedulable(..., mode="symbol")`.
    Symbol granularity is parked (SYMBOL_MODE_DESIGN.md) so that call now refuses -- but the
    structural fact is orthogonal to leasing and must KEEP holding: symbol-level parcels are
    still indexed, and the frozen-contract subsystem is built on them. So this asserts the
    spans directly rather than through the parked scheduling relation."""
    parcels, graph, blast, contracts = classified
    calc_funcs = sorted(
        (p for p in parcels if p.path == "calc.py" and p.kind == "function"),
        key=lambda p: p.symbol,
    )
    assert len(calc_funcs) >= 2, calc_funcs
    a, b = calc_funcs[0], calc_funcs[1]
    assert a.byte_start is not None and b.byte_start is not None
    assert a.byte_end is not None and b.byte_end is not None
    assert a.byte_end <= b.byte_start or b.byte_end <= a.byte_start, (a, b)
    # File granularity (the only enforced mode) serializes them regardless -- same file.
    assert co_schedulable(a, b, mode="file") is False


def test_at_least_one_high_fan_in_frozen_contract(classified):
    """Money-shot #3's precondition: a frozen contract (blast_radius >= FREEZE_THRESHOLD,
    imported/called across a module boundary) exists to change under an exclusive lease."""
    parcels, graph, blast, contracts = classified
    assert contracts, "expected >=1 frozen contract"
    for c in contracts:
        assert blast.get(c.symbol, 0) >= FREEZE_THRESHOLD
        assert graph.is_cross_module(c.symbol)
    assert any(c.symbol == "calc.py::add" for c in contracts), [
        c.symbol for c in contracts
    ]


def test_sample_repo_pytest_suite_is_green():
    """The merge gate money-shots #1 and #5 depend on: sample_repo's own suite passes."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=str(SAMPLE_REPO),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
