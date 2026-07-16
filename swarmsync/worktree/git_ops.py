"""git worktree lifecycle via subprocess. DESIGN.md §5.1, §5.4.

Unit U8. Each agent gets a dedicated worktree + branch so filesystem-level same-file
collisions are structurally impossible (DESIGN §5.1): two agents editing the same file in
two different worktrees simply can't clobber each other's working-tree bytes -- the OS-level
isolation is git's, not ours. Merge-time reconciliation is a separate, later concern
(DESIGN §5.4, the serial test-gated integrator, U10) -- this module only wraps the git
plumbing that unit needs; it does no scheduling/locking/test-running itself.

API (thin subprocess wrappers around `git`, no shelling through a real shell -- every call is
an argv list, never `shell=True`, so paths/branch names with spaces are safe):
  init_repo(path, initial_branch="integration") -> str        # git init + initial commit; returns its sha
  add_worktree(repo, name, base_commit=None)     -> Path       # git worktree add .worktrees/<name> -b <name>
  remove_worktree(repo, name, delete_branch=True) -> None      # git worktree remove --force (+ branch -D)
  commit_all(worktree, message, allow_empty=False) -> str      # git add -A && git commit; returns its sha
  current_commit(repo, ref="HEAD")               -> str        # git rev-parse
  merge_branch(repo, branch, into="integration") -> (bool, list[str])  # --no-ff; conflict paths on failure
  changed_files(repo, branch, base)              -> list[str]  # git diff --name-only base..branch
  reset_hard(repo, commit, branch=None)          -> None        # undo a landed-but-rejected merge (U10)

`merge_branch` is the primitive the integrator (U10) serializes calls to. Because the
scheduler only ever runs file-disjoint (or span-disjoint) work concurrently (DESIGN §5.4),
a clean merge is the expected case; a textual conflict is a hard signal of touch-set
misprediction -> the integrator rejects + re-plans, it does NOT auto-resolve. On conflict this
module aborts the in-progress merge itself before returning, so the `into` branch's working
tree is always left exactly as it was pre-call (DESIGN §5.4's "leave trunk untouched" on
reject) -- callers never have to clean up a half-finished merge.

`init_repo` is a test/demo helper (real deployments point at an existing repo that already has
history) -- it configures a **repo-local** (never global) commit identity and disables gpg
signing so commits succeed unattended in a sandbox with no git identity configured, without
touching the operator's global git config.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


class GitOpsError(RuntimeError):
    """A git subprocess invocation failed in a way that isn't a normal, handled outcome.

    A merge textual conflict is NOT this -- that's a expected, handled result surfaced as
    `(ok=False, conflict_paths)` from `merge_branch`. This is for everything else: a bad ref,
    a dirty worktree, git itself missing, etc.
    """


def _run(
    args: list[str], cwd: Path | str, check: bool = True
) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    if check and result.returncode != 0:
        raise GitOpsError(
            f"`{' '.join(args)}` in {cwd} failed (exit {result.returncode}):\n"
            f"{result.stderr.strip()}"
        )
    return result


def init_repo(path: Path | str, initial_branch: str = "integration") -> str:
    """git init a fresh repo at `path` with one initial commit on `initial_branch`.

    Whatever files already exist under `path` (created by the caller before this call) are
    committed as-is; if `path` is empty, an `--allow-empty` commit is made so there's still a
    real base commit to cut agent branches from. Returns the initial commit's sha -- handy as
    a default `base_commit` for `add_worktree`.

    `initial_branch` defaults to "integration" so the repo's main checkout (this `path`,
    never a worktree) IS the shared integration branch's working tree by construction --
    `merge_branch`'s own default `into="integration"` lands here with no extra setup.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-b", initial_branch], cwd=path)
    _run(["git", "config", "user.email", "swarm-sync@example.local"], cwd=path)
    _run(["git", "config", "user.name", "swarm-sync"], cwd=path)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=path)
    gitignore = path / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".worktrees/\n")
    _run(["git", "add", "-A"], cwd=path)
    _run(["git", "commit", "-m", "init", "--allow-empty"], cwd=path)
    return current_commit(path)


def add_worktree(repo: Path | str, name: str, base_commit: str | None = None) -> Path:
    """Create an isolated worktree + branch `name` cut from `base_commit`.

    `base_commit` defaults to `repo`'s current HEAD. Returns the worktree's filesystem path
    (`<repo>/.worktrees/<name>`) -- a plain directory containing a full checkout on its own
    branch, per DESIGN §5.1: two agents in two worktrees can never collide at the filesystem
    level, even when they both touch the same source file.
    """
    repo = Path(repo)
    if base_commit is None:
        base_commit = current_commit(repo)
    worktree_path = repo / ".worktrees" / name
    _run(
        ["git", "worktree", "add", str(worktree_path), "-b", name, base_commit],
        cwd=repo,
    )
    return worktree_path


