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
from swarmsync.classifier.graph import SymbolModeError
from swarmsync.coordinator import broker
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


def test_resolve_task_refuses_symbol_mode_even_when_the_symbol_parcel_exists(conn_and_client):
    """`mod_a.py::helper` is a real, indexed parcel -- the one input shape symbol mode used
    to resolve to a symbol id (the other two shapes always fell back to the module id, so
    they would 'pass' a broken guard by accident). It must refuse anyway."""
    conn, _client = conn_and_client
    task = broker.Task(
        task_id="t",
        targets=[("mod_a.py", "helper")],
        mutator=mutators.edit_function_body,
    )
    assert broker._load_parcel(conn, "mod_a.py::helper") is not None
    with pytest.raises(SymbolModeError, match="parked"):
        broker.resolve_task(conn, task, mode="symbol")
    # File granularity still collapses the same hint to the whole-file parcel.
    assert broker.resolve_task(conn, task, mode="file") == ["mod_a.py::<module>"]


def test_two_symbols_in_one_file_serialize_at_file_granularity(conn_and_client):
    """Was `..._symbol_mode_two_symbols_same_file_are_schedulable`. The file-mode half is
    orthogonal and still the enforced guarantee, so it stays; the symbol half asserted the
    exact capability that is now parked (two agents in one file at once), so it now asserts
    the refusal instead."""
    conn, _client = conn_and_client
    task_helper = broker.Task(
        task_id="a", targets=[("mod_a.py", "helper")], mutator=mutators.edit_function_body
    )
    task_other = broker.Task(
        task_id="b", targets=[("mod_a.py", "other")], mutator=mutators.edit_function_body
    )
    # file mode: same file -> NOT co-schedulable (the enforced default, and now the only one).
    assert broker.schedulable(conn, task_helper, task_other, mode="file") is False
    with pytest.raises(SymbolModeError, match="parked"):
        broker.schedulable(conn, task_helper, task_other, mode="symbol")


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


# --- symbol granularity is parked: it must refuse from EVERY public entry point ---------


def test_symbol_mode_refuses_from_every_public_broker_entry_point(conn_and_client, repo):
    """Every public function here that takes a granularity `mode` must refuse "symbol" --
    a guard on one entry point is not a guard. `run`/`group_schedulable` are ALSO checked
    with an empty task list: they must refuse on their own, not by luck of delegating to
    `resolve_task` (which an empty list never reaches)."""
    conn, client = conn_and_client
    r, _base = repo
    task_a = broker.Task(
        task_id="a", targets=[("mod_a.py", "helper")], mutator=mutators.edit_function_body
    )
    task_b = broker.Task(
        task_id="b", targets=[("mod_b.py", "compute")], mutator=mutators.edit_function_body
    )

    with pytest.raises(SymbolModeError):
        broker.resolve_task(conn, task_a, mode="symbol")
    with pytest.raises(SymbolModeError):
        broker.schedulable(conn, task_a, task_b, mode="symbol")
    with pytest.raises(SymbolModeError):
        broker.group_schedulable(conn, [task_a, task_b], mode="symbol")
    with pytest.raises(SymbolModeError):
        broker.group_schedulable(conn, [], mode="symbol")
    with pytest.raises(SymbolModeError):
        broker.run(conn, r, [task_a], client, n_agents=1, mode="symbol")
    with pytest.raises(SymbolModeError):
        broker.run(conn, r, [], client, n_agents=1, mode="symbol")


def test_refused_symbol_mode_run_leases_nothing_and_leaves_no_events(conn_and_client, repo):
    """`run` must refuse BEFORE any side effect: a refusal that had already leased or
    spawned would be worse than no guard, since the caller would think nothing happened."""
    conn, client = conn_and_client
    r, base = repo
    before = len(client.events(since=0))
    task = broker.Task(
        task_id="a",
        targets=[("mod_a.py", "helper")],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_body": "return 0"},
        base_commit=base,
    )
    with pytest.raises(SymbolModeError):
        broker.run(conn, r, [task], client, n_agents=1, mode="symbol")
    assert len(client.events(since=0)) == before
    assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0


