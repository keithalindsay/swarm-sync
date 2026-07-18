"""U9 — Agent client + runner + mutators. DESIGN.md §4.3, §2.

Done when (BUILD_PLAN.md): against a running TestClient server, one run_agent
declares intent, acquires a write-lease, edits a function in its worktree via
a mutator, commits, posts parcel_update, and releases -- verified by the
resulting event sequence and a committed diff.
"""
from __future__ import annotations

import json
import subprocess
import textwrap
import time

import pytest
from fastapi.testclient import TestClient

from swarmsync.agent import mutators
from swarmsync.agent.client import BlackboardClient
from swarmsync.agent.runner import run_agent
from swarmsync.server.app import create_app
from swarmsync.worktree import git_ops


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "mod_a.py").write_text(
        textwrap.dedent(
            """\
            def helper(x, y=1):
                return x + y


            def other(z):
                return z * 2
            """
        ),
        encoding="utf-8",
    )
    base = git_ops.init_repo(r)
    return r, base


@pytest.fixture()
def client(tmp_path, repo):
    r, _base = repo
    app = create_app(tmp_path / "blackboard.db")
    with TestClient(app) as c:
        resp = c.post("/index", json={"root": str(r)})
        assert resp.status_code == 200
        yield BlackboardClient(c)


# --- done-when: full lifecycle ------------------------------------------------


def test_run_agent_full_lifecycle(client, repo):
    r, base = repo
    result = run_agent(
        agent_id="agent-1",
        client=client,
        repo=r,
        task="rewrite helper",
        target_parcels=["mod_a.py::helper"],
        mutator=mutators.edit_function_body,
        mutator_kwargs={
            "path": "mod_a.py",
            "symbol": "helper",
            "new_body": "return x + y + 100",
        },
        base_commit=base,
        heartbeat_interval=0.05,
    )

    assert result.status == "done"
    assert result.commit_sha is not None
    assert result.commit_sha != base
    assert "mod_a.py::helper" in result.updated_parcels

    # exactly the touched file changed (from the integrator's own diff of the
    # branch it landed) -- S5 cleaned up the ephemeral worktree/branch, so assert
    # against the durable integrate result, not a now-removed branch ref.
    assert result.integrate_result["changed_files"] == ["mod_a.py"]
    # U10's integrator is real now -- run_agent's own POST /integrate call
    # landed the branch, so the main checkout (== "integration") has the change.
    assert "x + y + 100" in (r / "mod_a.py").read_text()
    # S5: run_agent removes its worktree + branch after integrate/release so a
    # rerun with the same agent_id doesn't collide or leak.
    assert not (r / ".worktrees" / "agent-1").exists()

    # event sequence: planned -> lease_granted -> done -> released, all for agent-1.
    events = client.events(since=0)
    by_agent = [(e["type"], e["agent_id"]) for e in events]
    assert ("planned", "agent-1") in by_agent
    assert ("lease_granted", "agent-1") in by_agent
    assert ("done", "agent-1") in by_agent
    assert ("released", "agent-1") in by_agent
    # ordering: planned before lease_granted before done before released
    order = [t for t, a in by_agent if a == "agent-1"]
    assert order.index("planned") < order.index("lease_granted")
    assert order.index("lease_granted") < order.index("done")
    assert order.index("done") < order.index("released")

    # parcel/update landed the freshly re-derived content_hash (not a
    # self-reported one), and the lease is freed.
    parcels = {p["id"]: p for p in client.parcels()}
    helper_row = parcels["mod_a.py::helper"]
    assert helper_row["content_hash"] == result.updated_parcels["mod_a.py::helper"]
    assert helper_row["active_leases"] == []

    # /integrate now runs the real U10 integrator: clean merge, no test suite
    # in this tiny fixture repo (nothing to gate on) -> lands.
    assert result.integrate_result is not None
    assert result.integrate_result["status"] == "merged"
    # WP4.4/A5: the client no longer smuggles the HTTP transport status into
    # the integrator's domain payload.
    assert "_status_code" not in result.integrate_result


