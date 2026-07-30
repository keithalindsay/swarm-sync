"""H1 / H4 / H7 at realistic scale: does trunk actually stay green, do merges
actually serialize, and do worktrees actually not leak -- on a real 10k-line repo
with a genuinely transitive import graph, rather than on a 96-line fixture.

One wave, three tasks, all in distinct files (so all three are co-schedulable and
dispatch concurrently at file granularity):

  * two `edit_function_body` tasks that are semantics-PRESERVING (verified: the
    full 252-test suite is green with both applied), and
  * one `break_a_test` on `codelearner/retrieve/lexical.py::escape_fts_query`, a
    module with REAL dependents -- five sibling modules import from it and
    `gate._reverse_dep_files` reaches 7 of 11 test files from it. The dependency
    is asserted from the graph in `test_the_break_target_has_real_dependents`,
    not assumed.

RESULT UP FRONT: H1's WEAK form holds -- the broken branch is refused and trunk
ends green. H1's STRONG form ("it never lands at all") is **FALSIFIED**. The
assertion was written as the plan asked, so that it would fail on a
land-then-revert; it failed. `integrator.integrate` merges to trunk BEFORE it
runs the gate, so trunk's ref AND its working tree carry the un-gated broken
merge for the whole duration of the gate, and the reflog records it. See
`test_h1_strong_form_is_falsified_the_broken_merge_lands_then_reverts` for the
evidence and the exact scope of the claim.

The evidence is guarded against being vacuous in both directions
(`test_the_reflog_evidence_is_not_vacuous`: positive control, negative control,
and proof the mutator produced the poison at all), and the same window is shown
to be reachable from a PASSING gate too
(`test_the_ungated_window_is_reachable_from_a_passing_gate_too`), so the finding
is about merge-then-verify rather than about one verdict.

Run with `-s` to see the measured summary (setup wall-clock, peak concurrent
worktrees, events by type) that the report quotes.
"""
from __future__ import annotations

import collections
from pathlib import Path

import pytest

from swarmsync.coordinator import gate, integrator
from tests.scale import harness

BENIGN_A, BENIGN_B = harness.BENIGN_EDITS
BREAK = harness.BREAK_TARGET

TASK_BENIGN_A = "scale-benign-content-hash"
TASK_BENIGN_B = "scale-benign-facts-only"
TASK_BREAK = "scale-break-escape-fts-query"


@pytest.fixture(scope="module")
def scale():
    """The expensive setup, ONCE for this module: clone + blackboard + index.

    Module-scoped on purpose -- a clone and a 567-parcel index per test would be
    pure waste. Measured at ~0.3s for setup; the cost in this file is entirely in
    the gate's pytest runs, not here.
    """
    with harness.scale_blackboard() as sr:
        yield sr


@pytest.fixture(scope="module")
def wave(scale):
    """Dispatch the one wave and hand back `(ScaleRepo, results)`.

    Also snapshots the event log and the repo's post-run worktree state BEFORE
    anything else can perturb them, and prints the measured summary on teardown.
    """
    tasks = [
        harness.edit_task(TASK_BENIGN_A, base_commit=scale.base_commit, **BENIGN_A),
        harness.edit_task(TASK_BENIGN_B, base_commit=scale.base_commit, **BENIGN_B),
        harness.break_task(
            TASK_BREAK,
            path=BREAK["path"],
            symbol=BREAK["symbol"],
            message=harness.BREAK_SENTINEL,
            base_commit=scale.base_commit,
        ),
    ]
    # `watch=` observes the TRUNK checkout, read-only, for the poison the broken
    # task writes -- that is the evidence for the H1 strong-form result.
    results = scale.run(
        tasks, n_agents=3, watch=(BREAK["path"], harness.BREAK_SENTINEL)
    )
    yield scale, results
    counts = scale.events_by_type()
    print(
        "\n--- scale summary (H1/H4/H7) ---"
        f"\nsetup wall-clock:          {scale.setup_seconds:.2f}s"
        f"\nparcels / contracts:       {scale.parcel_count} / {scale.contract_count}"
        f"\npeak concurrent worktrees: {scale.peak_worktrees}"
        f"\nintegrate spans:           {len(harness.integrate_spans(scale.events()))}"
        f"\nun-gated trunk window:     {scale.watch_window_seconds:.1f}s "
        f"({len(scale.watch_hits)} samples saw the broken code ON TRUNK)"
        f"\nlease_denied events:       {counts.get('lease_denied', 0)}"
        f"\nmerge_rejected events:     {counts.get('merge_rejected', 0)}"
        f"\nevents by type:            {dict(sorted(counts.items()))}"
        f"\ntotal events:              {sum(counts.values())}"
    )


