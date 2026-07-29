"""One swarm-sync server per repo, enforced by an OS-level lock.

`server/app.py`'s `integrate_lock` is an `asyncio.Lock` -- PROCESS-LOCAL. It
serializes concurrent `POST /integrate` requests inside one server and does
nothing at all about a SECOND `swarmsync-serve` process started on the same
`--root` (a different port, a different DB file, so neither `check_single_root`
nor `bind_managed_root` fires). Two such servers integrate into the same
`integration` checkout simultaneously: `fatal: Unable to create
'.git/index.lock'`, `MERGE_HEAD exists`, and a trunk left dirty with a
half-applied merge -- which violates `git_ops.merge_branch`'s documented
contract that `into`'s tree is left exactly as it was pre-call.

The guard is an `flock` on `<root>/.git/swarmsync.lock` taken at server startup
and held for the server's life. These tests pin: it refuses a second server, it
is genuinely cross-PROCESS (not a Python-level lock), it is released on
shutdown, it does not confuse two different repos, and both the launcher and
`swarmsync doctor` surface it in words an operator can act on.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys

import pytest
from starlette.testclient import TestClient

from swarmsync import cli, repolock
from swarmsync.server import serve
from swarmsync.server.app import create_app
from swarmsync.worktree import git_ops


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A real git repo, set as the one managed root."""
    root = tmp_path / "repo"
    git_ops.init_repo(root)
    monkeypatch.setenv("SWARMSYNC_ROOTS", str(root))
    return root


def test_a_second_server_on_the_same_repo_refuses_to_boot(repo, tmp_path):
    """The bug: two `swarmsync-serve` processes, same --root, different DBs and
    ports. Neither MultiRootError nor ManagedRootMismatchError sees it."""
    app1 = create_app(tmp_path / "one.db", reaper_interval=None)
    app2 = create_app(tmp_path / "two.db", reaper_interval=None)

    with TestClient(app1) as c1:
        assert c1.get("/health").status_code == 200
        with pytest.raises(repolock.RepoLockHeldError) as excinfo:
            with TestClient(app2):
                pass

    message = str(excinfo.value)
    assert str(repo) in message, "the refusal must name the repo"
    assert str(repolock.lock_path_for(repo)) in message
    assert "already" in message.lower()


def test_the_lock_is_os_level_not_process_local(repo, tmp_path):
    """The whole point: an `asyncio.Lock`/`threading.Lock` would not stop ANOTHER
    PROCESS. Hold the lock from a real child process and prove the server refuses.

    This is the test that fails if the guard is ever downgraded to an in-process
    primitive -- which is exactly what the shipped `integrate_lock` was.
    """
    lock_path = repolock.lock_path_for(repo)
    assert lock_path is not None
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys\n"
            "fd = open(sys.argv[1], 'a+')\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "sys.stdout.write('locked\\n'); sys.stdout.flush()\n"
            "sys.stdin.readline()\n",
            str(lock_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdin is not None
        assert child.stdout.readline().strip() == "locked"

        with pytest.raises(repolock.RepoLockHeldError):
            with TestClient(create_app(tmp_path / "bb.db", reaper_interval=None)):
                pass
    finally:
        child.stdin.write("go\n")
        child.stdin.close()
        child.wait(timeout=10)


def test_the_lock_is_released_on_shutdown_so_a_restart_boots(repo, tmp_path):
    """A refusal that outlived the first server would make every restart fail."""
    with TestClient(create_app(tmp_path / "one.db", reaper_interval=None)) as c:
        assert c.get("/health").status_code == 200

    # Same repo, brand-new server: must boot cleanly.
    with TestClient(create_app(tmp_path / "two.db", reaper_interval=None)) as c:
        assert c.get("/health").status_code == 200


def test_two_servers_on_two_different_repos_both_boot(tmp_path, monkeypatch):
    """The guard is per-REPO, not global: coordinating two repos with two servers
    is the documented supported shape (see `check_single_root`)."""
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    git_ops.init_repo(repo_a)
    git_ops.init_repo(repo_b)

    monkeypatch.setenv("SWARMSYNC_ROOTS", str(repo_a))
    app_a = create_app(tmp_path / "a.db", reaper_interval=None)
    with TestClient(app_a) as ca:
        assert ca.get("/health").status_code == 200
        monkeypatch.setenv("SWARMSYNC_ROOTS", str(repo_b))
        with TestClient(create_app(tmp_path / "b.db", reaper_interval=None)) as cb:
            assert cb.get("/health").status_code == 200


def test_a_root_that_is_not_a_git_repo_is_not_locked(tmp_path, monkeypatch):
    """No `.git` means no trunk to corrupt and nowhere sanctioned to put the lock
    file, so the guard stands down rather than inventing a location. Stated
    behavior, pinned so it can't change silently."""
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("SWARMSYNC_ROOTS", str(plain))

    assert repolock.lock_path_for(plain) is None
    with TestClient(create_app(tmp_path / "one.db", reaper_interval=None)):
        with TestClient(create_app(tmp_path / "two.db", reaper_interval=None)) as c:
            assert c.get("/health").status_code == 200


def test_the_lock_file_records_the_holding_pid(repo, tmp_path):
    """So the refusal (and doctor) can name a process the operator can go kill."""
    with TestClient(create_app(tmp_path / "one.db", reaper_interval=None)):
        lock_path = repolock.lock_path_for(repo)
        assert lock_path is not None and lock_path.exists()
        assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
        assert repolock.holder_pid_if_held(repo) == os.getpid()

    assert repolock.holder_pid_if_held(repo) is None  # released


# --- the launcher's refusal posture (same as MultiRootError) ----------------------


def test_serve_main_refuses_to_start_when_the_repo_is_already_served(
    repo, tmp_path, monkeypatch, capsys
):
    """`swarmsync-serve` must fail with a readable SystemExit -- the posture it
    already uses for MultiRootError -- not a uvicorn startup traceback."""
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarmsync-serve", "--root", str(repo), "--db", str(tmp_path / "bb.db")],
    )

    with TestClient(create_app(tmp_path / "held.db", reaper_interval=None)):
        with pytest.raises(SystemExit) as excinfo:
            serve.main()

    message = str(excinfo.value)
    assert "swarm-sync" in message
    assert str(repo) in message


