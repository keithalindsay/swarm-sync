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
  - agent_id fallback: agent_id -> session_id -> per-invocation unique id (degraded)
  - blackboard unreachable / raises -> fail-open ALLOW
  - malformed stdin -> fail-open ALLOW
  - postupdate re-hashes the edited file and posts content_hash/state_summary
  - release releases only the calling agent's own active leases
  - release runs even when the opt-in gate reads inactive (the one exempt
    subcommand -- a missed release is fail-STUCK, not fail-open), quietly and
    without stamping repo state, still scoped to the caller's own agent_id
  - session-start POSTs /index when reachable, no-ops when not, and is
    deliberately NOT exempt from the gate

`session-start` is NOT exempt from the opt-in gate. See the block above the
session-start tests for why the asymmetry with `release` is deliberate.
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


@pytest.mark.parametrize("subcommand", ["postupdate", "session-start"])
def test_inactive_repo_postupdate_and_session_start_are_also_noop(monkeypatch, repo, subcommand):
    """`release` is deliberately NOT in this list -- see the `_UNGATED_RELEASE` block
    below. Everything else keeps the zero-network-call opt-in guarantee."""
    monkeypatch.delenv("SWARMSYNC_ACTIVE", raising=False)
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
    assert decision["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    # WP2.4 U3: an informative deny -- file, holder, the renewal caveat, and an
    # actionable pointer; and NOT the old misleading "retry shortly".
    assert "mod_a.py" in reason
    assert "agent-0" in reason
    assert "renews while its holder stays active" in reason
    assert "swarmsync holds" in reason  # WP5.1: the CLI work-discovery pointer
    assert "/leases" in reason  # with the raw endpoint named as the fallback
    assert "retry shortly" not in reason

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


def test_agent_id_neither_present_is_degraded_unique_not_shared_main(monkeypatch, repo, indexed_client):
    """WP2.1 C2: a payload with NEITHER agent_id nor session_id must NOT collapse to a
    shared `"main"` constant -- for a lock, a shared identity is UNDER-protection (two
    distinct agents fused into one holder, both allowed). It must instead get a
    per-invocation-unique identity and a stderr warning that coordination is degraded.
    Pre-fix this returned the constant `"main"`."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo))
    assert "agent_id" not in payload and "session_id" not in payload
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    leases = indexed_client.get("/leases").json()
    holder = leases[0]["agent_id"]
    assert holder != "main"  # never the shared constant
    assert holder.startswith("swarmsync-unidentified-")  # a per-invocation-unique id
    assert "DEGRADED" in err  # operator is told coordination is degraded


def test_agent_id_neither_present_two_invocations_do_not_share_identity(monkeypatch, repo, indexed_client):
    """The false-sharing footgun made concrete: two separate no-id invocations must get
    DIFFERENT identities, so the second is not silently treated as the first holder (and
    thus waved through onto a file the first 'holds'). Pre-fix both were `"main"` and the
    second sailed through as the same holder."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    p1 = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo))
    p2 = _payload("Edit", file_path=str(repo / "mod_b.py"), cwd=str(repo))
    _run("precheck", p1, http_factory=_http_factory(indexed_client))
    _run("precheck", p2, http_factory=_http_factory(indexed_client))

    holders = {ls["parcel_id"]: ls["agent_id"] for ls in indexed_client.get("/leases").json()}
    id_a = holders[f"mod_a.py::{MODULE_SYMBOL}"]
    id_b = holders[f"mod_b.py::{MODULE_SYMBOL}"]
    assert id_a != id_b, "two unidentified invocations collapsed to one shared holder"


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
    # The real flow is PreToolUse (precheck acquires the write lease) -> edit ->
    # PostToolUse (postupdate). /parcel/update now requires the caller to hold that
    # lease (C5), so mirror the precheck by acquiring it as the same agent first.
    assert indexed_client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": f"mod_a.py::{MODULE_SYMBOL}", "mode": "write"},
    ).json()["granted"]
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
    # Hold the write lease so postupdate is actually allowed to mutate the parcel
    # (C5) -- otherwise both updates are refused and `first == second` passes
    # vacuously without exercising the state_summary heuristic at all.
    assert indexed_client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": f"mod_a.py::{MODULE_SYMBOL}", "mode": "write"},
    ).json()["granted"]
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="agent-1")

    _run("postupdate", payload, http_factory=_http_factory(indexed_client))
    first = {p["id"]: p for p in indexed_client.get("/parcels").json()}[
        f"mod_a.py::{MODULE_SYMBOL}"
    ]["state_summary"]

    _run("postupdate", payload, http_factory=_http_factory(indexed_client))
    second = {p["id"]: p for p in indexed_client.get("/parcels").json()}[
        f"mod_a.py::{MODULE_SYMBOL}"
    ]["state_summary"]

    # The update genuinely landed (not a vacuous refusal) AND is deterministic.
    assert "agent-1" in first
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