# --- preconditions: we are really testing the real repo, at real scale -------------


def test_the_index_is_the_real_repo_at_the_scale_the_plan_claims(scale):
    """The premise of every other assertion here. Also records where the execution
    plan's stated numbers are STALE: it says 380 parcels / 66 contracts; the real
    index is 567 / 86 over 45 files. Reported, not quietly accommodated."""
    assert scale.parcel_count == harness.MEASURED_PARCELS
    assert scale.contract_count == harness.MEASURED_CONTRACTS
    assert scale.parcel_count != harness.PLAN_STATED_PARCELS  # the plan is stale
    paths = {
        row["path"]
        for row in scale.conn.execute("SELECT DISTINCT path FROM parcels").fetchall()
    }
    assert len(paths) == harness.MEASURED_FILES
    source = {p for p in paths if p.startswith("codelearner/")}
    tests_ = {p for p in paths if p.startswith("tests/")}
    assert len(source) == harness.MEASURED_SOURCE_MODULES
    assert len(tests_) == harness.MEASURED_TEST_FILES


def test_the_break_target_has_real_dependents(scale):
    """VERIFY the dependency rather than assuming it (the plan is explicit about
    this). Two independent checks:

      1. the classifier's own graph shows sibling modules importing the target's
         file, and
      2. `gate._reverse_dep_files` -- the exact function impact selection uses --
         reaches real TEST files from it, including ones whose source never names
         the changed symbol (indirect dependents).

    Without this, a `break_a_test` on a leaf module would make H1 pass for the
    wrong reason: nothing would exercise it, the gate would go green, and the
    rejection we assert would never even be tested.
    """
    target_file = BREAK["path"]
    affected = gate._reverse_dep_files(Path(scale.root), {target_file})

    source_dependents = sorted(
        f for f in affected if f.startswith("codelearner/") and f != target_file
    )
    test_dependents = sorted(f for f in affected if f.startswith("tests/"))

    assert len(source_dependents) >= 4, source_dependents
    for sibling in ("codelearner/retrieve/search.py", "codelearner/retrieve/fuse.py"):
        assert sibling in source_dependents, (sibling, source_dependents)
    # The direct import edge, read off the source rather than the graph.
    assert "from .lexical import" in scale.file_text("codelearner/retrieve/search.py")

    assert len(test_dependents) >= 5, test_dependents
    # An INDIRECT dependent: test_retrieve.py never mentions `escape_fts_query`,
    # yet it breaks when it raises -- that is what impact selection has to see.
    assert "tests/test_retrieve.py" in test_dependents
    assert BREAK["symbol"] not in scale.file_text("tests/test_retrieve.py")


# --- H1: trunk stays green ---------------------------------------------------------


def test_h1_the_broken_task_comes_back_rejected(wave):
    """The gate must reject the poisoned branch, and say why."""
    _scale, results = wave
    broken = results[TASK_BREAK]

    # The RUN succeeded (the agent did its job); the MERGE is what was refused.
    assert broken.status == "done", broken
    assert broken.integrate_result is not None
    assert broken.integrate_result["status"] == "merge_rejected", broken.integrate_result
    reason = broken.integrate_result.get("reason") or ""
    assert "impact tests failed" in reason, reason
    # The rejection came from the pytest gate seeing real failures, not from a
    # merge conflict or an environment error masquerading as one.
    log = broken.integrate_result.get("test_log") or ""
    assert "failed" in log.lower() or "error" in log.lower(), log[-2000:]
    assert harness.BREAK_SENTINEL in log, (
        "the gate's log does not mention the injected RuntimeError, so the "
        "rejection may not be the failure we injected: " + log[-2000:]
    )


def test_h1_the_benign_tasks_landed(wave):
    """The wave has to be non-vacuous: if the gate rejected EVERYTHING (e.g. for
    an environment reason), 'trunk stayed green' would be trivially true and would
    prove nothing. Both benign edits must be on trunk."""
    scale, results = wave
    for task_id in (TASK_BENIGN_A, TASK_BENIGN_B):
        result = results[task_id]
        assert result.status == "done", result
        assert result.integrate_result["status"] == "merged", result.integrate_result

    assert "digest = hashlib.sha256(source)" in scale.file_text(BENIGN_A["path"])
    assert "kept = [hit for hit in hits" in scale.file_text(BENIGN_B["path"])
    # ...and both landed as real merge commits on trunk (`--no-ff`, so each is
    # its own commit), while the refused one is absent from trunk's history.
    subjects = scale.trunk_log()
    merges = [line for line in subjects if f"into {harness.TRUNK}" in line]
    assert len(merges) == 2, subjects
    for task_id in (TASK_BENIGN_A, TASK_BENIGN_B):
        assert any(task_id in line for line in merges), (task_id, merges)
    assert not any(TASK_BREAK in line for line in subjects), subjects


