"""Standalone launcher `swarmsync.server.serve` (swarmsync-serve entry point).

Smoke coverage: `main()` parses its argparse surface, builds a real blackboard
app via `create_app`, and hands it to `uvicorn.run` bound to localhost by
default. `uvicorn.run` is stubbed so the process never actually binds a socket.
"""
from __future__ import annotations

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