# --- release is UNGATED: it runs even when the opt-in gate reads inactive ----------
#
# THE DEFECT. Every signal `_is_active` consults is derived from a PATH, and a
# SubagentStop payload carries no edit target, so `_repo_root` falls back to the
# session `cwd` -- which a Claude Code subagent inherits from its parent and which is
# routinely OUTSIDE the repo being coordinated. The gate then read "inactive" for a
# session that was very much coordinating, `release` no-opped, and the finished
# agent's write leases were reclaimed only by 300s TTL expiry. A 3-agent dogfood
# logged 94 `lease_granted` / 0 `released`; all 5 of its denials named holders that
# had already stopped, and one agent was starved of both files it needed.
#
# A missed release is not fail-OPEN (which lets work through, and is the documented
# policy precisely so a broken setup never blocks editing) -- it is fail-STUCK: it
# HOLDS a lock. So `release` is exempt from the gate. It is the one subcommand that
# can be, because it cannot act on a repo: it deletes only rows whose `agent_id` is
# this very caller's.


def _inactive(monkeypatch, repo):
    monkeypatch.delenv("SWARMSYNC_ACTIVE", raising=False)
    assert not (repo / adapter.ACTIVE_MARKER_FILENAME).exists()


def test_release_runs_even_when_the_opt_in_gate_reads_inactive(monkeypatch, repo, indexed_client):
    """REGRESSION: the lease must be gone after `release` even with NO activation
    signal the adapter can see -- no `SWARMSYNC_ACTIVE`, no marker at the resolved
    repo root, no marker at the payload `cwd`. That is exactly the state a subagent
    whose cwd sits outside the coordinated repo presents.

    Pre-fix this returned 0 having made zero network calls, and the lease below was
    still `active` afterwards.
    """
    _inactive(monkeypatch, repo)
    granted = indexed_client.post(
        "/lease",
        json={"agent_id": "sub-A", "parcel_id": f"mod_a.py::{MODULE_SYMBOL}", "mode": "write"},
    ).json()
    assert granted["granted"]

    payload = _payload("SubagentStop", cwd=str(repo), agent_id="sub-A", session_id="sess-1")
    code, out, err = _run("release", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    assert indexed_client.get("/leases").json() == [], "ungated release did not free the lease"
    events = indexed_client.get("/events").json()
    assert any(e["type"] == "released" for e in events), "no `released` event was recorded"


def test_ungated_release_still_only_touches_the_calling_agents_leases(
    monkeypatch, repo, indexed_client
):
    """The DANGEROUS mutation this fix must not become: a `release` that runs without
    the opt-in gate and releases leases it does not own. Ungating widens WHEN release
    runs, never WHAT it may free -- the `agent_id` filter is the whole safety argument
    for ungating it, so it is pinned here on the ungated path specifically (the gated
    path has its own coverage above)."""
    _inactive(monkeypatch, repo)
    for agent, mod in (("sub-A", "mod_a.py"), ("sub-B", "mod_b.py"), ("sub-B", "mod_c.py")):
        r = indexed_client.post(
            "/lease",
            json={"agent_id": agent, "parcel_id": f"{mod}::{MODULE_SYMBOL}", "mode": "write"},
        ).json()
        assert r["granted"]

    payload = _payload("SubagentStop", cwd=str(repo), agent_id="sub-A", session_id="sess-shared")
    code, out, err = _run("release", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    survivors = sorted(lease["parcel_id"] for lease in indexed_client.get("/leases").json())
    assert survivors == [f"mod_b.py::{MODULE_SYMBOL}", f"mod_c.py::{MODULE_SYMBOL}"]


def test_ungated_release_is_silent_when_the_blackboard_is_absent(monkeypatch, repo):
    """Quietness is load-bearing, not cosmetic. `release` is now wired to run in every
    repo, including the many that never opted in, so the common case is "no server
    listening at all". If that emitted the umbrella's `failing open (...)` note it
    would put one line of pure noise on hook stderr per finished subagent per project
    -- the stream an operator is supposed to read when coordination misbehaves. The
    ungated path therefore swallows the absent-blackboard case in `_dispatch` instead
    of letting it reach `main()`'s umbrella."""
    _inactive(monkeypatch, repo)
    payload = _payload("SubagentStop", cwd=str(repo), agent_id="sub-A", session_id="sess-1")
    code, out, err = _run("release", payload, http_factory=lambda base_url: _ExplodingHttp())

    assert code == 0
    assert out == "", out
    assert err == "", err


def test_ungated_release_does_not_stamp_last_contact(monkeypatch, repo, indexed_client):
    """`.swarmsync-last-contact` is repo state that feeds the C10 fail-CLOSED tier, and
    on the ungated path `repo_root` is a cwd-derived GUESS -- the very input we stopped
    trusting. Writing the stamp there would litter unrelated directories with
    swarm-sync state on every SubagentStop, so the ungated path never stamps."""
    _inactive(monkeypatch, repo)
    indexed_client.post(
        "/lease",
        json={"agent_id": "sub-A", "parcel_id": f"mod_a.py::{MODULE_SYMBOL}", "mode": "write"},
    )
    stamp = repo / adapter.LAST_CONTACT_FILENAME
    assert not stamp.exists()

    payload = _payload("SubagentStop", cwd=str(repo), agent_id="sub-A", session_id="sess-1")
    code, out, err = _run("release", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    assert indexed_client.get("/leases").json() == []
    assert not stamp.exists(), "ungated release stamped last-contact into an un-opted-in tree"


def test_ungated_release_with_no_identity_makes_no_request_and_says_nothing(monkeypatch, repo):
    """A payload with neither `agent_id` nor `session_id` can hold no lease, so there is
    nothing to release. The gated path mints a degraded per-invocation id and warns on
    stderr about it; on the ungated path that warning would fire in projects that never
    opted in and name an action nobody can take, so the ungated path skips the call
    entirely rather than talking to the blackboard about a random uuid."""
    _inactive(monkeypatch, repo)
    payload = _payload("SubagentStop", cwd=str(repo))
    code, out, err = _run("release", payload, http_factory=lambda base_url: _ExplodingHttp())

    assert code == 0
    assert out == "" and err == ""


def test_ungated_release_prefers_agent_id_over_session_id(monkeypatch, repo, indexed_client):
    """Identity precedence on the ungated path must match `_agent_id`: `agent_id` (unique
    per subagent) beats `session_id` (SHARED by every subagent of one session). Getting
    this backwards would make one subagent's stop release its live siblings' leases."""
    _inactive(monkeypatch, repo)
    for agent, mod in (("sub-A", "mod_a.py"), ("sess-shared", "mod_b.py")):
        indexed_client.post(
            "/lease",
            json={"agent_id": agent, "parcel_id": f"{mod}::{MODULE_SYMBOL}", "mode": "write"},
        )

    payload = _payload("SubagentStop", cwd=str(repo), agent_id="sub-A", session_id="sess-shared")
    code, out, err = _run("release", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    survivors = [lease["agent_id"] for lease in indexed_client.get("/leases").json()]
    assert survivors == ["sess-shared"]


# --- session-start: POST /index when reachable, no-op otherwise ------------------
#
# Deliberately NOT ungated alongside `release`, though its payload has the same shape
# (no edit target). Two reasons, both asymmetries with `release`:
#
#   * Blast radius runs the other way. `release` only DELETES rows keyed by the
#     caller's own agent_id; `session-start` WRITES -- `POST /index {root: <a
#     cwd-derived guess>}`. With no marker to consult there is nothing to make that
#     guess right, and `run_index` keys parcel ids relative to whatever root it is
#     handed, so a guess that happens to fall under a broad `SWARMSYNC_ROOTS` would
#     mint divergent ghost ids (the C12 failure) into a blackboard whose repo never
#     opted in. A guess that does not is a 403. Neither outcome is worth having.
#   * Its miss is not fail-STUCK. This `/index` is a documented best-effort warm-up
#     that holds no lock: `cmd_precheck` passes `ensure_parcel=True`, so a parcel that
#     was never indexed is created at acquire time and leasing works regardless. A
#     skipped session-start costs a pre-warmed parcel/contract table, not a 300s lock
#     on another agent's file.
#
# The right fix here is a different one -- resolve the root from the server's own
# managed root (`GET /health` -> `root`), which is the only root `/index` can accept
# anyway -- and it is a behavior change to a best-effort path, not this defect.


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
    # Real flow holds the write lease from the PreToolUse precheck; /parcel/update
    # requires it (C5), so acquire it as the same agent before postupdate.
    assert indexed_client.post(
        "/lease", json={"agent_id": "a1", "parcel_id": parcel_id, "mode": "write"}
    ).json()["granted"]

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


# =====================================================================================
# WP2.1 -- agent identity: trust agent_id, kill silent false-sharing (C2)
#   Uses the VERIFIED current Claude Code payload shapes:
#     - main-thread PreToolUse: has session_id, NO agent_id
#     - subagent PreToolUse:     has agent_id (unique per subagent) + shared session_id
#     - SubagentStop:            has agent_id identifying WHICH subagent stopped
# =====================================================================================


def test_subagent_payload_leases_under_its_unique_agent_id(monkeypatch, repo, indexed_client):
    """A subagent's payload carries a unique agent_id AND the shared session_id; the
    lease identity must be the agent_id (what distinguishes sibling subagents), not the
    session_id they all share."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload(
        "Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo),
        agent_id="subagent-7", session_id="sess-shared", agent_type="Explore",
    )
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))
    assert code == 0
    leases = indexed_client.get("/leases").json()
    assert leases[0]["agent_id"] == "subagent-7"  # the agent_id, never the shared session


def test_main_thread_payload_leases_under_session_id(monkeypatch, repo, indexed_client):
    """A main-thread payload has NO agent_id; the whole session is one editor, so its
    session_id is the right lease identity there."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    payload = _payload(
        "Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), session_id="sess-main"
    )
    assert "agent_id" not in payload
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))
    assert code == 0
    leases = indexed_client.get("/leases").json()
    assert leases[0]["agent_id"] == "sess-main"


def test_subagent_stop_releases_only_the_stopping_subagents_leases(monkeypatch, repo, indexed_client):
    """WP2.1 sibling isolation: two subagents of ONE session (shared session_id, distinct
    agent_ids) each hold a lease. A SubagentStop for one subagent (payload agent_id names
    it) must release ONLY that subagent's lease, never its sibling's. A release scoped to
    the shared session_id would wrongly free the sibling."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    # Two siblings under one session, each holding a different file.
    assert indexed_client.post(
        "/lease",
        json={"agent_id": "sub-A", "parcel_id": f"mod_a.py::{MODULE_SYMBOL}", "mode": "write"},
    ).json()["granted"]
    assert indexed_client.post(
        "/lease",
        json={"agent_id": "sub-B", "parcel_id": f"mod_b.py::{MODULE_SYMBOL}", "mode": "write"},
    ).json()["granted"]

    # SubagentStop fires for sub-A (payload identifies the stopping subagent by agent_id).
    stop_a = _payload("SubagentStop", cwd=str(repo), agent_id="sub-A", session_id="sess-shared")
    code, out, err = _run("release", stop_a, http_factory=_http_factory(indexed_client))
    assert code == 0

    remaining = {ls["agent_id"] for ls in indexed_client.get("/leases").json()}
    assert remaining == {"sub-B"}, "SubagentStop freed a sibling's lease, not just its own"


# =====================================================================================
# WP2.2 -- parcel ids from the git root, not cwd (C12)
# =====================================================================================


def test_parcel_id_is_git_root_relative_not_cwd_relative(monkeypatch, tmp_path):
    """A session running in a SUBDIR of the repo must key the file's parcel on its
    git-root-relative id (`pkg/a.py::<module>`), matching the server's root-relative id --
    not `a.py::<module>` relative to the subdir cwd. Pre-fix (`_repo_root` = cwd) the two
    diverged."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    repo = tmp_path / "myrepo"
    (repo / "pkg").mkdir(parents=True)
    (repo / ".git").mkdir()  # marks the git toplevel
    (repo / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")

    app = create_app(tmp_path / "bb.db")
    with TestClient(app) as c:
        # cwd is the SUBDIR, but the file's absolute path is under the repo.
        payload = _payload(
            "Edit", file_path=str(repo / "pkg" / "a.py"), cwd=str(repo / "pkg"), agent_id="a1"
        )
        code, out, err = _run("precheck", payload, http_factory=_http_factory(c))
        assert code == 0
        leases = c.get("/leases").json()
        assert len(leases) == 1
        assert leases[0]["parcel_id"] == f"pkg/a.py::{MODULE_SYMBOL}"  # ROOT-relative


def test_two_agents_in_different_cwds_collide_on_one_lease(monkeypatch, tmp_path):
    """The bug C12 prevents: two agents editing ONE physical file from DIFFERENT cwds must
    contend for ONE lease. Pre-fix the subdir agent minted a divergent parcel id (a ghost
    row) and got a SECOND write lease on the same file -- the exact collision the lease
    exists to prevent. Here the second agent must be DENIED and only one lease can exist."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    repo = tmp_path / "myrepo"
    (repo / "pkg").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")

    app = create_app(tmp_path / "bb.db")
    with TestClient(app) as c:
        factory = _http_factory(c)
        # Agent 1 runs from the repo root.
        code_a, out_a, _ = _run(
            "precheck",
            _payload("Edit", file_path=str(repo / "pkg" / "a.py"), cwd=str(repo), agent_id="A"),
            http_factory=factory,
        )
        assert (code_a, out_a) == (0, "")

        # Agent 2 runs from the SUBDIR -- same file, must be DENIED (same lease).
        code_b, out_b, _ = _run(
            "precheck",
            _payload("Edit", file_path=str(repo / "pkg" / "a.py"), cwd=str(repo / "pkg"), agent_id="B"),
            http_factory=factory,
        )
        assert code_b == 0
        assert out_b != "", "subdir agent got a SECOND lease on one file (ghost parcel id)"
        assert json.loads(out_b)["hookSpecificOutput"]["permissionDecision"] == "deny"

        held = c.get("/leases").json()
        assert len(held) == 1, f"one physical file took {len(held)} leases: {held}"


def test_repo_root_without_git_keeps_cwd_behavior(tmp_path):
    """No `.git` anywhere up the tree -> nothing to discover -> unchanged cwd behavior."""
    sub = tmp_path / "no_git_repo" / "inner"
    sub.mkdir(parents=True)
    assert adapter._repo_root({"cwd": str(sub)}) == sub.resolve()


def test_a_hook_denial_reaches_the_event_log(monkeypatch, tmp_path):
    """REGRESSION -- contention has to be auditable on the path that produces it.

    `precheck` used to read `GET /leases` and return a denial from what it said. A
    read emits nothing, so a denial decided that way never reached `events`. Measured
    over a real three-agent run: 356 events, 35 grants, and **0 denials logged**
    while the agents reported 8 of them. `swarmsync events` could not answer "how
    much did my swarm contend, and on which files" for the Claude Code hook -- the
    primary integration, and the only one most users will ever touch.

    Losing an acquire emits `lease_denied`. Declining to attempt one emits nothing.
    """
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")

    app = create_app(tmp_path / "bb.db")
    with TestClient(app) as c:
        factory = _http_factory(c)
        payload_a = _payload("Edit", file_path=str(repo / "a.py"), cwd=str(repo), agent_id="A")
        payload_b = _payload("Edit", file_path=str(repo / "a.py"), cwd=str(repo), agent_id="B")

        assert _run("precheck", payload_a, http_factory=factory)[1] == "", "A should hold it"
        code_b, out_b, _ = _run("precheck", payload_b, http_factory=factory)
        assert code_b == 0
        assert json.loads(out_b)["hookSpecificOutput"]["permissionDecision"] == "deny"

        events = c.get("/events?since=0").json()
        denials = [e for e in events if "denied" in str(e.get("type"))]
        assert denials, (
            "a hook denial left no trace in the event log -- contention is "
            f"unauditable. types seen: {sorted({str(e.get('type')) for e in events})}"
        )
        assert any(e.get("agent_id") == "B" for e in denials)


def test_repo_root_comes_from_the_edit_target_not_the_session_cwd(tmp_path):
    """A session running OUTSIDE the repo it is editing must still resolve that repo.

    Keying on cwd assumes the session lives inside the repo it edits. A Claude Code
    subagent inherits its parent's cwd, which is routinely a workspace root or another
    project entirely -- so the assumption fails in the most ordinary setup there is."""
    repo = tmp_path / "coordinated"
    (repo / "pkg").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")

    outside = tmp_path / "somewhere_else"
    outside.mkdir()

    payload = _payload("Edit", file_path=str(repo / "pkg" / "a.py"), cwd=str(outside))
    assert adapter._repo_root(payload) == repo.resolve()


def test_repo_root_falls_back_to_cwd_when_there_is_no_edit_target(tmp_path):
    """`release` and `session-start` carry no file path. cwd stays the answer for them."""
    repo = tmp_path / "coordinated"
    (repo / ".git").mkdir(parents=True)
    assert adapter._repo_root({"cwd": str(repo)}) == repo.resolve()
    assert adapter._repo_root({"cwd": str(repo), "tool_input": {}}) == repo.resolve()


def test_repo_root_falls_back_to_cwd_when_the_target_is_outside_any_checkout(tmp_path):
    """A scratchpad write has no repo to discover -- cwd remains the best answer, and
    the fallback must not resolve to the filesystem root."""
    repo = tmp_path / "coordinated"
    (repo / ".git").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "note.txt").write_text("tmp\n", encoding="utf-8")

    payload = _payload("Write", file_path=str(scratch / "note.txt"), cwd=str(repo))
    assert adapter._repo_root(payload) == repo.resolve()


def test_an_agent_outside_the_repo_is_still_denied_a_held_file(monkeypatch, tmp_path):
    """REGRESSION -- the production failure, end to end.

    Found by running three concurrent agents against a real repo: swarm-sync recorded
    0 leases and 0 events while they modified four shared files. The agents' cwd was the
    parent session's workspace, so `_repo_root` walked up from there, `_is_active` found
    no marker, and `_dispatch` returned a silent zero-network-call ALLOW.

    What made it dangerous rather than merely wrong: stderr stayed empty, and
    `swarmsync doctor` reported all eight checks green throughout -- from the server's
    side nothing WAS misconfigured. Neither of the two fail-open modes the README
    documents (wrong managed root, wrong port) covers it.

    Holding the file, the lease and everything else constant, only cwd varying:
        process=repo     payload=repo     -> DENY
        process=outside  payload=repo     -> DENY
        process=repo     payload=outside  -> silent ALLOW   <- this test
        process=outside  payload=outside  -> silent ALLOW   <- this test
    """
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    repo = tmp_path / "coordinated"
    (repo / "pkg").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")

    outside = tmp_path / "workspace_root"
    outside.mkdir()

    app = create_app(tmp_path / "bb.db")
    with TestClient(app) as c:
        factory = _http_factory(c)

        # Agent A takes the lease from inside the repo.
        code_a, out_a, _ = _run(
            "precheck",
            _payload("Edit", file_path=str(repo / "pkg" / "a.py"), cwd=str(repo), agent_id="A"),
            http_factory=factory,
        )
        assert (code_a, out_a) == (0, "")

        # Agent B edits the SAME file with a cwd outside the repo entirely.
        code_b, out_b, _ = _run(
            "precheck",
            _payload(
                "Edit", file_path=str(repo / "pkg" / "a.py"), cwd=str(outside), agent_id="B"
            ),
            http_factory=factory,
        )
        assert code_b == 0
        assert out_b != "", (
            "an agent whose cwd is outside the repo edited a LEASED file with no "
            "coordination at all -- silently"
        )
        assert json.loads(out_b)["hookSpecificOutput"]["permissionDecision"] == "deny"

        held = c.get("/leases").json()
        assert len(held) == 1, f"one physical file took {len(held)} leases: {held}"
        assert held[0]["agent_id"] == "A"


# =====================================================================================
# WP2.3 -- timeout inversion + fail-closed under active coordination (C10)
# =====================================================================================


def test_hook_timeout_is_above_server_busy_timeout():
    """Part (a): the hook HTTP client timeout must sit ABOVE the server's SQLite
    busy_timeout, or a merely-busy (contended) blackboard times the hook out and the
    fail path un-gates the tree. Pre-fix the client used 2s vs the server's 5s."""
    from swarmsync.blackboard import db

    assert adapter._DEFAULT_TIMEOUT_SECONDS > db.BUSY_TIMEOUT_SECONDS
    # and the real client actually carries that read timeout
    c = adapter._default_http_factory("http://127.0.0.1:8787")
    try:
        assert c.timeout.read == adapter._DEFAULT_TIMEOUT_SECONDS
    finally:
        c.close()


def test_successful_precheck_records_a_last_contact_stamp(monkeypatch, repo, indexed_client):
    """A successful blackboard contact stamps `.swarmsync-last-contact` at the repo root,
    so the two-tier fail policy can later tell contention from absence."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    assert not (repo / adapter.LAST_CONTACT_FILENAME).exists()
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="a1")
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))
    assert code == 0
    assert (repo / adapter.LAST_CONTACT_FILENAME).exists()


def test_fail_open_when_unreachable_and_no_recent_contact(monkeypatch, repo):
    """Tier 1 (unchanged default): active but the blackboard is unreachable AND there is
    NO evidence of recent successful coordination -> fail OPEN, so a broken/never-started
    setup never bricks a real editing session."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    assert not (repo / adapter.LAST_CONTACT_FILENAME).exists()
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="a1")
    code, out, err = _run("precheck", payload, http_factory=lambda base_url: _ExplodingHttp())
    assert code == 0
    assert out == ""  # ALLOW
    assert "failing open" in err


def test_fail_closed_when_unreachable_during_active_coordination_with_recent_contact(
    monkeypatch, repo
):
    """Tier 2 (the new fail-CLOSED tier): active coordination WITH a recent successful
    contact on record, and the blackboard is now unreachable -> the silence is contention,
    not absence, so precheck fails CLOSED with a retry-deny instead of silently un-gating
    the shared tree. Reverting part (b) makes this ALLOW (out == '') and fail."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    # Recent successful contact on record (as if a prior call had reached the server).
    (repo / adapter.LAST_CONTACT_FILENAME).write_text(str(time.time()), encoding="utf-8")

    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="a1")
    code, out, err = _run("precheck", payload, http_factory=lambda base_url: _ExplodingHttp())
    assert code == 0
    assert out != "", "fail-CLOSED tier did not deny under active contention"
    decision = json.loads(out)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert "retry" in reason.lower()
    assert "mod_a.py" in reason


def test_fail_closed_tier_ignores_stale_contact(monkeypatch, repo):
    """A STALE contact stamp (older than the recency window) reads as absence, not
    contention -> back to fail OPEN. Guards the tier boundary from over-applying."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    old = time.time() - adapter.RECENT_CONTACT_WINDOW_SECONDS - 10
    (repo / adapter.LAST_CONTACT_FILENAME).write_text(str(old), encoding="utf-8")

    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="a1")
    code, out, err = _run("precheck", payload, http_factory=lambda base_url: _ExplodingHttp())
    assert code == 0
    assert out == ""  # fail OPEN -- stale contact is not evidence of active coordination