def test_h1_code_learners_own_suite_is_green_on_trunk(wave):
    """The headline. code-learner's OWN 252-test suite, run with code-learner's
    3.12 venv (never swarm-sync's 3.11), on trunk as the broker left it."""
    scale, _results = wave
    assert harness.BREAK_SENTINEL not in scale.file_text(BREAK["path"])
    ok, log = scale.suite_green()
    assert ok, f"trunk is NOT green after the wave:\n{log}"


def test_h1_strong_form_is_falsified_the_broken_merge_lands_then_reverts(wave):
    """H1 STRONG FORM: **FALSIFIED**, and this test records the falsification.

    The plan asked for "not merely reverted -- it never lands at all", written so
    it fails if the change lands-then-reverts. It was written that way, and it
    failed. What actually happens, measured (see the report for numbers):

      * `integrator.integrate` merges to trunk FIRST and learns the verdict SECOND
        (step 2 then step 3 of its own docstring). So for the whole duration of the
        gate, `refs/heads/integration` points at a merge commit containing the
        broken code, and the trunk CHECKOUT ON DISK contains it too -- `merge_branch`
        does `git checkout integration` before merging.
      * The reflog proves it durably: exactly one entry
        (`merge <attempt>: Merge made by the 'ort' strategy`) carries the poison,
        followed by the `reset: moving to <pre-merge sha>` that undoes it.
      * A worktree cut from trunk during that window inherits the poison, which is
        why `in_head_commit` is asserted below and not just `in_worktree`.

    So the honest statement is: **landed then reverted, not never-landed.** The
    weak form of H1 holds (the branch is refused; trunk ends green -- the two tests
    above). The strong form does not, and it is not a bug in the sense of an
    accident: `open_integrations` + `reconcile_orphaned_integrations` exist
    precisely because this window is real and can outlive a crash. What it does
    contradict is the README's "so trunk is never poisoned" (§How it works) and its
    `trunk stays green` flow diagram. The guarantees TABLE is careful and correct
    ("a break is caught before it *survives* on trunk").

    This test now asserts the measured reality. If someone later makes the gate run
    BEFORE the merge, this test will fail -- which is the correct signal to update
    it, and the reason it is written as an equality on the hit count rather than a
    vague "at most one".
    """
    scale, _results = wave

    hits = scale.reflog_hits(harness.BREAK_SENTINEL, BREAK["path"])
    assert len(hits) == 1, (
        "expected exactly one poisoned trunk reflog entry (the un-gated merge that "
        f"the gate then rolled back); got {len(hits)}: {hits}"
    )
    poisoned_sha = hits[0]

    # `reflog()` is newest-first, so the entry that UNDID the poison sits at
    # index-1 relative to it. (It is not necessarily index 0: a benign task's merge
    # can land after the rejected one in the same wave.)
    entries = scale.reflog()
    index = next(i for i, e in enumerate(entries) if e["sha"] == poisoned_sha)
    assert "Merge made by" in entries[index]["message"], entries[index]
    assert index > 0, "the poisoned merge is trunk's current tip -- it never got rolled back"
    undo = entries[index - 1]
    assert undo["message"].startswith("reset: moving to"), undo
    assert undo["sha"] != poisoned_sha
    # Trunk's tip today carries no poison either way.
    assert harness.BREAK_SENTINEL not in (
        scale.blob_at(harness.TRUNK, BREAK["path"]) or ""
    )
    # The poisoned commit is not an ancestor of trunk -- it left no trace in history,
    # only in the reflog. That is the difference between "reverted" and "never landed".
    assert not scale.is_ancestor(poisoned_sha)

    # It was not merely a ref flicker: trunk's WORKING TREE really carried the
    # broken code, and so did trunk's HEAD COMMIT -- so any agent whose worktree
    # was cut from trunk in this window forked from the poison.
    assert scale.watch_hits, (
        "the trunk watcher never saw the poison on disk, so this test cannot say "
        "the window is observable -- investigate before trusting either outcome"
    )
    assert any(hit["in_head_commit"] for hit in scale.watch_hits), scale.watch_hits[:3]
    assert scale.watch_hits[0]["head_sha"] == poisoned_sha, (
        scale.watch_hits[0],
        poisoned_sha,
    )
    assert scale.watch_window_seconds > 1.0, scale.watch_window_seconds

    # ...and it really was cleaned up: no unresolved orphan is left behind, so the
    # window closed properly rather than stranding an un-gated merge.
    assert integrator.unresolved_orphan_count(scale.conn) == 0


