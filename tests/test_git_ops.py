"""U8 — git worktree ops. DESIGN.md §5.1, §5.4.

Done when:
  - on a temp repo, add_worktree creates an isolated branch dir
  - edits+commit_all in two worktrees on disjoint files both merge_branch into
    integration with no conflict
  - an overlapping edit returns ok=False with conflict paths
"""
from __future__ import annotations


import contextlib
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from swarmsync.worktree import git_ops

SWARMSYNC_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "fileA.txt").write_text("line1\n")
    (r / "fileB.txt").write_text("lineB-orig\n")
    base = git_ops.init_repo(r)
    return r, base


def test_init_repo_creates_integration_branch_with_initial_commit(repo):
    r, base = repo
    assert len(base) == 40  # full sha
    branch = git_ops._run(["git", "branch", "--show-current"], cwd=r).stdout.strip()
    assert branch == "integration"
    assert (r / "fileA.txt").read_text() == "line1\n"


def test_add_worktree_creates_isolated_branch_dir(repo):
    r, base = repo
    wt = git_ops.add_worktree(r, "agentA", base)
    assert wt == r / ".worktrees" / "agentA"
    assert wt.is_dir()
    assert (wt / "fileA.txt").read_text() == "line1\n"
    branch = git_ops._run(["git", "branch", "--show-current"], cwd=wt).stdout.strip()
    assert branch == "agentA"
    # editing inside the worktree must never touch the main repo checkout's file
    (wt / "fileA.txt").write_text("mutated-in-worktree\n")
    assert (r / "fileA.txt").read_text() == "line1\n"


def test_add_worktree_defaults_base_commit_to_head(repo):
    r, base = repo
    wt = git_ops.add_worktree(r, "agentZ")
    assert git_ops.current_commit(wt) == base


def test_disjoint_edits_both_merge_cleanly(repo):
    r, base = repo
    wt_a = git_ops.add_worktree(r, "agentA", base)
    wt_b = git_ops.add_worktree(r, "agentB", base)

    (wt_a / "fileA.txt").write_text("lineA\n")
    sha_a = git_ops.commit_all(wt_a, "agent A edits fileA")

    (wt_b / "fileB.txt").write_text("lineB-edited\n")
    sha_b = git_ops.commit_all(wt_b, "agent B edits fileB")

    assert sha_a != base and sha_b != base

    ok_a, conflicts_a = git_ops.merge_branch(r, "agentA", into="integration")
    assert ok_a is True
    assert conflicts_a == []

    ok_b, conflicts_b = git_ops.merge_branch(r, "agentB", into="integration")
    assert ok_b is True
    assert conflicts_b == []

    # integration now carries both agents' edits
    assert (r / "fileA.txt").read_text() == "lineA\n"
    assert (r / "fileB.txt").read_text() == "lineB-edited\n"


def test_changed_files_reports_touched_paths(repo):
    r, base = repo
    wt_a = git_ops.add_worktree(r, "agentC", base)
    (wt_a / "fileA.txt").write_text("changed\n")
    git_ops.commit_all(wt_a, "touch fileA only")
    touched = git_ops.changed_files(r, "agentC", base)
    assert touched == ["fileA.txt"]


def test_overlapping_edit_returns_conflict(repo):
    r, base = repo
    wt_a = git_ops.add_worktree(r, "agentA", base)
    wt_c = git_ops.add_worktree(r, "agentC", base)  # both cut from the same base

    (wt_a / "fileA.txt").write_text("lineA\n")
    git_ops.commit_all(wt_a, "agent A edits fileA")
    ok_a, conflicts_a = git_ops.merge_branch(r, "agentA", into="integration")
    assert ok_a is True and conflicts_a == []

    # agentC's branch, cut from the pre-A base, edits the SAME line differently
    (wt_c / "fileA.txt").write_text("lineC\n")
    git_ops.commit_all(wt_c, "agent C edits fileA differently")

    ok_c, conflicts_c = git_ops.merge_branch(r, "agentC", into="integration")
    assert ok_c is False
    assert conflicts_c == ["fileA.txt"]

    # trunk (integration) must be left exactly as it was pre-call: agentA's edit intact,
    # no merge-in-progress state left dangling. `.worktrees/` itself is legitimately untracked
    # (it holds other checkouts, not integration source) so ignore that one expected line.
    assert (r / "fileA.txt").read_text() == "lineA\n"
    status_lines = [
        line
        for line in git_ops._run(["git", "status", "--porcelain"], cwd=r).stdout.splitlines()
        if ".worktrees" not in line
    ]
    assert status_lines == []
    merge_head = r / ".git" / "MERGE_HEAD"
    assert not merge_head.exists()


