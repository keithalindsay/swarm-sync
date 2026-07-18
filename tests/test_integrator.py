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


def test_gate_timeout_is_bounded_even_when_a_descendant_escapes_the_group_kill(
    tmp_path, monkeypatch
):
    """The timeout must bound the gate even when the killpg does NOT reach everything.

    R4 found the hole in R3's own timeout fix: `communicate()` waits for EOF on the
    pipes, not for the direct child to die, and EOF only comes when every holder of
    the write end exits. A grandchild that re-`setsid`s is in its own process group,
    survives the group kill, and keeps the inherited pipe open -- so the unbounded
    drain blocked for that descendant's whole lifetime while `post_integrate` held
    the global `integrate_lock`. That is the permanent wedge the timeout exists to
    prevent, reinstated by the fix's own cleanup path.

    `addopts = -s` turns pytest's fd-capture off so the gate's real pipe reaches the
    grandchild -- a setting the merged, agent-authored repo controls.
    """
    repo = tmp_path / "escaperepo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -s\n", encoding="utf-8")
    (repo / "mod_a.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")
    (repo / "tests" / "test_daemon.py").write_text(
        "import subprocess, time\n\n\n"
        "def test_starts_a_background_service():\n"
        "    subprocess.Popen(['sleep', '120'], start_new_session=True)\n"
        "    time.sleep(120)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SWARMSYNC_GATE_TIMEOUT", "2")

    started = time.monotonic()
    ok, log = integrator.run_impact_tests(repo, ["mod_a.py"])
    elapsed = time.monotonic() - started

    assert ok is False
    assert "exceeded" in log
    # 2s gate + a bounded drain. Without the drain bound this blocks ~120s on the
    # escaped `sleep`, holding the global integrate lock the whole time.
    assert elapsed < 30, (
        f"gate took {elapsed:.1f}s: a descendant that escaped the group kill still "
        f"wedges the gate (and the global integrate_lock) on the drain"
    )


def test_timed_out_gate_is_actually_killed_not_just_abandoned(tmp_path, monkeypatch):
    """The timeout must KILL the gate's process tree, not merely stop waiting on it.

    R4's mutation dimension replaced `_kill_process_group(proc)` with `pass` and the
    whole suite stayed green: the sibling test asserts only the (ok, log) verdict and
    the elapsed bound, and the bounded drain added alongside the kill MASKS its
    deletion -- with no kill, `communicate(timeout=...)` simply expires, `_close_streams`
    runs, and the function still returns False, so every assertion still held while a
    runaway pytest tree survived. Each timed-out /integrate would then permanently leak
    a process running the agent-authored branch's arbitrary test code, reparented to
    init, holding the repo and burning CPU forever.

    So assert the thing that actually matters: after the timeout, the gate's process
    group is gone.
    """
    repo = tmp_path / "killrepo"
    (repo / "tests").mkdir(parents=True)
    (repo / "mod_a.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")
    (repo / "tests" / "test_hang.py").write_text(
        "import time\n\n\ndef test_hangs():\n    time.sleep(120)\n", encoding="utf-8"
    )
    monkeypatch.setenv("SWARMSYNC_GATE_TIMEOUT", "2")

    spawned: list = []
    real_popen = integrator.subprocess.Popen

    def recording_popen(*a, **kw):
        proc = real_popen(*a, **kw)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(integrator.subprocess, "Popen", recording_popen)

    ok, log = integrator.run_impact_tests(repo, ["mod_a.py"])
    assert ok is False
    assert spawned, "no gate process was spawned"

    proc = spawned[0]
    # The direct child must be dead...
    assert proc.poll() is not None, "the timed-out gate process is still running"
    # ...and so must its whole group: `os.killpg(pgid, 0)` raises once the group is
    # gone. (`start_new_session=True` makes the child its own group leader, so its
    # pid IS the pgid.)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except (ProcessLookupError, PermissionError):
            break
        time.sleep(0.1)
    else:
        raise AssertionError(
            "the gate's process group survived the timeout: the runaway test tree is "
            "still running and will never be reaped"
        )