def test_the_reflog_evidence_is_not_vacuous(wave):
    """MUTATION/CONTROL CHECK for the evidence the test above rests on.

    A `reflog_hits` that returned something for everything -- or nothing for
    everything -- would make the falsification meaningless in either direction. So:
    a positive control (content that demonstrably DID land is found), a negative
    control (content that never existed is not found), and confirmation that the
    mutator really produced the poison at all (it survives on the parked
    `rejected/*` branch, which is where WP3.5 says a refused attempt's commits stay
    reachable).
    """
    scale, _results = wave

    # Positive control: a benign edit that landed IS found in trunk's reflog.
    assert scale.reflog_hits("digest = hashlib.sha256(source)", BENIGN_A["path"])
    # Negative control: a string nothing ever wrote is NOT found.
    assert scale.reflog_hits("SWARMSYNC-NEVER-WRITTEN-a91f42", BREAK["path"]) == []
    # And the poison was genuinely produced by the mutator.
    parked = [b for b in scale.branches() if b.startswith("rejected/")]
    assert parked, scale.branches()
    assert [
        b
        for b in parked
        if harness.BREAK_SENTINEL in (scale.blob_at(b, BREAK["path"]) or "")
    ], f"no parked rejected/* branch carries the poison: {parked}"


def test_the_ungated_window_is_reachable_from_a_passing_gate_too(monkeypatch):
    """The un-gated window above is not specific to a red gate.

    Drives `integrate`'s OTHER rollback path: the gate passes, then the post-merge
    re-index raises, so `_reject_and_reset` rolls trunk back (`integration_error`).
    Same signature -- poisoned merge in the reflog, clean final state -- from a
    completely different failure. That matters for the report: the exposure is a
    property of merge-then-verify, not of the gate's verdict.

    Cheap (the gate is stubbed, so no pytest run) and on its own throwaway clone,
    since it poisons history.
    """
    with harness.scale_blackboard() as sr:
        monkeypatch.setattr(
            integrator, "run_impact_tests", lambda *a, **k: (True, "gate stubbed green")
        )
        real_run_index = integrator.run_index
        calls = {"n": 0}

        def failing_run_index(conn, repo, **kwargs):
            """Fail the FIRST (post-merge) re-index only. `_reject_and_reset`'s own
            compensating re-index must still work, or the rollback would report a
            second failure and muddy the scenario."""
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("scale-harness: injected post-merge failure")
            return real_run_index(conn, repo, **kwargs)

        monkeypatch.setattr(integrator, "run_index", failing_run_index)

        task = harness.break_task(
            "scale-land-then-revert",
            path=BREAK["path"],
            symbol=BREAK["symbol"],
            message=harness.BREAK_SENTINEL,
            base_commit=sr.base_commit,
        )
        results = sr.run([task], n_agents=1)
        outcome = results["scale-land-then-revert"].integrate_result
        assert outcome["status"] == "merge_rejected", outcome
        assert "post-merge integration failed" in (outcome.get("reason") or ""), outcome
        assert calls["n"] >= 1, "the injected post-merge failure never fired"

        # Final state clean...
        assert harness.BREAK_SENTINEL not in sr.file_text(BREAK["path"])
        assert sr.trunk_log()[0].endswith("swarm-sync scale harness: trunk base")
        # ...reflog says it was there. A final-state-only check would have missed it.
        assert sr.reflog_hits(harness.BREAK_SENTINEL, BREAK["path"])
        assert integrator.unresolved_orphan_count(sr.conn) == 0


# --- H4: merges serialize -----------------------------------------------------------


def test_h4_no_two_integrations_interleave(wave):
    """From the EVENT LOG ALONE (not from the absence of corruption): every
    `integrate_started` reaches its own terminal verdict before the next begins.

    Pairing is by the terminal event's `started_seq`, which is how
    `integrator.integrate` records it -- matching on branch/repo would let a
    reused attempt id attribute a verdict to the wrong start.
    """
    scale, _results = wave
    rows = scale.events()

    # THE rule, asserted first so a mutation that breaks serialization fails HERE
    # and not on some downstream consequence of it.
    assert harness.serialization_violations(rows) == []

    spans = harness.integrate_spans(rows)
    # Non-vacuous: three tasks, three integrates, from ONE concurrent wave.
    assert len(spans) == 3, spans
    assert all(span["end_seq"] is not None for span in spans), spans
    assert {span["end_type"] for span in spans} == {"merged", "merge_rejected"}, spans

    # ...and the wave really was concurrent, so serialization had something to do:
    # all three agents' worktrees existed at the same time.
    assert scale.peak_worktrees >= 2, (
        f"peak concurrent worktrees was {scale.peak_worktrees}: the wave ran "
        "serially, so 'merges serialize' was never actually under test"
    )