def test_remove_worktree_tears_down_dir_and_branch(repo):
    r, base = repo
    git_ops.add_worktree(r, "agentD", base)
    assert (r / ".worktrees" / "agentD").is_dir()
    git_ops.remove_worktree(r, "agentD")
    assert not (r / ".worktrees" / "agentD").exists()
    branches = git_ops._run(["git", "branch", "--list", "agentD"], cwd=r).stdout
    assert branches.strip() == ""


def test_merge_nonexistent_branch_raises_git_ops_error(repo):
    r, base = repo
    with pytest.raises(git_ops.GitOpsError):
        git_ops.merge_branch(r, "no-such-branch", into="integration")


# --- S2 regression: option-injection via a leading-'-' ref/branch/path ------------


def test_merge_branch_rejects_option_like_branch_before_running_git(repo, monkeypatch):
    """A branch named like a git option (`--upload-pack=...`) must be REJECTED,
    never handed to git. Before the guard, `shell=False` did not help: git parses
    a leading-'-' argv entry as an OPTION, so the value would be executed as a
    flag rather than treated as a branch name.
    """
    r, base = repo

    ran = []
    real_run = git_ops._run
    monkeypatch.setattr(
        git_ops, "_run", lambda *a, **k: (ran.append(a[0]), real_run(*a, **k))[1]
    )

    with pytest.raises(git_ops.GitOpsError, match="begins with '-'"):
        git_ops.merge_branch(r, "--upload-pack=touch /tmp/pwned", into="integration")

    # Rejected up front -- not a single git subprocess was spawned.
    assert ran == []


def test_add_worktree_rejects_option_like_name(repo):
    r, base = repo
    # `add_worktree` validates the name against a strict allow-list rather than
    # only rejecting a leading '-', because the name becomes a filesystem path that
    # gets recursively deleted -- so the message is the allow-list's, not
    # `_reject_option_like`'s. The behaviour under test is unchanged: an
    # option-like name never reaches a git subprocess.
    with pytest.raises(git_ops.GitOpsError, match="refusing unsafe"):
        git_ops.add_worktree(r, "--output=/tmp/pwned", base)
    # nothing was created for the rejected name
    assert not (r / ".worktrees" / "--output=/tmp/pwned").exists()


def test_add_worktree_rejects_option_like_base_commit(repo):
    r, base = repo
    with pytest.raises(git_ops.GitOpsError, match="begins with '-'"):
        git_ops.add_worktree(r, "agentX", "--upload-pack=evil")


@pytest.mark.parametrize(
    "call",
    [
        lambda r, base: git_ops.current_commit(r, "--not-a-ref"),
        lambda r, base: git_ops.changed_files(r, "--evil", base),
        lambda r, base: git_ops.reset_hard(r, "--evil"),
        lambda r, base: git_ops.init_repo(r / "sub", initial_branch="-x"),
    ],
)
def test_git_ops_reject_option_like_user_args(repo, call):
    r, base = repo
    with pytest.raises(git_ops.GitOpsError, match="begins with '-'"):
        call(r, base)


def test_normal_branch_names_still_work_after_hardening(repo):
    """The guard must not regress ordinary usage: a plain branch still merges."""
    r, base = repo
    wt = git_ops.add_worktree(r, "agentOK", base)
    (wt / "fileA.txt").write_text("edited-by-agentOK\n")
    git_ops.commit_all(wt, "agentOK edits fileA")
    ok, conflicts = git_ops.merge_branch(r, "agentOK", into="integration")
    assert ok is True and conflicts == []
    assert (r / "fileA.txt").read_text() == "edited-by-agentOK\n"


# --- R3 P1-2: worktree names are path fragments, not just git refs -----------------


@pytest.mark.parametrize(
    "evil",
    [
        "../../../escape",
        "..",
        "a/../../escape",
        "sub/dir",
        "/tmp/absolute",
        "-rf",
        "",
    ],
)
def test_add_worktree_rejects_traversal_and_separator_names(repo, evil):
    """A worktree name becomes `<repo>/.worktrees/<name>` and is then RECURSIVELY
    DELETED by `_prune_stale_worktree` before any git process runs. So the guard
    must reject path traversal, separators and absolute paths -- not merely names
    that look like git options.
    """
    r, base = repo
    with pytest.raises(git_ops.GitOpsError, match="refusing unsafe"):
        git_ops.add_worktree(r, evil, base)


