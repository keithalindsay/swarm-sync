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
    assert any(l["agent_id"] == "agent-1" for l in leases)


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
    assert [l["agent_id"] for l in leases] == ["agent-2"]


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
    assert out == ""
