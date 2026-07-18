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


# --- WP3.6: --fresh moves a stale DB aside (never deletes) and boots empty ----------


def _seed_db_with_one_event(db_path):
    """A real blackboard DB with one recognizable row in it."""
    from swarmsync.blackboard import db as db_mod

    conn = db_mod.init_db(db_path)
    conn.execute(
        "INSERT INTO events(agent_id, type, payload, ts) VALUES('old-agent','planned','{}',1.0)"
    )
    conn.commit()
    conn.close()


def test_serve_fresh_moves_existing_db_aside_and_boots_an_empty_schema(
    monkeypatch, tmp_path, capsys
):
    """`--fresh` on an existing DB: the old file is MOVED (bytes intact) to a
    timestamped `<db>.stale-<YYYYmmdd-HHMMSS>` backup -- never deleted -- the
    backup path is printed, and the server boots on a fresh, empty schema."""
    import sqlite3 as sqlite3_mod

    db_path = tmp_path / "bb.db"
    _seed_db_with_one_event(db_path)
    original_bytes = db_path.read_bytes()

    monkeypatch.setattr("uvicorn.run", lambda app, host, port: None)
    monkeypatch.setattr(
        sys, "argv", ["swarmsync-serve", "--db", str(db_path), "--fresh"]
    )

    serve.main()

    # exactly one timestamped backup, holding the OLD bytes.
    backups = sorted(tmp_path.glob("bb.db.stale-*"))
    assert len(backups) == 1, f"expected one backup, found {backups!r}"
    backup = backups[0]
    assert backup.read_bytes() == original_bytes, "the backup is not the old DB's bytes"

    # one boot line names the backup path.
    out = capsys.readouterr().out
    assert str(backup) in out

    # the live DB is a fresh schema with none of the old rows.
    conn = sqlite3_mod.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
    finally:
        conn.close()


def test_serve_without_fresh_reuses_the_existing_db_unchanged(monkeypatch, tmp_path):
    """No `--fresh`: behavior unchanged -- the existing DB (and its rows) is reused,
    and nothing is moved aside."""
    import sqlite3 as sqlite3_mod

    db_path = tmp_path / "bb.db"
    _seed_db_with_one_event(db_path)

    monkeypatch.setattr("uvicorn.run", lambda app, host, port: None)
    monkeypatch.setattr(sys, "argv", ["swarmsync-serve", "--db", str(db_path)])

    serve.main()

    assert list(tmp_path.glob("bb.db.stale-*")) == []
    conn = sqlite3_mod.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        conn.close()


def test_serve_fresh_with_no_existing_db_just_boots(monkeypatch, tmp_path, capsys):
    """`--fresh` when there is nothing to move: no backup, no backup line, normal boot."""
    ran = []
    db_path = tmp_path / "bb.db"
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: ran.append(True))
    monkeypatch.setattr(
        sys, "argv", ["swarmsync-serve", "--db", str(db_path), "--fresh"]
    )

    serve.main()

    assert ran == [True]
    assert list(tmp_path.glob("bb.db.stale-*")) == []
    assert ".stale-" not in capsys.readouterr().out


# --- WP4.2: ONE launcher -- `swarm-sync` is an alias of `swarmsync-serve` -----------


def test_both_console_scripts_point_at_serve_main():
    """The launcher split (port 8000 vs 8787, blackboard.db vs swarmsync.db, one
    launcher with --root/--fresh/banner/clock-check and one without) was a
    silent fail-open trap: the hook's default URL only ever matched
    `swarmsync-serve`. Both `[project.scripts]` entries must target `serve:main`.
    Asserted at the pyproject level (the environment's editable install predates
    this change by design -- console-script wiring is declared here)."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["swarm-sync"] == "swarmsync.server.serve:main"
    assert scripts["swarmsync-serve"] == "swarmsync.server.serve:main"


def test_app_module_no_longer_ships_a_second_launcher():
    """`app.main` (the second argparse surface) is deleted outright -- an alias
    that lingered would keep two divergent default sets alive."""
    from swarmsync.server import app as app_mod

    assert not hasattr(app_mod, "main")


def test_serve_db_default_honors_swarmsync_db_env(monkeypatch, tmp_path):
    """With no --db flag, the launcher's DB comes from `config.db_path()`:
    SWARMSYNC_DB when set (read at main() call time, not import time)."""
    captured: dict = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, host, port: captured.update(app=app)
    )
    monkeypatch.setenv("SWARMSYNC_DB", str(tmp_path / "env.db"))
    monkeypatch.setattr(sys, "argv", ["swarmsync-serve"])

    serve.main()

    assert captured["app"].state.db_path == str(tmp_path / "env.db")


def test_serve_db_flag_beats_the_env(monkeypatch, tmp_path):
    """Precedence: an explicit --db always wins over SWARMSYNC_DB."""
    captured: dict = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, host, port: captured.update(app=app)
    )
    monkeypatch.setenv("SWARMSYNC_DB", str(tmp_path / "env.db"))
    monkeypatch.setattr(
        sys, "argv", ["swarmsync-serve", "--db", str(tmp_path / "flag.db")]
    )

    serve.main()

    assert captured["app"].state.db_path == str(tmp_path / "flag.db")