def test_run_agent_backs_off_on_lease_denied(client, repo):
    r, base = repo
    held = client.lease("agent-0", "mod_a.py::helper", mode="write")
    assert held["granted"] is True

    result = run_agent(
        agent_id="agent-1",
        client=client,
        repo=r,
        task="rewrite helper",
        target_parcels=["mod_a.py::helper"],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_body": "return 0"},
        base_commit=base,
    )

    assert result.status == "lease_denied"
    assert result.denied_parcels == ["mod_a.py::helper"]
    assert result.lease_ids == {}
    # never got as far as creating a worktree.
    assert not (r / ".worktrees" / "agent-1").exists()

    events = client.events(since=0)
    denied = [
        e for e in events if e["type"] == "lease_denied" and e["agent_id"] == "agent-1"
    ]
    assert len(denied) == 1

    # agent-0's original lease is untouched by agent-1's backoff.
    leases = client.leases()
    assert any(lease["agent_id"] == "agent-0" for lease in leases)


def test_run_agent_releases_already_held_leases_on_a_mid_acquire_denial(client, repo):
    """Multi-parcel partial-lease rollback (DESIGN §5.2): a task that gets some
    of its target leases but is DENIED partway must release every lease it
    already holds and back off cleanly -- never leave an orphaned lock. Here
    agent-1 targets [helper, other]; agent-0 already holds `other`, so agent-1
    acquires `helper` first, then is denied on `other` and must release
    `helper`. Proof it was released: a fresh agent-2 can immediately acquire
    `helper` (on a non-rolling-back runner, helper would stay locked by agent-1
    and agent-2 would be denied)."""
    r, base = repo
    held = client.lease("agent-0", "mod_a.py::other", mode="write")
    assert held["granted"] is True

    result = run_agent(
        agent_id="agent-1",
        client=client,
        repo=r,
        task="rewrite both",
        # helper is acquirable; other is already held -> denial mid-acquire.
        target_parcels=["mod_a.py::helper", "mod_a.py::other"],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_body": "return 0"},
        base_commit=base,
    )

    assert result.status == "lease_denied"
    assert result.denied_parcels == ["mod_a.py::other"]
    # never created a worktree for a task that couldn't get all its locks.
    assert not (r / ".worktrees" / "agent-1").exists()

    # agent-1 must hold NO active lease -- the partially-acquired `helper` was
    # rolled back (released), not orphaned.
    active = client.leases()
    assert not any(le["agent_id"] == "agent-1" for le in active), (
        f"agent-1 leaked a partial lease: {active!r}"
    )

    # ...and because helper is genuinely free again, a fresh agent can take it.
    regrab = client.lease("agent-2", "mod_a.py::helper", mode="write")
    assert regrab["granted"] is True, "helper was not released on rollback"


def test_heartbeater_survives_a_raising_heartbeat_and_keeps_beating():
    """The background `_Heartbeater` thread must never die on a failed beat
    (DESIGN §6 'server went away' -- a lost beat is a legitimate outcome the
    reaper handles). A heartbeat that RAISES must be swallowed and the loop must
    keep beating on the next tick. Pins the per-beat try/except in `_run`."""
    import threading
    import time

    from swarmsync.agent.runner import _Heartbeater

    lock = threading.Lock()
    calls = {"total": 0, "ok": 0}

    class _FlakyClient:
        def heartbeat(self, agent_id, lease_id):
            with lock:
                calls["total"] += 1
                n = calls["total"]
            if n == 1:
                # first beat blows up -- must NOT kill the daemon thread.
                raise RuntimeError("server went away")
            with lock:
                calls["ok"] += 1

    hb = _Heartbeater(_FlakyClient(), "agent-x", interval=0.02)
    hb.add(lease_id=1)
    hb.start()
    try:
        deadline = time.time() + 3.0
        # survived the raise iff it produced >=2 successful beats afterward.
        while time.time() < deadline and calls["ok"] < 2:
            time.sleep(0.02)
        assert calls["total"] >= 1
        assert calls["ok"] >= 2, f"heartbeater died after a raising beat: {calls!r}"
        assert hb._thread is not None and hb._thread.is_alive()
    finally:
        hb.stop()

    assert not (hb._thread is not None and hb._thread.is_alive())