def test_symbol_mode_refusal_message_is_actionable(conn_and_client):
    conn, _client = conn_and_client
    task = broker.Task(
        task_id="t", targets=[("mod_a.py", "helper")], mutator=mutators.edit_function_body
    )
    with pytest.raises(SymbolModeError) as exc:
        broker.resolve_task(conn, task, mode="symbol")
    msg = str(exc.value)
    assert "parked" in msg
    assert "string match" in msg  # WHY: the lease store's conflict rule
    assert "SYMBOL_MODE_DESIGN.md" in msg  # where the revival plan lives
    assert "mode='file'" in msg  # what to do instead


def test_every_granularity_taking_entry_point_is_covered_by_the_guard_sweep():
    """Tripwire: if someone adds a NEW public function that takes a granularity `mode`,
    this fails until they add it to the sweep above. Without this, "unreachable from every
    entry point" silently decays to "unreachable from the entry points that existed today".

    Identified by the `mode: str = "file"` signature -- the granularity convention here.
    (`agent.runner`/`agent.client`'s `mode` is the lease READ/WRITE mode, a different axis:
    it defaults to "write" and so is correctly not matched.)
    """
    import inspect

    from swarmsync.classifier import graph as graph_mod

    covered = {
        (graph_mod.__name__, "co_schedulable"),
        (broker.__name__, "resolve_task"),
        (broker.__name__, "schedulable"),
        (broker.__name__, "group_schedulable"),
        (broker.__name__, "run"),
    }
    found = set()
    for module in (graph_mod, broker):
        for name, fn in vars(module).items():
            if name.startswith("_") or not inspect.isfunction(fn):
                continue
            if fn.__module__ != module.__name__:
                continue  # imported symbol, not this module's own entry point
            param = inspect.signature(fn).parameters.get("mode")
            if param is not None and param.default == "file":
                found.add((module.__name__, name))
    assert found == covered, (
        f"granularity entry points changed: {found ^ covered}. Add the new one to "
        "test_symbol_mode_refuses_from_every_public_broker_entry_point (and guard it "
        "with check_file_granularity) or update this tripwire."
    )


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


def test_broker_run_still_detects_and_announces_a_contract_change_at_file_granularity(
    frozen_conn_and_client, frozen_repo
):
    """Was `test_broker_auto_upgrades_frozen_contract_target_to_exclusive_lease`, which drove
    this through `broker.run(..., mode="symbol")` and asserted BOTH halves of DESIGN §5.3:
    the preventive half (a frozen-contract target is upgraded to an EXCLUSIVE lease) and the
    detective half (a landed signature change emits `contract_change`).

    Symbol granularity is parked, and the preventive half is inert without it: contracts are
    only extracted for function/class parcels, while file mode resolves every target to its
    `<module>` parcel, so no target is ever in `frozen_ids`. That half moves to the unit test
    below, which pins the parked mechanism directly.

    The detective half is NOT parked -- it still ships, because `integrate` re-indexes and
    diffs `type_hash` on merge regardless of what was leased. So this test keeps it, now on
    the mode the product actually ships (`mode="file"`), where it had no coverage before.
    """
    conn, client = frozen_conn_and_client
    r, base = frozen_repo
    task = broker.Task(
        task_id="change-helper-signature",
        targets=[("mod_a.py", "helper")],
        mutator=mutators.change_signature,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_sig": "def helper(x, scale=1)"},
        base_commit=base,
    )

    results = broker.run(conn, r, [task], client, n_agents=1, mode="file")
    result = results["change-helper-signature"]
    assert result.status == "done"
    # The whole file was leased, not the symbol -- that IS the parked decision, asserted.
    assert "mod_a.py::<module>" in result.lease_modes_used
    assert "mod_a.py::helper" not in result.lease_modes_used

    events = client.events(since=0)
    # A real contract_change event landed for the symbol anyway (DESIGN §5.3).
    changes = [
        json.loads(e["payload"])
        for e in events
        if e["type"] == "contract_change"
        and json.loads(e["payload"]).get("symbol") == "mod_a.py::helper"
    ]
    assert changes and "scale" in changes[0]["new_signature"]