def test_zero_io_invocation_does_not_stamp_contact(monkeypatch, repo, tmp_path_factory):
    """R6 adversarial review P1: completing a dispatch is NOT contact. A precheck whose
    target resolves OUTSIDE the repo makes zero network calls, so it must NOT write the
    last-contact stamp. The old unconditional post-dispatch stamp flipped a NEVER-started
    server into fail-closed denials (a scratchpad Write stamped; the next in-repo Edit
    found "recent contact" and denied) and the perpetual re-stamping defeated the 60s
    self-heal after a deliberate server stop. Reverting the stamp-on-positive-contact
    fix (`_ContactRecordingHttp`) makes both halves of this test fail."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    outside = tmp_path_factory.mktemp("outside-the-repo") / "scratch.py"
    outside.write_text("x = 1\n", encoding="utf-8")

    def down_server(base_url):
        return _ExplodingHttp()  # every REAL call would raise; zero-I/O paths never call

    # 1) Out-of-repo Write completes with zero blackboard I/O -> must leave NO stamp.
    payload = _payload("Write", file_path=str(outside), cwd=str(repo), agent_id="a1")
    code, out, err = _run("precheck", payload, http_factory=down_server)
    assert code == 0
    assert not (repo / adapter.LAST_CONTACT_FILENAME).exists(), (
        "zero-I/O invocation wrote a contact stamp -- fail-closed can now fire against "
        "a server that was never up"
    )

    # 2) In-repo Edit against the never-up server: no stamp on record -> fail OPEN.
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="a1")
    code, out, err = _run("precheck", payload, http_factory=down_server)
    assert code == 0
    assert out == "", "never-started server must fail OPEN, not deny"


def test_is_active_honors_marker_at_cwd_when_git_toplevel_differs(tmp_path):
    """R6 adversarial review P2 (activation split): the guard checks the marker at
    $CLAUDE_PROJECT_DIR while the adapter resolves repo_root to the git TOPLEVEL. With a
    project dir that is a SUBDIR of a larger git repo, a marker at the project dir must
    activate the adapter too -- previously no single marker location activated both
    halves. Reverting the cwd check in `_is_active` fails this."""
    toplevel = tmp_path / "monorepo"
    (toplevel / ".git").mkdir(parents=True)
    project = toplevel / "proj"
    project.mkdir()
    (project / ".swarmsync-active").touch()

    assert adapter._is_active({}, toplevel, project) is True
    # Sanity on both boundaries: no cwd arg -> old behavior (toplevel only, inactive
    # here); marker at the toplevel alone still activates regardless of cwd.
    assert adapter._is_active({}, toplevel) is False
    (toplevel / ".swarmsync-active").touch()
    assert adapter._is_active({}, toplevel, project) is True


def test_marker_at_project_subdir_activates_dispatch_end_to_end(monkeypatch, tmp_path):
    """End-to-end half of the activation-split fix: a payload whose cwd is a marker-
    bearing SUBDIR of a bigger git repo must get past the opt-in gate (previously:
    silent inactive no-op). The blackboard is down, so getting past the gate shows up
    as the umbrella's fail-open stderr note instead of the inactive path's silence."""
    monkeypatch.delenv("SWARMSYNC_ACTIVE", raising=False)
    toplevel = tmp_path / "monorepo"
    (toplevel / ".git").mkdir(parents=True)
    project = toplevel / "proj"
    project.mkdir()
    (project / ".swarmsync-active").touch()
    target = project / "x.py"
    target.write_text("y = 2\n", encoding="utf-8")

    payload = _payload("Edit", file_path=str(target), cwd=str(project), agent_id="a1")
    code, out, err = _run("precheck", payload, http_factory=lambda base_url: _ExplodingHttp())
    assert code == 0
    assert out == ""  # no recent contact -> still fail OPEN, not a deny
    assert err != "", (
        "dispatch never engaged -- the marker at the project subdir did not activate "
        "the adapter (activation split regressed)"
    )


# =====================================================================================
# WP2.4 -- deny messages that inform (U3): consume LeaseResult.holder{,_ttl_expires_at}
# =====================================================================================


def test_deny_reason_reports_holder_ttl_remaining(monkeypatch, repo, indexed_client):
    """The deny names roughly how much TTL is left on the current hold, derived from the
    holder's ttl_expires_at already in the leases/acquire response."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    assert indexed_client.post(
        "/lease",
        json={"agent_id": "agent-0", "parcel_id": f"mod_a.py::{MODULE_SYMBOL}",
              "mode": "write", "ttl": 300},
    ).json()["granted"]

    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="agent-1")
    code, out, err = _run("precheck", payload, http_factory=_http_factory(indexed_client))
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "left on the current hold" in reason


