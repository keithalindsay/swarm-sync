"""swarmsync/hooks/adapter.py -- Claude Code HOOKS adapter for lease coordination.

Drives `adapter.main()` exactly the way Claude Code's hook runner would: a
JSON payload on stdin, a subcommand as `argv[0]`. The blackboard is a real
`fastapi.testclient.TestClient` wrapping `create_app` (no real network/server
needed -- `BlackboardClient` and this adapter both duck-type over it), injected
via `http_factory` so no `httpx.Client`/real socket is ever created.

Coverage (see this unit's brief):
  - inactive repo -> silent no-op ALLOW for every subcommand, zero network calls
  - non-edit tool -> ALLOW
  - free parcel -> acquire + ALLOW
  - parcel held by another agent -> DENY with the exact reason string
  - parcel held by the SAME agent -> ALLOW (no self-deny, no duplicate acquire)
  - agent_id fallback: agent_id -> session_id -> "main"
  - blackboard unreachable / raises -> fail-open ALLOW
  - malformed stdin -> fail-open ALLOW
  - postupdate re-hashes the edited file and posts content_hash/state_summary
  - release releases only the calling agent's own active leases
  - session-start POSTs /index when reachable, no-ops when not
"""
from __future__ import annotations

import io
import json
import textwrap
import time

import pytest
from fastapi.testclient import TestClient

from swarmsync.classifier.indexer import MODULE_SYMBOL, parse_file
from swarmsync.hooks import adapter
from swarmsync.server.app import create_app


# --- fixtures ----------------------------------------------------------------------