def test_serve_main_starts_normally_when_the_repo_is_free(repo, tmp_path, monkeypatch):
    ran: dict = {}
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: ran.update(ok=True))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarmsync-serve", "--root", str(repo), "--db", str(tmp_path / "bb.db")],
    )
    serve.main()
    assert ran == {"ok": True}


# --- swarmsync doctor surfaces it -------------------------------------------------


def _doctor(argv):
    from swarmsync.agent.client import BlackboardClient

    args = cli._build_parser().parse_args(argv)
    out = io.StringIO()
    code = cli.run(args, BlackboardClient("http://127.0.0.1:9", timeout=1), out)
    return code, out.getvalue()


def _check(text, label):
    for line in text.splitlines():
        if f"] {label}:" in line:
            return "ok" if line.lstrip().startswith("[ok") else "FAIL"
    return None


def test_doctor_flags_a_repo_already_served_by_an_unreachable_server(
    repo, tmp_path, monkeypatch
):
    """The operator's actual symptom: `swarmsync doctor` says the server is
    unreachable, and the real cause is that ANOTHER server already owns this repo
    on a different port. Doctor must say so instead of leaving them to guess."""
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    with TestClient(create_app(tmp_path / "held.db", reaper_interval=None)):
        code, text = _doctor(["--url", "http://127.0.0.1:9", "--timeout", "1", "doctor"])

    assert code != 0
    assert _check(text, "single server on this repo") == "FAIL"
    assert str(os.getpid()) in text, "doctor must name the holding pid"


def test_doctor_passes_the_repo_lock_check_when_nothing_holds_the_repo(
    repo, tmp_path, monkeypatch
):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    _, text = _doctor(["--url", "http://127.0.0.1:9", "--timeout", "1", "doctor"])

    assert _check(text, "single server on this repo") == "ok"


def test_doctor_reports_ok_when_the_reachable_server_is_the_lock_holder(
    repo, tmp_path, monkeypatch
):
    """The healthy steady state: a server is up on this repo AND holds its lock."""
    from swarmsync.agent.client import BlackboardClient

    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    app = create_app(tmp_path / "bb.db", reaper_interval=None)
    with TestClient(app) as tc:
        args = cli._build_parser().parse_args(["doctor"])
        out = io.StringIO()
        cli.run(args, BlackboardClient(tc), out)
        text = out.getvalue()

    assert _check(text, "single server on this repo") == "ok"
    assert _check(text, "managed root == repo") == "ok"