def test_gate_runs_with_its_containment_flags(tmp_path, monkeypatch):
    """The gate's sandbox flags must actually reach the subprocess.

    R4's mutation dimension deleted PYTEST_DISABLE_PLUGIN_AUTOLOAD, `-p no:cacheprovider`
    and `--import-mode=importlib` -- each left the suite green, because nothing asserted
    the gate's command line or environment at all.

    Scoped honestly: the gate runs the merged branch's code BY DESIGN (README says so),
    so these are defense-in-depth narrowing WHAT it does -- not a security boundary.
    But they are silently droppable in a refactor, and then the gate auto-loads
    third-party plugins and conftest side effects from $PATH while running an untrusted
    branch, and prepend-import mutates the parent's sys.path.
    """
    repo = tmp_path / "flagrepo"
    (repo / "tests").mkdir(parents=True)
    (repo / "mod_a.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")
    (repo / "tests" / "test_a.py").write_text(
        "from mod_a import helper\n\n\ndef test_h():\n    assert helper(1) == 1\n",
        encoding="utf-8",
    )

    seen: dict = {}
    real_popen = integrator.subprocess.Popen

    def recording_popen(cmd, *a, **kw):
        seen["cmd"] = list(cmd)
        seen["env"] = dict(kw.get("env") or {})
        seen["new_session"] = kw.get("start_new_session")
        return real_popen(cmd, *a, **kw)

    monkeypatch.setattr(integrator.subprocess, "Popen", recording_popen)
    integrator.run_impact_tests(repo, ["mod_a.py"])

    assert seen["env"].get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1", (
        "gate would auto-load third-party pytest plugins while running an untrusted branch"
    )
    assert "no:cacheprovider" in seen["cmd"], "gate would write .pytest_cache into the repo"
    assert "--import-mode=importlib" in seen["cmd"], (
        "prepend import-mode lets the branch mutate the parent's sys.path/module namespace"
    )
    # start_new_session is what makes the timeout's process-GROUP kill possible.
    assert seen["new_session"] is True, (
        "without start_new_session the gate shares our process group: the timeout's "
        "killpg would target our own group, and a runaway tree could not be killed"
    )


def test_kill_process_group_never_signals_our_own_group(tmp_path, monkeypatch):
    """A gate timeout must never SIGKILL the server itself.

    `_kill_process_group` SIGKILLs the group `getpgid(child)` reports. That is safe
    only while the gate is spawned with `start_new_session=True` so it LEADS its own
    group. If that stops holding, the child is in OUR group and the killpg takes the
    coordinator down with it -- the server dies to reap a hanging test.

    Not hypothetical: a mutation run that flipped `start_new_session` to False had its
    own harness SIGKILLed by this code path.
    """
    # A child deliberately spawned in OUR process group (start_new_session absent).
    proc = integrator.subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=integrator.subprocess.PIPE,
        stderr=integrator.subprocess.PIPE,
    )
    assert os.getpgid(proc.pid) == os.getpgrp(), "setup: child should share our group"

    killed_groups: list = []
    real_killpg = os.killpg
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed_groups.append(pgid))

    try:
        integrator._kill_process_group(proc)
        assert os.getpgrp() not in killed_groups, (
            "_kill_process_group signalled OUR OWN process group: a gate timeout would "
            "SIGKILL the coordinator"
        )
    finally:
        monkeypatch.setattr(os, "killpg", real_killpg)
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

    # ...and the child is still killed directly, so the timeout still does its job.
    assert proc.poll() is not None


# --- R5 P0: a crash mid-integrate must not leave an un-gated merge on trunk --------