def test_add_worktree_traversal_cannot_delete_a_directory_outside_the_repo(repo, tmp_path):
    """The exploit, end to end: `add_worktree` recursively deleted an arbitrary
    directory OUTSIDE the repo before running git.

    Reachable in normal use -- the broker derives the worktree name from
    `task.task_id` and `run_agent` passes it straight through. Note the first call in
    a fresh repo silently no-ops (`.worktrees/` doesn't exist yet, so `exists()` is
    False); the deletion fires from the SECOND agent onward, which is exactly why
    tests missed it. This creates `.worktrees/` first so the delete would really fire.
    """
    r, base = repo
    (r / ".worktrees").mkdir(exist_ok=True)

    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    victim_file = victim_dir / "precious.txt"
    victim_file.write_text("do not delete me", encoding="utf-8")

    # `<repo>/.worktrees/../../victim` == tmp_path/victim
    evil = f"../../{victim_dir.name}"
    with pytest.raises(git_ops.GitOpsError):
        git_ops.add_worktree(r, evil, base)

    assert victim_dir.exists(), "add_worktree deleted a directory outside the repo"
    assert victim_file.read_text(encoding="utf-8") == "do not delete me"


def test_add_worktree_still_accepts_ordinary_agent_ids(repo):
    """The allow-list must not break the names the system actually uses."""
    r, base = repo
    for name in ("agent-1", "agent_x", "task.42", "T7"):
        wt = git_ops.add_worktree(r, name, base)
        assert wt.exists()
        git_ops.remove_worktree(r, name, delete_branch=True)


# --- WP3.5 (P2 + C6-interim): rejected branches are parked out of pruning's reach ---


def _commit_reachable(repo, sha) -> bool:
    result = git_ops._run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, check=False
    )
    return result.returncode == 0


def test_prune_stale_worktree_never_deletes_a_rejected_branch(repo):
    """Defense in depth: even if a stale worktree sits ON a `rejected/*` branch,
    `_prune_stale_worktree` removes the worktree but must KEEP the branch -- it is
    the only reference to a rejected attempt's commits."""
    r, base = repo
    # A parked branch with a unique commit on it.
    wt = git_ops.add_worktree(r, "victim", base)
    (wt / "fileA.txt").write_text("rejected work\n")
    sha = git_ops.commit_all(wt, "work that was rejected")
    git_ops._run(["git", "worktree", "remove", "--force", str(wt)], cwd=r)
    git_ops._run(["git", "branch", "-m", "victim", "rejected/victim-20260718"], cwd=r)
    # A stale worktree checked out ON the rejected branch (no -b: existing branch).
    stale = r / ".worktrees" / "stale-on-rejected"
    git_ops._run(
        ["git", "worktree", "add", str(stale), "rejected/victim-20260718"], cwd=r
    )

    git_ops._prune_stale_worktree(r, "rejected/victim-20260718", stale)

    assert not stale.exists()  # the worktree itself IS pruned
    branches = git_ops._run(
        ["git", "branch", "--list", "rejected/victim-20260718"], cwd=r
    ).stdout
    assert branches.strip() != "", "prune deleted a rejected/* branch"
    assert _commit_reachable(r, sha)


def test_rejected_branch_parked_and_commits_survive_rerun_of_same_attempt_id(repo):
    """The preserved-commits contract, end to end at this layer: broker attempt ids
    are deterministic (`{task_id}-attempt-{n}`), so a re-run of the same task list
    reuses the SAME branch name -- and `add_worktree`'s prune ran `git branch -D`
    on it unconditionally, destroying the rejected attempt's only commits before
    doing anything else. Post-WP3.5 the rejection path PARKS the work under a
    timestamped `rejected/*` ref that pruning never touches."""
    from swarmsync.agent.runner import _cleanup_worktree

    r, base = repo
    name = "task-1-attempt-1"
    wt = git_ops.add_worktree(r, name, base)
    (wt / "fileA.txt").write_text("work the integrator will reject\n")
    sha = git_ops.commit_all(wt, "rejected attempt's commit")

    # The runner's rejection-path cleanup: keep the work, park it out of reach.
    _cleanup_worktree(r, name, delete_branch=False, park_branch=True)

    # Re-run the same task list -> same deterministic attempt id.
    wt2 = git_ops.add_worktree(r, name, base)
    assert wt2.exists()

    # The rejected commits are still reachable, from a rejected/* ref.
    assert _commit_reachable(r, sha), (
        "re-running the same task id destroyed the rejected attempt's commits"
    )
    parked = git_ops._run(
        ["git", "for-each-ref", "--format=%(refname:short) %(objectname)",
         "refs/heads/rejected/"],
        cwd=r,
    ).stdout.strip().splitlines()
    assert any(
        line.startswith(f"rejected/{name}-") and line.endswith(sha) for line in parked
    ), f"no rejected/* ref points at the preserved commit: {parked!r}"


