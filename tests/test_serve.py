"""Standalone launcher `swarmsync.server.serve` (swarmsync-serve entry point).

Smoke coverage: `main()` parses its argparse surface, builds a real blackboard
app via `create_app`, and hands it to `uvicorn.run` bound to localhost by
default. `uvicorn.run` is stubbed so the process never actually binds a socket.
"""
from __future__ import annotations

import os
import sys
import time

import pytest
from fastapi import FastAPI

from swarmsync.server import serve


def test_serve_main_builds_app_and_runs_uvicorn_on_localhost_defaults(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run(app, host, port):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setattr(sys, "argv", ["swarmsync-serve", "--db", str(tmp_path / "bb.db")])

    serve.main()

    assert isinstance(captured["app"], FastAPI)  # a real wired blackboard app
    assert captured["host"] == "127.0.0.1"  # localhost by default, not 0.0.0.0
    assert captured["port"] == 8787


def test_serve_main_host_and_port_are_overridable(monkeypatch, tmp_path):
    captured: dict = {}
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host, port: captured.update(host=host, port=port),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarmsync-serve", "--host", "0.0.0.0", "--port", "9191", "--db", str(tmp_path / "bb.db")],
    )

    serve.main()

    assert captured == {"host": "0.0.0.0", "port": 9191}


def test_serve_main_help_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["swarmsync-serve", "--help"])
    with pytest.raises(SystemExit) as exc:
        serve.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--host" in out and "--port" in out and "--db" in out


# --- R3 P1-10: managed roots must be explicit and visible at boot ------------------


def test_serve_root_flag_sets_the_single_managed_root(monkeypatch, tmp_path, capsys):
    """`--root` is the explicit way to name the ONE repo this server coordinates.

    Getting the root wrong does not raise -- it makes /index 403, which leaves the
    parcel map empty, which makes every hook fail open: silently NO coordination. So
    the root must be settable without knowing about an env var, and printed at boot
    where an operator will see it.
    """
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: None)
    monkeypatch.delenv("SWARMSYNC_ROOTS", raising=False)
    repo = tmp_path / "a"
    repo.mkdir()
    monkeypatch.setattr(
        sys, "argv", ["swarmsync-serve", "--db", str(tmp_path / "bb.db"), "--root", str(repo)]
    )

    serve.main()

    assert os.environ["SWARMSYNC_ROOTS"] == str(repo)
    out = capsys.readouterr().out
    assert "managed root" in out
    assert str(repo) in out, "the operator is never shown which root is active"


def test_serve_refuses_to_start_with_more_than_one_managed_root(monkeypatch, tmp_path, capsys):
    """Multi-root is data corruption with a plural-looking config, so refuse it.

    Parcel ids are `<relpath>::<symbol>` relative to the root, with no repo qualifier,
    so two roots sharing a filename (utils.py, __init__.py, conftest.py -- i.e. always)
    collide on the SAME parcel id: rows overwrite each other and a lease on one repo's
    file locks the other's. R4 found this while R3 had just shipped and documented a
    repeatable --root, which is exactly how an operator would hit it.
    """
    ran = []
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: ran.append(True))
    a, b = tmp_path / "repoA", tmp_path / "repoB"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("SWARMSYNC_ROOTS", os.pathsep.join([str(a), str(b)]))
    monkeypatch.setattr(sys, "argv", ["swarmsync-serve", "--db", str(tmp_path / "bb.db")])

    with pytest.raises(SystemExit) as excinfo:
        serve.main()

    assert ran == [], "the server started anyway on a corrupting config"
    assert "ONE repo per server" in str(excinfo.value)


def test_root_flag_is_not_repeatable(monkeypatch, tmp_path):
    """The repeatable --root was itself the footgun: it advertised a mode that
    corrupts the blackboard. argparse must reject the second one rather than silently
    keep the last."""
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: None)
    monkeypatch.delenv("SWARMSYNC_ROOTS", raising=False)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarmsync-serve", "--db", str(tmp_path / "bb.db"), "--root", str(a), "--root", str(b)],
    )
    # argparse keeps the LAST value for a non-append option; the important property is
    # that it can never produce a multi-root env.
    serve.main()
    assert os.pathsep not in os.environ["SWARMSYNC_ROOTS"]


def test_serve_announces_its_root_even_when_defaulted(monkeypatch, tmp_path, capsys):
    """With no --root and no SWARMSYNC_ROOTS the root defaults to the launch cwd --
    the exact silent-misconfiguration case -- so the boot line matters most here."""
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: None)
    monkeypatch.delenv("SWARMSYNC_ROOTS", raising=False)
    monkeypatch.setattr(sys, "argv", ["swarmsync-serve", "--db", str(tmp_path / "bb.db")])

    serve.main()

    out = capsys.readouterr().out
    assert "managed root" in out
    assert "403" in out, "the 403 consequence is not surfaced at boot"


# --- C13: startup refuses to serve on a bad host clock -----------------------------


def test_serve_starts_when_clocks_agree(monkeypatch, tmp_path):
    """The clock-agreement assertion must be a no-op under normal conditions: SQLite's
    julianday('now') and Python's time.time() agree to well under a second, so `main`
    proceeds to build the app and call uvicorn.run."""
    ran = []
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: ran.append(True))
    monkeypatch.setattr(sys, "argv", ["swarmsync-serve", "--db", str(tmp_path / "bb.db")])

    serve.main()

    assert ran == [True]


def test_serve_refuses_to_start_when_clocks_disagree(monkeypatch, tmp_path):
    """C13: lease liveness is checked on SQLite's clock while leases are stamped from
    Python's, an unstated cross-clock invariant. If the two disagree, a lease can look
    alive to one path and expired to the other (double-lease). Simulate a wrong host
    clock by pushing Python's time an hour off SQLite's; startup must refuse rather
    than serve a config that silently corrupts lease liveness."""
    ran = []
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: ran.append(True))
    real_time = time.time
    monkeypatch.setattr(serve.time, "time", lambda: real_time() + 3600.0)
    monkeypatch.setattr(sys, "argv", ["swarmsync-serve", "--db", str(tmp_path / "bb.db")])

    with pytest.raises(SystemExit) as excinfo:
        serve.main()

    assert ran == [], "the server started anyway on a skewed clock"
    assert "clock" in str(excinfo.value).lower()


def test_assert_clock_agreement_passes_in_this_environment():
    """Direct call: the real host's SQLite and Python clocks must agree (this is the
    invariant every other test implicitly relies on)."""
    serve.assert_clock_agreement()  # must not raise
