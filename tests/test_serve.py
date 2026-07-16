"""Standalone launcher `swarmsync.server.serve` (swarmsync-serve entry point).

Smoke coverage: `main()` parses its argparse surface, builds a real blackboard
app via `create_app`, and hands it to `uvicorn.run` bound to localhost by
default. `uvicorn.run` is stubbed so the process never actually binds a socket.
"""
from __future__ import annotations

import os
import sys

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


def test_serve_root_flag_sets_managed_roots(monkeypatch, tmp_path, capsys):
    """`--root` is the explicit way to say which repo this server may coordinate.

    Getting the managed roots wrong does not raise -- it makes /index 403, which
    leaves the parcel map empty, which makes every hook fail open: silently NO
    coordination. So the roots must be settable without knowing about an env var,
    and must be printed at boot where an operator will actually see them.
    """
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: None)
    monkeypatch.delenv("SWARMSYNC_ROOTS", raising=False)
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarmsync-serve",
            "--db",
            str(tmp_path / "bb.db"),
            "--root",
            str(repo_a),
            "--root",
            str(repo_b),
        ],
    )

    serve.main()

    roots = os.environ["SWARMSYNC_ROOTS"].split(os.pathsep)
    assert str(repo_a) in roots and str(repo_b) in roots

    out = capsys.readouterr().out
    assert "managed roots" in out
    assert str(repo_a) in out, "the operator is never shown which roots are active"


def test_serve_announces_managed_roots_even_when_defaulted(monkeypatch, tmp_path, capsys):
    """With no --root and no SWARMSYNC_ROOTS the roots default to the launch cwd --
    the exact silent-misconfiguration case -- so the boot line matters most here."""
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: None)
    monkeypatch.delenv("SWARMSYNC_ROOTS", raising=False)
    monkeypatch.setattr(sys, "argv", ["swarmsync-serve", "--db", str(tmp_path / "bb.db")])

    serve.main()

    out = capsys.readouterr().out
    assert "managed roots" in out
    assert "403" in out, "the 403 consequence is not surfaced at boot"
