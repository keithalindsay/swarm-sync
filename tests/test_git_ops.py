"""U8 — git worktree ops. DESIGN.md §5.1, §5.4.

Done when:
  - on a temp repo, add_worktree creates an isolated branch dir
  - edits+commit_all in two worktrees on disjoint files both merge_branch into
    integration with no conflict
  - an overlapping edit returns ok=False with conflict paths
"""
from __future__ import annotations


import logging
import subprocess

import pytest

from swarmsync.worktree import git_ops


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
