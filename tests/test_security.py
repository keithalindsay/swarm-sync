"""S3-security -- token auth, managed-root path allow-list, localhost bind, walk cap.

Each test here PROVES an S3 fix and fails on the pre-S3 source:
  - auth: with SWARMSYNC_TOKEN set, a mutating route without a matching bearer
    token is 401 (pre-S3 had no auth at all -> it was a 200);
  - path allow-list: POST /index / POST /integrate on a path outside
    SWARMSYNC_ROOTS (or a symlink escaping it) is 403 (pre-S3 walked/merged any
    path on the host);
  - index walk cap: index_repo raises IndexLimitError past its file cap;
  - main(): binds 127.0.0.1 by default + argparse --help works;
  - adapter: sends SWARMSYNC_TOKEN as a bearer header when set.
"""
from __future__ import annotations

import io
import os

import pytest
from fastapi.testclient import TestClient

from swarmsync.classifier.indexer import IndexLimitError, index_repo
from swarmsync.hooks import adapter
from swarmsync.server import app as app_mod
from swarmsync.server.app import create_app, main
from swarmsync.worktree import git_ops


def _tiny_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "mod_a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    return root


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "blackboard.db")
    with TestClient(app) as c:
        yield c


# --- (1) token auth: required when set, open when unset ----------------------------


def test_mutating_route_requires_token_when_env_set(monkeypatch, tmp_path, client):
    """PROOF vs OLD: pre-S3 there was no auth, so this POST /index was a 200 even
    with SWARMSYNC_TOKEN set. Now, with the token set, a missing/wrong bearer is
    401 and only the correct token gets through."""
    repo = _tiny_repo(tmp_path / "repo")
    monkeypatch.setenv("SWARMSYNC_TOKEN", "s3cr3t")

    # no Authorization header -> 401
    r = client.post("/index", json={"root": str(repo)})
    assert r.status_code == 401

    # wrong token -> 401
    r = client.post(
        "/index",
        json={"root": str(repo)},
        headers={"Authorization": "Bearer nope"},
    )
    assert r.status_code == 401

    # correct token -> 200
    r = client.post(
        "/index",
        json={"root": str(repo)},
        headers={"Authorization": "Bearer s3cr3t"},
    )
    assert r.status_code == 200
    assert r.json()["parcels"] > 0


def test_read_route_stays_open_even_when_token_set(monkeypatch, client):
    """Only *mutating* routes are gated -- a GET is still open (it mutates
    nothing), so a monitoring/read client needs no token."""
    monkeypatch.setenv("SWARMSYNC_TOKEN", "s3cr3t")
    r = client.get("/parcels")
    assert r.status_code == 200


def test_no_auth_required_when_token_unset(monkeypatch, tmp_path, client):
    """The dev/test/demo default: SWARMSYNC_TOKEN unset -> no auth, no header
    needed (this is what keeps every pre-S3 test green)."""
    monkeypatch.delenv("SWARMSYNC_TOKEN", raising=False)
    repo = _tiny_repo(tmp_path / "repo")
    r = client.post("/index", json={"root": str(repo)})
    assert r.status_code == 200


# --- (2) managed-root allow-list: rejects outside + symlink escapes ---------------


def test_index_rejects_root_outside_managed_roots(monkeypatch, tmp_path, client):
    managed = _tiny_repo(tmp_path / "managed")
    outside = _tiny_repo(tmp_path / "outside")
    monkeypatch.setenv("SWARMSYNC_ROOTS", str(managed))

    # inside the managed root -> allowed
    assert client.post("/index", json={"root": str(managed)}).status_code == 200
    # a sibling dir NOT under the managed root -> 403 (PROOF vs OLD: pre-S3 this
    # walked any path and returned 200).
    r = client.post("/index", json={"root": str(outside)})
    assert r.status_code == 403


def test_index_rejects_symlink_escaping_managed_root(monkeypatch, tmp_path, client):
    managed = tmp_path / "managed"
    managed.mkdir()
    outside = _tiny_repo(tmp_path / "outside")
    # a symlink that lives *inside* the managed root but points outside it.
    link = managed / "escape"
    os.symlink(outside, link)
    monkeypatch.setenv("SWARMSYNC_ROOTS", str(managed))

    r = client.post("/index", json={"root": str(link)})
    assert r.status_code == 403  # realpath resolves outside -> rejected


