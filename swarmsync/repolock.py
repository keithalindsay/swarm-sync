"""ONE swarm-sync server per repo, enforced by an OS-level `flock`.

`server/app.py` already refuses two *roots* (`check_single_root`) and a DB reused
against a *different* root (`db.bind_managed_root`). Neither sees the case that
actually corrupts a repo: TWO `swarmsync-serve` PROCESSES on the SAME `--root`,
on different ports, with different DB files. Both pass every existing gate.

The endpoint-level serialization that is supposed to make integration safe --
`app.state.integrate_lock` -- is an `asyncio.Lock`, i.e. process-local. It cannot
see the other process at all. Two servers therefore run `git checkout` / `git
merge` / `git reset --hard` against the SAME `integration` working tree at the
same time, which reproducibly yields `fatal: Unable to create
'.git/index.lock'`, `MERGE_HEAD exists`, and a trunk left dirty with a
half-applied merge -- breaking `git_ops.merge_branch`'s documented contract that
`into`'s tree is left exactly as it was pre-call, and with it the product's
headline "trunk is always test-green".

So the guard has to live where both processes can see it: the filesystem.
`flock(2)` on `<root>/.git/swarmsync.lock` is exactly right --

  * it is advisory but universally honored between cooperating processes (we are
    both processes);
  * it is owned by the OPEN FILE DESCRIPTION, so the kernel releases it when the
    holder exits for ANY reason -- SIGKILL, OOM, power loss. A stale lock file
    can never wedge a repo forever, which a PID file or a lock directory could;
  * `.git/` is the one directory that unambiguously belongs to this repo, is not
    part of the working tree (so it never shows up in `git status`, a diff, or a
    merge), and already houses git's own `index.lock` for the same purpose.

A root with no `.git` directory is deliberately NOT locked: there is no trunk to
corrupt and no sanctioned place to put the lock file, and inventing one under the
user's working tree would be worse than the risk. `lock_path_for` returns None
there and every caller stands down.

The refusal posture matches `MultiRootError` / `ManagedRootMismatchError`: loud,
at startup, before anything is served, naming both the repo and the process that
already holds it.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Optional, Union

StrPath = Union[str, Path]

# The lock file's name inside `<root>/.git/`. Named for the product so an
# operator who finds it in `.git/` knows immediately what put it there.
LOCK_FILENAME = "swarmsync.lock"


class RepoLockHeldError(RuntimeError):
    """Another process already coordinates this repo. Raised at server startup.

    Same class of refusal as `app.MultiRootError` and `db.ManagedRootMismatchError`:
    a configuration that cannot work must not appear to."""


def lock_path_for(root: StrPath) -> Optional[Path]:
    """`<root>/.git/swarmsync.lock`, or None when `root` is not a git repo.

    Only a real `.git` DIRECTORY qualifies. A `.git` FILE (a linked worktree or a
    submodule) points elsewhere, and writing the lock into the shared parent
    would silently make two genuinely different checkouts contend, so those are
    treated the same as "not a git repo": no lock, no guard, no surprise.
    """
    git_dir = Path(root) / ".git"
    if not git_dir.is_dir():
        return None
    return git_dir / LOCK_FILENAME


def holder_pid_if_held(root: StrPath) -> Optional[int]:
    """The pid recorded in this repo's lock file if some process holds it, else None.

    Probes with a NON-BLOCKING exclusive `flock` on a separate descriptor:
    `flock` treats descriptors independently even within one process, so this
    answers truthfully whether ANY process (including this one, via the server's
    own descriptor) currently holds the lock. The probe lock is released
    immediately; the return value is the whole result.

    The pid is read from the file's contents and is best-effort/advisory -- it is
    for the human in the error message, never for a correctness decision (the
    `flock` itself is the decision). A None return means "nobody holds it";
    a returned int means "held", even if the pid text was unreadable, in which
    case the pid is reported as 0.
    """
    path = lock_path_for(root)
    if path is None or not path.exists():
        return None
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Held by someone. Read the pid for the message; never fail on it.
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                return 0
            try:
                return int(text)
            except ValueError:
                return 0
        # We got it -- so nobody held it. Drop our probe lock again.
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


class RepoLock:
    """The exclusive right to coordinate one repo, held for a server's lifetime.

    `acquire()` at startup, `release()` at shutdown. Both are no-ops when the root
    is not a git repo (see `lock_path_for`), so callers need no special-casing.
    """

    def __init__(self, root: StrPath) -> None:
        self.root = str(root)
        self.path = lock_path_for(root)
        self._fd: Optional[int] = None

    @property
    def active(self) -> bool:
        """Whether this lock is actually holding an OS lock right now."""
        return self._fd is not None

    def acquire(self) -> None:
        """Take the repo's lock, or raise `RepoLockHeldError` naming the holder.

        Non-blocking on purpose: a server that WAITED for the other one would
        look like a slow boot and then silently start coordinating a repo whose
        DB it does not share. Refusing immediately is the honest outcome.
        """
        if self.path is None:
            return
        # The pid probe must happen BEFORE we take the lock (afterwards our own
        # write would have overwritten the holder's pid).
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            holder = holder_pid_if_held(self.root)
            who = f"process {holder}" if holder else "another process"
            raise RepoLockHeldError(
                f"swarm-sync is already coordinating {self.root!r}: {who} holds "
                f"{self.path}.\n"
                "Two servers on one repo run `git merge`/`git reset --hard` against "
                "the SAME working tree concurrently, which leaves trunk dirty with a "
                "half-applied merge (`.git/index.lock` / `MERGE_HEAD exists`) and "
                "silently falsifies the guarantee that trunk is always test-green.\n"
                "Remedy: talk to the server that is already running (point "
                "$SWARMSYNC_URL at its port), or stop it before starting this one. "
                "A second REPO needs a second server with its own --root."
            ) from exc
        # Record who holds it, for the next process's error message and `doctor`.
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        """Drop the lock. Idempotent; safe to call when nothing was acquired.

        The kernel would release it on process exit anyway -- this exists so a
        server restarted IN-PROCESS (every `TestClient` lifespan, and any
        supervisor that re-enters the app) does not refuse its own successor.
        """
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)
