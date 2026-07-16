"""U8 — git worktree ops. DESIGN.md §5.1, §5.4.

Done when:
  - on a temp repo, add_worktree creates an isolated branch dir
  - edits+commit_all in two worktrees on disjoint files both merge_branch into
    integration with no conflict
  - an overlapping edit returns ok=False with conflict paths
"""
from __future__ import annotations

from pathlib import Path

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