@pytest.fixture()
def repo(tmp_path):
    """Two-file repo so lease-filtering (release) has more than one parcel."""
    (tmp_path / "mod_a.py").write_text(
        textwrap.dedent(
            """\
            def helper(x, y=1):
                return x + y
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "mod_b.py").write_text(
        textwrap.dedent(
            """\
            def other(z):
                return z * 2
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "mod_c.py").write_text(
        textwrap.dedent(
            """\
            def another(w):
                return w - 1
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def test_client(tmp_path):
    app = create_app(tmp_path / "blackboard.db")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def indexed_client(test_client, repo):
    """The same TestClient, with `repo` already `/index`-ed."""
    r = test_client.post("/index", json={"root": str(repo)})
    assert r.status_code == 200
    assert r.json()["parcels"] > 0
    return test_client


def _http_factory(client):
    """`http_factory` the adapter expects: base_url -> http-like object.
    The TestClient is already bound to the app; the base_url argument is
    irrelevant to it, so this simply ignores it."""
    return lambda base_url: client


def _payload(tool_name, file_path=None, cwd=None, agent_id=None, session_id=None, **extra):
    body = {
        "tool_name": tool_name,
        "tool_input": ({"file_path": file_path} if file_path else {}),
        "cwd": cwd,
    }
    if agent_id is not None:
        body["agent_id"] = agent_id
    if session_id is not None:
        body["session_id"] = session_id
    body.update(extra)
    return body


def _run(subcommand, payload, http_factory=None):
    """Drive `adapter.main` and return (exit_code, stdout_text, stderr_text)."""
    out, err = io.StringIO(), io.StringIO()
    code = adapter.main(
        [subcommand],
        stdin=io.StringIO(json.dumps(payload)),
        http_factory=http_factory,
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue()


class _ExplodingHttp:
    """Stands in for an unreachable/broken blackboard: every call raises."""

    def get(self, *a, **kw):
        raise RuntimeError("connection refused (simulated)")

    def post(self, *a, **kw):
        raise RuntimeError("connection refused (simulated)")


class _ExplodingFactory:
    """Fails the test if the adapter ever tries to build an http client at
    all -- used to prove the inactive-repo path makes zero network calls."""

    def __call__(self, base_url):
        raise AssertionError("http_factory should not be called when inactive")


# --- inactive repo: silent no-op ALLOW, zero network calls -------------------------


def test_inactive_repo_is_a_silent_noop_allow(monkeypatch, repo):
    monkeypatch.delenv("SWARMSYNC_ACTIVE", raising=False)
    assert not (repo / ".swarmsync-active").exists()

    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("precheck", payload, http_factory=_ExplodingFactory())

    assert code == 0
    assert out == ""
    assert err == ""


def test_inactive_repo_release_and_postupdate_and_session_start_are_also_noop(monkeypatch, repo):
    monkeypatch.delenv("SWARMSYNC_ACTIVE", raising=False)
    for subcommand in ("release", "postupdate", "session-start"):
        payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="a1")
        code, out, err = _run(subcommand, payload, http_factory=_ExplodingFactory())
        assert code == 0
        assert out == err == ""


# --- activation via marker file (the other opt-in path) ----------------------------


def test_marker_file_activates_without_env_var(monkeypatch, repo, indexed_client):
    monkeypatch.delenv("SWARMSYNC_ACTIVE", raising=False)
    (repo / ".swarmsync-active").touch()

    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    assert out == ""
    leases = indexed_client.get("/leases").json()
    assert any(lease["agent_id"] == "agent-1" for lease in leases)


# --- non-edit tool -> ALLOW ---------------------------------------------------------


def test_non_edit_tool_allows_without_leasing(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload("Bash", cwd=str(repo), agent_id="agent-1", command="ls")
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    assert out == ""
    assert indexed_client.get("/leases").json() == []


# --- free parcel -> acquire + ALLOW -------------------------------------------------


def test_free_parcel_acquires_write_lease_and_allows(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    assert out == ""  # ALLOW prints nothing

    leases = indexed_client.get("/leases").json()
    assert len(leases) == 1
    assert leases[0]["parcel_id"] == f"mod_a.py::{MODULE_SYMBOL}"
    assert leases[0]["agent_id"] == "agent-1"
    assert leases[0]["mode"] == "write"


def test_relative_file_path_resolves_against_cwd(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload("Write", file_path="mod_a.py", cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    assert out == ""
    leases = indexed_client.get("/leases").json()
    assert leases[0]["parcel_id"] == f"mod_a.py::{MODULE_SYMBOL}"


# --- held by another agent -> DENY with the exact reason string -------------------


def test_parcel_held_by_another_agent_denies_with_reason(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    held = indexed_client.post(
        "/lease",
        json={"agent_id": "agent-0", "parcel_id": f"mod_a.py::{MODULE_SYMBOL}", "mode": "write"},
    ).json()
    assert held["granted"] is True

    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    assert out != ""
    decision = json.loads(out)
    assert decision == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "swarm-sync: mod_a.py is leased by agent-0; "
                "pick different work or retry shortly."
            ),
        }
    }

    # agent-1 never actually acquired anything -- only agent-0's lease exists.
    leases = indexed_client.get("/leases").json()
    assert len(leases) == 1
    assert leases[0]["agent_id"] == "agent-0"


# --- held by the SAME agent -> ALLOW, no self-deny, no duplicate acquire ----------


def test_parcel_held_by_same_agent_allows_no_self_deny(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    held = indexed_client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": f"mod_a.py::{MODULE_SYMBOL}", "mode": "write"},
    ).json()
    assert held["granted"] is True

    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    assert out == ""  # ALLOW -- re-editing your own leased file must not self-deny

    # still exactly one active lease -- precheck did not try to acquire again.
    leases = indexed_client.get("/leases").json()
    assert len(leases) == 1
    assert leases[0]["agent_id"] == "agent-1"
    assert leases[0]["id"] == held["lease_id"]


# --- C8 (P2): the batched-Edit race must not make an agent deny ITSELF -----------


class _StaleThenRealLeases:
    """Wraps a real `BlackboardClient`, but the FIRST `.leases()` read reports an
    empty board -- the exact stale view precheck B holds in Claude Code's batched
    parallel-Edit race: agent A's write lease has already landed on the server, but
    B's `GET /leases` predates it, so B sees nothing and takes the acquire path.
    `lease()`/`heartbeat()` and every later `leases()` hit the REAL backend, so the
    CAS (and the deny-path re-read) see A's real lease."""

    def __init__(self, real):
        self._real = real
        self._reads = 0

    def leases(self):
        self._reads += 1
        return [] if self._reads == 1 else self._real.leases()

    def lease(self, *a, **kw):
        return self._real.lease(*a, **kw)

    def heartbeat(self, *a, **kw):
        return self._real.heartbeat(*a, **kw)


def test_precheck_batched_edit_race_does_not_self_deny(monkeypatch, repo, indexed_client):
    """Two parallel Edits from ONE agent: both prechecks see no lease and both POST
    /lease. The second, losing the CAS pre-fix, re-read the holder and named it in the
    deny WITHOUT checking it was ITSELF -- blocking the agent from a file it just
    locked, blaming itself. With acquire made idempotent for the same
    (parcel, agent, mode), the second precheck's acquire is GRANTED and precheck
    returns ALLOW (None); the self-naming deny path is never reached."""
    from swarmsync.agent.client import BlackboardClient

    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    real = BlackboardClient(indexed_client)
    parcel_id = f"mod_a.py::{MODULE_SYMBOL}"

    # precheck A already landed agent-A's write lease on the server.
    assert real.lease("agent-A", parcel_id, mode="write", ensure_parcel=True)["granted"]

    # precheck B runs with a stale (pre-A) read -> acquire path. Must ALLOW, and must
    # NEVER emit a deny that names agent-A (itself) as the blocker.
    client = _StaleThenRealLeases(real)
    result = adapter.cmd_precheck(
        "Edit", {"file_path": str(repo / "mod_a.py")}, client, repo, "agent-A"
    )
    assert result is None, f"batched-edit race self-denied: {result}"


# --- agent_id fallback chain: agent_id -> session_id -> "main" -------------------


def test_agent_id_falls_back_to_session_id_when_absent(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload(
        "Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), session_id="sess-42"
    )
    assert "agent_id" not in payload
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    leases = indexed_client.get("/leases").json()
    assert leases[0]["agent_id"] == "sess-42"


def test_agent_id_falls_back_to_main_when_neither_present(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo))
    assert "agent_id" not in payload and "session_id" not in payload
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    leases = indexed_client.get("/leases").json()
    assert leases[0]["agent_id"] == "main"


# --- blackboard unreachable/raises -> fail-open ALLOW -----------------------------


def test_blackboard_unreachable_fails_open_allow(monkeypatch, repo):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("precheck", payload, http_factory=lambda base_url: _ExplodingHttp())

    assert code == 0
    assert out == ""  # never a deny on an internal/connectivity error
    assert "failing open" in err


def test_blackboard_unreachable_fails_open_for_every_subcommand(monkeypatch, repo):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    for subcommand in ("precheck", "postupdate", "release", "session-start"):
        payload = _payload(
            "Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="agent-1"
        )
        code, out, err = _run(subcommand, payload, http_factory=lambda base_url: _ExplodingHttp())
        assert code == 0
        assert out == ""
        assert "failing open" in err


# --- malformed stdin -> fail-open ALLOW -------------------------------------------


def test_malformed_stdin_fails_open_allow(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    out, err = io.StringIO(), io.StringIO()
    code = adapter.main(
        ["precheck"],
        stdin=io.StringIO("{not valid json at all"),
        http_factory=_http_factory(indexed_client),
        out=out,
        err=err,
    )
    assert code == 0
    assert out.getvalue() == ""
    assert "failing open" in err.getvalue()


def test_empty_stdin_fails_open_allow(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    out, err = io.StringIO(), io.StringIO()
    code = adapter.main(
        ["precheck"],
        stdin=io.StringIO(""),
        http_factory=_http_factory(indexed_client),
        out=out,
        err=err,
    )
    # Empty stdin parses to `{}` -- no tool_name, no cwd override (falls back
    # to os.getcwd()), no agent_id -- this must NOT be treated as an error at
    # all (it is valid "nothing to do" input), just an ALLOW no-op.
    assert code == 0
    assert out.getvalue() == ""


# --- postupdate: re-hashes the edited file, posts content_hash + summary ---------


def test_postupdate_rehashes_file_and_updates_parcel(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    # Simulate the tool having already applied the edit on disk (PostToolUse
    # fires after the edit), then this hook re-derives the hash from it.
    (repo / "mod_a.py").write_text(
        "def helper(x, y=1):\n    return x + y + 999\n", encoding="utf-8"
    )
    expected_parcels = parse_file(repo / "mod_a.py", rel_path="mod_a.py")
    expected = next(p for p in expected_parcels if p.id == f"mod_a.py::{MODULE_SYMBOL}")

    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("postupdate", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    row = {p["id"]: p for p in indexed_client.get("/parcels").json()}[
        f"mod_a.py::{MODULE_SYMBOL}"
    ]
    assert row["content_hash"] == expected.content_hash
    assert "agent-1" in row["state_summary"]
    assert "mod_a.py" in row["state_summary"]


def test_postupdate_is_deterministic_for_unchanged_content(monkeypatch, repo, indexed_client):
    """Same file content + same agent_id -> byte-identical state_summary on a
    second run (DESIGN's `state_summary` heuristic is deterministic, no
    wall-clock/random component)."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="agent-1")

    _run("postupdate", payload, http_factory=_http_factory(indexed_client))
    first = {p["id"]: p for p in indexed_client.get("/parcels").json()}[
        f"mod_a.py::{MODULE_SYMBOL}"
    ]["state_summary"]

    _run("postupdate", payload, http_factory=_http_factory(indexed_client))
    second = {p["id"]: p for p in indexed_client.get("/parcels").json()}[
        f"mod_a.py::{MODULE_SYMBOL}"
    ]["state_summary"]

    assert first == second


def test_postupdate_non_edit_tool_is_noop(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    before = {p["id"]: p for p in indexed_client.get("/parcels").json()}[
        f"mod_a.py::{MODULE_SYMBOL}"
    ]
    payload = _payload("Bash", cwd=str(repo), agent_id="agent-1", command="ls")
    code, out, err = _run("postupdate", payload, http_factory=_http_factory(indexed_client))
    assert code == 0
    after = {p["id"]: p for p in indexed_client.get("/parcels").json()}[
        f"mod_a.py::{MODULE_SYMBOL}"
    ]
    assert before == after


# --- release: only this agent's own active leases ---------------------------------


def test_release_only_releases_calling_agents_leases(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    lease_a1 = indexed_client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": f"mod_a.py::{MODULE_SYMBOL}", "mode": "write"},
    ).json()
    lease_a2 = indexed_client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": f"mod_b.py::{MODULE_SYMBOL}", "mode": "write"},
    ).json()
    lease_b = indexed_client.post(
        "/lease",
        json={"agent_id": "agent-2", "parcel_id": f"mod_c.py::{MODULE_SYMBOL}", "mode": "read"},
    ).json()
    assert lease_a1["granted"] and lease_a2["granted"] and lease_b["granted"]

    payload = _payload("Stop", cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("release", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    leases = indexed_client.get("/leases").json()
    assert [lease["agent_id"] for lease in leases] == ["agent-2"]


def test_release_is_a_noop_when_agent_holds_no_leases(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload("Stop", cwd=str(repo), agent_id="agent-nobody")
    code, out, err = _run("release", payload, http_factory=_http_factory(indexed_client))
    assert code == 0
    assert indexed_client.get("/leases").json() == []


# --- session-start: POST /index when reachable, no-op otherwise ------------------


def test_session_start_indexes_repo_when_reachable(monkeypatch, repo, test_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    assert test_client.get("/parcels").json() == []

    payload = _payload("", cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("session-start", payload, http_factory=_http_factory(test_client))

    assert code == 0
    assert test_client.get("/parcels").json() != []


def test_session_start_noop_when_blackboard_unreachable(monkeypatch, repo):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload("", cwd=str(repo), agent_id="agent-1")
    code, out, err = _run(
        "session-start", payload, http_factory=lambda base_url: _ExplodingHttp()
    )
    assert code == 0
    assert "failing open" in err


# --- file outside the repo root: nothing to lease, ALLOW --------------------------


def test_file_path_outside_repo_root_allows(monkeypatch, repo, indexed_client, tmp_path_factory):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    outside = tmp_path_factory.mktemp("elsewhere") / "other.py"
    outside.write_text("x = 1\n", encoding="utf-8")

    payload = _payload("Edit", file_path=str(outside), cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    assert out == ""
    assert indexed_client.get("/leases").json() == []


# --- unknown subcommand: no-op ALLOW, not an error --------------------------------


def test_unknown_subcommand_is_a_noop_allow(monkeypatch, repo, indexed_client):
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("some-future-hook", payload, http_factory=_http_factory(indexed_client))
    assert code == 0


# --- S5 keepalive: precheck/postupdate refresh the lease TTL -----------------------


def _lease_for(client, parcel_id):
    return next(
        (ls for ls in client.get("/leases").json() if ls["parcel_id"] == parcel_id),
        None,
    )


def test_precheck_refreshes_ttl_on_own_held_lease(monkeypatch, repo, indexed_client):
    """S5: a precheck for a file THIS agent already leases must bump the lease's
    TTL (keepalive), not just silently ALLOW -- otherwise the 30s server TTL
    expires during think time and the reaper hands the file to someone else.
    Fails on pre-S5 (precheck returned None with no heartbeat)."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    monkeypatch.setenv("SWARMSYNC_LEASE_TTL", "0.5")
    parcel_id = f"mod_a.py::{MODULE_SYMBOL}"
    held = indexed_client.post(
        "/lease",
        json={"agent_id": "a1", "parcel_id": parcel_id, "mode": "write", "ttl": 0.5},
    ).json()
    assert held["granted"]
    before = _lease_for(indexed_client, parcel_id)

    time.sleep(0.2)  # still inside the 0.5s window -- lease is live
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="a1")
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))
    assert code == 0
    assert out == ""  # ALLOW (own lease), no self-deny

    after = _lease_for(indexed_client, parcel_id)
    assert after is not None
    assert after["id"] == before["id"]  # SAME lease -- renewed, not re-acquired
    assert after["ttl_expires_at"] > before["ttl_expires_at"]  # TTL pushed forward


def test_postupdate_refreshes_ttl_on_own_held_lease(monkeypatch, repo, indexed_client):
    """S5: postupdate must also renew the lease TTL. Fails on pre-S5 (postupdate
    only re-hashed the file, never touched the lease)."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    monkeypatch.setenv("SWARMSYNC_LEASE_TTL", "0.5")
    parcel_id = f"mod_a.py::{MODULE_SYMBOL}"
    held = indexed_client.post(
        "/lease",
        json={"agent_id": "a1", "parcel_id": parcel_id, "mode": "write", "ttl": 0.5},
    ).json()
    assert held["granted"]
    before = _lease_for(indexed_client, parcel_id)

    time.sleep(0.2)
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="a1")
    code, out, err = _run("postupdate", payload, http_factory=_http_factory(indexed_client))
    assert code == 0

    after = _lease_for(indexed_client, parcel_id)
    assert after is not None
    assert after["id"] == before["id"]
    assert after["ttl_expires_at"] > before["ttl_expires_at"]


def test_keepalive_prevents_expiry_across_a_ttl_window(monkeypatch, repo, indexed_client):
    """S5 (the named regression): a hook-held lease survives a wall-clock window
    LONGER than a single TTL because each postupdate keeps renewing it -- and a
    different agent stays locked out the whole time. Pre-S5 the lease expired one
    TTL after acquisition (postupdate never renewed) and the other agent could
    grab the file."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    monkeypatch.setenv("SWARMSYNC_LEASE_TTL", "0.4")
    parcel_id = f"mod_a.py::{MODULE_SYMBOL}"
    # Seed a1's short-TTL lease directly (stand-in for the initial acquire); the
    # behavior under test is that postupdate keepalive keeps it alive from here.
    held = indexed_client.post(
        "/lease",
        json={"agent_id": "a1", "parcel_id": parcel_id, "mode": "write", "ttl": 0.4},
    ).json()
    assert held["granted"]

    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="a1")
    for _ in range(6):  # 6 * 0.15s = 0.9s elapsed, well past the 0.4s TTL
        time.sleep(0.15)
        _run("postupdate", payload, http_factory=_http_factory(indexed_client))

    active = _lease_for(indexed_client, parcel_id)
    assert active is not None, "keepalive should have kept a1's lease alive"
    assert active["id"] == held["lease_id"]
    assert active["agent_id"] == "a1"

    # a different agent is still denied -- the one-agent-per-file promise held.
    other = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="a2")
    code, out, err = _run("precheck", other, http_factory=_http_factory(indexed_client))
    assert out != ""
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- S5: unparseable edit -> dirty marker, not a silent no-op ----------------------


def test_postupdate_pushes_dirty_marker_when_edit_is_unparseable(
    monkeypatch, repo, indexed_client
):
    """S5: if an edit leaves the file syntactically invalid, postupdate must push
    a raw-byte content_hash + a DIRTY/UNPARSEABLE marker so the blackboard stops
    advertising the STALE last-good hash. Pre-S5 the SyntaxError propagated to the
    fail-open umbrella -> silent no-op -> the parcel still showed the old hash."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    parcel_id = f"mod_a.py::{MODULE_SYMBOL}"
    good_hash = {p["id"]: p for p in indexed_client.get("/parcels").json()}[parcel_id][
        "content_hash"
    ]

    # simulate the edit having left mod_a.py with a syntax error on disk.
    (repo / "mod_a.py").write_text(
        "def helper(x, y=1:\n    return x + y\n", encoding="utf-8"
    )
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="a1")
    code, out, err = _run("postupdate", payload, http_factory=_http_factory(indexed_client))
    assert code == 0

    after = {p["id"]: p for p in indexed_client.get("/parcels").json()}[parcel_id]
    assert after["content_hash"] != good_hash  # NOT the stale last-good value
    assert "DIRTY/UNPARSEABLE" in after["state_summary"]


# --- S5: an indexed symlinked .py stays leasable (no silent bypass) ---------------


def test_indexed_symlinked_py_stays_leasable(monkeypatch, tmp_path, tmp_path_factory):
    """S5 symlink policy: a `.py` that is a symlink (pointing outside the repo)
    is indexed under its own in-repo name, so the hook must lease it under that
    same name. Pre-S5 `_relpath` followed the leaf symlink to its target, which
    resolved outside the repo -> None -> the edit silently bypassed the lease
    entirely (no lease acquired)."""
    outside = tmp_path_factory.mktemp("outside") / "real_impl.py"
    outside.write_text("def impl():\n    return 1\n", encoding="utf-8")
    repo_dir = tmp_path_factory.mktemp("repo_symlink")
    (repo_dir / "linked.py").symlink_to(outside)

    app = create_app(tmp_path / "bb.db")
    with TestClient(app) as c:
        assert c.post("/index", json={"root": str(repo_dir)}).status_code == 200
        parcel_ids = {p["id"] for p in c.get("/parcels").json()}
        # indexed under its own on-disk name, not the symlink target's:
        assert f"linked.py::{MODULE_SYMBOL}" in parcel_ids

        monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
        payload = _payload(
            "Edit", file_path=str(repo_dir / "linked.py"), cwd=str(repo_dir), agent_id="a1"
        )
        code, out, err = _run("precheck", payload, http_factory=_http_factory(c))
        assert code == 0
        assert out == ""  # ALLOW after acquiring -- not a silent bypass

        leases = c.get("/leases").json()
        assert len(leases) == 1
        assert leases[0]["parcel_id"] == f"linked.py::{MODULE_SYMBOL}"
        assert leases[0]["agent_id"] == "a1"
    assert out == ""


# --- R3 P0-2: unindexed files must be coordinated, not silently ungated ------------


@pytest.mark.parametrize(
    "filename, why",
    [
        ("package.json", "non-Python: the classifier only walks *.py"),
        ("deploy.yaml", "non-Python"),
        ("Dockerfile", "non-Python, no extension"),
        ("brand_new.py", "created after the last POST /index, so it has no parcel yet"),
    ],
)
def test_precheck_gates_files_the_classifier_never_indexed(
    monkeypatch, repo, indexed_client, filename, why
):
    """An edit to an unindexed file must take a real lease -- not fail open.

    `leases.parcel_id` is an FK to `parcels(id)`, so leasing a parcel the classifier
    never emitted raised IntegrityError -> 500 -> raise_for_status -> main()'s
    deliberate fail-open umbrella -> exit 0 with NO lease and NO deny. Since
    `indexer.index_repo` only walks `*.py`, that silently ungated every `.ts`,
    `.yaml`, `package.json` and every newly-created file.

    That is worse here than anywhere else in the system: hook-driven subagents share
    ONE working tree (`_repo_root = payload['cwd']`), so DESIGN §5.1's worktree
    isolation does not exist on this surface and the lease is the only thing standing
    between two agents and a lost edit.
    """
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    (repo / filename).write_text("{}\n", encoding="utf-8")

    payload = _payload("Edit", file_path=str(repo / filename), cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    assert out == "", f"expected ALLOW for the first agent on {filename} ({why})"
    assert "failing open" not in err, (
        f"{filename} fell through the fail-open umbrella instead of being leased "
        f"({why}): {err.strip()}"
    )

    leases = indexed_client.get("/leases").json()
    held = [lease for lease in leases if lease["parcel_id"] == f"{filename}::{MODULE_SYMBOL}"]
    assert len(held) == 1, f"no lease was taken on {filename} -- it is ungated ({why})"
    assert held[0]["agent_id"] == "agent-1"
    assert held[0]["mode"] == "write"


def test_second_agent_is_denied_an_unindexed_file_held_by_the_first(
    monkeypatch, repo, indexed_client
):
    """The invariant P0-2 actually broke: one agent per file, for ANY file.

    Two subagents told to add a dependency to package.json would both be allowed,
    both write it in the same shared working tree, and the last writer would silently
    destroy the other's edit.
    """
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    (repo / "package.json").write_text('{"deps": {}}\n', encoding="utf-8")
    factory = _http_factory(indexed_client)

    code_a, out_a, _ = _run(
        "precheck",
        _payload("Edit", file_path=str(repo / "package.json"), cwd=str(repo), agent_id="agent-A"),
        http_factory=factory,
    )
    assert (code_a, out_a) == (0, "")  # A gets it

    code_b, out_b, _ = _run(
        "precheck",
        _payload("Edit", file_path=str(repo / "package.json"), cwd=str(repo), agent_id="agent-B"),
        http_factory=factory,
    )
    assert code_b == 0
    assert out_b != "", "agent-B was ALLOWED to edit a file agent-A holds -- last writer wins"
    decision = json.loads(out_b)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "agent-A" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_unreachable_blackboard_still_fails_open(monkeypatch, repo):
    """The fail-open umbrella must still cover TRANSIENT failure.

    P0-2's fix distinguishes "this parcel does not exist" (a deterministic property
    of the file -> lease it) from "the blackboard is down" (transient -> allow, so a
    dead coordinator never bricks the user's session). This pins the second half so
    the fix can't be over-applied into failing closed on an outage.
    """
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("precheck", payload, http_factory=lambda base_url: _ExplodingHttp())

    assert code == 0
    assert out == ""
    assert "failing open" in err


# --- R5: an in-repo symlink alias is ONE file, so it takes ONE lease ---------------


def test_in_repo_symlink_alias_shares_one_lease_with_its_target(
    monkeypatch, tmp_path, tmp_path_factory
):
    """Editing `link.py` and editing `real.py` must contend for the SAME lease.

    R4 found the hook keyed leases on the unresolved leaf name, so two paths aliasing
    ONE inode produced two different parcel ids and therefore two independent write
    leases on one physical file -- in the ONE working tree hook subagents share, where
    the lease is the only protection. Last writer wins, silently: the exact collision
    this system exists to prevent.
    """
    repo_dir = tmp_path_factory.mktemp("repo_alias")
    (repo_dir / "real.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")
    (repo_dir / "link.py").symlink_to(repo_dir / "real.py")

    app = create_app(tmp_path / "bb.db")
    with TestClient(app) as c:
        assert c.post("/index", json={"root": str(repo_dir)}).status_code == 200
        parcel_ids = {p["id"] for p in c.get("/parcels").json()}

        # The alias is not a second file: only the canonical name is a parcel.
        assert f"real.py::{MODULE_SYMBOL}" in parcel_ids
        assert not any(pid.startswith("link.py") for pid in parcel_ids), (
            f"the alias got its own parcels -- one inode, two leases: {sorted(parcel_ids)}"
        )

        monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
        factory = _http_factory(c)

        # A edits via the real name...
        code_a, out_a, _ = _run(
            "precheck",
            _payload("Edit", file_path=str(repo_dir / "real.py"), cwd=str(repo_dir), agent_id="A"),
            http_factory=factory,
        )
        assert (code_a, out_a) == (0, "")

        # ...B edits via the alias and must be DENIED: it is the same file.
        code_b, out_b, _ = _run(
            "precheck",
            _payload("Edit", file_path=str(repo_dir / "link.py"), cwd=str(repo_dir), agent_id="B"),
            http_factory=factory,
        )
        assert code_b == 0
        assert out_b != "", (
            "B was ALLOWED to edit the same inode A holds, via a symlink alias -- "
            "last writer wins and A's edit is silently destroyed"
        )
        assert json.loads(out_b)["hookSpecificOutput"]["permissionDecision"] == "deny"

        held = c.get("/leases").json()
        assert len(held) == 1, f"one inode took {len(held)} leases: {held}"
        assert held[0]["parcel_id"] == f"real.py::{MODULE_SYMBOL}"


# --- C9: SWARMSYNC_LEASE_TTL must be validated, not silently trusted ----------------


def test_hook_lease_ttl_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("SWARMSYNC_LEASE_TTL", raising=False)
    assert adapter._hook_lease_ttl() == adapter.DEFAULT_HOOK_LEASE_TTL_SECONDS


def test_hook_lease_ttl_zero_falls_back_to_default_not_disable(monkeypatch):
    """C9: a single config typo `SWARMSYNC_LEASE_TTL=0` used to parse as a valid float
    and silently disable ALL hook-path lease protection (every lease born expired ->
    two writers granted while prechecks kept saying "allow"). It must instead fall
    back to the safe default and say so on stderr, never returning the poison value."""
    monkeypatch.setenv("SWARMSYNC_LEASE_TTL", "0")
    err = io.StringIO()
    assert adapter._hook_lease_ttl(err=err) == adapter.DEFAULT_HOOK_LEASE_TTL_SECONDS
    assert "SWARMSYNC_LEASE_TTL" in err.getvalue()


def test_hook_lease_ttl_negative_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SWARMSYNC_LEASE_TTL", "-5")
    err = io.StringIO()
    assert adapter._hook_lease_ttl(err=err) == adapter.DEFAULT_HOOK_LEASE_TTL_SECONDS
    assert err.getvalue() != ""


def test_hook_lease_ttl_non_float_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SWARMSYNC_LEASE_TTL", "not-a-number")
    err = io.StringIO()
    assert adapter._hook_lease_ttl(err=err) == adapter.DEFAULT_HOOK_LEASE_TTL_SECONDS
    assert "not a number" in err.getvalue()


def test_hook_lease_ttl_over_ceiling_falls_back_to_default(monkeypatch):
    """The symmetric hazard: an absurdly large TTL is an effectively permanent lease
    the reaper never fires on. Rejected the same way as ttl<=0."""
    monkeypatch.setenv("SWARMSYNC_LEASE_TTL", "999999999")
    err = io.StringIO()
    assert adapter._hook_lease_ttl(err=err) == adapter.DEFAULT_HOOK_LEASE_TTL_SECONDS
    assert err.getvalue() != ""


def test_hook_lease_ttl_below_floor_is_used_but_warns(monkeypatch):
    """Defense-in-depth floor (C13): a sub-floor TTL is still HONORED (the keepalive
    tests run sub-second TTLs on purpose) but warned about."""
    monkeypatch.setenv("SWARMSYNC_LEASE_TTL", "0.5")
    err = io.StringIO()
    assert adapter._hook_lease_ttl(err=err) == 0.5  # used, not clamped
    assert "floor" in err.getvalue()


def test_hook_lease_ttl_in_bounds_value_is_used_without_warning(monkeypatch):
    monkeypatch.setenv("SWARMSYNC_LEASE_TTL", "120")
    err = io.StringIO()
    assert adapter._hook_lease_ttl(err=err) == 120.0
    assert err.getvalue() == ""