def test_frozen_contract_target_is_upgraded_to_an_exclusive_lease_parked_mechanism(
    frozen_conn_and_client, frozen_repo
):
    """DESIGN §5.3's PREVENTIVE half, kept alive as a unit test while it is parked.

    `_run_task_once` upgrades any target parcel that is in `frozen_ids` to an EXCLUSIVE
    lease. Via `broker.run` this is now unreachable (see the test above), so this drives
    `_run_task_once` directly with an injected `frozen_ids` -- the shape `load_scheduling_graph`
    would produce if the resolved target were a symbol parcel. This is deliberately NOT a
    claim that the upgrade fires in the shipping product: it pins the mechanism so the
    SYMBOL_MODE_DESIGN.md revival has something that fails if someone deletes it meanwhile.
    """
    conn, client = frozen_conn_and_client
    r, base = frozen_repo
    task = broker.Task(
        task_id="change-helper-signature",
        targets=[("mod_a.py", "helper")],
        mutator=mutators.change_signature,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_sig": "def helper(x, scale=1)"},
        base_commit=base,
    )
    # At file granularity the target resolves to the whole-file parcel; freeze THAT id so
    # the upgrade has something to bite on without reaching for the parked symbol mode.
    module_id = "mod_a.py::<module>"
    assert broker.resolve_task(conn, task, mode="file") == [module_id]

    result = broker._run_task_once(
        conn, r, client, task, agent_id="upgrade-unit", mode="file", frozen_ids={module_id}
    )
    assert result.status == "done"
    assert result.lease_modes_used.get(module_id) == "exclusive"

    granted = [
        json.loads(e["payload"])
        for e in client.events(since=0)
        if e["type"] == "lease_granted"
        and json.loads(e["payload"]).get("parcel_id") == module_id
    ]
    assert granted and all(g["mode"] == "exclusive" for g in granted)


# --- C11 (WP3.6): broker failure containment -----------------------------------------


def _exploding_mutator(worktree, **kwargs):
    raise RuntimeError("boom mid-edit")


def test_broker_contains_a_crashing_task_and_keeps_sibling_results(conn_and_client, repo):
    """C11: one task crashing must not abort the whole run. Before the fix the
    mutator's RuntimeError propagated through run_agent and out of
    `future.result()`, so `broker.run` raised and EVERY other task's result was
    discarded (the sibling's edit had even landed on trunk already -- the caller
    just never got told)."""
    conn, client = conn_and_client
    r, base = repo
    task_bad = broker.Task(
        task_id="crashing-task",
        targets=[("mod_a.py", "helper")],
        mutator=_exploding_mutator,
        base_commit=base,
    )
    task_good = broker.Task(
        task_id="edit-compute",
        targets=[("mod_b.py", "compute")],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_b.py", "symbol": "compute", "new_body": "return n * 10"},
        base_commit=base,
    )

    # disjoint targets -> same wave, genuinely concurrent dispatch.
    results = broker.run(conn, r, [task_bad, task_good], client, n_agents=2)

    # the sibling's result survived AND its edit landed.
    assert results["edit-compute"].status == "done"
    assert "return n * 10" in (r / "mod_b.py").read_text()

    # the crashing task is recorded as an error result for THAT task only.
    bad = results["crashing-task"]
    assert bad.status == "error"
    assert bad.error_type == "RuntimeError"
    assert bad.error is not None and "boom mid-edit" in bad.error

    # and the crashed attempt's lease was released, not leaked until TTL.
    active = client.leases()
    assert active == [], f"a crashed task leaked active leases: {active!r}"


