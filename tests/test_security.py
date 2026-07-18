"""S3-security -- token auth, managed-root path allow-list, localhost bind, walk cap.

Each test here PROVES an S3 fix and fails on the pre-S3 source:
  - auth: with SWARMSYNC_TOKEN set, a mutating route without a matching bearer
    token is 401 (pre-S3 had no auth at all -> it was a 200);
  - path allow-list: POST /index / POST /integrate on a path outside
    SWARMSYNC_ROOTS (or a symlink escaping it) is 403 (pre-S3 walked/merged any
    path on the host);
  - index walk cap: index_repo raises IndexLimitError past its file cap;
  - main(): the (WP4.2-unified) launcher binds 127.0.0.1 by default + argparse
    --help works;
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
from swarmsync.server import serve as serve_mod
from swarmsync.server.app import create_app
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


def test_index_rejects_sibling_prefix_of_managed_root(monkeypatch, tmp_path, client):
    """M-2: a path whose PARENT DIR NAME merely extends the managed root is a DIFFERENT
    tree and must be rejected.

    The allow-list check is `real == root or real.startswith(root + os.sep)`. The
    `+ os.sep` is load-bearing: with managed root `.../managed`, the sibling `.../managed-evil`
    string-starts-with the root but is NOT under it. Dropping `+ os.sep` (the documented
    M-2 mutation survivor) makes a bare `startswith(root)` accept it -- the existing
    outside-path and symlink tests both use paths that are NOT string-prefixes of the
    root, so neither catches that mutation. This one does.
    """
    managed = _tiny_repo(tmp_path / "managed")
    # A SIBLING whose name extends the root string ("managed" is a prefix of
    # "managed-evil") but which lives in a completely different directory tree.
    evil = _tiny_repo(tmp_path / "managed-evil")
    monkeypatch.setenv("SWARMSYNC_ROOTS", str(managed))

    # sanity: the attacker path really IS a string-prefix match on the root...
    assert str(evil).startswith(str(managed))
    # ...yet it must be rejected, because it is not UNDER the managed root.
    r = client.post("/index", json={"root": str(evil)})
    assert r.status_code == 403

    # a real file inside that sibling tree is likewise rejected.
    r = client.post("/index", json={"root": str(evil / "mod_a.py")})
    assert r.status_code == 403

    # control: the genuine managed root is still accepted, so 403 above is the
    # boundary check firing, not a blanket refusal.
    assert client.post("/index", json={"root": str(managed)}).status_code == 200


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
# WP4.2: the `swarm-sync` console script is now an alias of `swarmsync-serve`
# (`serve.main`) -- app.py's second launcher (port 8000, no clock assertion) is
# gone. These tests keep asserting the S3 property (localhost bind by default,
# never 0.0.0.0) against the ONE launcher; the default port is 8787, the only
# port the hook adapter's default SWARMSYNC_URL ever matched.


def test_main_binds_localhost_by_default(monkeypatch, tmp_path):
    captured = {}

    def fake_run(app, host, port):
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("uvicorn.run", fake_run)
    serve_mod.main(["--db", str(tmp_path / "bb.db")])
    assert captured["host"] == "127.0.0.1"  # PROOF vs OLD: pre-S3 was 0.0.0.0
    assert captured["port"] == 8787  # WP4.2: the unified launcher's one default port


def test_main_host_is_overridable_via_argparse(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host, port: captured.update(host=host, port=port),
    )
    serve_mod.main(
        ["--host", "0.0.0.0", "--port", "9999", "--db", str(tmp_path / "bb.db")]
    )
    assert captured == {"host": "0.0.0.0", "port": 9999}


def test_main_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        serve_mod.main(["--help"])
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


# --- R4 mutation finding: the auth guards were present but UNDEFENDED --------------

# Every mutating route, with a minimal valid body. Kept as data so the test below can
# assert the list is COMPLETE against the running app -- a new mutating route added
# without a guard must fail here rather than ship silently.
MUTATING_ROUTES = [
    ("/index", {"root": "/tmp/nonexistent-for-auth-check"}),
    ("/intent", {"agent_id": "a", "task": "t", "target_parcels": ["x.py::<module>"]}),
    ("/lease", {"agent_id": "a", "parcel_id": "x.py::<module>", "mode": "write"}),
    ("/heartbeat", {"agent_id": "a", "lease_id": 1}),
    ("/release", {"agent_id": "a", "lease_id": 1}),
    ("/parcel/update", {"agent_id": "a", "parcel_id": "x.py::<module>", "content_hash": "h"}),
    ("/integrate", {"agent_id": "a", "branch": "b", "repo": "/tmp/nonexistent-for-auth-check"}),
]


@pytest.mark.parametrize("path, body", MUTATING_ROUTES, ids=[r[0] for r in MUTATING_ROUTES])
def test_every_mutating_route_401s_without_a_token(monkeypatch, client, path, body):
    """EVERY mutating route must reject an unauthenticated caller -- not just the two
    that happened to have a test.

    R4's mutation dimension deleted `dependencies=[Depends(require_token)]` from
    /heartbeat, /release, /parcel/update and /integrate, one at a time, and the full
    suite stayed GREEN each time: only /index and /lease had any negative auth
    assertion. The guards were present in source, so this was never a live hole -- it
    was a hole in the net that is supposed to keep them present. R3 reported three of
    these as undefended and they were still undefended two rounds later, which is
    exactly how a guard gets dropped by a refactor and ships.

    Asserting 401 (not 403/422): auth must be decided BEFORE the body is validated or
    the work is done, so a bogus path/lease_id in the body must not change the answer.
    """
    monkeypatch.setenv("SWARMSYNC_TOKEN", "s3cr3t")

    r = client.post(path, json=body)
    assert r.status_code == 401, f"{path} accepted a request with NO token"

    r = client.post(path, json=body, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401, f"{path} accepted a WRONG token"


def test_the_mutating_route_list_is_complete(client):
    """Pins the list above against the real app, so a newly-added POST route cannot
    quietly escape the auth check by simply not being listed here."""
    app_posts = {
        route.path
        for route in client.app.routes
        if getattr(route, "methods", None) and "POST" in route.methods
    }
    listed = {path for path, _ in MUTATING_ROUTES}
    missing = app_posts - listed
    assert not missing, (
        f"POST route(s) {sorted(missing)} are not covered by the auth test. Add them to "
        f"MUTATING_ROUTES (and give them a require_token guard) rather than deleting this."
    )


# --- R5: multi-root is data corruption, so the server must refuse to serve it ------


def test_app_refuses_to_start_with_multiple_managed_roots(monkeypatch, tmp_path):
    """The refusal must live in the SERVER, not only in the launcher.

    Parcel ids are `<relpath>::<symbol>` relative to the indexed root and carry no repo
    qualifier, so two roots that share a filename -- utils.py, __init__.py, conftest.py,
    i.e. essentially always -- produce the SAME id for different files: upsert
    overwrites one repo's rows with the other's, a write lease on that id locks BOTH
    repos' files, and integrate's re-index clobbers the other root wholesale. Silently.

    So it must fail at startup, on the path any deployment takes, not just via
    `swarmsync-serve`'s argv (an operator setting SWARMSYNC_ROOTS and running uvicorn
    directly must get the same answer).
    """
    from swarmsync.server.app import MultiRootError

    a, b = tmp_path / "repoA", tmp_path / "repoB"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("SWARMSYNC_ROOTS", os.pathsep.join([str(a), str(b)]))

    app = create_app(tmp_path / "bb.db")
    with pytest.raises(MultiRootError, match="ONE repo per server"):
        with TestClient(app):  # entering the context runs lifespan == startup
            pass


def test_app_starts_normally_with_exactly_one_root(monkeypatch, tmp_path):
    """The other half: one root is the supported case and must keep working."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("SWARMSYNC_ROOTS", str(repo))

    app = create_app(tmp_path / "bb.db")
    with TestClient(app) as c:
        assert c.get("/parcels").status_code == 200
