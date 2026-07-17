"""U3 — Dependency graph, blast radius, frozen contracts. DESIGN.md §3 (steps 3-6).

Done when: on a 3-module fixture, a symbol imported by 3+ others has blast_radius >= 3
and is returned as a frozen contract with a signature+type_hash; co_schedulable is False
for two symbols in one file (file mode) and True for symbols in different files.
"""
from __future__ import annotations

import textwrap

import pytest

from swarmsync.classifier.graph import (
    FREEZE_THRESHOLD,
    SymbolModeError,
    build_graph,
    blast_radius,
    check_file_granularity,
    co_schedulable,
    extract_contracts,
)
from swarmsync.classifier.indexer import index_repo


@pytest.fixture()
def fixture_repo(tmp_path):
    """mod_a.helper is imported/called by mod_b, mod_c, and mod_d (3 distinct
    importers, via 3 different import styles) -- a frozen-contract candidate.
    mod_b also has a second, purely-local function (unused_local) to exercise
    same-file co_schedulable, and mod_e is a fully unrelated file."""
    (tmp_path / "mod_a.py").write_text(
        textwrap.dedent(
            """\
            def helper(x, y=1):
                return x + y


            def _private(x):
                return x
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "mod_b.py").write_text(
        textwrap.dedent(
            """\
            from mod_a import helper


            def use_b():
                return helper(1)


            def unused_local():
                return 42
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "mod_c.py").write_text(
        textwrap.dedent(
            """\
            from mod_a import helper as h


            def use_c():
                return h(2)
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "mod_d.py").write_text(
        textwrap.dedent(
            """\
            import mod_a


            def use_d():
                return mod_a.helper(3)
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "mod_e.py").write_text(
        textwrap.dedent(
            """\
            def standalone():
                return "unrelated"
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def graph_and_parcels(fixture_repo):
    parcels = index_repo(fixture_repo)
    graph = build_graph(parcels, fixture_repo)
    return graph, parcels


def _find(parcels, parcel_id):
    for p in parcels:
        if p.id == parcel_id:
            return p
    raise KeyError(parcel_id)


def test_blast_radius_of_heavily_imported_symbol_is_at_least_three(graph_and_parcels):
    graph, _ = graph_and_parcels
    blast = blast_radius(graph)
    assert blast["mod_a.py::helper"] >= 3


def test_blast_radius_of_unrelated_symbol_is_low(graph_and_parcels):
    graph, _ = graph_and_parcels
    blast = blast_radius(graph)
    assert blast["mod_e.py::standalone"] == 0
    assert blast["mod_a.py::_private"] == 0


def test_import_edges_resolve_all_three_call_styles(graph_and_parcels):
    graph, _ = graph_and_parcels
    dependents = graph.reverse_edges["mod_a.py::helper"]
    assert "mod_b.py::use_b" in dependents  # from mod_a import helper
    assert "mod_c.py::use_c" in dependents  # from mod_a import helper as h
    assert "mod_d.py::use_d" in dependents  # import mod_a; mod_a.helper(...)


def test_helper_is_flagged_cross_module(graph_and_parcels):
    graph, _ = graph_and_parcels
    assert graph.is_cross_module("mod_a.py::helper")
    assert not graph.is_cross_module("mod_e.py::standalone")


def test_extract_contracts_returns_frozen_contract_for_helper(graph_and_parcels):
    graph, parcels = graph_and_parcels
    blast = blast_radius(graph)
    contracts = extract_contracts(parcels, graph, blast, threshold=FREEZE_THRESHOLD)
    by_symbol = {c.symbol: c for c in contracts}
    assert "mod_a.py::helper" in by_symbol
    contract = by_symbol["mod_a.py::helper"]
    assert contract.frozen == 1
    assert contract.version == 1
    assert contract.signature == "helper(x, y=1)"
    assert isinstance(contract.type_hash, str) and len(contract.type_hash) == 64


def test_extract_contracts_excludes_low_blast_and_non_cross_module_symbols(graph_and_parcels):
    graph, parcels = graph_and_parcels
    blast = blast_radius(graph)
    contracts = extract_contracts(parcels, graph, blast, threshold=FREEZE_THRESHOLD)
    symbols = {c.symbol for c in contracts}
    assert "mod_e.py::standalone" not in symbols
    assert "mod_b.py::use_b" not in symbols
    assert "mod_a.py::_private" not in symbols


def test_contract_signature_changes_type_hash(fixture_repo):
    # Sanity: type_hash is a hash of the signature, not a constant.
    parcels = index_repo(fixture_repo)
    graph = build_graph(parcels, fixture_repo)
    sig, hashed = graph.signatures["mod_a.py::helper"]
    import hashlib

    assert hashed == hashlib.sha256(sig.encode("utf-8")).hexdigest()


def test_co_schedulable_false_for_two_symbols_in_one_file(graph_and_parcels):
    _, parcels = graph_and_parcels
    a = _find(parcels, "mod_b.py::use_b")
    b = _find(parcels, "mod_b.py::unused_local")
    assert co_schedulable(a, b, mode="file") is False


def test_co_schedulable_true_for_symbols_in_different_files(graph_and_parcels):
    _, parcels = graph_and_parcels
    a = _find(parcels, "mod_b.py::use_b")
    b = _find(parcels, "mod_c.py::use_c")
    assert co_schedulable(a, b, mode="file") is True


def test_co_schedulable_symbol_mode_disjoint_spans_in_same_file(graph_and_parcels):
    _, parcels = graph_and_parcels
    a = _find(parcels, "mod_b.py::use_b")
    b = _find(parcels, "mod_b.py::unused_local")
    # These two used to be co-schedulable under mode="symbol" (disjoint concrete spans in
    # one file). Symbol granularity is now parked and refuses -- the exact pair the parked
    # mode existed to allow is the sharpest input to prove the refusal on.
    with pytest.raises(SymbolModeError, match="parked"):
        co_schedulable(a, b, mode="symbol")
    # ...and at file granularity (the only mode) they share a file, so they serialize.
    assert co_schedulable(a, b, mode="file") is False


def test_co_schedulable_false_for_a_parcel_against_itself(graph_and_parcels):
    """Orthogonal to granularity: a parcel is never co-schedulable with itself. Used to be
    asserted via mode="symbol" (identical spans overlap); file mode gives it too (same path)."""
    _, parcels = graph_and_parcels
    a = _find(parcels, "mod_b.py::use_b")
    assert co_schedulable(a, a, mode="file") is False


def test_check_file_granularity_accepts_file_and_refuses_symbol():
    assert check_file_granularity("file") == "file"
    with pytest.raises(SymbolModeError, match="parked"):
        check_file_granularity("symbol")
    # ...and an unrecognized mode is still a plain ValueError, not a symbol-mode refusal.
    with pytest.raises(ValueError) as exc:
        check_file_granularity("bogus")
    assert not isinstance(exc.value, SymbolModeError)


def test_symbol_mode_error_is_a_valueerror_so_existing_callers_still_catch_it():
    """`resolve_task`/`co_schedulable` have always documented ValueError for a bad mode, and
    callers catch that. The refusal must not escape a `except ValueError` handler."""
    assert issubclass(SymbolModeError, ValueError)


def test_co_schedulable_rejects_unknown_mode(graph_and_parcels):
    _, parcels = graph_and_parcels
    a = _find(parcels, "mod_b.py::use_b")
    b = _find(parcels, "mod_c.py::use_c")
    with pytest.raises(ValueError):
        co_schedulable(a, b, mode="bogus")


def test_co_schedulable_frozen_contract_clause_blocks_dependent_pair(graph_and_parcels):
    graph, parcels = graph_and_parcels
    blast = blast_radius(graph)
    contracts = extract_contracts(parcels, graph, blast, threshold=FREEZE_THRESHOLD)
    frozen_ids = {c.symbol for c in contracts}
    assert "mod_a.py::helper" in frozen_ids

    helper = _find(parcels, "mod_a.py::helper")
    use_b = _find(parcels, "mod_b.py::use_b")
    # Different files -> structurally disjoint, but use_b depends on the frozen helper.
    assert co_schedulable(helper, use_b, mode="file") is True  # no frozen_ids passed
    assert (
        co_schedulable(helper, use_b, mode="file", graph=graph, frozen_ids=frozen_ids)
        is False
    )


def test_co_schedulable_frozen_contract_clause_allows_unrelated_pair(graph_and_parcels):
    graph, parcels = graph_and_parcels
    blast = blast_radius(graph)
    contracts = extract_contracts(parcels, graph, blast, threshold=FREEZE_THRESHOLD)
    frozen_ids = {c.symbol for c in contracts}

    helper = _find(parcels, "mod_a.py::helper")
    standalone = _find(parcels, "mod_e.py::standalone")
    assert (
        co_schedulable(helper, standalone, mode="file", graph=graph, frozen_ids=frozen_ids)
        is True
    )


def test_build_graph_and_blast_radius_are_deterministic(fixture_repo):
    parcels1 = index_repo(fixture_repo)
    graph1 = build_graph(parcels1, fixture_repo)
    blast1 = blast_radius(graph1)

    parcels2 = index_repo(fixture_repo)
    graph2 = build_graph(parcels2, fixture_repo)
    blast2 = blast_radius(graph2)

    assert blast1 == blast2


# --- R4 mutation M-1: the parse guard is real, and was undefended -----------------


def test_build_graph_survives_a_file_that_is_broken_on_disk_right_now(tmp_path):
    """A file that parsed at index time is ROUTINELY broken on disk in a live swarm.

    R4's mutation dimension deleted `SyntaxError` from build_graph's per-file guard
    and the whole suite stayed green. The guard is NOT dead code: `index_repo` skips
    unparseable files, so via that path no parcel exists to re-parse -- but
    `broker.load_scheduling_graph` and `integrator._reverse_dep_files` build parcels
    from the DATABASE (rows indexed when the file was valid) and then re-read the file
    from DISK. Agents are editing that tree; mid-edit it does not parse.

    Without the guard that raises straight out of build_graph, so the broker schedules
    NO task for ANY file and /integrate 500s -- one transiently-broken file bricks the
    whole coordinator. That is the same class as the `kind='file'` ValidationError and
    the `local_symbols` KeyError, both of which were P0s: every reader of the parcel
    map re-parses the world and dies on one bad file.
    """
    from swarmsync.classifier.indexer import parse_file

    good = tmp_path / "good.py"
    good.write_text("def kept(x):\n    return x\n", encoding="utf-8")
    broken = tmp_path / "broken.py"
    broken.write_text("def helper(x):\n    return x\n", encoding="utf-8")

    # Index both while they are valid, as POST /index would.
    parcels = list(parse_file(good, rel_path="good.py")) + list(
        parse_file(broken, rel_path="broken.py")
    )

    # An agent is mid-edit: the file no longer parses.
    broken.write_text("def helper(x:\n", encoding="utf-8")

    graph = build_graph(parcels, tmp_path)  # must not raise

    # The unaffected file is still fully scheduled against.
    assert "good.py::kept" in graph.signatures, (
        "a transiently-broken sibling file wiped out scheduling for everything else"
    )


def test_build_graph_survives_an_undecodable_file_on_disk(tmp_path):
    """Same guard, the other reachable arm: a file that is valid UTF-8 at index time
    and binary garbage on disk now (a bad write, a merge artifact)."""
    from swarmsync.classifier.indexer import parse_file

    good = tmp_path / "good.py"
    good.write_text("def kept(x):\n    return x\n", encoding="utf-8")
    weird = tmp_path / "weird.py"
    weird.write_text("def w(x):\n    return x\n", encoding="utf-8")
    parcels = list(parse_file(good, rel_path="good.py")) + list(
        parse_file(weird, rel_path="weird.py")
    )

    weird.write_bytes(b"\xff\xfe\x00\x01 not utf-8 \xc3\x28")

    graph = build_graph(parcels, tmp_path)
    assert "good.py::kept" in graph.signatures