def test_broker_records_an_error_result_when_the_runner_itself_raises(
    conn_and_client, repo, monkeypatch
):
    """Even if run_agent somehow raises PAST its own containment, the broker must
    catch it per-task, record an error result, and keep the run going."""
    conn, client = conn_and_client
    r, base = repo
    real_run_agent = broker.run_agent

    def raising_run_agent(*args, **kwargs):
        if kwargs["task"] == "raises-out-of-the-runner":
            raise RuntimeError("runner escaped containment")
        return real_run_agent(*args, **kwargs)

    monkeypatch.setattr(broker, "run_agent", raising_run_agent)

    task_bad = broker.Task(
        task_id="raises-out-of-the-runner",
        targets=[("mod_a.py", "helper")],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_body": "return 0"},
        base_commit=base,
    )
    task_good = broker.Task(
        task_id="edit-compute",
        targets=[("mod_b.py", "compute")],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": "mod_b.py", "symbol": "compute", "new_body": "return n * 10"},
        base_commit=base,
    )

    results = broker.run(conn, r, [task_bad, task_good], client, n_agents=2)

    assert results["edit-compute"].status == "done"
    bad = results["raises-out-of-the-runner"]
    assert bad.status == "error"
    assert bad.error_type == "RuntimeError"
    assert bad.error is not None and "escaped containment" in bad.error


def test_broker_retries_once_on_transient_gitops_error_and_succeeds(
    conn_and_client, repo, caplog
):
    """A GitOpsError is git's own transient ref/index lock contention -- exactly
    what concurrent `git worktree add`/checkout in one repo can hit -- so the
    broker grants ONE bounded retry, and logs it so an operator can see it."""
    import logging

    conn, client = conn_and_client
    r, base = repo
    calls = {"n": 0}

    def transient_git_failure_mutator(worktree, path, symbol, new_body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise git_ops.GitOpsError("fatal: Unable to create '.git/index.lock': File exists")
        mutators.edit_function_body(worktree, path, symbol, new_body)

    task = broker.Task(
        task_id="edit-helper",
        targets=[("mod_a.py", "helper")],
        mutator=transient_git_failure_mutator,
        mutator_kwargs={"path": "mod_a.py", "symbol": "helper", "new_body": "return x - y"},
        base_commit=base,
    )

    with caplog.at_level(logging.WARNING, logger="swarmsync.coordinator.broker"):
        results = broker.run(conn, r, [task], client, n_agents=1)

    assert calls["n"] == 2  # first attempt failed, the ONE retry ran
    assert results["edit-helper"].status == "done"
    assert "return x - y" in (r / "mod_a.py").read_text()
    # the retry is visible: a logged, inspectable note naming the task.
    retry_notes = [
        rec for rec in caplog.records if "retry" in rec.message.lower() and "edit-helper" in rec.message
    ]
    assert retry_notes, "the GitOpsError retry left no inspectable trace"


def test_broker_gitops_retry_is_bounded_second_failure_records_the_error(
    conn_and_client, repo
):
    """The GitOpsError retry is ONE retry, not a loop: a second failure records
    the error result for the task and moves on."""
    conn, client = conn_and_client
    r, base = repo
    calls = {"n": 0}

    def always_git_failure(worktree, **kwargs):
        calls["n"] += 1
        raise git_ops.GitOpsError("lock contention that never clears")

    task = broker.Task(
        task_id="edit-helper",
        targets=[("mod_a.py", "helper")],
        mutator=always_git_failure,
        base_commit=base,
    )

    results = broker.run(conn, r, [task], client, n_agents=1)

    assert calls["n"] == 2, "expected exactly one original attempt + one retry"
    result = results["edit-helper"]
    assert result.status == "error"
    assert result.error_type == "GitOpsError"
    # nothing leaked despite two crashed attempts.
    assert client.leases() == []


def test_broker_non_gitops_error_is_not_retried(conn_and_client, repo):
    """The bounded retry exists for git's transient lock contention ONLY -- an
    arbitrary crash is not presumed transient and gets no second run (a mutator
    with side effects must not be silently re-run on an unknown failure)."""
    conn, client = conn_and_client
    r, base = repo
    calls = {"n": 0}

    def counting_explosion(worktree, **kwargs):
        calls["n"] += 1
        raise RuntimeError("not a git lock problem")

    task = broker.Task(
        task_id="crashing-task",
        targets=[("mod_a.py", "helper")],
        mutator=counting_explosion,
        base_commit=base,
    )

    results = broker.run(conn, r, [task], client, n_agents=1)

    assert calls["n"] == 1
    assert results["crashing-task"].status == "error"
    assert results["crashing-task"].error_type == "RuntimeError"