class _DenyingAcquireClient:
    """Free on read, but the acquire DENIES with holder info -- the lost-race deny path.
    Counts leases() calls to prove the deny message is built from the acquire response,
    NOT a second GET /leases round-trip (WP2.4)."""

    def __init__(self):
        self.leases_calls = 0

    def leases(self):
        self.leases_calls += 1
        return []  # free on the initial read -> precheck takes the acquire path

    def lease(self, *a, **kw):
        return {
            "granted": False,
            "holder": "agent-Z",
            "holder_ttl_expires_at": time.time() + 123,
        }


def test_deny_consumes_the_acquire_result_and_reads_leases_not_at_all(repo):
    """The deny names the holder + TTL straight from the acquire `LeaseResult`.

    WP2.4 removed a SECOND `/leases` read from this path (it had been 2). The count
    is now ZERO: precheck no longer reads before it acquires at all, because deciding
    a denial from a read emits no event and made hook-path contention invisible --
    `swarmsync events` reported 0 denials across a three-agent run that hit 8.

    Pinned at exactly 0 rather than `<= 1` on purpose. A regression to read-first
    would still satisfy "no second round-trip" while silently restoring the
    observability hole this count is really guarding.
    """
    client = _DenyingAcquireClient()
    result = adapter.cmd_precheck(
        "Edit", {"file_path": str(repo / "mod_a.py")}, client, repo, "agent-1"
    )
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "agent-Z" in reason
    assert "left on the current hold" in reason
    assert "renews while its holder stays active" in reason
    assert client.leases_calls == 0, (
        "precheck read /leases before acquiring; a denial decided from a read emits "
        "no lease_denied event and is invisible to `swarmsync events`"
    )


