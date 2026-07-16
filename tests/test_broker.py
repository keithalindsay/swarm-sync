"""U12 — Broker (task->parcel scheduling). DESIGN.md §5, §6.

Done when (BUILD_PLAN.md): given 3 tasks (2 disjoint, 1 overlapping), the
broker dispatches the 2 disjoint concurrently and serializes the overlapping
one; a task whose agent is reaped is reassigned and completes.
"""
from __future__ import annotations

import json
import textwrap
import threading
import time

import pytest
from fastapi.testclient import TestClient

from swarmsync.agent import mutators
from swarmsync.agent.client import BlackboardClient
from swarmsync.coordinator import broker, reaper
from swarmsync.server import leases as leases_mod
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
    (r / "mod_b.py").write_text(
        textwrap.dedent(
            """\
            def compute(n):
                return n + 1
            """
        ),
        encoding="utf-8",
    )
    base = git_ops.init_repo(r)
    return r, base


@pytest.fixture()
def conn_and_client(tmp_path, repo):
    r, _base = repo
    # reaper_interval=None: no background asyncio loop -- these tests drive
    # reap_once/decay explicitly (via the broker's own retry loop), no need
    # to race a real-time reaper against the test body.
    app = create_app(tmp_path / "blackboard.db", reaper_interval=None)
    with TestClient(app) as c:
        resp = c.post("/index", json={"root": str(r)})
        assert resp.status_code == 200
        yield app.state.conn, BlackboardClient(c)


# --- done-when part 1: 2 disjoint tasks dispatch concurrently, 1 overlapping
# task serializes -----------------------------------------------------------

_timeline_lock = threading.Lock()


def _timed_edit(worktree, path, symbol, new_body, label, timeline, hold=0.0):
    """Test-only mutator: records a (label, "start"/"end", monotonic-clock)
    entry around the real edit so the test can prove two tasks' wall-clock
    windows genuinely overlapped (concurrent dispatch) or didn't (serialized).
    """
    with _timeline_lock:
        timeline.append((label, "start", time.monotonic()))
    if hold:
        time.sleep(hold)
    mutators.edit_function_body(worktree, path, symbol, new_body)
    with _timeline_lock:
        timeline.append((label, "end", time.monotonic()))


def test_broker_dispatches_disjoint_concurrently_and_serializes_overlap(
    conn_and_client, repo
):
    conn, client = conn_and_client
    r, base = repo
    timeline: list[tuple[str, str, float]] = []

    task_a = broker.Task(
        task_id="edit-helper",
        targets=[("mod_a.py", "helper")],
        mutator=_timed_edit,
        mutator_kwargs={
            "path": "mod_a.py",
            "symbol": "helper",
            "new_body": "return x - y",
            "label": "A",
            "timeline": timeline,
            "hold": 0.3,
        },
        base_commit=base,
    )
    task_b = broker.Task(
        task_id="edit-compute",
        targets=[("mod_b.py", "compute")],
        mutator=_timed_edit,
        mutator_kwargs={
            "path": "mod_b.py",
            "symbol": "compute",
            "new_body": "return n * 10",
            "label": "B",
            "timeline": timeline,
            "hold": 0.3,
        },
        base_commit=base,
    )
    task_c = broker.Task(
        task_id="edit-other",
        targets=[("mod_a.py", "other")],  # same FILE as task_a -> file-mode overlap
        mutator=_timed_edit,
        mutator_kwargs={
            "path": "mod_a.py",
            "symbol": "other",
            "new_body": "return z + 5",
            "label": "C",
            "timeline": timeline,
        },
        base_commit=base,
    )

    # Sanity-check the scheduling relation directly (BUILD_PLAN's literal
    # resolve_task/schedulable/group_schedulable surface) before running.
    assert broker.resolve_task(conn, task_a, mode="file") == ["mod_a.py::<module>"]
    assert broker.resolve_task(conn, task_c, mode="file") == ["mod_a.py::<module>"]
    assert broker.resolve_task(conn, task_b, mode="file") == ["mod_b.py::<module>"]
    assert broker.schedulable(conn, task_a, task_b, mode="file") is True
    assert broker.schedulable(conn, task_a, task_c, mode="file") is False

    waves = broker.group_schedulable(conn, [task_a, task_b, task_c], mode="file")
    assert [t.task_id for t in waves[0]] == ["edit-helper", "edit-compute"]
    assert [t.task_id for t in waves[1]] == ["edit-other"]

    results = broker.run(conn, r, [task_a, task_b, task_c], client, n_agents=2)

    assert results["edit-helper"].status == "done"
    assert results["edit-compute"].status == "done"
    assert results["edit-other"].status == "done"
    for res in results.values():
        assert res.integrate_result is not None
        assert res.integrate_result["status"] == "merged"

    by_label = {}
    for label, kind, ts in timeline:
        by_label.setdefault(label, {})[kind] = ts

    # A and B (the disjoint wave) genuinely overlapped in wall-clock time --
    # proof of real concurrent dispatch, not just "both eventually ran."
    assert by_label["A"]["start"] < by_label["B"]["end"]
    assert by_label["B"]["start"] < by_label["A"]["end"]

    # C (the overlapping/serialized task) only started once BOTH of the
    # first wave's tasks had finished -- proof the broker waited for the
    # whole co-schedulable wave to drain before dispatching the conflicting
    # task, i.e. it was truly serialized after them, not merely lucky timing.
    assert by_label["C"]["start"] >= by_label["A"]["end"]
    assert by_label["C"]["start"] >= by_label["B"]["end"]

    # All three edits actually landed on trunk (the main checkout == "integration").
    merged_a = (r / "mod_a.py").read_text()
    merged_b = (r / "mod_b.py").read_text()
    assert "return x - y" in merged_a
    assert "return z + 5" in merged_a
    assert "return n * 10" in merged_b


