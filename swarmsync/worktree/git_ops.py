"""git worktree lifecycle via subprocess. DESIGN.md §5.1, §5.4.

Unit U8. Each agent gets a dedicated worktree + branch so filesystem-level same-file
collisions are structurally impossible (DESIGN §5.1): two agents editing the same file in
two different worktrees simply can't clobber each other's working-tree bytes -- the OS-level
isolation is git's, not ours. Merge-time reconciliation is a separate, later concern
(DESIGN §5.4, the serial test-gated integrator, U10) -- this module only wraps the git
plumbing that unit needs; it does no scheduling/locking/test-running itself.

SECURITY: every user-derived ref/branch/path is checked by `_reject_option_like` before any
git process starts -- a value beginning with '-' (e.g. a branch literally named
`--upload-pack=<cmd>`) is refused, because git would otherwise parse it as an OPTION rather
than data (argument injection, which `shell=False` does NOT prevent). As defense in depth,
user-derived positionals are additionally fenced with `--end-of-options`/`--` separators on the
argv (except `git rev-parse`, which echoes `--end-of-options` onto stdout -- there the leading-'-'
rejection is the guard).

API (thin subprocess wrappers around `git`, no shelling through a real shell -- every call is
an argv list, never `shell=True`, so paths/branch names with spaces are safe):
  init_repo(path, initial_branch="integration") -> str        # git init + initial commit; returns its sha
  add_worktree(repo, name, base_commit=None)     -> Path       # git worktree add .worktrees/<name> -b <name>
  remove_worktree(repo, name, delete_branch=True) -> None      # git worktree remove --force (+ branch -D)
  prune_orphan_worktrees(repo, skip=())          -> list[str]  # reclaim worktrees whose owner process died (WP4.7)
  commit_all(worktree, message, allow_empty=False) -> str      # git add -A && git commit; returns its sha
  current_commit(repo, ref="HEAD")               -> str        # git rev-parse
  merge_branch(repo, branch, into="integration") -> (bool, list[str])  # --no-ff; conflict paths on failure
  changed_files(repo, branch, base)              -> list[str]  # git diff --name-only base..branch
  reset_hard(repo, commit, branch=None)          -> None        # undo a landed-but-rejected merge (U10)
  park_branch(repo, name)                        -> str         # rejected/<name>-<UTC ts> ref pruning never deletes (WP3.5)

`merge_branch` is the primitive the integrator (U10) serializes calls to. Because the
scheduler only ever runs file-disjoint (or span-disjoint) work concurrently (DESIGN §5.4),
a clean merge is the expected case; a textual conflict is a hard signal of touch-set
misprediction -> the integrator rejects + re-plans, it does NOT auto-resolve. On conflict this
module aborts the in-progress merge itself before returning, so the `into` branch's working
tree is always left exactly as it was pre-call (DESIGN §5.4's "leave trunk untouched" on
reject) -- callers never have to clean up a half-finished merge.

CRASH CLEANUP (WP4.7): a worktree is created together with an OWNERSHIP LOCK --
`flock` on `<repo>/.git/swarmsync-worktrees/<name>.lock`, held by the creating
process for as long as it lives. `prune_orphan_worktrees` (run at the top of every
`add_worktree`) reclaims any worktree whose lock is unheld, i.e. whose creator is
provably gone, after committing + parking whatever uncommitted work it held. This
is what makes DESIGN §6's "the orphan worktree branch is discarded" true for a
SIGKILLed agent: nothing else ever removed such a worktree, because the S5 prune
below matches only the SAME name while the broker retries under a new one.
Ownership is deliberately the KERNEL's answer to "is that process alive", not a
lease TTL and not a timeout -- see `prune_orphan_worktrees` for why that
distinction is the whole safety argument.

`init_repo` is a test/demo helper (real deployments point at an existing repo that already has
history) -- it configures a **repo-local** (never global) commit identity and disables gpg
signing so commits succeed unattended in a sandbox with no git identity configured, without
touching the operator's global git config.
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# WP3.5: namespace for parked rejected-attempt branches. Everything under this
# prefix is OFF-LIMITS to `_prune_stale_worktree`'s branch delete -- a parked
# branch is the only reference to a rejected attempt's commits.
REJECTED_BRANCH_PREFIX = "rejected/"

# WP4.7 (H6a): where a worktree's OWNERSHIP LOCK lives -- one file per worktree
# under `<repo>/.git/swarmsync-worktrees/<name>.lock`, flock'ed for as long as the
# process that created that worktree is alive. `.git/` for the reasons
# `repolock.py` already spells out (it belongs to this repo, is not part of any
# working tree, so it never shows up in `git status`/a diff/a merge, and already
# houses git's own `index.lock`), one file per worktree rather than one per repo
# because the question being asked is per-worktree.
WORKTREE_LOCK_DIRNAME = "swarmsync-worktrees"
WORKTREE_LOCK_SUFFIX = ".lock"

# Ownership locks held by THIS process, keyed `(resolved repo, worktree name)`.
# The fd has to stay open for the worktree's whole working life: `flock` is owned
# by the open file DESCRIPTION, which is precisely why it answers the question
# `_prune_orphan_worktrees` needs answered -- the kernel drops it when the holder
# exits for ANY reason (SIGKILL, OOM, power loss), while a pid file would survive
# the crash it is supposed to detect.
_OWNED_LOCKS: dict[tuple[str, str], int] = {}
_OWNED_LOCKS_MUTEX = threading.Lock()


class GitOpsError(RuntimeError):
    """A git subprocess invocation failed in a way that isn't a normal, handled outcome.

    A merge textual conflict is NOT this -- that's a expected, handled result surfaced as
    `(ok=False, conflict_paths)` from `merge_branch`. This is for everything else: a bad ref,
    a dirty worktree, git itself missing, etc.
    """


def _reject_option_like(value: str, kind: str) -> str:
    """Refuse any user-derived ref/branch/path that begins with '-'.

    git parses a leading-'-' argument as an OPTION, not a positional, so an
    attacker-chosen branch/ref/path like `--upload-pack=<cmd>` or `--output=...`
    would be executed as a flag rather than treated as data (argument injection,
    even with `shell=False`). We reject these up front -- before ANY git process
    runs -- and additionally pass `--end-of-options`/`--` separators on the argv
    below as defense in depth. An empty string is allowed through here (it is not
    option-like); git itself rejects it as a bad ref where relevant.
    """
    if value is not None and str(value).startswith("-"):
        raise GitOpsError(
            f"refusing {kind} that begins with '-': {value!r} "
            f"(git would parse it as an option, not a positional argument)"
        )
    return value


_SAFE_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _reject_unsafe_name(name: str, kind: str = "worktree/branch name") -> str:
    """Refuse any worktree/branch name that isn't a plain, single-segment token.

    SECURITY: option-rejection alone is not enough here, because the dangerous sink
    is not git's argv -- it is `shutil.rmtree`. `add_worktree` builds
    `repo/.worktrees/<name>` and hands it to `_prune_stale_worktree`, which rmtree's
    it BEFORE any git process runs, so a name is a filesystem path fragment first and
    a git ref second:

        '../../../tmp/evil' -> <repo>/.worktrees/../../../tmp/evil   (escapes the repo)
        '/etc/passwd'       -> /etc/passwd    (pathlib DISCARDS the prefix on an
                                               absolute right-hand operand)

    Names are agent ids / task ids, which are legitimately plain tokens, so an
    allow-list costs nothing and closes traversal, absolute paths, separators, NUL,
    and leading '-' in one predicate. `add_worktree` additionally asserts the
    resolved path is contained in `.worktrees/` -- defense in depth, since this is a
    recursive delete.
    """
    if not isinstance(name, str) or not _SAFE_NAME_RE.match(name):
        raise GitOpsError(
            f"refusing unsafe {kind}: {name!r} (must match {_SAFE_NAME_RE.pattern} -- "
            f"path separators, '..', absolute paths and leading '-' are rejected "
            f"because this name becomes a filesystem path that gets recursively deleted)"
        )
    return name


def _lock_key(repo: Path, name: str) -> tuple[str, str]:
    """Identity of one worktree's ownership lock within this process.

    The repo path is resolved so `add_worktree("/repo", n)` and
    `remove_worktree("/repo/.", n)` refer to the same lock. `resolve()` on a
    path that doesn't exist is still well-defined (strict=False by default).
    """
    return (str(Path(repo).resolve()), name)


def _worktree_lock_path(repo: Path, name: str) -> Optional[Path]:
    """`<repo>/.git/swarmsync-worktrees/<name>.lock`, or None when there is no
    sanctioned place to put it.

    Only a real `.git` DIRECTORY qualifies, exactly as in `repolock.lock_path_for`:
    a `.git` FILE means `repo` is itself a linked worktree (or a submodule) whose
    real git dir is shared elsewhere, and writing an ownership lock into that
    shared parent would make two genuinely different checkouts contend. There we
    stand down -- no lock is taken, and (because `_prune_orphan_worktrees`
    requires a lock file to exist before it will delete anything) no worktree in
    such a repo is ever swept. Leaking a directory is the safe failure here.

    `name` is always validated by `_reject_unsafe_name` before this is called, so
    it cannot traverse out of the lock directory.
    """
    git_dir = Path(repo) / ".git"
    if not git_dir.is_dir():
        return None
    return git_dir / WORKTREE_LOCK_DIRNAME / f"{name}{WORKTREE_LOCK_SUFFIX}"


def _acquire_worktree_lock(repo: Path, name: str) -> bool:
    """Take (and KEEP) this process's ownership lock on worktree `name`.

    Returns whether the lock is now held by this process. A `False` return is
    never fatal -- it only means this worktree is not eligible for orphan
    sweeping (see `_prune_orphan_worktrees`), which is the conservative side.

    The one case where `False` means something is worth logging: the lock is
    already held by ANOTHER live process, i.e. two live agents share one
    `agent_id` and therefore one `.worktrees/<name>` directory. That was already
    broken before this lock existed (they clobber each other's working-tree
    bytes); it is now at least visible in the log.
    """
    path = _worktree_lock_path(repo, name)
    if path is None:
        return False
    _release_worktree_lock(repo, name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        logger.debug("could not open worktree ownership lock %s: %s", path, exc)
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        logger.warning(
            "worktree %r in %s is already owned by another LIVE process (%s is "
            "flock'ed) -- two agents sharing one worktree directory will clobber "
            "each other; this one will not be orphan-swept",
            name,
            repo,
            path,
        )
        return False
    try:  # advisory, for a human reading the file; the flock is the decision
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    with _OWNED_LOCKS_MUTEX:
        _OWNED_LOCKS[_lock_key(repo, name)] = fd
    return True


def _release_worktree_lock(repo: Path, name: str, unlink: bool = False) -> None:
    """Drop this process's ownership lock on `name` (idempotent).

    The kernel would drop it at process exit anyway; this exists so one process
    can create, remove and RE-create the same worktree name (every rerun, every
    `remove_worktree` + `add_worktree` pair in a test) without contending with
    its own earlier descriptor -- `flock` treats descriptors independently even
    within a single process.
    """
    with _OWNED_LOCKS_MUTEX:
        fd = _OWNED_LOCKS.pop(_lock_key(repo, name), None)
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)
    if unlink:
        path = _worktree_lock_path(repo, name)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass


def _claim_if_unowned(path: Path) -> Optional[int]:
    """An fd holding `path`'s flock iff NOBODY held it, else None.

    "Nobody holds it" is the whole orphan test: the worktree's creator flock'ed
    this file and never released it, so an unheld lock means that process is
    gone. The returned fd keeps the lock held for the caller's teardown, so a
    concurrent sweeper (or a concurrent `add_worktree` for the same name) cannot
    interleave with it; the caller must close it.

    Deliberately does NOT create the file (`O_RDWR`, no `O_CREAT`): a missing
    lock file means "not ours to delete", never "unowned".
    """
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)  # someone alive holds it
        return None
    return fd


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
    _reject_option_like(initial_branch, "initial branch name")
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


def _prune_stale_worktree(repo: Path, name: str, worktree_path: Path) -> None:
    """Best-effort teardown of a leftover same-named worktree + branch so a
    rerun's `git worktree add -b <name>` doesn't collide (S5 idempotency).

    A prior run that crashed (or wasn't cleaned up) can leave any of: a
    registered worktree, a stale worktree admin entry whose dir is already gone,
    an orphaned on-disk dir git no longer tracks, and/or the branch. Every step
    is `check=False` / `ignore_errors=True` -- if there is nothing to prune this
    is a harmless no-op. `git worktree prune` only drops entries whose working
    dir is MISSING, so a concurrently-added sibling worktree (its dir present) is
    never affected -- safe under the broker's concurrent `add_worktree` calls.

    Destructive BY CONTRACT, and that is the difference from its WP4.7 sibling
    `prune_orphan_worktrees`: the caller named this exact worktree, so it owns the
    name and is explicitly asking for a fresh checkout of it (S5 rerun
    idempotency) -- uncommitted bytes there are discarded and the branch deleted.
    The orphan sweep touches names the caller never mentioned, so it must instead
    preserve first. Do not "unify" the two: the asymmetry is the safety property.
    """
    _run(
        ["git", "worktree", "remove", "--force", "--", str(worktree_path)],
        cwd=repo,
        check=False,
    )
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
    _run(["git", "worktree", "prune"], cwd=repo, check=False)
    # WP3.5: NEVER delete a parked `rejected/*` branch (see `park_branch`) -- it is
    # the ONLY reference to a rejected attempt's commits, and broker attempt ids
    # are deterministic (`{task_id}-attempt-{n}`), so re-running the same task
    # list reaches this prune with the same name before doing anything else.
    # Defense in depth: even if a stale worktree sits ON a rejected branch, the
    # worktree is pruned above but the branch survives.
    if not name.startswith(REJECTED_BRANCH_PREFIX):
        _run(["git", "branch", "-D", "--end-of-options", name], cwd=repo, check=False)


def _discard_orphan_worktree(repo: Path, name: str, worktree_path: Path) -> bool:
    """Preserve, then remove, ONE worktree whose owning process is gone.

    Returns whether the directory was removed. Preservation first, because a
    crashed agent's worktree is exactly where genuinely uncommitted work sits
    (DESIGN §6's crash-mid-edit row):

      1. anything uncommitted is COMMITTED onto the orphan's own branch, so the
         partial edit survives the directory being deleted;
      2. that branch tip is then PARKED under `rejected/<name>-<UTC ts>` (WP3.5's
         namespace, which pruning never touches), so it survives a later rerun
         under the same deterministic attempt id too;
      3. only then is the worktree removed -- and its branch deleted ONLY if
         parking actually succeeded. `delete_branch=parked is not None` is the
         invariant that matters: this function never drops the last reference to
         a commit.

    If the preserving commit FAILS, the worktree is left alone and `False` is
    returned: the uncommitted bytes on disk are then the only copy in existence,
    and leaking a directory is strictly better than deleting the only copy.
    Nothing here can reach trunk -- `rejected/*` is never merged, and the merge
    is gated regardless.
    """
    dirty = _run(["git", "status", "--porcelain"], cwd=worktree_path, check=False)
    if dirty.returncode == 0 and dirty.stdout.strip():
        try:
            commit_all(
                worktree_path,
                f"swarm-sync: uncommitted work recovered from orphaned worktree {name}",
            )
        except GitOpsError as exc:
            logger.warning(
                "orphaned worktree %s has uncommitted work that could not be "
                "committed (%s) -- LEAVING it in place: those bytes are the only "
                "copy, so a disk leak is preferable to destroying them",
                worktree_path,
                exc,
            )
            return False
    parked: Optional[str] = None
    try:
        parked = park_branch(repo, name)
    except GitOpsError as exc:
        # e.g. the crash predated `git worktree add -b`, so there is no branch to
        # park. Then there are no commits to lose either, and `delete_branch`
        # stays False below.
        logger.debug(
            "orphaned worktree %s: park_branch failed (%s) -- keeping branch %r",
            worktree_path,
            exc,
            name,
        )
    remove_worktree(repo, name, delete_branch=parked is not None)
    logger.info(
        "reclaimed orphaned worktree %s (its owning process is gone); work "
        "preserved as %s",
        worktree_path,
        parked if parked is not None else f"branch {name!r}",
    )
    return True


def prune_orphan_worktrees(
    repo: Path | str, skip: Iterable[str] = ()
) -> list[str]:
    """Reclaim every worktree under `<repo>/.worktrees/` whose OWNER PROCESS DIED.

    WP4.7, the H6a leak: `_prune_stale_worktree` only ever matched the SAME name,
    while a crashed task is retried under a different agent id
    (`{task_id}-attempt-{n+1}`), so a SIGKILLed agent's worktree was never
    reclaimed by anything -- one leaked checkout per crash, for the lifetime of
    the repo (measured: 540 KiB survived both the lease reap and the task
    reassignment, still listed in `git worktree list`). DESIGN §6 claims "the
    orphan worktree branch is discarded"; this is what makes that true.

    A worktree is a candidate ONLY when all of these hold -- every one of them is
    a guard against deleting a LIVE agent's uncommitted work, which is the one
    genuinely dangerous thing this function could do:

      * its directory is a real directory (not a symlink) directly under
        `.worktrees/`, and its name is a plain `_SAFE_NAME_RE` token, i.e. a name
        `add_worktree` itself could have produced;
      * it is not in `skip` (the caller's own name -- `add_worktree` prunes that
        one itself, S5, and must not have it swept out from under it);
      * this process does not itself own it (`_OWNED_LOCKS`);
      * an ownership lock file EXISTS for it -- a worktree created before this
        mechanism existed, or by hand, is never touched;
      * and that lock is currently held by NOBODY. Liveness is the kernel's
        answer, not a timeout: `flock` is released on process death however the
        process died, so "unheld" means "the creator is gone", never merely "the
        creator is slow". A heartbeat that merely lapsed, an agent stuck in an
        unbounded gate, a paused process -- all still hold their lock and are all
        skipped. That is the difference between this and reaping on lease TTL,
        where an expired lease does NOT imply a dead process.

    Returns the names reclaimed. Best-effort throughout: a candidate whose work
    cannot be preserved is left alone (see `_discard_orphan_worktree`).
    """
    repo = Path(repo)
    root = repo / ".worktrees"
    if not root.is_dir():
        return []
    skipped = set(skip)
    reclaimed: list[str] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        name = entry.name
        if name in skipped or entry.is_symlink() or not entry.is_dir():
            continue
        if not _SAFE_NAME_RE.match(name):
            continue
        lock_path = _worktree_lock_path(repo, name)
        if lock_path is None or not lock_path.exists():
            continue
        with _OWNED_LOCKS_MUTEX:
            if _lock_key(repo, name) in _OWNED_LOCKS:
                continue  # this process is the live owner
        fd = _claim_if_unowned(lock_path)
        if fd is None:
            continue  # a live process owns it
        try:
            if _discard_orphan_worktree(repo, name, entry):
                reclaimed.append(name)
        finally:
            # Unlink BEFORE unlocking, and while still holding the lock: if an
            # `add_worktree(name)` raced us and re-created the file, it now holds
            # its flock on an unlinked inode, so later sweeps see no lock file
            # and skip its live worktree. That degrades to leaking, never to
            # deleting a live worktree.
            try:
                lock_path.unlink()
            except OSError:
                pass
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
    return reclaimed


def add_worktree(repo: Path | str, name: str, base_commit: str | None = None) -> Path:
    """Create an isolated worktree + branch `name` cut from `base_commit`.

    `base_commit` defaults to `repo`'s current HEAD. Returns the worktree's filesystem path
    (`<repo>/.worktrees/<name>`) -- a plain directory containing a full checkout on its own
    branch, per DESIGN §5.1: two agents in two worktrees can never collide at the filesystem
    level, even when they both touch the same source file.

    IDEMPOTENT (S5): a stale same-named worktree/branch left by a prior (crashed
    or un-cleaned) run is pruned first, so a rerun with the same agent_id/name
    doesn't collide on `git worktree add -b` ("branch already exists" / "path
    already exists") -- reruns don't leak or fail.

    ALSO SWEEPS OTHER AGENTS' ORPHANS (WP4.7): the S5 prune above only ever
    matches this same `name`, so a crashed agent's worktree -- retried by the
    broker under a DIFFERENT id -- was never reclaimed by anything. Every call
    therefore also runs `prune_orphan_worktrees`, which reclaims any worktree
    whose creating process is provably dead (its `flock` is unheld) after
    preserving its work. Cleanup stays LAZY: it happens when the next agent
    starts in this repo, not on a timer, and nothing here can touch a worktree
    whose owner is still alive. This worktree's own ownership lock is taken
    BEFORE the directory is created, so no other process can ever see this
    directory in an "exists but unowned" state.
    """
    _reject_unsafe_name(name, "worktree/branch name")
    repo = Path(repo)
    if base_commit is None:
        base_commit = current_commit(repo)
    _reject_option_like(base_commit, "base commit")
    worktree_path = repo / ".worktrees" / name
    # Defense in depth behind `_reject_unsafe_name`: `_prune_stale_worktree` runs a
    # recursive delete on this path, so prove containment rather than trust the name
    # predicate alone. `.worktrees` may not exist yet on a first run, hence parents.
    worktrees_root = (repo / ".worktrees").resolve()
    if not worktree_path.resolve().is_relative_to(worktrees_root):
        raise GitOpsError(
            f"refusing worktree path outside {worktrees_root}: {worktree_path!r}"
        )
    prune_orphan_worktrees(repo, skip=(name,))
    _prune_stale_worktree(repo, name, worktree_path)
    _acquire_worktree_lock(repo, name)
    # `--` after the options separates the positional <path> <commit-ish> from any
    # further option parsing, so neither can be smuggled in as a flag.
    try:
        _run(
            ["git", "worktree", "add", "-b", name, "--", str(worktree_path), base_commit],
            cwd=repo,
        )
    except GitOpsError:
        # No worktree was created, so nothing owns this name: drop the lock rather
        # than leave a held-but-meaningless one (which would also pin an fd for
        # the life of the process).
        _release_worktree_lock(repo, name, unlink=True)
        raise
    return worktree_path


def remove_worktree(repo: Path | str, name: str, delete_branch: bool = True) -> None:
    """Tear down the worktree at `.worktrees/<name>` (and, by default, its branch too).

    Used to discard an orphaned/reaped agent's worktree (DESIGN §6: "the orphan worktree
    branch is discarded"). Branch deletion is best-effort (`check=False`) -- it's cleanup, not
    a step whose failure should block the caller.

    Removing the worktree is `check=False` for the same reason: a caller cleaning up
    after a run that never got as far as creating a worktree (or that already had it
    removed) still needs the `delete_branch` step below to run. With `check=True` the
    missing-worktree case raised and silently skipped the branch delete, so a leaked
    branch from a prior run was never actually pruned.

    `check=False` alone leaked, though: under concurrent git (`index.lock` contention
    at 16-way parallelism, reproduced in 2 of 5 runs) the remove fails, the directory
    survives, and `git worktree prune` can never reclaim it -- prune only drops entries
    whose directory is MISSING. In a long-lived server that is an unbounded disk leak.
    So a failed remove falls back to the same rmtree-then-prune sequence
    `_prune_stale_worktree` already uses.

    Also drops this worktree's ownership lock (WP4.7) -- the worktree is gone, so
    there is nothing left to own, and the same name must be re-creatable by this
    same process immediately afterwards.
    """
    _reject_unsafe_name(name, "worktree/branch name")
    repo = Path(repo)
    _release_worktree_lock(repo, name, unlink=True)
    worktree_path = repo / ".worktrees" / name
    removed = _run(
        ["git", "worktree", "remove", "--force", "--", str(worktree_path)],
        cwd=repo,
        check=False,
    )
    if removed.returncode != 0 and worktree_path.exists():
        logger.warning(
            "git worktree remove failed for %s (exit %d): %s -- falling back to "
            "rmtree + prune so the directory is not leaked",
            worktree_path,
            removed.returncode,
            removed.stderr.strip(),
        )
        shutil.rmtree(worktree_path, ignore_errors=True)
        _run(["git", "worktree", "prune"], cwd=repo, check=False)
    if delete_branch:
        _run(["git", "branch", "-D", "--end-of-options", name], cwd=repo, check=False)


def park_branch(repo: Path | str, name: str) -> str:
    """Park branch `name`'s tip under a timestamped `rejected/<name>-<UTC>` ref (WP3.5).

    Called by the agent runner on the kept-branch rejection path: the integrator
    reset trunk, so branch `name` is the ONLY reference to the rejected attempt's
    commits -- yet `name` is a deterministic broker attempt id
    (`{task_id}-attempt-{n}`), and `_prune_stale_worktree` deletes that exact
    branch name at the top of every future `add_worktree`. Parking puts a second
    ref on the commits in a namespace pruning never touches (see the
    `REJECTED_BRANCH_PREFIX` guard in `_prune_stale_worktree`), under a
    timestamped name no future attempt can collide with.

    The parked ref is CREATED ALONGSIDE the original branch rather than renaming
    it away: the R3 P1-6 contract (tests/test_agent.py) pins the original branch
    name surviving its own rejection for the immediate rebase-and-resubmit story;
    durability across RE-RUNS is the parked ref's job (the rerun's prune deletes
    the original name, but the commits stay reachable from `rejected/*`).
    Timestamp is UTC to microseconds; on the (pathological) collision the suffix
    is bumped rather than clobbering an existing parked ref. Returns the parked
    branch name. Parked refs accumulate by design -- they are the preserved
    evidence -- and are an operator's to garbage-collect deliberately.
    """
    _reject_unsafe_name(name, "branch name to park")
    repo = Path(repo)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    parked = f"{REJECTED_BRANCH_PREFIX}{name}-{stamp}"
    attempt = 1
    while _run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{parked}"],
        cwd=repo,
        check=False,
    ).returncode == 0:
        attempt += 1
        parked = f"{REJECTED_BRANCH_PREFIX}{name}-{stamp}-{attempt}"
    # `git branch <new> <start>`: creates the ref without any checkout. Both
    # positionals are fenced behind `--end-of-options`; `parked` is derived from
    # the validated `name` plus our own constant prefix/timestamp.
    _run(["git", "branch", "--end-of-options", parked, name], cwd=repo)
    return parked


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
    # No `--end-of-options` here: `git rev-parse` ECHOES that marker onto stdout,
    # which would corrupt the sha we parse back out. Rejecting a leading-'-' ref is
    # the guard for this call instead.
    _reject_option_like(ref, "ref")
    result = _run(["git", "rev-parse", ref], cwd=Path(repo))
    return result.stdout.strip()


def changed_files(repo: Path | str, branch: str, base: str) -> list[str]:
    """Files that differ between `base` and `branch`'s tip (`git diff --name-only base..branch`).

    Used by the integrator (U10) to resolve which parcels a merged branch actually touched,
    for impact test selection + re-indexing (DESIGN §5.4).

    `--no-renames` (WP3.5): with git's default rename detection, a renamed file
    reports ONLY its new path under `--name-only`, so the OLD path never entered
    the changed set -- the integrator's ghost retirement (and impact selection
    keyed on the old module's stem) never saw that the old file was deleted.
    Disabling detection reports the rename as delete(old) + add(new): strictly
    MORE files, never fewer, so impact selection only gets more conservative.
    """
    _reject_option_like(base, "base ref")
    _reject_option_like(branch, "branch")
    result = _run(
        ["git", "diff", "--name-only", "--no-renames", "--end-of-options", f"{base}..{branch}"],
        cwd=Path(repo),
    )
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
    _reject_option_like(commit, "commit")
    repo = Path(repo)
    if branch is not None:
        _reject_option_like(branch, "branch")
        # `git checkout` does NOT accept `--end-of-options`; a trailing `--`
        # separates the ref from any pathspec parsing instead.
        _run(["git", "checkout", branch, "--"], cwd=repo)
    _run(["git", "reset", "--hard", commit, "--"], cwd=repo)


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
    _reject_option_like(into, "target branch")
    _reject_option_like(branch, "branch")
    repo = Path(repo)
    # `git checkout` does not take `--end-of-options`; trailing `--` disambiguates.
    _run(["git", "checkout", into, "--"], cwd=repo)
    # `-m <msg>` must precede `--end-of-options`; everything after the marker is
    # treated as a positional (the branch to merge), never as an option.
    result = _run(
        [
            "git", "merge", "--no-ff", "-m", f"merge {branch} into {into}",
            "--end-of-options", branch,
        ],
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