def remove_worktree(repo: Path | str, name: str, delete_branch: bool = True) -> None:
    """Tear down the worktree at `.worktrees/<name>` (and, by default, its branch too).

    Used to discard an orphaned/reaped agent's worktree (DESIGN §6: "the orphan worktree
    branch is discarded"). Branch deletion is best-effort (`check=False`) -- it's cleanup, not
    a step whose failure should block the caller.
    """
    repo = Path(repo)
    worktree_path = repo / ".worktrees" / name
    _run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=repo)
    if delete_branch:
        _run(["git", "branch", "-D", name], cwd=repo, check=False)


def commit_all(worktree: Path | str, message: str, allow_empty: bool = False) -> str:
    """`git add -A && git commit` inside `worktree`. Returns the new commit's sha."""
    worktree = Path(worktree)
    _run(["git", "add", "-A"], cwd=worktree)
    args = ["git", "commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    _run(args, cwd=worktree)
    return current_commit(worktree)


def current_commit(repo: Path | str, ref: str = "HEAD") -> str:
    """`git rev-parse ref`, stripped. Works against a main repo checkout or a worktree."""
    result = _run(["git", "rev-parse", ref], cwd=Path(repo))
    return result.stdout.strip()


def changed_files(repo: Path | str, branch: str, base: str) -> list[str]:
    """Files that differ between `base` and `branch`'s tip (`git diff --name-only base..branch`).

    Used by the integrator (U10) to resolve which parcels a merged branch actually touched,
    for impact test selection + re-indexing (DESIGN §5.4).
    """
    result = _run(["git", "diff", "--name-only", f"{base}..{branch}"], cwd=Path(repo))
    return [line for line in result.stdout.splitlines() if line]


def reset_hard(repo: Path | str, commit: str, branch: Optional[str] = None) -> None:
    """`git reset --hard commit` in the main repo checkout at `repo`.

    Used by the integrator (U10) to undo a merge that landed cleanly but then
    failed the pytest gate (DESIGN §5.4 step 3: "Red -> reject ... trunk
    untouched"). `merge_branch` only aborts an in-progress *conflicted* merge
    itself; a merge that succeeds and is later rejected by the test gate has
    already been committed onto `into`'s branch, so undoing it needs this
    separate primitive -- a different failure point in the two-step flow.

    If `branch` is given, checks it out first (defensive -- callers normally
    invoke this immediately after `merge_branch`, which already leaves the
    working tree on `into`, so this is usually a no-op checkout).
    """
    repo = Path(repo)
    if branch is not None:
        _run(["git", "checkout", branch], cwd=repo)
    _run(["git", "reset", "--hard", commit], cwd=repo)


def merge_branch(
    repo: Path | str, branch: str, into: str = "integration"
) -> tuple[bool, list[str]]:
    """Merge `branch` into `into` (default the shared integration branch), `--no-ff`.

    Runs inside the MAIN repo checkout at `repo` -- never inside a worktree -- since `into`'s
    working tree lives there (per `init_repo`'s `initial_branch` choice). Not internally
    locked: the integrator (U10) is responsible for calling this serially, one branch at a
    time (DESIGN §5.4's "serial test-gated integrator"), since each call mutates `into`'s
    shared working tree in place.

    Returns `(True, [])` on a clean merge. On a textual conflict, aborts the in-progress merge
    before returning so `into`'s tree is left exactly as it was pre-call, and returns
    `(False, <sorted conflicted paths>)`. A merge failure that is NOT a textual conflict
    (bad branch name, dirty tree, etc. -- no conflicted paths to show for it) raises
    `GitOpsError` instead of silently reporting `(False, [])`.
    """
    repo = Path(repo)
    _run(["git", "checkout", into], cwd=repo)
    result = _run(
        ["git", "merge", "--no-ff", branch, "-m", f"merge {branch} into {into}"],
        cwd=repo,
        check=False,
    )
    if result.returncode == 0:
        return True, []

    status = _run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo)
    conflicts = sorted(p for p in status.stdout.splitlines() if p)
    _run(["git", "merge", "--abort"], cwd=repo, check=False)

    if not conflicts:
        raise GitOpsError(
            f"git merge {branch} into {into} failed with no conflicted paths "
            f"(exit {result.returncode}):\n{result.stderr.strip()}"
        )
    return False, conflicts
