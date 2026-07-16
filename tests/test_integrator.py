"""U10 — Serial test-gated integrator. DESIGN.md §5.4, §5.5.

Done when (BUILD_PLAN.md): two disjoint-file branches integrate serially and
both land with green tests + `merged` events; a branch whose edit breaks a
sample test is `merge_rejected`, is reset out, and leaves `integration` tests
green.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time

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


def test_stale_frozen_contract_type_hash_short_circuits_to_needs_rebase(conn, repo):
    """DESIGN §5.5's optimistic re-check resolves a read-dep id against
    `parcels.content_hash` first and FALLS BACK to `contracts.type_hash` when
    the id names a frozen contract with no parcel row of that id (the
    contract-only branch of `_check_read_deps`). A drifted `type_hash` -> no
    merge attempted, `needs_rebase`; a matching one lets the merge proceed.

    The matching-hash assertion is what pins the contracts fallback: without it,
    a contract-only id would miss the parcels lookup (current=None), never
    match its real type_hash, and wrongly short-circuit even a fresh branch."""
    r, base = repo
    run_index(conn, r)

    # A frozen contract whose SYMBOL is not any parcel's id -> forces the
    # `_check_read_deps` contracts.type_hash lookup path (the parcels lookup misses).
    conn.execute(
        "INSERT INTO contracts (symbol, signature, type_hash, frozen, version) "
        "VALUES (?, ?, ?, ?, ?)",
        ("phantom.py::ghost", "ghost(x)", "TYPEHASH-A", 1, 1),
    )

    # Drifted type_hash -> needs_rebase, and NO merge is attempted.
    stale = integrator.integrate(
        conn,
        r,
        "branch-need-not-exist",
        agent_id="agent-stale",
        expected_read_deps={"phantom.py::ghost": "TYPEHASH-OLD"},
    )
    assert stale.status == "needs_rebase"
    assert stale.stale_deps == ["phantom.py::ghost"]
    rebase_events = [e for e in events_mod.tail(conn, since_seq=0) if e.type == "needs_rebase"]
    assert len(rebase_events) == 1
    assert rebase_events[0].agent_id == "agent-stale"

    # Matching type_hash -> the contract fallback resolves it as fresh and the
    # merge proceeds normally (proves the branch really read contracts, not None).
    worktree = git_ops.add_worktree(r, "agent-contract-fresh", base)
    (worktree / "mod_b.py").write_text(
        "def other(y):\n    return y * 30\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree, "agent-contract-fresh: edit other")
    fresh = integrator.integrate(
        conn,
        r,
        "agent-contract-fresh",
        base_commit=base,
        agent_id="agent-contract-fresh",
        expected_read_deps={"phantom.py::ghost": "TYPEHASH-A"},
    )
    assert fresh.status == "merged"


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


def test_impact_selection_runs_a_transitively_affected_test(tmp_path):
    """S5: impact selection must not skip a test that exercises the changed file
    only INDIRECTLY. `test_mid` calls `mid.use()` which calls the changed
    `base.core()`, but `test_mid`'s source never names `base` -- so the old
    bare-stem-substring scan (stem `base`) skipped it while still selecting
    `test_base` (which DOES name `base`), meaning no full-suite fallback either.
    The change breaks `test_mid` but not `test_base`, so the old selector merged
    a broken change (`test_mid` never ran); the new dependency-graph reverse-dep
    selector runs `test_mid`, catches the break, and rejects the merge."""
    r = tmp_path / "repo"
    r.mkdir()
    (r / "base.py").write_text("def core():\n    return 1\n", encoding="utf-8")
    (r / "mid.py").write_text(
        "from base import core\n\n\ndef use():\n    return core()\n", encoding="utf-8"
    )
    tests = r / "tests"
    tests.mkdir()
    # names 'base' -> substring-selected; asserts the POST-change value so it
    # passes after the edit (this is what suppresses the full-suite fallback).
    (tests / "test_base.py").write_text(
        "from base import core\n\n\ndef test_core():\n    assert core() == 2\n",
        encoding="utf-8",
    )
    # names 'mid' only, NEVER 'base' -> substring selection SKIPS it, but it
    # transitively exercises base.core (use() -> core()). This is the affected
    # test the old selector wrongly dropped.
    (tests / "test_mid.py").write_text(
        "from mid import use\n\n\ndef test_use():\n    assert use() == 1\n",
        encoding="utf-8",
    )
    base = git_ops.init_repo(r)
    conn = db.init_db(tmp_path / "bb.db")
    try:
        run_index(conn, r)
        wt = git_ops.add_worktree(r, "agent-break", base)
        # base.core: return 1 -> 2. test_base (asserts 2) passes; test_mid
        # (asserts use()==1, now 2) fails -- the transitively affected test.
        (wt / "base.py").write_text("def core():\n    return 2\n", encoding="utf-8")
        git_ops.commit_all(wt, "agent-break: core returns 2")

        result = integrator.integrate(
            conn, r, "agent-break", base_commit=base, agent_id="agent-break"
        )
        assert result.status == "merge_rejected"
        assert result.reason == "impact tests failed"
        assert "test_mid" in result.test_log  # the affected test really ran
    finally:
        conn.close()


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


# --- R3 P1-4: the test gate must not run unbounded under the global lock -----------


def test_run_impact_tests_kills_a_hanging_gate_and_rejects(tmp_path, monkeypatch):
    """A branch whose tests never terminate must be REJECTED, not wedge the system.

    The gate executes just-merged, agent-authored test code while
    `app.post_integrate` holds the ONE global `integrate_lock`, so before the timeout
    a single infinite loop in any agent's branch queued every other agent's
    /integrate behind it forever -- no cancellation, no recovery short of a restart,
    and trunk left carrying the un-gated merge commit.

    This test HANGS FOREVER if the timeout is removed, so it is its own proof.
    """
    repo = tmp_path / "hangrepo"
    (repo / "tests").mkdir(parents=True)
    (repo / "mod_a.py").write_text("def helper(x):\n    return x + 1\n", encoding="utf-8")
    (repo / "tests" / "test_hang.py").write_text(
        "import time\n\n\ndef test_hangs():\n    time.sleep(600)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SWARMSYNC_GATE_TIMEOUT", "2")

    started = time.monotonic()
    ok, log = integrator.run_impact_tests(repo, ["mod_a.py"])
    elapsed = time.monotonic() - started

    assert ok is False, "a non-terminating gate must be a rejection, not a pass"
    assert "exceeded" in log
    assert elapsed < 60, f"gate was not killed at its timeout (took {elapsed:.1f}s)"


def test_gate_timeout_is_configurable_and_falls_back_to_the_default():
    """Operators must be able to raise the ceiling for a genuinely slow suite, and a
    junk value must not silently disable the gate's only protection."""
    assert integrator._gate_timeout() == integrator.DEFAULT_GATE_TIMEOUT_SECONDS
    for junk in ("not-a-number", "0", "-5", ""):
        os.environ["SWARMSYNC_GATE_TIMEOUT"] = junk
        try:
            assert integrator._gate_timeout() == integrator.DEFAULT_GATE_TIMEOUT_SECONDS
        finally:
            del os.environ["SWARMSYNC_GATE_TIMEOUT"]
    os.environ["SWARMSYNC_GATE_TIMEOUT"] = "12.5"
    try:
        assert integrator._gate_timeout() == 12.5
    finally:
        del os.environ["SWARMSYNC_GATE_TIMEOUT"]