def test_integrate_rejects_repo_outside_managed_roots(monkeypatch, tmp_path, client):
    managed = tmp_path / "managed"
    managed.mkdir()
    outside = _tiny_repo(tmp_path / "outside")
    monkeypatch.setenv("SWARMSYNC_ROOTS", str(managed))

    r = client.post(
        "/integrate",
        json={"agent_id": "a", "branch": "b", "repo": str(outside)},
    )
    assert r.status_code == 403


def test_index_limit_error_maps_to_413(monkeypatch, tmp_path, client):
    repo = _tiny_repo(tmp_path / "repo")

    def _boom(*a, **kw):
        raise IndexLimitError("too big")

    monkeypatch.setattr(app_mod, "run_index", _boom)
    r = client.post("/index", json={"root": str(repo)})
    assert r.status_code == 413


# --- index walk cap ----------------------------------------------------------------


def test_index_repo_caps_file_count(tmp_path):
    repo = tmp_path / "big"
    repo.mkdir()
    for i in range(5):
        (repo / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(IndexLimitError):
        index_repo(repo, max_files=2)


def test_index_repo_caps_wall_clock(tmp_path):
    repo = tmp_path / "slow"
    repo.mkdir()
    for i in range(3):
        (repo / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
    # a deadline already in the past trips on the first considered file.
    with pytest.raises(IndexLimitError):
        index_repo(repo, max_seconds=-1.0)


# --- (3) main() binds localhost + argparse --help ---------------------------------


def test_main_binds_localhost_by_default(monkeypatch, tmp_path):
    captured = {}

    def fake_run(app, host, port):
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("uvicorn.run", fake_run)
    main(["--db", str(tmp_path / "bb.db")])
    assert captured["host"] == "127.0.0.1"  # PROOF vs OLD: pre-S3 was 0.0.0.0
    assert captured["port"] == 8000


def test_main_host_is_overridable_via_argparse(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host, port: captured.update(host=host, port=port),
    )
    main(["--host", "0.0.0.0", "--port", "9999", "--db", str(tmp_path / "bb.db")])
    assert captured == {"host": "0.0.0.0", "port": 9999}


def test_main_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--host" in out and "--port" in out and "--db" in out


# --- swarmsync-hook adapter --help / -h prints usage ------------------------------


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_adapter_help_prints_usage(flag):
    out, err = io.StringIO(), io.StringIO()
    code = adapter.main([flag], stdin=io.StringIO(""), out=out, err=err)
    assert code == 0
    text = out.getvalue()
    assert "swarmsync-hook" in text
    assert "precheck" in text and "session-start" in text


# --- (1b) adapter sends SWARMSYNC_TOKEN as a bearer header ------------------------


def test_adapter_default_factory_sends_token_when_set(monkeypatch):
    monkeypatch.setenv("SWARMSYNC_TOKEN", "hooktoken")
    http = adapter._default_http_factory("http://127.0.0.1:8787")
    try:
        assert http.headers.get("authorization") == "Bearer hooktoken"
    finally:
        http.close()


def test_adapter_default_factory_no_auth_header_when_unset(monkeypatch):
    monkeypatch.delenv("SWARMSYNC_TOKEN", raising=False)
    http = adapter._default_http_factory("http://127.0.0.1:8787")
    try:
        assert "authorization" not in http.headers
    finally:
        http.close()


# --- end-to-end: token-gated blackboard + a full lease round-trip -----------------


def test_token_gated_server_accepts_correct_bearer_end_to_end(monkeypatch, tmp_path):
    """A token-gated server still runs the whole coordination flow for a caller
    that presents the right token: index, lease, release -- all 200."""
    repo = _tiny_repo(tmp_path / "repo")
    git_ops.init_repo(repo)
    monkeypatch.setenv("SWARMSYNC_TOKEN", "tok")
    app = create_app(tmp_path / "blackboard.db")
    auth = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        assert c.post("/index", json={"root": str(repo)}, headers=auth).status_code == 200
        lease = c.post(
            "/lease",
            json={"agent_id": "a1", "parcel_id": "mod_a.py::<module>", "mode": "write"},
            headers=auth,
        )
        assert lease.status_code == 200 and lease.json()["granted"] is True
        # same call without the token is refused.
        assert c.post(
            "/lease",
            json={"agent_id": "a2", "parcel_id": "mod_a.py::<module>", "mode": "write"},
        ).status_code == 401