# --- done-when part 2: a task whose agent is reaped is reassigned and
# completes -------------------------------------------------------------


def test_broker_reassigns_task_after_original_agent_is_reaped(conn_and_client, repo):
    conn, client = conn_and_client
    r, base = repo

    # Simulate a previous agent ("agent-dead") that took the write-lease for
    # this task and then crashed: the lease is ACTIVE and unexpired for a
    # short window (so the broker's first dispatch attempt genuinely loses
    # the CAS race -> lease_denied), then ages out shortly after.
    held = leases_mod.acquire(
        conn, "mod_a.py::<module>", "agent-dead", mode="write", ttl=0.3, intent="edit-helper"
    )
    assert held.granted is True

    task = broker.Task(
        task_id="edit-helper",
        targets=[("mod_a.py", "helper")],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_body": "return x + y + 999"},
        base_commit=base,
        max_attempts=5,
    )

    results = broker.run(
        conn, r, [task], client, n_agents=1, retry_backoff=0.5
    )

    result = results["edit-helper"]
    assert result.status == "done"
    assert result.integrate_result is not None
    assert result.integrate_result["status"] == "merged"
    assert "return x + y + 999" in (r / "mod_a.py").read_text()

    events = client.events(since=0)
    denied = [e for e in events if e["type"] == "lease_denied"]
    assert len(denied) >= 1  # the first attempt genuinely lost the CAS race

    reaped = [e for e in events if e["type"] == "reaped" and e["agent_id"] == "agent-dead"]
    assert len(reaped) == 1  # the dead agent's lease really was reaped, not just expired

    # the row that actually landed the edit was a FRESH agent, not "agent-dead".
    done_events = [e for e in events if e["type"] == "done"]
    assert any(e["agent_id"] != "agent-dead" for e in done_events)
    assert not any(e["agent_id"] == "agent-dead" for e in done_events)

    # sequencing: lease_denied (attempt 1) < reaped (bookkeeping pass before
    # attempt 2) < the eventual done.
    order = [e["type"] for e in events]
    assert order.index("lease_denied") < order.index("reaped")
    assert order.index("reaped") < order.index("done")


# --- unit-level coverage of resolve_task/schedulable/group_schedulable ------


def test_resolve_task_symbol_mode_uses_the_named_symbol(conn_and_client):
    conn, _client = conn_and_client
    task = broker.Task(
        task_id="t",
        targets=[("mod_a.py", "helper")],
        mutator=mutators.edit_function_body,
    )
    assert broker.resolve_task(conn, task, mode="symbol") == ["mod_a.py::helper"]


def test_resolve_task_symbol_mode_falls_back_to_module_for_bare_file_hint(conn_and_client):
    conn, _client = conn_and_client
    task = broker.Task(
        task_id="t",
        targets=[("mod_a.py", None)],
        mutator=mutators.edit_function_body,
    )
    assert broker.resolve_task(conn, task, mode="symbol") == ["mod_a.py::<module>"]


def test_resolve_task_symbol_mode_falls_back_when_symbol_doesnt_exist(conn_and_client):
    conn, _client = conn_and_client
    task = broker.Task(
        task_id="t",
        targets=[("mod_a.py", "nonexistent_symbol")],
        mutator=mutators.edit_function_body,
    )
    assert broker.resolve_task(conn, task, mode="symbol") == ["mod_a.py::<module>"]


def test_resolve_task_symbol_mode_two_symbols_same_file_are_schedulable(conn_and_client):
    conn, _client = conn_and_client
    task_helper = broker.Task(
        task_id="a", targets=[("mod_a.py", "helper")], mutator=mutators.edit_function_body
    )
    task_other = broker.Task(
        task_id="b", targets=[("mod_a.py", "other")], mutator=mutators.edit_function_body
    )
    # file mode: same file -> NOT co-schedulable (the enforced default).
    assert broker.schedulable(conn, task_helper, task_other, mode="file") is False
    # symbol mode: disjoint byte spans in the same file -> co-schedulable.
    assert broker.schedulable(conn, task_helper, task_other, mode="symbol") is True