# --- WP3.5 (C6-interim): a deleted file gets an honest tombstone, not a stale hash --


def test_postupdate_pushes_deleted_tombstone_when_file_is_gone(
    monkeypatch, repo, indexed_client
):
    """If the edit removed the file from disk, postupdate must NOT return early
    leaving the blackboard advertising the last-good content_hash of a file that
    no longer exists. It pushes the DELETED sentinel hash + a state_summary with
    a clear DELETED marker naming the agent (same shape as the DIRTY/UNPARSEABLE
    mechanism). This matters on the HOOK path, where no integrator re-index ever
    runs to supersede the ghost."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    parcel_id = f"mod_a.py::{MODULE_SYMBOL}"
    good_hash = {p["id"]: p for p in indexed_client.get("/parcels").json()}[parcel_id][
        "content_hash"
    ]
    # Real flow: the deleting agent holds the write lease from its precheck.
    assert indexed_client.post(
        "/lease", json={"agent_id": "a1", "parcel_id": parcel_id, "mode": "write"}
    ).json()["granted"]

    (repo / "mod_a.py").unlink()  # the edit deleted the file

    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="a1")
    code, out, err = _run("postupdate", payload, http_factory=_http_factory(indexed_client))
    assert code == 0

    after = {p["id"]: p for p in indexed_client.get("/parcels").json()}[parcel_id]
    assert after["content_hash"] != good_hash, (
        "blackboard still advertises the stale last-good hash of a deleted file"
    )
    assert after["content_hash"] == adapter.DELETED_SENTINEL_HASH
    assert "DELETED" in after["state_summary"]
    assert "a1" in after["state_summary"]


def test_postupdate_deleted_tombstone_is_deterministic(monkeypatch, repo, indexed_client):
    """Two tombstones for the same deleted file + agent are byte-identical
    (state_summary stays deterministic, like the DIRTY marker)."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    parcel_id = f"mod_a.py::{MODULE_SYMBOL}"
    assert indexed_client.post(
        "/lease", json={"agent_id": "a1", "parcel_id": parcel_id, "mode": "write"}
    ).json()["granted"]
    (repo / "mod_a.py").unlink()
    payload = _payload("Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="a1")

    _run("postupdate", payload, http_factory=_http_factory(indexed_client))
    first = {p["id"]: p for p in indexed_client.get("/parcels").json()}[parcel_id]

    _run("postupdate", payload, http_factory=_http_factory(indexed_client))
    second = {p["id"]: p for p in indexed_client.get("/parcels").json()}[parcel_id]

    assert "DELETED" in first["state_summary"]
    assert (first["content_hash"], first["state_summary"]) == (
        second["content_hash"], second["state_summary"]
    )


def test_postupdate_refused_tombstone_fails_open_with_logged_note(
    monkeypatch, repo, indexed_client
):
    """Edge: the deleting agent's lease lapsed before the tombstone landed --
    /parcel/update refuses it (WP1.4 requires the write lease). The hook must
    stay fail-open: exit 0, a note on stderr, no crash, parcel left as-is."""
    monkeypatch.setenv("SWARMSYNC_ACTIVE", "1")
    parcel_id = f"mod_a.py::{MODULE_SYMBOL}"
    before = {p["id"]: p for p in indexed_client.get("/parcels").json()}[parcel_id]

    (repo / "mod_a.py").unlink()  # deleted, but NO lease held by this agent

    payload = _payload(
        "Edit", file_path=str(repo / "mod_a.py"), cwd=str(repo), agent_id="no-lease"
    )
    code, out, err = _run("postupdate", payload, http_factory=_http_factory(indexed_client))

    assert code == 0
    assert out == ""  # postupdate never emits deny JSON
    assert "tombstone" in err.lower()
    after = {p["id"]: p for p in indexed_client.get("/parcels").json()}[parcel_id]
    assert after == before  # refused update changed nothing
