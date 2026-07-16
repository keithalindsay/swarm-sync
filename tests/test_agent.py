"""U9 — Agent client + runner + mutators. DESIGN.md §4.3, §2.

Done when (BUILD_PLAN.md): against a running TestClient server, one run_agent
declares intent, acquires a write-lease, edits a function in its worktree via
a mutator, commits, posts parcel_update, and releases -- verified by the
resulting event sequence and a committed diff.
"""
from __future__ import annotations

import json
import textwrap

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

    # committed diff: exactly the touched file changed on agent-1's branch,
    # and the new body actually landed there.
    touched = git_ops.changed_files(r, "agent-1", base)
    assert touched == ["mod_a.py"]
    worktree = r / ".worktrees" / "agent-1"
    assert "x + y + 100" in (worktree / "mod_a.py").read_text()
    # U10's integrator is real now -- run_agent's own POST /integrate call
    # landed the branch, so the main checkout (== "integration") has the change.
    assert "x + y + 100" in (r / "mod_a.py").read_text()

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
    assert result.integrate_result["_status_code"] == 200
    assert result.integrate_result["status"] == "merged"


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
    assert any(l["agent_id"] == "agent-0" for l in leases)


def test_two_agents_disjoint_functions_same_file_both_land(client, repo):
    """Building block of money-shot #1: two agents editing DIFFERENT functions
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

    ok_a, conflicts_a = git_ops.merge_branch(r, "agent-a", into="integration")
    ok_b, conflicts_b = git_ops.merge_branch(r, "agent-b", into="integration")
    assert (ok_a, conflicts_a) == (True, [])
    assert (ok_b, conflicts_b) == (True, [])

    merged_text = (r / "mod_a.py").read_text()
    assert "return x - y" in merged_text
    assert "return z * 3" in merged_text


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