# --- R3 P1-5: rolling back trunk must roll back the blackboard too -----------------


def test_post_reindex_failure_rolls_the_blackboard_back_with_trunk(conn, repo, monkeypatch):
    """A merge rejected AFTER re-indexing must not leave the blackboard describing
    the code that was thrown away.

    Step 4 calls `run_index`, which COMMITS its own transaction (SQLite has one
    transaction per connection and no nesting), so by the time anything after it
    fails -- the `regenerate_summary`/UPDATE loop, the contract-diff SELECT, or a
    transient `database is locked` from another writer -- those rows are already
    persisted from a merge that is about to be reset out. The S1 atomicity guard
    reset trunk with `git reset --hard` and stopped there.

    That is not cosmetic: `_check_read_deps` (step 1) compares other agents'
    plan-time snapshots against exactly these columns, so a phantom hash spuriously
    bounces innocent agents with `needs_rebase` and clears an agent that
    re-snapshotted to merge against state that never landed.
    """
    r, base = repo
    run_index(conn, r)

    def _hash_of(parcel_id):
        row = conn.execute(
            "SELECT content_hash FROM parcels WHERE id = ?", (parcel_id,)
        ).fetchone()
        return row["content_hash"] if row else None

    before = _hash_of("mod_a.py::helper")
    assert before is not None
    pre_head = git_ops.current_commit(r, ref="integration")

    # A branch that merges cleanly and PASSES the gate, so we reach step 4. The
    # edit must change helper's real content_hash (a comment-only change does not --
    # the hash is computed over parsed source, so a trailing comment is invisible to
    # it and would make this test pass for the wrong reason). `tests/test_a.py` is
    # value-agnostic on purpose, so an arithmetic tweak still passes the gate.
    worktree = git_ops.add_worktree(r, "agent-x", base)
    (worktree / "mod_a.py").write_text(
        "def helper(x):\n    return x + 999\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree, "agent-x: arithmetic tweak the gate accepts")

    # ...then blow up AFTER run_index has already committed the new hashes.
    def boom(*a, **kw):
        raise RuntimeError("simulated post-reindex failure")

    monkeypatch.setattr(integrator, "regenerate_summary", boom)

    result = integrator.integrate(conn, r, "agent-x", base_commit=base, agent_id="agent-x")
    assert result.status == "merge_rejected"

    # git rolled back...
    assert git_ops.current_commit(r, ref="integration") == pre_head
    assert "999" not in (r / "mod_a.py").read_text(encoding="utf-8")

    # ...and so must the blackboard, which is the projection every agent reads.
    assert _hash_of("mod_a.py::helper") == before, (
        "blackboard still carries the REJECTED merge's content_hash while trunk has "
        "been reset: agents now plan against a state that never landed"
    )
