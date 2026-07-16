"""U4 — Index API population. DESIGN.md §3 (output), §4.2 (POST /index).

Done when: running the index over `sample_repo/` (or a fixture) inserts one row per
parcel and one row per frozen contract; re-running updates in place (no duplicates).
"""
from __future__ import annotations

import textwrap

import pytest

from swarmsync.blackboard import db
from swarmsync.classifier.indexer import index_repo
from swarmsync.classifier.store import run_index, upsert_contracts, upsert_parcels


@pytest.fixture()
def fixture_repo(tmp_path):
    """Same shape as test_graph.py's fixture: mod_a.helper is imported/called by
    three other modules (a frozen-contract candidate), mod_b also has a second,
    purely-local function, and mod_e is unrelated."""
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
def conn(tmp_path):
    c = db.init_db(tmp_path / "blackboard.db")
    yield c
    c.close()


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_run_index_inserts_one_row_per_parcel(conn, fixture_repo):
    result = run_index(conn, fixture_repo)
    expected_parcels = index_repo(fixture_repo)
    assert len(result.parcels) == len(expected_parcels)
    assert _count(conn, "parcels") == len(expected_parcels)


def test_run_index_inserts_one_row_per_frozen_contract(conn, fixture_repo):
    result = run_index(conn, fixture_repo)
    assert len(result.contracts) >= 1
    by_symbol = {c.symbol for c in result.contracts}
    assert "mod_a.py::helper" in by_symbol
    assert _count(conn, "contracts") == len(result.contracts)


def test_run_index_parcel_rows_have_expected_fields(conn, fixture_repo):
    run_index(conn, fixture_repo)
    row = conn.execute(
        "SELECT * FROM parcels WHERE id = ?", ("mod_a.py::helper",)
    ).fetchone()
    assert row is not None
    assert row["path"] == "mod_a.py"
    assert row["kind"] == "function"
    assert row["blast_radius"] >= 3
    assert row["content_hash"] is not None
    assert row["byte_start"] is not None and row["byte_end"] is not None
    # helper made the contract cut -> its parcel row's contract_hash should agree
    # with the contracts table's type_hash for the same symbol.
    contract_row = conn.execute(
        "SELECT * FROM contracts WHERE symbol = ?", ("mod_a.py::helper",)
    ).fetchone()
    assert contract_row is not None
    assert row["contract_hash"] == contract_row["type_hash"]


def test_run_index_non_contract_parcel_has_no_contract_hash(conn, fixture_repo):
    run_index(conn, fixture_repo)
    row = conn.execute(
        "SELECT * FROM parcels WHERE id = ?", ("mod_e.py::standalone",)
    ).fetchone()
    assert row is not None
    assert row["contract_hash"] is None


def test_rerunning_index_does_not_duplicate_rows(conn, fixture_repo):
    run_index(conn, fixture_repo)
    n_parcels_1 = _count(conn, "parcels")
    n_contracts_1 = _count(conn, "contracts")

    run_index(conn, fixture_repo)
    n_parcels_2 = _count(conn, "parcels")
    n_contracts_2 = _count(conn, "contracts")

    assert n_parcels_1 == n_parcels_2
    assert n_contracts_1 == n_contracts_2
    assert n_parcels_1 > 0
    assert n_contracts_1 > 0


def test_rerunning_index_updates_rows_in_place(conn, fixture_repo):
    run_index(conn, fixture_repo)
    first_updated_at = conn.execute(
        "SELECT updated_at FROM parcels WHERE id = ?", ("mod_a.py::helper",)
    ).fetchone()["updated_at"]

    # Mutate the source (change helper's body -> new content_hash) and re-index.
    (fixture_repo / "mod_a.py").write_text(
        textwrap.dedent(
            """\
            def helper(x, y=1):
                return x + y + 1


            def _private(x):
                return x
            """
        ),
        encoding="utf-8",
    )
    run_index(conn, fixture_repo)

    row = conn.execute(
        "SELECT * FROM parcels WHERE id = ?", ("mod_a.py::helper",)
    ).fetchone()
    assert row["updated_at"] >= first_updated_at
    # content_hash changed because the source slice changed.
    expected = [p for p in index_repo(fixture_repo) if p.id == "mod_a.py::helper"][0]
    assert row["content_hash"] == expected.content_hash
    # still only one row for this id (no duplicate insert).
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM parcels WHERE id = ?", ("mod_a.py::helper",)
        ).fetchone()[0]
        == 1
    )


def test_rerunning_index_preserves_existing_state_summary(conn, fixture_repo):
    run_index(conn, fixture_repo)
    conn.execute(
        "UPDATE parcels SET state_summary = ? WHERE id = ?",
        ("agent wrote this note", "mod_a.py::helper"),
    )
    run_index(conn, fixture_repo)
    row = conn.execute(
        "SELECT state_summary FROM parcels WHERE id = ?", ("mod_a.py::helper",)
    ).fetchone()
    assert row["state_summary"] == "agent wrote this note"


def test_contract_version_bumps_when_signature_changes_on_rerun(conn, fixture_repo):
    run_index(conn, fixture_repo)
    v1 = conn.execute(
        "SELECT version, type_hash FROM contracts WHERE symbol = ?",
        ("mod_a.py::helper",),
    ).fetchone()
    assert v1["version"] == 1

    # Change helper's signature (params) -> new signature -> new type_hash.
    (fixture_repo / "mod_a.py").write_text(
        textwrap.dedent(
            """\
            def helper(x, y=1, z=2):
                return x + y + z


            def _private(x):
                return x
            """
        ),
        encoding="utf-8",
    )
    run_index(conn, fixture_repo)
    v2 = conn.execute(
        "SELECT version, type_hash FROM contracts WHERE symbol = ?",
        ("mod_a.py::helper",),
    ).fetchone()
    assert v2["type_hash"] != v1["type_hash"]
    assert v2["version"] == 2


def test_contract_version_unchanged_when_signature_is_the_same_on_rerun(
    conn, fixture_repo
):
    run_index(conn, fixture_repo)
    run_index(conn, fixture_repo)
    row = conn.execute(
        "SELECT version FROM contracts WHERE symbol = ?", ("mod_a.py::helper",)
    ).fetchone()
    assert row["version"] == 1


def test_upsert_parcels_no_op_on_empty_list(conn):
    upsert_parcels(conn, [])
    assert _count(conn, "parcels") == 0


def test_upsert_contracts_no_op_on_empty_list(conn):
    upsert_contracts(conn, [])
    assert _count(conn, "contracts") == 0


def test_run_index_on_real_sample_repo_dir(conn):
    # sample_repo/ was built out in U13: calc.py/formats.py/api.py + their own
    # tests/, a real cross-module import/call graph with >=1 frozen contract.
    # (See tests/test_sample_repo.py for the unit's own, more detailed done-when
    # assertions -- this is just proving run_index/POST-/index's own code path
    # against the real fixture repo, not a near-empty one.)
    import swarmsync
    from pathlib import Path

    repo_root = Path(swarmsync.__file__).resolve().parent.parent / "sample_repo"
    result = run_index(conn, repo_root)
    assert len(result.parcels) > 0
    assert len(result.contracts) > 0
    assert _count(conn, "parcels") == len(result.parcels)
    assert _count(conn, "contracts") == len(result.contracts)