def test_h4_the_serialization_detector_is_not_vacuous():
    """MUTATION CHECK for the H4 detector: fed an interleaved log it must complain.

    A checker that returns `[]` for everything would make the assertion above pass
    forever. These are the two failure shapes it exists to catch.
    """
    def ev(seq, type_, **payload):
        return {"seq": seq, "type": type_, "agent_id": None, "data": payload, "ts": 0.0}

    serial = [
        ev(1, "integrate_started", branch="a"),
        ev(2, "merged", started_seq=1),
        ev(3, "integrate_started", branch="b"),
        ev(4, "merge_rejected", started_seq=3),
    ]
    assert harness.serialization_violations(serial) == []

    overlapping = [
        ev(1, "integrate_started", branch="a"),
        ev(2, "integrate_started", branch="b"),  # began while 1 was open
        ev(3, "merged", started_seq=1),
        ev(4, "merged", started_seq=2),
    ]
    problems = harness.serialization_violations(overlapping)
    assert problems and "still open" in problems[0], problems

    mispaired = [
        ev(1, "integrate_started", branch="a"),
        ev(2, "merged", started_seq=99),  # closes a start that is not the open one
    ]
    assert harness.serialization_violations(mispaired), "mis-pairing went unnoticed"


# --- H7: no worktree leaks ------------------------------------------------------------


def test_h7_no_worktrees_leak_after_the_run(wave):
    """After the wave -- one landed, one landed, one refused -- git must know about
    exactly one worktree (the trunk checkout) and `.worktrees/` must be empty.

    Branches are checked too, because that is where the leak would show up next:
    a landed agent's branch is deleted (its commits are in trunk's history), while
    the REFUSED attempt's branch is deliberately kept AND parked under
    `rejected/*` -- that is `_cleanup_worktree`'s documented contract (WP3.5), not
    a leak, since trunk was reset and the branch is the only reference left.
    """
    scale, _results = wave

    registered = scale.worktrees()
    assert registered == [str(scale.root)], registered
    assert scale.worktree_residue() == [], scale.worktree_residue()

    branches = scale.branches()
    landed_attempts = [
        b for b in branches if b.startswith((TASK_BENIGN_A, TASK_BENIGN_B))
    ]
    assert landed_attempts == [], (
        f"a landed agent's branch was not cleaned up: {landed_attempts}"
    )
    kept = [b for b in branches if b.startswith(TASK_BREAK)]
    parked = [b for b in branches if b.startswith("rejected/")]
    assert kept, "the refused attempt's branch was deleted -- its commits are lost"
    assert parked, "the refused attempt was not parked under rejected/*"


def test_h7_the_worktree_check_is_not_vacuous(scale):
    """MUTATION CHECK for the H7 assertion: `worktrees()`/`worktree_residue()` must
    actually see an extra worktree when one exists. Creates one, sees it, removes
    it, sees it gone -- so a green H7 means "none present", not "we cannot tell"."""
    from swarmsync.worktree import git_ops

    name = "scale-h7-probe"
    path = git_ops.add_worktree(scale.root, name)
    try:
        assert str(path) in scale.worktrees(), scale.worktrees()
        assert name in scale.worktree_residue(), scale.worktree_residue()
    finally:
        git_ops.remove_worktree(scale.root, name)
    assert scale.worktrees() == [str(scale.root)], scale.worktrees()
    assert scale.worktree_residue() == [], scale.worktree_residue()


def test_the_event_log_records_the_whole_run(wave):
    """A cheap completeness check on the log everything else is read from: the
    event types a three-task wave must produce are all present, and the counts are
    consistent with two landings and one refusal."""
    scale, _results = wave
    counts: collections.Counter = scale.events_by_type()

    for required in ("planned", "lease_granted", "done", "released", "integrate_started"):
        assert counts[required] >= 3, (required, dict(counts))
    assert counts["merged"] == 2, dict(counts)
    assert counts["merge_rejected"] == 1, dict(counts)
    assert counts["reindexed"] == 2, dict(counts)
    # No task should have had to retry: three distinct files, one wave.
    assert counts["lease_denied"] == 0, dict(counts)