def test_two_agents_disjoint_functions_same_file_both_land(client, repo):
    """Building block of test case #1: two agents editing DIFFERENT functions
    in the SAME file both complete run_agent independently (no lease
    contention) and merge with zero conflicts."""
    r, base = repo
    result_a = run_agent(
        agent_id="agent-a",
        client=client,
        repo=r,
        task="edit helper",
        target_parcels=["mod_a.py::helper"],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_body": "return x - y"},
        base_commit=base,
    )
    result_b = run_agent(
        agent_id="agent-b",
        client=client,
        repo=r,
        task="edit other",
        target_parcels=["mod_a.py::other"],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_a.py", "symbol": "other", "new_body": "return z * 3"},
        base_commit=base,
    )
    assert result_a.status == "done"
    assert result_b.status == "done"

    # each run_agent already landed its own branch via POST /integrate; a clean
    # (conflict-free) merge is exactly what `status="merged"` reports. S5 then
    # tore down both worktrees/branches, so re-merging by name is no longer
    # possible -- assert on the durable trunk state + the merged verdicts instead.
    assert result_a.integrate_result["status"] == "merged"
    assert result_b.integrate_result["status"] == "merged"
    assert result_a.integrate_result["conflicts"] == []
    assert result_b.integrate_result["conflicts"] == []

    merged_text = (r / "mod_a.py").read_text()
    assert "return x - y" in merged_text
    assert "return z * 3" in merged_text


def _branch_exists(repo, name):
    out = subprocess.run(
        ["git", "branch", "--list", name],
        cwd=str(repo),
        capture_output=True,
        text=True,
    ).stdout
    return out.strip() != ""


def test_run_agent_cleans_up_worktree_and_branch_and_rerun_is_idempotent(client, repo):
    """S5: run_agent tears its worktree + branch down after integrate/release, and
    add_worktree prunes a stale same-named leftover -- so a rerun under the SAME
    agent_id neither leaks nor collides. Pre-S5 the first run left
    `.worktrees/agent-x` + branch `agent-x` behind, and the second run's
    `git worktree add -b agent-x` raised (branch/path already exists)."""
    r, base = repo

    def _run_once():
        return run_agent(
            agent_id="agent-x",
            client=client,
            repo=r,
            task="rewrite helper",
            target_parcels=["mod_a.py::helper"],
            mutator=mutators.edit_function_body,
            mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_body": "return x + y + 7"},
            base_commit=base,
            heartbeat_interval=0.05,
        )

    first = _run_once()
    assert first.status == "done"
    # no leak: the ephemeral worktree dir and branch are both gone.
    assert not (r / ".worktrees" / "agent-x").exists()
    assert not _branch_exists(r, "agent-x")

    # rerun with the same agent_id succeeds (idempotent add_worktree), no collision.
    second = _run_once()
    assert second.status == "done"
    assert not (r / ".worktrees" / "agent-x").exists()
    assert not _branch_exists(r, "agent-x")


def test_run_agent_fetches_read_contracts(client, repo):
    r, base = repo
    result = run_agent(
        agent_id="agent-1",
        client=client,
        repo=r,
        task="edit other, read helper's contract",
        target_parcels=["mod_a.py::other"],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_a.py", "symbol": "other", "new_body": "return z + 1"},
        base_commit=base,
        read_contracts=["mod_a.py::helper"],
    )
    assert result.status == "done"
    # helper isn't a frozen contract in this tiny 1-file fixture (blast_radius
    # never reaches the freeze threshold) -- contract() gracefully returns None.
    assert result.contract_snapshot == {"mod_a.py::helper": None}


