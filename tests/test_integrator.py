"""U10 — Serial test-gated integrator. DESIGN.md §5.4, §5.5.

Done when (BUILD_PLAN.md): two disjoint-file branches integrate serially and
both land with green tests + `merged` events; a branch whose edit breaks a
sample test is `merge_rejected`, is reset out, and leaves `integration` tests
green.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from swarmsync.blackboard import db
from swarmsync.classifier.store import run_index
from swarmsync.coordinator import integrator
from swarmsync.server import events as events_mod
from swarmsync.worktree import git_ops


def _write_repo(root):
    (root / "mod_a.py").write_text(
        textwrap.dedent(
            """\
            def helper(x):
                return x + 1
            """
        ),
        encoding="utf-8",
    )
    (root / "mod_b.py").write_text(
        textwrap.dedent(
            """\
            def other(y):
                return y * 2
            """
        ),
        encoding="utf-8",
    )
    (root / "mod_c.py").write_text(
        textwrap.dedent(
            """\
            def broken():
                return 1
            """
        ),
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text(
        textwrap.dedent(
            """\
            from mod_a import helper


            def test_helper():
                # value-agnostic on purpose -- these tests only need to survive
                # arithmetic tweaks to helper()'s return value; mod_c's test is
                # the one that actually catches a broken symbol (raises).
                assert isinstance(helper(1), int)
            """
        ),
        encoding="utf-8",
    )
    (tests / "test_b.py").write_text(
        textwrap.dedent(
            """\
            from mod_b import other


            def test_other():
                assert isinstance(other(2), int)
            """
        ),
        encoding="utf-8",
    )
    (tests / "test_c.py").write_text(
        textwrap.dedent(
            """\
            from mod_c import broken


            def test_broken():
                assert broken() == 1
            """
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _write_repo(r)
    base = git_ops.init_repo(r)
    return r, base


@pytest.fixture()
def conn(tmp_path):
    c = db.init_db(tmp_path / "blackboard.db")
    yield c
    c.close()


def _full_suite_green(repo) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"], cwd=str(repo), capture_output=True, text=True
    )
    return result.returncode == 0


# --- done-when: two disjoint-file branches land serially, both green ---------------


def test_two_disjoint_branches_integrate_serially_and_both_land(conn, repo):
    r, base = repo
    run_index(conn, r)  # pre-populate parcels/contracts, as POST /index would

    worktree_a = git_ops.add_worktree(r, "agent-a", base)
    (worktree_a / "mod_a.py").write_text(
        "def helper(x):\n    y = x + 1\n    return y\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree_a, "agent-a: refactor helper (behavior-preserving)")

    worktree_b = git_ops.add_worktree(r, "agent-b", base)
    (worktree_b / "mod_b.py").write_text(
        "def other(y):\n    z = y * 2\n    return z\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree_b, "agent-b: refactor other (behavior-preserving)")

    result_a = integrator.integrate(
        conn, r, "agent-a", base_commit=base, agent_id="agent-a"
    )
    assert result_a.status == "merged"
    assert result_a.merged_commit is not None
    assert result_a.changed_files == ["mod_a.py"]
    assert "mod_a.py::helper" in result_a.reindexed_parcels

    result_b = integrator.integrate(
        conn, r, "agent-b", base_commit=base, agent_id="agent-b"
    )
    assert result_b.status == "merged"
    assert result_b.changed_files == ["mod_b.py"]
    assert "mod_b.py::other" in result_b.reindexed_parcels

    # both edits actually landed on trunk (the main checkout == "integration").
    assert "y = x + 1" in (r / "mod_a.py").read_text()
    assert "z = y * 2" in (r / "mod_b.py").read_text()

    # zero textual collisions -- both merges reported no conflicts.
    assert result_a.conflicts == []
    assert result_b.conflicts == []

    # `merged` events for both, in order.
    events = events_mod.tail(conn, since_seq=0)
    merged_branches = [
        e.agent_id for e in events if e.type == "merged"
    ]
    assert merged_branches == ["agent-a", "agent-b"]

    # trunk's own test suite is green throughout.
    assert _full_suite_green(r)


# --- done-when: a test-breaking branch is rejected, reset out, trunk stays green ----


def test_test_breaking_branch_is_rejected_and_reset_out(conn, repo):
    r, base = repo
    run_index(conn, r)

    # land a good branch first so there's real trunk state to protect.
    worktree_a = git_ops.add_worktree(r, "agent-a", base)
    (worktree_a / "mod_a.py").write_text(
        "def helper(x):\n    return x + 1  # unchanged behavior\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree_a, "agent-a: harmless comment")
    good = integrator.integrate(conn, r, "agent-a", base_commit=base, agent_id="agent-a")
    assert good.status == "merged"

    pre_reject_head = git_ops.current_commit(r, ref="integration")

    # agent-c breaks mod_c.py::broken so its own test fails.
    worktree_c = git_ops.add_worktree(r, "agent-c", base)
    (worktree_c / "mod_c.py").write_text(
        "def broken():\n    raise RuntimeError('boom')\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree_c, "agent-c: break broken()")

    result_c = integrator.integrate(
        conn, r, "agent-c", base_commit=base, agent_id="agent-c"
    )
    assert result_c.status == "merge_rejected"
    assert result_c.reason == "impact tests failed"
    assert result_c.changed_files == ["mod_c.py"]
    assert "boom" in result_c.test_log or "RuntimeError" in result_c.test_log

    # trunk was reset back to exactly its pre-reject state -- the bad edit
    # never landed, and agent-a's earlier good merge is still intact.
    assert git_ops.current_commit(r, ref="integration") == pre_reject_head
    assert "raise RuntimeError" not in (r / "mod_c.py").read_text()
    assert "def broken():\n    return 1" in (r / "mod_c.py").read_text()
    assert "x + 1" in (r / "mod_a.py").read_text()  # agent-a's landed change stays

    events = events_mod.tail(conn, since_seq=0)
    rejected = [e for e in events if e.type == "merge_rejected"]
    assert len(rejected) == 1
    assert rejected[0].agent_id == "agent-c"

    # trunk's test suite is green -- the rejected branch never poisoned it.
    assert _full_suite_green(r)

    # agent-c's own branch still exists (bounced back, not discarded) with its
    # bad commit intact -- only `integration` was rolled back.
    worktree_c_text = (worktree_c / "mod_c.py").read_text()
    assert "raise RuntimeError" in worktree_c_text


# --- a genuine textual conflict is rejected (touch-set misprediction) --------------


def test_overlapping_edit_is_rejected_as_merge_conflict(conn, repo):
    r, base = repo
    run_index(conn, r)

    worktree_a = git_ops.add_worktree(r, "agent-a", base)
    (worktree_a / "mod_a.py").write_text(
        "def helper(x):\n    return x + 111\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree_a, "agent-a: change helper")
    result_a = integrator.integrate(conn, r, "agent-a", base_commit=base, agent_id="agent-a")
    assert result_a.status == "merged"

    # agent-b forked from the SAME original base and rewrites the identical
    # line of mod_a.py differently -- a genuine touch-set misprediction.
    worktree_b = git_ops.add_worktree(r, "agent-b", base)
    (worktree_b / "mod_a.py").write_text(
        "def helper(x):\n    return x + 222\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree_b, "agent-b: change helper differently")

    result_b = integrator.integrate(conn, r, "agent-b", base_commit=base, agent_id="agent-b")
    assert result_b.status == "merge_rejected"
    assert result_b.conflicts == ["mod_a.py"]
    assert "misprediction" in result_b.reason

    events = events_mod.tail(conn, since_seq=0)
    rejected = [e for e in events if e.type == "merge_rejected" and e.agent_id == "agent-b"]
    assert len(rejected) == 1

    # trunk keeps agent-a's landed change, untouched by the failed merge attempt.
    assert "x + 111" in (r / "mod_a.py").read_text()


# --- optimistic re-check (DESIGN §5.5): stale read-dep -> needs_rebase, no merge ---


def test_stale_read_dependency_short_circuits_to_needs_rebase(conn, repo):
    r, base = repo
    run_index(conn, r)

    row = conn.execute(
        "SELECT content_hash FROM parcels WHERE id = ?", ("mod_a.py::helper",)
    ).fetchone()
    assert row is not None
    real_hash = row["content_hash"]
    stale_expected = {"mod_a.py::helper": "not-the-real-hash"}

    result = integrator.integrate(
        conn,
        r,
        "some-branch-that-need-not-even-exist",
        agent_id="agent-z",
        expected_read_deps=stale_expected,
    )
    assert result.status == "needs_rebase"
    assert result.stale_deps == ["mod_a.py::helper"]

    events = events_mod.tail(conn, since_seq=0)
    rebase_events = [e for e in events if e.type == "needs_rebase"]
    assert len(rebase_events) == 1
    assert rebase_events[0].agent_id == "agent-z"

    # a matching (non-stale) hash does NOT short-circuit -- merge proceeds normally.
    fresh_expected = {"mod_a.py::helper": real_hash}
    worktree = git_ops.add_worktree(r, "agent-fresh", base)
    (worktree / "mod_b.py").write_text(
        "def other(y):\n    return y * 20\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree, "agent-fresh: edit other")
    result2 = integrator.integrate(
        conn,
        r,
        "agent-fresh",
        base_commit=base,
        agent_id="agent-fresh",
        expected_read_deps=fresh_expected,
    )
    assert result2.status == "merged"


# --- impact test selection: full-suite fallback when nothing matches --------------


def test_run_impact_tests_full_suite_fallback_when_no_test_dir(tmp_path):
    r = tmp_path / "no_tests_repo"
    r.mkdir()
    (r / "mod_x.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    ok, log = integrator.run_impact_tests(r, ["mod_x.py"])
    assert ok is True  # "no tests collected" (exit 5) is not a rejection reason


def test_run_impact_tests_selects_only_the_reachable_test_file(repo):
    r, _base = repo
    ok, log = integrator.run_impact_tests(r, ["mod_a.py"])
    assert ok is True
    # only test_a.py's single test ran (not all 3 across test_a/b/c) -- proof
    # impact selection actually narrowed the pytest invocation, not a
    # full-suite fallback.
    assert "1 passed" in log


# --- U15: frozen-contract change detection + contract_change event ----------------


def test_contract_change_event_emitted_when_a_frozen_signature_lands(tmp_path):
    """DESIGN §5.3 (money-shot #3): a merge that genuinely changes a frozen
    contract's signature must emit a real `contract_change` event carrying
    the old/new signature + version, and report the symbol on
    `IntegrateResult.contract_changes` -- driven straight off a real
    before/after `type_hash` diff (never an agent's self-report)."""
    r = tmp_path / "contract_repo"
    r.mkdir()
    (r / "mod_a.py").write_text("def helper(x):\n    return x + 1\n", encoding="utf-8")
    (r / "mod_d.py").write_text(
        "from mod_a import helper\n\n\ndef caller(x):\n    return helper(x) + 1\n",
        encoding="utf-8",
    )
    base = git_ops.init_repo(r)
    conn = db.init_db(tmp_path / "contract_bb.db")
    try:
        # threshold=1: mod_d.py::caller's single cross-module call is enough
        # to make mod_a.py::helper a frozen contract for this focused test
        # (sample_repo's own FREEZE_THRESHOLD=3 default is exercised end to
        # end by the demo/test_demo.py suite instead).
        run_index(conn, r, threshold=1)
        before = conn.execute(
            "SELECT signature, type_hash, version FROM contracts WHERE symbol = ?",
            ("mod_a.py::helper",),
        ).fetchone()
        assert before is not None, "mod_a.py::helper should be frozen at threshold=1"
        assert "scale" not in before["signature"]

        worktree = git_ops.add_worktree(r, "agent-freeze", base)
        (worktree / "mod_a.py").write_text(
            "def helper(x, scale=1):\n    return x + 1\n", encoding="utf-8"
        )
        git_ops.commit_all(worktree, "agent-freeze: add scale param to helper")

        result = integrator.integrate(
            conn, r, "agent-freeze", base_commit=base, agent_id="agent-freeze", threshold=1,
        )
        assert result.status == "merged"
        assert result.contract_changes == ["mod_a.py::helper"]

        events = events_mod.tail(conn, since_seq=0)
        changes = [e for e in events if e.type == "contract_change"]
        assert len(changes) == 1
        assert changes[0].agent_id == "agent-freeze"
        payload = json.loads(changes[0].payload)
        assert payload["symbol"] == "mod_a.py::helper"
        assert "scale" not in payload["old_signature"]
        assert "scale" in payload["new_signature"]
        assert payload["new_version"] == payload["old_version"] + 1

        after = conn.execute(
            "SELECT version FROM contracts WHERE symbol = ?", ("mod_a.py::helper",)
        ).fetchone()
        assert after["version"] == before["version"] + 1
    finally:
        conn.close()


def test_no_contract_change_event_when_an_unrelated_branch_merges(repo, conn):
    """A branch that touches no frozen contract's file at all must never emit
    a spurious `contract_change` (mod_a.py/mod_b.py/mod_c.py's own fixture
    functions have blast_radius 0 at the default threshold, so nothing here
    is frozen in the first place -- this pins that down explicitly)."""
    r, base = repo
    run_index(conn, r)

    worktree = git_ops.add_worktree(r, "agent-plain", base)
    (worktree / "mod_b.py").write_text(
        "def other(y):\n    return y * 3\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree, "agent-plain: tweak other")

    result = integrator.integrate(conn, r, "agent-plain", base_commit=base, agent_id="agent-plain")
    assert result.status == "merged"
    assert result.contract_changes == []

    events = events_mod.tail(conn, since_seq=0)
    assert not [e for e in events if e.type == "contract_change"]