def test_park_branch_names_never_collide_across_repeated_rejections(repo):
    """Two rejections of the same attempt id must park under distinct names."""
    r, base = repo
    name = "task-2-attempt-1"

    wt = git_ops.add_worktree(r, name, base)
    (wt / "fileA.txt").write_text("first rejected try\n")
    git_ops.commit_all(wt, "first rejection")
    git_ops.remove_worktree(r, name, delete_branch=False)
    first = git_ops.park_branch(r, name)

    wt = git_ops.add_worktree(r, name, base)
    (wt / "fileA.txt").write_text("second rejected try\n")
    git_ops.commit_all(wt, "second rejection")
    git_ops.remove_worktree(r, name, delete_branch=False)
    second = git_ops.park_branch(r, name)

    assert first != second
    assert first.startswith("rejected/") and second.startswith("rejected/")
    for parked in (first, second):
        out = git_ops._run(["git", "branch", "--list", parked], cwd=r).stdout
        assert out.strip() != ""


def test_remove_worktree_rmtrees_the_dir_when_git_remove_fails(repo, monkeypatch, caplog):
    """`git worktree remove` is check=False so a caller cleaning up after a run
    that never created a worktree still reaches the branch delete. But under
    concurrent git (index.lock contention) the remove can FAIL with the directory
    still present -- and `git worktree prune` can never reclaim that, because prune
    only drops entries whose directory is MISSING. In a long-lived server that is
    an unbounded disk leak, so a failed remove falls back to rmtree + prune."""
    r, base = repo
    name = "agent-leaky"
    wt = git_ops.add_worktree(r, name, base)
    assert wt.exists()

    real_run = git_ops._run
    pruned = []

    def flaky_run(args, cwd, check=True):
        if "worktree" in args and "remove" in args:
            # what git actually does when another process holds index.lock
            return subprocess.CompletedProcess(
                args, 128, stdout="", stderr="fatal: Unable to create index.lock"
            )
        if "worktree" in args and "prune" in args:
            pruned.append(tuple(args))
        return real_run(args, cwd=cwd, check=check)

    monkeypatch.setattr(git_ops, "_run", flaky_run)

    with caplog.at_level(logging.WARNING):
        git_ops.remove_worktree(r, name, delete_branch=False)

    assert not wt.exists(), "a failed `git worktree remove` must not leak the directory"
    assert pruned, "the stale admin entry must be pruned after the rmtree"
    assert any("leaked" in rec.message or "rmtree" in rec.message for rec in caplog.records), (
        "a silent fallback is nearly as bad as the leak -- it must be observable"
    )


# --- WP4.7: a SIGKILLed agent's worktree (the H6a leak) -----------------------------
#
# These need a REAL, separate OS process: the mechanism under test is that the
# kernel releases the worktree's `flock` when its owner dies however it died, and a
# thread cannot be SIGKILLed (killing it would take this test process with it).
# `sys.path` is prepended rather than relying on the install so the child imports
# THIS tree.

ORPHAN_SENTINEL = "PARTIAL-EDIT-FROM-A-PROCESS-ABOUT-TO-BE-SIGKILLED"

_OWNER_PROCESS = f"""
import sys, time
sys.path.insert(0, {str(SWARMSYNC_ROOT)!r})
from pathlib import Path
from swarmsync.worktree import git_ops
repo, name, base = sys.argv[1:4]
worktree = git_ops.add_worktree(repo, name, base)
(Path(worktree) / "fileA.txt").write_text({ORPHAN_SENTINEL!r} + "\\n")
print("ready", flush=True)
time.sleep(600)  # only the parent's SIGKILL ends this process
"""