def test_resolve_task_raises_on_unindexed_file(conn_and_client):
    conn, _client = conn_and_client
    task = broker.Task(
        task_id="t",
        targets=[("nope.py", None)],
        mutator=mutators.edit_function_body,
    )
    with pytest.raises(ValueError):
        broker.resolve_task(conn, task)


def test_resolve_task_raises_on_unknown_mode(conn_and_client):
    conn, _client = conn_and_client
    task = broker.Task(task_id="t", targets=[("mod_a.py", None)], mutator=mutators.edit_function_body)
    with pytest.raises(ValueError):
        broker.resolve_task(conn, task, mode="bogus")


def test_group_schedulable_all_disjoint_is_one_wave(conn_and_client):
    conn, _client = conn_and_client
    tasks = [
        broker.Task(task_id="a", targets=[("mod_a.py", "helper")], mutator=mutators.edit_function_body),
        broker.Task(task_id="b", targets=[("mod_b.py", "compute")], mutator=mutators.edit_function_body),
    ]
    waves = broker.group_schedulable(conn, tasks, mode="file")
    assert len(waves) == 1
    assert {t.task_id for t in waves[0]} == {"a", "b"}


def test_read_deps_pass_through_to_contract_snapshot(conn_and_client, repo):
    conn, client = conn_and_client
    r, base = repo
    task = broker.Task(
        task_id="edit-other-read-helper",
        targets=[("mod_a.py", "other")],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_a.py", "symbol": "other", "new_body": "return z + 1"},
        base_commit=base,
        read_deps=["mod_a.py::helper"],
    )
    results = broker.run(conn, r, [task], client, n_agents=1)
    result = results["edit-other-read-helper"]
    assert result.status == "done"
    assert result.contract_snapshot == {"mod_a.py::helper": None}


# --- U15: frozen-contract targets are auto-upgraded to an EXCLUSIVE lease ---------


@pytest.fixture()
def frozen_repo(tmp_path):
    """A fixture repo with THREE real cross-module callers of `mod_a.py::helper`
    -- enough to clear the DEFAULT `FREEZE_THRESHOLD` (3) so it's a frozen
    contract at both /index time and at the integrator's own re-index (on
    merge) without needing a non-default threshold anywhere, unlike `repo`'s
    plain `mod_a.py`/`mod_b.py` pair (no cross-file references at all --
    never frozen at any threshold)."""
    r = tmp_path / "frozen_repo"
    r.mkdir()
    (r / "mod_a.py").write_text("def helper(x):\n    return x + 1\n", encoding="utf-8")
    for name, offset in (("mod_b", 1), ("mod_c", 2), ("mod_d", 3)):
        (r / f"{name}.py").write_text(
            f"from mod_a import helper\n\n\ndef caller(x):\n    return helper(x) + {offset}\n",
            encoding="utf-8",
        )
    base = git_ops.init_repo(r)
    return r, base


@pytest.fixture()
def frozen_conn_and_client(tmp_path, frozen_repo):
    r, _base = frozen_repo
    app = create_app(tmp_path / "frozen_blackboard.db", reaper_interval=None)
    with TestClient(app) as c:
        resp = c.post("/index", json={"root": str(r)})
        assert resp.status_code == 200
        assert resp.json()["contracts"] >= 1
        yield app.state.conn, BlackboardClient(c)


def test_broker_auto_upgrades_frozen_contract_target_to_exclusive_lease(
    frozen_conn_and_client, frozen_repo
):
    conn, client = frozen_conn_and_client
    r, base = frozen_repo
    task = broker.Task(
        task_id="change-helper-signature",
        targets=[("mod_a.py", "helper")],
        mutator=mutators.change_signature,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_sig": "def helper(x, scale=1)"},
        base_commit=base,
    )

    results = broker.run(conn, r, [task], client, n_agents=1, mode="symbol")
    result = results["change-helper-signature"]
    assert result.status == "done"
    assert result.lease_modes_used.get("mod_a.py::helper") == "exclusive"

    events = client.events(since=0)
    granted = [
        json.loads(e["payload"])
        for e in events
        if e["type"] == "lease_granted"
        and json.loads(e["payload"]).get("parcel_id") == "mod_a.py::helper"
    ]
    assert granted and all(g["mode"] == "exclusive" for g in granted)

    # ...and a real contract_change event landed for it (DESIGN §5.3).
    changes = [
        json.loads(e["payload"])
        for e in events
        if e["type"] == "contract_change"
        and json.loads(e["payload"]).get("symbol") == "mod_a.py::helper"
    ]
    assert changes and "scale" in changes[0]["new_signature"]