def test_integrate_records_its_intent_before_touching_trunk(conn, repo):
    """`integrate_started` must be durable BEFORE the merge, carrying the rollback sha.

    integrate merges to trunk and learns the verdict afterwards, so trunk carries an
    un-gated merge for as long as the gate runs (up to 600s). Without a durable record
    of that window, a SIGKILL in it is undetectable after the fact -- there is nothing
    that says a merge was ever provisional, or what to roll back to.
    """
    r, base = repo
    run_index(conn, r)
    pre = git_ops.current_commit(r, ref="integration")

    worktree = git_ops.add_worktree(r, "agent-i", base)
    (worktree / "mod_a.py").write_text(
        "def helper(x):\n    return x + 5\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree, "agent-i: edit")
    result = integrator.integrate(conn, r, "agent-i", base_commit=base, agent_id="agent-i")
    assert result.status == "merged"

    events = events_mod.tail(conn, since_seq=0)
    starts = [e for e in events if e.type == "integrate_started"]
    assert len(starts) == 1, "no durable record of the un-gated window"
    payload = json.loads(starts[0].payload)
    assert payload["trunk_sha_before"] == pre, "the recorded rollback point is wrong"
    assert payload["branch"] == "agent-i"

    # ...and the start is ordered BEFORE the verdict, or it proves nothing.
    types = [e.type for e in events]
    assert types.index("integrate_started") < types.index("merged")


def test_reconcile_rolls_trunk_back_out_of_an_orphaned_integrate(conn, repo):
    """A crash between the merge and the verdict must be undone at startup.

    Simulates exactly what SIGKILL/OOM leaves behind: an `integrate_started` event, a
    merge commit on trunk, and NO terminal event. R4's verifiers reproduced this
    against a real server with kill -9 -- trunk permanently carried the un-gated merge
    and nothing on restart could tell.
    """
    r, base = repo
    run_index(conn, r)
    pre = git_ops.current_commit(r, ref="integration")

    # Hand-build the orphan: intent recorded, merge landed, process died.
    worktree = git_ops.add_worktree(r, "agent-dead", base)
    (worktree / "mod_a.py").write_text(
        "def helper(x):\n    return 'never gated'\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree, "agent-dead: un-gated edit")
    events_mod.emit(
        conn,
        "integrate_started",
        "agent-dead",
        {
            "branch": "agent-dead",
            "into": "integration",
            "base_commit": base,
            "trunk_sha_before": pre,
            "repo": str(r),
        },
    )
    ok, _conflicts = git_ops.merge_branch(r, "agent-dead", into="integration")
    assert ok
    assert git_ops.current_commit(r, ref="integration") != pre
    assert "never gated" in (r / "mod_a.py").read_text(encoding="utf-8")

    reconciled = integrator.reconcile_orphaned_integrations(conn)

    assert len(reconciled) == 1
    assert git_ops.current_commit(r, ref="integration") == pre, (
        "the un-gated merge is STILL on trunk after reconciliation"
    )
    assert "never gated" not in (r / "mod_a.py").read_text(encoding="utf-8")
    orphaned = [e for e in events_mod.tail(conn, since_seq=0) if e.type == "integrate_orphaned"]
    assert len(orphaned) == 1, "the rollback left no audit trail"


def test_reconcile_does_not_destroy_landed_merges_on_a_second_restart(conn, repo):
    """Reconciliation must be IDEMPOTENT: once an orphan is rolled back and recorded,
    a later restart must not re-roll it and wipe the gated merges that landed since.

    This is finding C1. `integrate_orphaned` -- the event reconciliation emits to close
    an orphan -- was NOT in the terminal-event set, so the orphaned `integrate_started`
    stayed "open" forever. On any subsequent restart, replay found the same start
    unclosed, saw trunk had moved on, and `git reset --hard` back to the pre-orphan sha,
    DESTROYING every legitimate merge landed in between -- and it repeated every restart.
    """
    r, base = repo
    run_index(conn, r)
    pre = git_ops.current_commit(r, ref="integration")

    # --- step 1: an integrate is SIGKILLed mid-gate: intent recorded, merge landed,
    # no terminal event.
    worktree = git_ops.add_worktree(r, "agent-dead", base)
    (worktree / "mod_a.py").write_text(
        "def helper(x):\n    return 'never gated'\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree, "agent-dead: un-gated edit")
    events_mod.emit(
        conn,
        "integrate_started",
        "agent-dead",
        {
            "branch": "agent-dead",
            "into": "integration",
            "base_commit": base,
            "trunk_sha_before": pre,
            "repo": str(r),
        },
    )
    ok, _conflicts = git_ops.merge_branch(r, "agent-dead", into="integration")
    assert ok
    assert git_ops.current_commit(r, ref="integration") != pre

    # --- step 2: first restart. Reconciliation correctly rolls trunk back to `pre`.
    first = integrator.reconcile_orphaned_integrations(conn)
    assert len(first) == 1
    assert git_ops.current_commit(r, ref="integration") == pre

    # --- step 3: agents land legitimate, gated merges. Trunk moves forward.
    worktree_b = git_ops.add_worktree(r, "agent-b", base)
    (worktree_b / "mod_b.py").write_text(
        "def other(y):\n    z = y * 2\n    return z\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree_b, "agent-b: gated edit")
    assert integrator.integrate(
        conn, r, "agent-b", base_commit=base, agent_id="agent-b"
    ).status == "merged"

    worktree_c = git_ops.add_worktree(r, "agent-c", base)
    (worktree_c / "mod_c.py").write_text(
        "def broken():\n    result = 1\n    return result\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree_c, "agent-c: gated edit (behavior-preserving)")
    assert integrator.integrate(
        conn, r, "agent-c", base_commit=base, agent_id="agent-c"
    ).status == "merged"

    trunk_after_merges = git_ops.current_commit(r, ref="integration")
    assert trunk_after_merges != pre, "the legitimate merges did not land"

    # --- step 4: second restart, for any reason. Reconciliation must leave trunk alone.
    second = integrator.reconcile_orphaned_integrations(conn)

    assert git_ops.current_commit(r, ref="integration") == trunk_after_merges, (
        "second reconciliation reset trunk back out from under the gated merges -- "
        "the C1 double-restart data-loss bug"
    )
    assert second == [], (
        "the already-reconciled orphan was reconciled AGAIN on the second restart"
    )
    assert "z = y * 2" in (r / "mod_b.py").read_text(encoding="utf-8")
    assert "result = 1" in (r / "mod_c.py").read_text(encoding="utf-8")


def test_reconcile_leaves_completed_integrates_alone(conn, repo):
    """Reconciliation must never touch an integrate that reached a verdict -- a
    `merged` merge is trunk's real history, and resetting it would be the very data
    loss this is meant to prevent."""
    r, base = repo
    run_index(conn, r)

    worktree = git_ops.add_worktree(r, "agent-ok", base)
    (worktree / "mod_a.py").write_text(
        "def helper(x):\n    return x + 3\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree, "agent-ok: edit")
    assert integrator.integrate(
        conn, r, "agent-ok", base_commit=base, agent_id="agent-ok"
    ).status == "merged"
    landed = git_ops.current_commit(r, ref="integration")

    reconciled = integrator.reconcile_orphaned_integrations(conn)

    assert reconciled == [], "reconciliation tried to undo a completed integrate"
    assert git_ops.current_commit(r, ref="integration") == landed
    assert "x + 3" in (r / "mod_a.py").read_text(encoding="utf-8")


def test_reconcile_is_a_noop_when_the_crash_beat_the_merge(conn, repo):
    """If the process died before the merge landed, trunk is already correct and
    reconciliation must not rewrite history it did not cause."""
    r, base = repo
    run_index(conn, r)
    pre = git_ops.current_commit(r, ref="integration")

    events_mod.emit(
        conn,
        "integrate_started",
        "agent-early",
        {
            "branch": "agent-early",
            "into": "integration",
            "base_commit": base,
            "trunk_sha_before": pre,
            "repo": str(r),
        },
    )

    reconciled = integrator.reconcile_orphaned_integrations(conn)

    assert len(reconciled) == 1
    assert "no-op" in reconciled[0]["action"]
    assert git_ops.current_commit(r, ref="integration") == pre


def test_reconcile_never_raises_on_a_repo_that_is_gone(conn, tmp_path):
    """A vanished/moved repo must not stop the server booting."""
    events_mod.emit(
        conn,
        "integrate_started",
        "agent-x",
        {
            "branch": "b",
            "into": "integration",
            "trunk_sha_before": "0" * 40,
            "repo": str(tmp_path / "does-not-exist"),
        },
    )
    reconciled = integrator.reconcile_orphaned_integrations(conn)
    assert len(reconciled) == 1
    assert reconciled[0]["error"] is not None
    assert reconciled[0]["action"] == "FAILED"


@pytest.mark.parametrize(
    "rejection",
    ["merge_conflict", "gate_red", "integration_error"],
    ids=["conflict", "red-gate", "post-merge-error"],
)
def test_every_rejection_route_leaves_the_blackboard_matching_trunk(
    conn, repo, monkeypatch, rejection
):
    """No rejection route may leave the blackboard describing code that isn't on trunk.

    R3 fixed this for the routes that go through `_reject_and_reset`. R4 found the
    merge-CONFLICT path hand-rolled its own emit+return and therefore skipped the
    re-index -- so `runner.py`'s pre-integrate `/parcel/update` self-report survived a
    rejected conflict, leaving the blackboard holding a `content_hash` for a version of
    the file that exists in NO git ref. That is R3's own fix, applied to the path it
    happened to be looking at and not to its sibling.

    So this asserts the CLASS: parametrised over every way integrate can reject. Adding
    a new rejection reason without routing it through the compensation fails here.
    """
    r, base = repo
    run_index(conn, r)

    def _hash_of(pid):
        row = conn.execute("SELECT content_hash FROM parcels WHERE id = ?", (pid,)).fetchone()
        return row["content_hash"] if row else None

    truth = _hash_of("mod_a.py::helper")
    assert truth is not None

    if rejection == "merge_conflict":
        # Land one edit, then fork a conflicting one from the original base.
        wt_a = git_ops.add_worktree(r, "agent-a", base)
        (wt_a / "mod_a.py").write_text("def helper(x):\n    return x + 111\n", encoding="utf-8")
        git_ops.commit_all(wt_a, "agent-a: change helper")
        assert integrator.integrate(
            conn, r, "agent-a", base_commit=base, agent_id="agent-a"
        ).status == "merged"
        truth = _hash_of("mod_a.py::helper")  # trunk moved; this is the new truth

        wt = git_ops.add_worktree(r, "agent-b", base)
        (wt / "mod_a.py").write_text("def helper(x):\n    return x + 222\n", encoding="utf-8")
        git_ops.commit_all(wt, "agent-b: conflicting change")
        branch = "agent-b"
    elif rejection == "gate_red":
        wt = git_ops.add_worktree(r, "agent-c", base)
        (wt / "mod_c.py").write_text(
            "def broken():\n    raise RuntimeError('boom')\n", encoding="utf-8"
        )
        git_ops.commit_all(wt, "agent-c: break the test")
        branch = "agent-c"
    else:
        wt = git_ops.add_worktree(r, "agent-d", base)
        (wt / "mod_a.py").write_text("def helper(x):\n    return x + 999\n", encoding="utf-8")
        git_ops.commit_all(wt, "agent-d: fine edit that blows up post-merge")
        monkeypatch.setattr(
            integrator, "regenerate_summary", lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("simulated post-reindex failure")
            )
        )
        branch = "agent-d"

    # The agent's premature self-report: runner.py posts /parcel/update BEFORE
    # integrate, so a hash for code that may never land is already in the blackboard.
    conn.execute(
        "UPDATE parcels SET content_hash = 'agent-self-reported-never-landed' WHERE id = ?",
        ("mod_a.py::helper",),
    )

    result = integrator.integrate(conn, r, branch, base_commit=base, agent_id=branch)
    assert result.status == "merge_rejected", f"expected a rejection for {rejection}"

    assert _hash_of("mod_a.py::helper") == truth, (
        f"after a {rejection} rejection the blackboard still holds a hash for code that "
        f"is not on trunk: agents now plan against a state that never landed"
    )


# --- A8: a KeyboardInterrupt mid-gate must roll trunk back AND re-raise -------------


def test_keyboard_interrupt_mid_gate_rolls_trunk_back_and_reraises(conn, repo, monkeypatch):
    """An operator Ctrl-C (or uvicorn shutdown) during the gate must not leave an
    un-gated merge on trunk.

    `integrate` merges to trunk FIRST, then runs the pytest gate (up to 600s) to learn
    the verdict. A `KeyboardInterrupt`/`SystemExit` raised in that window is NOT an
    `Exception`, so it slips past the ordinary `except Exception` post-merge handler --
    the un-gated merge is already sitting on trunk. The `except BaseException` guard is
    the ONLY thing that (a) resets trunk back to the pre-merge sha and (b) re-raises the
    interrupt (it is the operator's to act on, not ours to swallow).

    This pins BOTH halves: swallow it and the interrupt is lost; catch it with a bare
    `except Exception` and the un-gated merge stays on trunk.
    """
    r, base = repo
    run_index(conn, r)
    pre = git_ops.current_commit(r, ref="integration")

    worktree = git_ops.add_worktree(r, "agent-int", base)
    (worktree / "mod_a.py").write_text(
        "def helper(x):\n    return 'un-gated, interrupted mid-merge'\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree, "agent-int: edit interrupted during the gate")

    # The merge lands on trunk, THEN the gate is entered -- simulate Ctrl-C there.
    def interrupt(*a, **kw):
        raise KeyboardInterrupt("operator hit Ctrl-C during the gate")

    monkeypatch.setattr(integrator, "run_impact_tests", interrupt)

    # (b) the interrupt propagates -- it is NOT swallowed.
    with pytest.raises(KeyboardInterrupt):
        integrator.integrate(conn, r, "agent-int", base_commit=base, agent_id="agent-int")

    # (a) trunk was reset to the exact pre-merge sha -- no un-gated merge survives.
    assert git_ops.current_commit(r, ref="integration") == pre, (
        "trunk still carries the un-gated merge after the interrupt was raised mid-gate"
    )
    assert "un-gated, interrupted" not in (r / "mod_a.py").read_text(encoding="utf-8")