@contextlib.contextmanager
def _worktree_owner(repo, name: str, base: str):
    """A real child process that creates `.worktrees/<name>`, leaves genuinely
    uncommitted work in it, and then hangs until it is killed."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _OWNER_PROCESS, str(repo), name, base],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert proc.stdout is not None
        first = proc.stdout.readline().strip()
        assert first == "ready", (
            f"the owner process never created its worktree: {first!r}"
            f"{proc.stdout.read()}"
        )
        yield proc
    finally:
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                os.kill(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=15)
        with contextlib.suppress(Exception):
            proc.stdout.close()  # type: ignore[union-attr]


def _parked_refs(repo, name: str) -> list[str]:
    out = git_ops._run(
        ["git", "for-each-ref", "--format=%(refname:short)", f"refs/heads/rejected/{name}-*"],
        cwd=repo,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def test_orphan_worktree_of_a_dead_process_is_reclaimed_under_a_different_name(repo):
    """THE H6a leak. A SIGKILLed agent left `.worktrees/<agent>` behind forever:
    the reaper only touches `leases`, the dead process ran no `finally`, and the one
    mechanism that removes a worktree -- `add_worktree`'s S5 prune -- matches the
    SAME name, while the broker retries under `{task}-attempt-{n+1}`. Measured at
    scale: 540 KiB survived the reap AND the reassignment, still in `git worktree
    list`. So the next agent to start in this repo must reclaim it -- and must not
    destroy the uncommitted work it held while doing so."""
    r, base = repo
    dead = "task7-attempt-1"
    orphan = r / ".worktrees" / dead

    with _worktree_owner(r, dead, base) as proc:
        assert orphan.is_dir()
        assert ORPHAN_SENTINEL in (orphan / "fileA.txt").read_text()
        assert git_ops._run(
            ["git", "status", "--porcelain"], cwd=orphan
        ).stdout.splitlines() == [" M fileA.txt"], "the work is not genuinely uncommitted"
        assert str(orphan) in git_ops._run(
            ["git", "worktree", "list", "--porcelain"], cwd=r
        ).stdout
        leaked_bytes = sum(
            f.stat().st_size for f in orphan.rglob("*") if f.is_file()
        )
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=15)

    # The retry: a DIFFERENT agent id, which is exactly why the S5 prune never fired.
    survivor = git_ops.add_worktree(r, "task7-attempt-2", base)

    assert not orphan.exists(), (
        f"the dead agent's worktree survived the next agent's start "
        f"({leaked_bytes} bytes) -- the H6a leak is back"
    )
    assert str(orphan) not in git_ops._run(
        ["git", "worktree", "list", "--porcelain"], cwd=r
    ).stdout, "still registered with git"
    assert survivor.is_dir(), "the sweep must not disturb the worktree being created"

    # ...and the uncommitted work is still RECOVERABLE, not destroyed: committed onto
    # the orphan's branch and parked under `rejected/*`, which pruning never touches.
    parked = _parked_refs(r, dead)
    assert len(parked) == 1, parked
    recovered = git_ops._run(["git", "show", f"{parked[0]}:fileA.txt"], cwd=r).stdout
    assert ORPHAN_SENTINEL in recovered, "the partial edit was DESTROYED, not preserved"
    # The original branch is dropped only because the parked ref now holds it.
    assert git_ops._run(["git", "branch", "--list", dead], cwd=r).stdout.strip() == ""


def test_sweep_never_touches_a_worktree_whose_owner_is_still_alive(repo):
    """The dangerous direction. Over-deletion here destroys a live agent's
    uncommitted work, so liveness must be the kernel's answer (an unheld `flock`),
    never a TTL, an mtime or an age: an agent whose heartbeat merely lapsed, or that
    is parked in an unbounded gate run, is still very much alive."""
    r, base = repo
    live_name = "still-working"
    live_worktree = r / ".worktrees" / live_name

    with _worktree_owner(r, live_name, base) as proc:
        git_ops.add_worktree(r, "someone-else", base)
        assert proc.poll() is None, "the owner died on its own; this proves nothing"
        assert live_worktree.is_dir(), "a LIVE agent's worktree was deleted"
        assert ORPHAN_SENTINEL in (live_worktree / "fileA.txt").read_text(), (
            "a live agent's uncommitted work was destroyed"
        )
        assert str(live_worktree) in git_ops._run(
            ["git", "worktree", "list", "--porcelain"], cwd=r
        ).stdout
        assert _parked_refs(r, live_name) == [], (
            "a live agent's branch was parked/rewritten under it"
        )


def test_sweep_never_touches_a_worktree_this_process_owns(repo):
    """Same guard, in-process: the broker creates a whole wave of worktrees from one
    process, and each `add_worktree` sweeps. `flock` is per open file DESCRIPTION, so
    a process's own lock is honored against its own probe -- the sibling that is
    mid-edit must survive its neighbours starting."""
    r, base = repo
    mine = git_ops.add_worktree(r, "wave-sibling-a", base)
    (mine / "fileA.txt").write_text("uncommitted-in-a-live-sibling\n")
    git_ops.add_worktree(r, "wave-sibling-b", base)
    assert mine.is_dir()
    assert (mine / "fileA.txt").read_text() == "uncommitted-in-a-live-sibling\n"


def test_sweep_never_touches_a_worktree_it_did_not_create(repo):
    """No ownership lock file -> not ours to delete. This covers a worktree made by
    hand (`git worktree add`), one left by a swarm-sync older than this mechanism,
    and a repo whose `.git` is a FILE (a linked worktree) where no lock can be
    written at all. Conservative on purpose: the cost is a leaked directory, the
    cost of guessing wrong is someone's work."""
    r, base = repo
    name = "not-ours"
    handmade = git_ops.add_worktree(r, name, base)
    (handmade / "fileA.txt").write_text("work-in-an-unowned-worktree\n")
    # Exactly the state a pre-WP4.7 (or hand-made) worktree is in: dir present, no
    # lock file, nobody in this process holding anything for it.
    git_ops._release_worktree_lock(r, name, unlink=True)
    assert git_ops._worktree_lock_path(r, name) is not None
    assert not git_ops._worktree_lock_path(r, name).exists()  # type: ignore[union-attr]

    git_ops.add_worktree(r, "a-new-agent", base)

    assert handmade.is_dir(), "a worktree with no ownership lock was swept anyway"
    assert (handmade / "fileA.txt").read_text() == "work-in-an-unowned-worktree\n"


