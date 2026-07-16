"""U7 — FastAPI server. DESIGN.md §4.2.

Done when (BUILD_PLAN.md): via TestClient, POST /index then GET /parcels returns the
map; POST /lease returns granted/denied; POST /intent, /heartbeat, /release,
/parcel/update, GET /contract/{sym}, GET /events?since= all return expected shapes.
"""
from __future__ import annotations

import textwrap

import pytest
from fastapi.testclient import TestClient

from swarmsync.server.app import create_app
from swarmsync.worktree import git_ops


@pytest.fixture()
def fixture_repo(tmp_path):
    """Same shape as test_graph.py/test_index_api.py's fixture: mod_a.helper is
    imported/called by three other modules (a frozen-contract candidate)."""
    (tmp_path / "mod_a.py").write_text(
        textwrap.dedent(
            """\
            def helper(x, y=1):
                return x + y


            def _private(x):
                return x
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "mod_b.py").write_text(
        textwrap.dedent(
            """\
            from mod_a import helper


            def use_b():
                return helper(1)


            def other_b():
                return 42
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "mod_c.py").write_text(
        textwrap.dedent(
            """\
            from mod_a import helper as h


            def use_c():
                return h(2)
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "mod_d.py").write_text(
        textwrap.dedent(
            """\
            import mod_a


            def use_d():
                return mod_a.helper(3)
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "blackboard.db")
    with TestClient(app) as c:
        yield c


def _index(client, fixture_repo):
    r = client.post("/index", json={"root": str(fixture_repo)})
    assert r.status_code == 200
    return r.json()


# --- done-when: POST /index then GET /parcels returns the map ---------------------


def test_index_then_get_parcels_returns_the_map(client, fixture_repo):
    body = _index(client, fixture_repo)
    assert body["parcels"] > 0
    assert body["contracts"] >= 1

    r = client.get("/parcels")
    assert r.status_code == 200
    parcels = r.json()
    assert isinstance(parcels, list)
    assert len(parcels) == body["parcels"]

    by_id = {p["id"]: p for p in parcels}
    assert "mod_a.py::helper" in by_id
    helper = by_id["mod_a.py::helper"]
    assert helper["path"] == "mod_a.py"
    assert helper["kind"] == "function"
    assert helper["blast_radius"] >= 3
    assert helper["content_hash"] is not None
    assert helper["active_leases"] == []  # no leases taken yet


def test_reindexing_does_not_duplicate_parcel_rows(client, fixture_repo):
    first = _index(client, fixture_repo)
    second = _index(client, fixture_repo)
    assert first["parcels"] == second["parcels"]
    assert first["contracts"] == second["contracts"]

    r = client.get("/parcels")
    ids = [p["id"] for p in r.json()]
    assert len(ids) == len(set(ids))


# --- done-when: POST /lease returns granted/denied ---------------------------------


def test_lease_returns_granted_then_denied_for_conflicting_write(client, fixture_repo):
    _index(client, fixture_repo)

    r1 = client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": "mod_a.py::helper", "mode": "write"},
    )
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["granted"] is True
    assert body1["lease_id"] is not None

    r2 = client.post(
        "/lease",
        json={"agent_id": "agent-2", "parcel_id": "mod_a.py::helper", "mode": "write"},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["granted"] is False
    assert body2["lease_id"] is None
    assert body2["reason"] is not None

    # GET /parcels reflects the active lease.
    r = client.get("/parcels")
    by_id = {p["id"]: p for p in r.json()}
    active = by_id["mod_a.py::helper"]["active_leases"]
    assert len(active) == 1
    assert active[0]["agent_id"] == "agent-1"
    assert active[0]["mode"] == "write"

    # GET /leases lists the same active lease.
    r = client.get("/leases")
    assert r.status_code == 200
    leases = r.json()
    assert len(leases) == 1
    assert leases[0]["agent_id"] == "agent-1"


def test_lease_read_read_both_granted(client, fixture_repo):
    _index(client, fixture_repo)
    r1 = client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": "mod_a.py::helper", "mode": "read"},
    )
    r2 = client.post(
        "/lease",
        json={"agent_id": "agent-2", "parcel_id": "mod_a.py::helper", "mode": "read"},
    )
    assert r1.json()["granted"] is True
    assert r2.json()["granted"] is True


# --- done-when: POST /intent returns expected shape --------------------------------


def test_intent_returns_expected_shape_and_emits_event(client, fixture_repo):
    _index(client, fixture_repo)
    r = client.post(
        "/intent",
        json={
            "agent_id": "agent-1",
            "task": "edit helper",
            "target_parcels": ["mod_a.py::helper"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == "agent-1"
    assert body["task"] == "edit helper"
    assert body["target_parcels"] == ["mod_a.py::helper"]
    assert "declared_at" in body

    events = client.get("/events?since=0").json()
    types = [e["type"] for e in events]
    assert "planned" in types


# --- done-when: POST /heartbeat returns expected shape -----------------------------


def test_heartbeat_returns_ok_true_for_owner_and_false_for_stranger(client, fixture_repo):
    _index(client, fixture_repo)
    lease = client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": "mod_a.py::helper", "mode": "write"},
    ).json()

    r_ok = client.post(
        "/heartbeat", json={"agent_id": "agent-1", "lease_id": lease["lease_id"]}
    )
    assert r_ok.status_code == 200
    assert r_ok.json() == {"ok": True}

    r_bad = client.post(
        "/heartbeat", json={"agent_id": "agent-2", "lease_id": lease["lease_id"]}
    )
    assert r_bad.status_code == 200
    assert r_bad.json() == {"ok": False}


# --- done-when: POST /release returns expected shape, frees the parcel -------------


def test_release_returns_ok_true_and_frees_the_parcel(client, fixture_repo):
    _index(client, fixture_repo)
    lease = client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": "mod_a.py::helper", "mode": "write"},
    ).json()

    r = client.post(
        "/release", json={"agent_id": "agent-1", "lease_id": lease["lease_id"]}
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # parcel is acquirable again
    r2 = client.post(
        "/lease",
        json={"agent_id": "agent-2", "parcel_id": "mod_a.py::helper", "mode": "write"},
    )
    assert r2.json()["granted"] is True

    # releasing again is a no-op, not an error
    r3 = client.post(
        "/release", json={"agent_id": "agent-1", "lease_id": lease["lease_id"]}
    )
    assert r3.json() == {"ok": False}


# --- done-when: POST /parcel/update returns expected shape -------------------------


def test_parcel_update_returns_expected_shape_and_updates_row(client, fixture_repo):
    _index(client, fixture_repo)
    r = client.post(
        "/parcel/update",
        json={
            "agent_id": "agent-1",
            "parcel_id": "mod_a.py::helper",
            "content_hash": "deadbeef",
            "state_summary": "now adds x and y with a comment",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["parcel_id"] == "mod_a.py::helper"

    row = {p["id"]: p for p in client.get("/parcels").json()}["mod_a.py::helper"]
    assert row["content_hash"] == "deadbeef"
    assert row["state_summary"] == "now adds x and y with a comment"

    events = client.get("/events?since=0").json()
    assert any(e["type"] == "done" for e in events)


def test_parcel_update_unknown_parcel_is_404(client, fixture_repo):
    _index(client, fixture_repo)
    r = client.post(
        "/parcel/update",
        json={
            "agent_id": "agent-1",
            "parcel_id": "does.not.exist",
            "content_hash": "deadbeef",
        },
    )
    assert r.status_code == 404


# --- done-when: GET /contract/{sym} returns expected shape -------------------------


def test_get_contract_returns_signature_and_version(client, fixture_repo):
    _index(client, fixture_repo)
    r = client.get("/contract/mod_a.py::helper")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "mod_a.py::helper"
    assert body["signature"] == "helper(x, y=1)"
    assert body["version"] == 1
    assert len(body["type_hash"]) == 64  # sha256 hex


def test_get_contract_unknown_symbol_is_404(client, fixture_repo):
    _index(client, fixture_repo)
    r = client.get("/contract/does.not.exist")
    assert r.status_code == 404


# --- done-when: GET /events?since= returns expected shape -------------------------


def test_events_since_filters_and_orders(client, fixture_repo):
    _index(client, fixture_repo)
    client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": "mod_a.py::helper", "mode": "write"},
    )
    client.post(
        "/lease",
        json={"agent_id": "agent-2", "parcel_id": "mod_a.py::helper", "mode": "write"},
    )

    all_events = client.get("/events?since=0").json()
    assert len(all_events) >= 2
    seqs = [e["seq"] for e in all_events]
    assert seqs == sorted(seqs)

    midpoint = all_events[0]["seq"]
    tail = client.get(f"/events?since={midpoint}").json()
    assert all(e["seq"] > midpoint for e in tail)
    assert len(tail) == len(all_events) - 1


def test_events_default_since_is_zero(client, fixture_repo):
    _index(client, fixture_repo)
    r = client.get("/events")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# --- POST /integrate: U10's real serial test-gated integrator ---------------------


def test_integrate_merges_a_clean_branch_via_the_real_integrator(tmp_path):
    target_repo = tmp_path / "target_repo"
    target_repo.mkdir()
    (target_repo / "mod_a.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    base = git_ops.init_repo(target_repo)
    worktree = git_ops.add_worktree(target_repo, "agent-x", base)
    (worktree / "mod_a.py").write_text(
        "def helper():\n    return 2\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree, "agent-x: bump helper")

    app = create_app(tmp_path / "blackboard.db")
    with TestClient(app) as c:
        c.post("/index", json={"root": str(target_repo)})
        r = c.post(
            "/integrate",
            json={
                "agent_id": "agent-x",
                "branch": "agent-x",
                "repo": str(target_repo),
                "base_commit": base,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "merged"
        assert body["changed_files"] == ["mod_a.py"]

        # the merge really landed on trunk (the main checkout == "integration").
        assert (target_repo / "mod_a.py").read_text() == "def helper():\n    return 2\n"

        events = c.get("/events").json()
        types = [e["type"] for e in events]
        assert "merged" in types
        assert "reindexed" in types


def test_integrate_rejects_a_textual_conflict(tmp_path):
    target_repo = tmp_path / "target_repo2"
    target_repo.mkdir()
    (target_repo / "mod_a.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    base = git_ops.init_repo(target_repo)

    worktree_a = git_ops.add_worktree(target_repo, "agent-a", base)
    (worktree_a / "mod_a.py").write_text(
        "def helper():\n    return 10\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree_a, "agent-a: change helper")

    worktree_b = git_ops.add_worktree(target_repo, "agent-b", base)
    (worktree_b / "mod_a.py").write_text(
        "def helper():\n    return 20\n", encoding="utf-8"
    )
    git_ops.commit_all(worktree_b, "agent-b: change helper differently")

    app = create_app(tmp_path / "blackboard.db")
    with TestClient(app) as c:
        c.post("/index", json={"root": str(target_repo)})
        r1 = c.post(
            "/integrate",
            json={
                "agent_id": "agent-a",
                "branch": "agent-a",
                "repo": str(target_repo),
                "base_commit": base,
            },
        )
        assert r1.json()["status"] == "merged"

        r2 = c.post(
            "/integrate",
            json={
                "agent_id": "agent-b",
                "branch": "agent-b",
                "repo": str(target_repo),
                "base_commit": base,
            },
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["status"] == "merge_rejected"
        assert body2["conflicts"] == ["mod_a.py"]

        # trunk keeps agent-a's change, untouched by the rejected attempt.
        assert (target_repo / "mod_a.py").read_text() == "def helper():\n    return 10\n"


# --- a second app instance / db_path is fully isolated -----------------------------


def test_two_apps_on_different_db_paths_are_isolated(tmp_path, fixture_repo):
    app1 = create_app(tmp_path / "one.db")
    app2 = create_app(tmp_path / "two.db")
    with TestClient(app1) as c1, TestClient(app2) as c2:
        _index(c1, fixture_repo)
        assert c1.get("/parcels").json() != []
        assert c2.get("/parcels").json() == []