# --- U15: per-parcel lease_modes override (DESIGN §5.3 enforcement point) ---------


def test_lease_modes_overrides_lease_mode_per_parcel(client, repo):
    """`lease_modes` (U15) lets a caller force a specific parcel's lease mode
    regardless of `lease_mode`'s task-wide default -- this is exactly the
    hook `coordinator.broker.run` (U15) uses to force `exclusive` on a
    frozen-contract target. Verified against the REAL event log, not just
    the returned dataclass, since `lease_modes_used` is only this function's
    own bookkeeping."""
    r, base = repo
    result = run_agent(
        agent_id="agent-exclusive",
        client=client,
        repo=r,
        task="edit helper under an exclusive override",
        target_parcels=["mod_a.py::helper", "mod_a.py::other"],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_body": "return x + y + 1"},
        base_commit=base,
        lease_mode="write",
        lease_modes={"mod_a.py::helper": "exclusive"},
    )
    assert result.status == "done"
    assert result.lease_modes_used == {
        "mod_a.py::helper": "exclusive",
        "mod_a.py::other": "write",
    }

    events = client.events(since=0)
    granted = {
        json.loads(e["payload"])["parcel_id"]: json.loads(e["payload"])["mode"]
        for e in events
        if e["type"] == "lease_granted" and e["agent_id"] == "agent-exclusive"
    }
    assert granted["mod_a.py::helper"] == "exclusive"
    assert granted["mod_a.py::other"] == "write"


# --- mutators ------------------------------------------------------------------


def test_edit_function_body_replaces_only_the_target_symbol(tmp_path):
    (tmp_path / "m.py").write_text(
        textwrap.dedent(
            """\
            def a():
                return 1


            def b():
                return 2
            """
        ),
        encoding="utf-8",
    )
    mutators.edit_function_body(tmp_path, "m.py", "a", "return 99")
    text = (tmp_path / "m.py").read_text()
    assert "return 99" in text
    assert "return 2" in text  # b untouched
    assert "def a():" in text and "def b():" in text  # signatures intact


def test_edit_function_body_on_a_method(tmp_path):
    (tmp_path / "m.py").write_text(
        textwrap.dedent(
            """\
            class C:
                def m(self):
                    return 1

                def n(self):
                    return 2
            """
        ),
        encoding="utf-8",
    )
    mutators.edit_function_body(tmp_path, "m.py", "C.m", "return 42")
    text = (tmp_path / "m.py").read_text()
    assert "return 42" in text
    assert "return 2" in text


def test_change_signature_updates_header_keeps_body(tmp_path):
    (tmp_path / "m.py").write_text(
        "def helper(x, y=1):\n    return x + y\n", encoding="utf-8"
    )
    mutators.change_signature(tmp_path, "m.py", "helper", "def helper(x, y=1, z=0)")
    text = (tmp_path / "m.py").read_text()
    assert text.startswith("def helper(x, y=1, z=0):\n")
    assert "return x + y" in text


def test_fix_call_site_replaces_within_symbol_only(tmp_path):
    (tmp_path / "m.py").write_text(
        textwrap.dedent(
            """\
            def use_b():
                return helper(1)


            def other_b():
                return helper(1)
            """
        ),
        encoding="utf-8",
    )
    mutators.fix_call_site(tmp_path, "m.py", "use_b", "helper(1)", "helper(1, 2)")
    text = (tmp_path / "m.py").read_text()
    assert "def use_b():\n    return helper(1, 2)" in text
    assert "def other_b():\n    return helper(1)" in text  # untouched