def test_orphan_is_left_alone_when_its_work_cannot_be_preserved(repo, monkeypatch, caplog):
    """If the preserving commit fails, the bytes on disk are the ONLY copy of that
    work. Leaking a directory then beats deleting it, and the choice must be
    observable rather than silent."""
    r, base = repo
    dead = "unpreservable"
    orphan = r / ".worktrees" / dead

    with _worktree_owner(r, dead, base) as proc:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=15)

    def refuse_commit(*args, **kwargs):
        raise git_ops.GitOpsError("simulated: index.lock contention")

    monkeypatch.setattr(git_ops, "commit_all", refuse_commit)
    with caplog.at_level(logging.WARNING):
        git_ops.add_worktree(r, "next-agent", base)

    assert orphan.is_dir(), "the only copy of the work was deleted"
    assert ORPHAN_SENTINEL in (orphan / "fileA.txt").read_text()
    assert any("only copy" in rec.message for rec in caplog.records), [
        rec.message for rec in caplog.records
    ]


def test_orphan_branch_is_never_deleted_unless_it_was_parked_first(repo, monkeypatch):
    """The invariant behind `delete_branch=parked is not None`: the sweep may drop the
    orphan's branch ONLY because `rejected/<name>-<ts>` now points at the same commits.
    If parking fails, the branch is the last reference and must survive."""
    r, base = repo
    dead = "unparkable"
    with _worktree_owner(r, dead, base) as proc:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=15)

    def refuse_park(*args, **kwargs):
        raise git_ops.GitOpsError("simulated: cannot create the parked ref")

    monkeypatch.setattr(git_ops, "park_branch", refuse_park)
    git_ops.add_worktree(r, "next-agent", base)

    assert not (r / ".worktrees" / dead).exists(), "the directory is still reclaimable"
    assert _parked_refs(r, dead) == []
    assert git_ops._run(["git", "branch", "--list", dead], cwd=r).stdout.strip() != "", (
        "the sweep deleted the LAST reference to the orphan's commits"
    )
    recovered = git_ops._run(["git", "show", f"{dead}:fileA.txt"], cwd=r).stdout
    assert ORPHAN_SENTINEL in recovered


def test_orphan_sweep_is_reported_and_returns_what_it_reclaimed(repo):
    """`prune_orphan_worktrees` is public (an operator/`doctor` may want to call it),
    so it reports what it did rather than sweeping silently."""
    r, base = repo
    dead = "reported-orphan"
    with _worktree_owner(r, dead, base) as proc:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=15)

    assert git_ops.prune_orphan_worktrees(r) == [dead]
    assert git_ops.prune_orphan_worktrees(r) == [], "not idempotent"