def test_fix_call_site_raises_if_not_found(tmp_path):
    (tmp_path / "m.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        mutators.fix_call_site(tmp_path, "m.py", "a", "nope(", "x(")


def test_break_a_test_makes_symbol_raise(tmp_path):
    (tmp_path / "m.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    mutators.break_a_test(tmp_path, "m.py", "a")
    text = (tmp_path / "m.py").read_text()
    assert "raise RuntimeError" in text


def test_slow_edit_applies_edit_then_returns_when_not_hanging(tmp_path):
    (tmp_path / "m.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    mutators.slow_edit(tmp_path, "m.py", "a", "return 2", hang=False, delay=0.01)
    assert "return 2" in (tmp_path / "m.py").read_text()


# --- R3 P0-1: the heartbeat must outlive the integrate gate ------------------------


def test_heartbeat_keeps_the_lease_alive_across_the_integrate_gate(client, repo, monkeypatch):
    """The lease must still be alive while `/integrate` runs its pytest gate.

    `run_agent` used to `heartbeater.stop()` in an inner `finally` that wrapped only
    add_worktree/mutator/commit_all, so /parcel_update and /integrate -- whose gate
    runs for an unbounded time (impact selection falls back to the FULL suite) -- ran
    with ZERO beats against a 30s TTL. Any gate slower than the TTL therefore got the
    still-working agent's lease reaped and re-granted, in write mode, to a second
    agent.

    Asserted the way the bug actually bites: the lease's `ttl_expires_at` must
    ADVANCE while integrate is in flight. Moving `stop()` back above step 6 makes
    this fail.
    """
    r, base = repo
    real_integrate = BlackboardClient.integrate
    observed: dict = {}

    def slow_integrate(self, agent_id, **kwargs):
        # Stand where the pytest gate stands: in the middle of /integrate, long
        # enough for several heartbeat intervals to elapse.
        before = client.leases()
        time.sleep(0.3)
        after = client.leases()
        observed["before"] = {r_["id"]: r_["ttl_expires_at"] for r_ in before}
        observed["after"] = {r_["id"]: r_["ttl_expires_at"] for r_ in after}
        return real_integrate(self, agent_id, **kwargs)

    monkeypatch.setattr(BlackboardClient, "integrate", slow_integrate)

    result = run_agent(
        "agent-hb",
        client,
        r,
        task="edit helper",
        target_parcels=["mod_a.py::helper"],
        mutator=mutators.edit_function_body,
        mutator_kwargs={
            "path": "mod_a.py",
            "symbol": "helper",
            "new_body": "return x + y + 7",
        },
        base_commit=base,
        lease_ttl=30.0,
        heartbeat_interval=0.05,
    )
    assert result.status == "done"

    lease_id = result.lease_ids["mod_a.py::helper"]
    assert lease_id in observed["before"], "lease vanished before integrate even started"
    assert observed["after"][lease_id] > observed["before"][lease_id], (
        "lease TTL did not advance during /integrate: the agent stopped heartbeating "
        "before its own gate ran, so a slow gate gets its lease reaped mid-flight"
    )


# --- R3 P1-6: a rejected agent's branch is the only copy of its work ---------------


@pytest.mark.parametrize("rejected_status", ["merge_rejected", "needs_rebase"])
def test_run_agent_keeps_its_branch_when_integrate_does_not_land(
    client, repo, monkeypatch, rejected_status
):
    """On a rejection the branch MUST survive: it is the only ref to the work.

    S5's worktree cleanup called `remove_worktree(delete_branch=True)`
    unconditionally after integrate returned -- including on merge_rejected
    (conflict, red gate, integration_error) and needs_rebase. The integrator
    explicitly `reset --hard`s trunk back to `pre_merge_sha` on those paths, so
    nothing else references the agent's commits and `git branch -D` made them
    unreachable (reflog only). That silently destroyed the work AND defeated DESIGN
    §5.5's bounce-back-and-rebase story: there is no branch left to rebase.
    """
    r, base = repo

    def rejecting_integrate(self, agent_id, **kwargs):
        return {"status": rejected_status, "branch": agent_id, "reason": "simulated"}

    monkeypatch.setattr(BlackboardClient, "integrate", rejecting_integrate)

    result = run_agent(
        agent_id="agent-rej",
        client=client,
        repo=r,
        task="break helper",
        target_parcels=["mod_a.py::helper"],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_body": "return 999"},
        base_commit=base,
        heartbeat_interval=0.05,
    )
    assert result.integrate_result["status"] == rejected_status

    assert _branch_exists(r, "agent-rej"), (
        f"branch was deleted after {rejected_status}: the agent's commits are now "
        f"unreachable and there is nothing left to rebase and resubmit"
    )
    # The commit itself is still reachable through that branch.
    reachable = subprocess.run(
        ["git", "cat-file", "-e", f"{result.commit_sha}^{{commit}}"],
        cwd=r,
        capture_output=True,
    )
    assert reachable.returncode == 0, "the rejected agent's commit is gone"
    # The ephemeral worktree dir is still cleaned up -- only the branch is kept.
    assert not (r / ".worktrees" / "agent-rej").exists()


def test_run_agent_still_deletes_its_branch_when_the_merge_lands(client, repo):
    """The other half: a LANDED merge's commits live in trunk, so the branch really
    is redundant and must still be cleaned up (no leaked refs per run)."""
    r, base = repo
    result = run_agent(
        agent_id="agent-ok",
        client=client,
        repo=r,
        task="rewrite helper",
        target_parcels=["mod_a.py::helper"],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_body": "return x + y + 7"},
        base_commit=base,
        heartbeat_interval=0.05,
    )
    assert result.integrate_result["status"] == "merged"
    assert not _branch_exists(r, "agent-ok")
    assert not (r / ".worktrees" / "agent-ok").exists()


# --- R4 P0: the client must outlive the gate it is waiting on ---------------------


def test_client_over_a_real_url_does_not_use_httpx_5s_default_timeout():
    """A real-socket client must not inherit httpx's 5s default.

    R4 found this and I reproduced it against a live server: `httpx.Client(base_url=...)`
    with no `timeout=` gets a 5s default, while the pytest gate `/integrate` blocks on
    bounds itself at 600s. So ANY gate slower than 5 seconds raised ReadTimeout in the
    agent WHILE THE SERVER WENT ON AND LANDED THE MERGE -- the agent reports failure and
    tears down believing its work was lost, while trunk has it.

    The whole suite drives a `TestClient`, which has no socket and no timeout, so
    nothing here could ever have caught it. These tests assert on the transport the
    client BUILDS, which is the only place the defect lives.
    """
    from swarmsync.agent import client as client_mod

    c = client_mod.BlackboardClient("http://127.0.0.1:8787")
    try:
        configured = c._http.timeout  # type: ignore[attr-defined]
        assert configured.read != 5.0, "client is on httpx's 5s default"
        assert configured.read == client_mod.DEFAULT_TIMEOUT_SECONDS
    finally:
        c.close()


def test_integrate_waits_longer_than_the_servers_gate_ceiling(monkeypatch):
    """The /integrate request window must exceed the gate's own ceiling + slack.

    Timing out on /integrate does not cancel anything -- the server keeps merging --
    so a client window shorter than the gate guarantees agent/trunk divergence rather
    than preventing it.
    """
    from swarmsync.agent import client as client_mod
    from swarmsync.coordinator import integrator

    # Default: client window must clear the integrator's default ceiling.
    assert client_mod._integrate_timeout() > integrator.DEFAULT_GATE_TIMEOUT_SECONDS

    # And it must track the operator's override, not just the default.
    monkeypatch.setenv("SWARMSYNC_GATE_TIMEOUT", "900")
    assert client_mod._integrate_timeout() > 900
    assert integrator._gate_timeout() == 900
    assert client_mod._integrate_timeout() > integrator._gate_timeout(), (
        "raising SWARMSYNC_GATE_TIMEOUT must widen the client window too, or the "
        "client starts timing out on gates the server is still allowed to run"
    )

    # A junk value must not collapse the window to something shorter than the gate.
    monkeypatch.setenv("SWARMSYNC_GATE_TIMEOUT", "not-a-number")
    assert client_mod._integrate_timeout() > integrator._gate_timeout()


def test_integrate_passes_its_long_timeout_to_a_real_transport_only():
    """The long window is applied per-request to a transport we own; an injected one
    (TestClient / a caller's own client) keeps its own policy -- passing `timeout` to
    a TestClient is a no-op that only earns a deprecation warning."""
    from swarmsync.agent import client as client_mod

    class _Recorder:
        def __init__(self):
            self.kwargs = None

        def post(self, url, **kwargs):
            self.kwargs = kwargs

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {"status": "merged"}

            return _R()

        def get(self, url, **kwargs):  # pragma: no cover - unused here
            raise AssertionError

    rec = _Recorder()
    injected = client_mod.BlackboardClient(rec)
    injected.integrate("a1", branch="a1", repo="/tmp/x")
    assert "timeout" not in rec.kwargs, "injected transport must keep its own timeout policy"

    # A client we own gets the long window explicitly on the integrate call.
    owned = client_mod.BlackboardClient("http://127.0.0.1:8787")
    owned._http = rec  # type: ignore[assignment]  # keep _owns_http=True
    owned.integrate("a1", branch="a1", repo="/tmp/x")
    assert rec.kwargs.get("timeout") == client_mod._integrate_timeout()


# --- C11 (WP3.6): in-process failure containment -----------------------------------


def _exploding_mutator(worktree, **kwargs):
    raise RuntimeError("boom mid-edit")


def test_run_agent_contains_a_raising_mutator_releases_leases_and_returns_error(client, repo):
    """C11: an exception in the work phase must NOT raise out of run_agent and must
    NOT leave the acquired leases active until TTL expiry. Before the fix the
    try/finally had no except path: the mutator's RuntimeError propagated to the
    caller and the write-lease on mod_a.py::helper stayed `active` (leaked)."""
    r, base = repo
    result = run_agent(
        agent_id="agent-crash",
        client=client,
        repo=r,
        task="crash mid-edit",
        target_parcels=["mod_a.py::helper"],
        mutator=_exploding_mutator,
        base_commit=base,
    )

    # structured error result, not a raise.
    assert result.status == "error"
    assert result.error_type == "RuntimeError"
    assert result.error is not None and "boom mid-edit" in result.error

    # every lease this attempt held was released -- nothing left active.
    active = client.leases()
    assert not any(le["agent_id"] == "agent-crash" for le in active), (
        f"agent-crash leaked a lease past its own crash: {active!r}"
    )
    # ...and the parcel is genuinely free again, immediately (no TTL wait).
    regrab = client.lease("agent-next", "mod_a.py::helper", mode="write")
    assert regrab["granted"] is True, "the crashed attempt's lease was not released"

    # the existing finally still tore the worktree down.
    assert not (r / ".worktrees" / "agent-crash").exists()


def test_run_agent_containment_covers_a_failing_commit_too(client, repo, monkeypatch):
    """The except path must cover the whole work phase, not just the mutator --
    commit_all raising (e.g. git's own transient ref/index lock contention) is the
    reachable-in-production case the broker's concurrency creates."""
    from swarmsync.agent import runner as runner_mod

    r, base = repo

    def failing_commit(worktree, message, allow_empty=False):
        raise git_ops.GitOpsError("fatal: Unable to create '.git/index.lock': File exists")

    monkeypatch.setattr(runner_mod.git_ops, "commit_all", failing_commit)

    result = run_agent(
        agent_id="agent-gitlock",
        client=client,
        repo=r,
        task="edit helper",
        target_parcels=["mod_a.py::helper"],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_body": "return 0"},
        base_commit=base,
    )
    assert result.status == "error"
    assert result.error_type == "GitOpsError"  # the broker's retry keys off this
    assert not any(le["agent_id"] == "agent-gitlock" for le in client.leases())
    assert not (r / ".worktrees" / "agent-gitlock").exists()


def test_run_agent_error_containment_swallows_a_failing_release_with_a_logged_note(
    client, repo, monkeypatch, caplog
):
    """Release-on-error is best-effort: if the release itself fails (server went
    away), containment must still return the error result -- with a logged note --
    and leave the lease to the reaper, never raise a second exception."""
    import logging

    r, base = repo

    real_release = BlackboardClient.release

    def failing_release(self, agent_id, lease_id):
        if agent_id == "agent-crash2":
            raise ConnectionError("server went away")
        return real_release(self, agent_id, lease_id)

    monkeypatch.setattr(BlackboardClient, "release", failing_release)

    with caplog.at_level(logging.WARNING, logger="swarmsync.agent.runner"):
        result = run_agent(
            agent_id="agent-crash2",
            client=client,
            repo=r,
            task="crash mid-edit",
            target_parcels=["mod_a.py::helper"],
            mutator=_exploding_mutator,
            base_commit=base,
        )

    assert result.status == "error"
    assert result.error_type == "RuntimeError"  # the ORIGINAL error, not the release's
    assert any("release" in rec.message.lower() for rec in caplog.records), (
        "the swallowed release failure left no logged note"
    )


def test_run_agent_does_not_mask_keyboard_interrupt(client, repo):
    """Containment catches Exception, not BaseException: a KeyboardInterrupt must
    still propagate (the operator is killing the process; masking it would turn
    Ctrl-C into a fake 'error' result). The finally still cleans the worktree."""
    r, base = repo

    def interrupted_mutator(worktree, **kwargs):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_agent(
            agent_id="agent-int",
            client=client,
            repo=r,
            task="interrupted",
            target_parcels=["mod_a.py::helper"],
            mutator=interrupted_mutator,
            base_commit=base,
        )
    # the existing finally still ran.
    assert not (r / ".worktrees" / "agent-int").exists()


# --- WP4.4 (A8): the two formerly-silent swallows now leave a log trace -------


def test_heartbeater_logs_a_failed_beat_and_does_not_raise(caplog):
    """A dying blackboard mid-run must leave a trace: `_Heartbeater._run`
    still swallows the failure (the daemon thread must never crash) but now
    logs it at WARNING with the lease + agent context."""
    import logging as _logging

    from swarmsync.agent.runner import _Heartbeater

    class _DyingClient:
        def heartbeat(self, agent_id, lease_id):
            hb._stop.set()  # end the beat loop right after this (failing) beat
            raise RuntimeError("blackboard went away")

    hb = _Heartbeater(_DyingClient(), "agent-hb", interval=0.01)
    hb.add(42)
    with caplog.at_level(_logging.WARNING, logger="swarmsync.agent.runner"):
        hb._run()  # run the loop synchronously; the failure must not escape
    beats = [r for r in caplog.records if "heartbeat for lease 42" in r.getMessage()]
    assert len(beats) == 1, "the swallowed heartbeat failure left no logged note"
    assert beats[0].levelno == _logging.WARNING
    assert "agent-hb" in beats[0].getMessage()


def test_cleanup_worktree_logs_swallowed_git_failures_and_does_not_raise(
    tmp_path, monkeypatch, caplog
):
    """`_cleanup_worktree` still swallows GitOpsError on both the park and the
    remove step (cleanup must never become the reason a run fails), but each
    swallow now logs at DEBUG with the agent context."""
    import logging as _logging

    from swarmsync.agent import runner
    from swarmsync.worktree.git_ops import GitOpsError

    def _boom(*args, **kwargs):
        raise GitOpsError("simulated git failure")

    monkeypatch.setattr(runner.git_ops, "park_branch", _boom)
    monkeypatch.setattr(runner.git_ops, "remove_worktree", _boom)
    with caplog.at_level(_logging.DEBUG, logger="swarmsync.agent.runner"):
        runner._cleanup_worktree(tmp_path, "agent-x", park_branch=True)  # no raise
    messages = [r.getMessage() for r in caplog.records]
    assert any("park_branch failed" in m and "agent-x" in m for m in messages), messages
    assert any("remove_worktree failed" in m and "agent-x" in m for m in messages), messages
