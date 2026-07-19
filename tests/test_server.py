"""U7 — FastAPI server. DESIGN.md §4.2.

Done when (BUILD_PLAN.md): via TestClient, POST /index then GET /parcels returns the
map; POST /lease returns granted/denied; POST /intent, /heartbeat, /release,
/parcel/update, GET /contract/{sym}, GET /events?since= all return expected shapes.
"""
from __future__ import annotations

import textwrap
import time

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


# --- C9: TTL must be validated; a ttl<=0 must never double-grant --------------------


def test_lease_rejects_zero_ttl_and_never_grants_two_writers(client, fixture_repo):
    """C9 regression. A `ttl <= 0` makes `ttl_expires_at = now + ttl` land in the
    past, so the lease is granted AND already expired: the CAS predicate treats it as
    non-blocking and a SECOND agent is ALSO granted -- two writers on one parcel, both
    told they hold the lock. Pre-fix this test would see two `granted: True` bodies;
    post-fix `POST /lease {"ttl": 0}` is a 422 and NOTHING is granted.
    """
    _index(client, fixture_repo)

    r1 = client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": "mod_a.py::helper",
              "mode": "write", "ttl": 0},
    )
    assert r1.status_code == 422, r1.text

    r2 = client.post(
        "/lease",
        json={"agent_id": "agent-2", "parcel_id": "mod_a.py::helper",
              "mode": "write", "ttl": 0},
    )
    assert r2.status_code == 422, r2.text

    # The load-bearing property: no lease was born at all, so there is nothing to
    # double-grant. (Pre-fix, two active write leases would exist here.)
    active = client.get("/leases").json()
    assert active == [], f"a rejected ttl still created leases: {active}"


def test_lease_rejects_negative_and_over_ceiling_ttl(client, fixture_repo):
    _index(client, fixture_repo)
    for bad in (-1.0, 86400.0 + 1):
        r = client.post(
            "/lease",
            json={"agent_id": "agent-1", "parcel_id": "mod_a.py::helper",
                  "mode": "write", "ttl": bad},
        )
        assert r.status_code == 422, f"ttl={bad} was accepted: {r.text}"
    assert client.get("/leases").json() == []


def test_lease_accepts_a_valid_positive_ttl(client, fixture_repo):
    """Guard against over-rejection: a sane in-bounds TTL still grants normally."""
    _index(client, fixture_repo)
    r = client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": "mod_a.py::helper",
              "mode": "write", "ttl": 30.0},
    )
    assert r.status_code == 200
    assert r.json()["granted"] is True


def test_heartbeat_rejects_nonpositive_ttl(client, fixture_repo):
    """C9: the same bound guards renewal -- a ttl<=0 heartbeat would push the lease
    into the past and revive the double-lease `heartbeat`'s liveness guard prevents."""
    _index(client, fixture_repo)
    lease = client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": "mod_a.py::helper", "mode": "write"},
    ).json()
    r = client.post(
        "/heartbeat",
        json={"agent_id": "agent-1", "lease_id": lease["lease_id"], "ttl": 0},
    )
    assert r.status_code == 422, r.text


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
    # C5: the caller must hold the write lease it is updating under.
    client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": "mod_a.py::helper", "mode": "write"},
    )
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


def _lease(client, agent_id, parcel_id, mode="write", ttl=None):
    body = {"agent_id": agent_id, "parcel_id": parcel_id, "mode": mode}
    if ttl is not None:
        body["ttl"] = ttl
    r = client.post("/lease", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --- C5: /parcel/update must require the caller to hold the write lease ------------


def test_parcel_update_rejects_writer_without_the_lease(client, fixture_repo):
    """C5 repro. Agent A holds the write lease on a parcel; a DIFFERENT client
    (agent B, holding no lease) must NOT be able to overwrite its content_hash.

    Pre-fix the UPDATE was keyed on parcel_id ONLY -- body.agent_id was used solely
    to label the emitted event -- so agent B's post clobbered the hash that
    integrator._check_read_deps compares plan-time snapshots against, spuriously
    bouncing agent A with needs_rebase. Post-fix agent B is soft-refused and the
    stored hash is UNCHANGED.
    """
    _index(client, fixture_repo)
    parcel = "mod_a.py::helper"

    # Agent A takes the write lease and posts a legitimate update.
    _lease(client, "agent-A", parcel, mode="write")
    ra = client.post(
        "/parcel/update",
        json={"agent_id": "agent-A", "parcel_id": parcel, "content_hash": "aaaa"},
    )
    assert ra.status_code == 200
    assert ra.json()["ok"] is True

    # Agent B holds NO lease and tries to overwrite the same parcel.
    rb = client.post(
        "/parcel/update",
        json={"agent_id": "agent-B", "parcel_id": parcel, "content_hash": "bbbb"},
    )
    assert rb.status_code == 200, rb.text
    body = rb.json()
    assert body["ok"] is False
    # The reason must name the ACTUAL current holder so the caller can act.
    assert "agent-A" in body["reason"]

    # The clobber did NOT land: the hash is still agent-A's value.
    row = {p["id"]: p for p in client.get("/parcels").json()}[parcel]
    assert row["content_hash"] == "aaaa"


def test_parcel_update_rejects_read_lease_holder(client, fixture_repo):
    """A READ lease is not enough: only write/exclusive holders may mutate the
    parcel's content_hash. Agent A holds the write lease; agent B holds only a
    (shared) read lease and must still be refused."""
    _index(client, fixture_repo)
    parcel = "mod_a.py::helper"
    _lease(client, "agent-A", parcel, mode="read")
    _lease(client, "agent-B", parcel, mode="read")

    rb = client.post(
        "/parcel/update",
        json={"agent_id": "agent-B", "parcel_id": parcel, "content_hash": "bbbb"},
    )
    assert rb.status_code == 200, rb.text
    assert rb.json()["ok"] is False


def test_parcel_update_succeeds_for_the_lease_holder(client, fixture_repo):
    """Positive path: the agent that holds the active write lease can update the
    parcel it is editing -- the legitimate broker/hook flow must stay open."""
    _index(client, fixture_repo)
    parcel = "mod_a.py::helper"
    _lease(client, "agent-A", parcel, mode="write")

    r = client.post(
        "/parcel/update",
        json={
            "agent_id": "agent-A",
            "parcel_id": parcel,
            "content_hash": "deadbeef",
            "state_summary": "edited by the holder",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["parcel_id"] == parcel
    assert "event_seq" in body

    row = {p["id"]: p for p in client.get("/parcels").json()}[parcel]
    assert row["content_hash"] == "deadbeef"
    assert row["state_summary"] == "edited by the holder"


def test_parcel_update_refused_once_the_write_lease_expires(client, fixture_repo):
    """Liveness is on SQLite's own clock: once the holder's lease EXPIRES, its own
    later update is refused (an expired lease is not ownership). Uses a tiny TTL so
    the lease lapses without a foreign acquirer, isolating the liveness predicate."""
    _index(client, fixture_repo)
    parcel = "mod_a.py::helper"
    _lease(client, "agent-A", parcel, mode="write", ttl=0.05)
    time.sleep(0.2)

    r = client.post(
        "/parcel/update",
        json={"agent_id": "agent-A", "parcel_id": parcel, "content_hash": "cccc"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False


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


# --- U8/WP3.4: the DB is bound to its repo at startup ------------------------------


def test_lifespan_binds_db_to_managed_root_and_refuses_a_different_root(
    tmp_path, monkeypatch
):
    """Parcel ids are root-relative, so reusing one DB file against a DIFFERENT
    root silently mixes two repos' parcel maps. First boot binds the root into
    `meta`; a later boot with another root must refuse to start, naming both
    roots (`db.ManagedRootMismatchError`). Same root again boots fine. Removing
    the `bind_managed_root` call in the lifespan makes the refusal assertion
    here fail."""
    from swarmsync.blackboard import db as db_mod

    db_path = tmp_path / "bb.db"
    root_a = tmp_path / "repo-a"
    root_a.mkdir()
    root_b = tmp_path / "repo-b"
    root_b.mkdir()

    monkeypatch.setenv("SWARMSYNC_ROOTS", str(root_a))
    with TestClient(create_app(db_path)) as c:
        assert c.get("/leases").status_code == 200
    # Same root, same DB: boots fine (binding is idempotent).
    with TestClient(create_app(db_path)) as c:
        assert c.get("/leases").status_code == 200

    # Different root, same DB: refused, loudly, before serving anything.
    monkeypatch.setenv("SWARMSYNC_ROOTS", str(root_b))
    with pytest.raises(db_mod.ManagedRootMismatchError) as excinfo:
        with TestClient(create_app(db_path)):
            pass
    # The message names both roots so the operator can pick a remedy.
    assert str(root_a) in str(excinfo.value)
    assert str(root_b) in str(excinfo.value)


# --- C4 regression: shutdown must close connections even if the reaper died --------


def test_lifespan_shutdown_closes_conn_even_if_reaper_task_failed(tmp_path, monkeypatch):
    """C4 part 3: a reaper task that stored an exception must not poison shutdown.

    On shutdown the lifespan does `await task`, which re-raises whatever the task
    stored. The old handler caught only `CancelledError`, so a stored
    `OperationalError` propagated OUT of shutdown -- aborting teardown BEFORE
    `reaper_conn.close()` / `conn.close()` ever ran (leaked handles, and the
    TestClient context-manager exit itself raised). The fix catches `Exception`
    so the connections are always closed.
    """
    import sqlite3

    from swarmsync.coordinator import reaper as reaper_mod

    async def poisoned_run(*args, **kwargs):
        # Model a reaper that died on a transient DB error and stored it on the task.
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reaper_mod, "run", poisoned_run)

    app = create_app(tmp_path / "blackboard.db", reaper_interval=0.05)
    conn = app.state.conn

    with TestClient(app):
        # Let the loop actually run the (immediately-failing) reaper task so it is
        # done-with-exception by the time shutdown awaits it.
        time.sleep(0.1)

    # Shutdown must have completed and closed the inspection connection despite the
    # poisoned reaper task -- operating on it now must raise "closed database".
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


# --- a second app instance / db_path is fully isolated -----------------------------


def test_two_apps_on_different_db_paths_are_isolated(tmp_path, fixture_repo):
    app1 = create_app(tmp_path / "one.db")
    app2 = create_app(tmp_path / "two.db")
    with TestClient(app1) as c1, TestClient(app2) as c2:
        _index(c1, fixture_repo)
        assert c1.get("/parcels").json() != []
        assert c2.get("/parcels").json() == []


# --- WP3.3 B (S5): request body-size cap -> 413 ------------------------------------


def test_oversized_body_is_rejected_with_413(client, monkeypatch):
    """A request whose declared Content-Length exceeds SWARMSYNC_MAX_BODY_BYTES must
    be rejected with 413 before any handler runs. The cap is read per request, so a
    small env value takes effect immediately."""
    monkeypatch.setenv("SWARMSYNC_MAX_BODY_BYTES", "1024")

    big = {"agent_id": "a", "parcel_id": "x.py::<module>", "padding": "z" * 5000}
    r = client.post("/lease", json=big)
    assert r.status_code == 413
    assert "SWARMSYNC_MAX_BODY_BYTES" in r.json()["detail"]


def test_normal_sized_bodies_pass_the_cap(client, fixture_repo, monkeypatch):
    """Under the cap (even a small one), every existing endpoint works unchanged --
    the middleware only inspects Content-Length, it never consumes the body."""
    monkeypatch.setenv("SWARMSYNC_MAX_BODY_BYTES", "100000")
    _index(client, fixture_repo)
    r = client.post(
        "/lease",
        json={"agent_id": "a1", "parcel_id": "mod_a.py::helper", "mode": "write"},
    )
    assert r.status_code == 200
    assert r.json()["granted"] is True


def test_body_cap_default_is_generous(client):
    """With the env knob unset, a comfortably-large-but-legitimate body (1 MB, well
    under the 10 MB default) is not rejected by the cap."""
    r = client.post(
        "/lease",
        json={
            "agent_id": "a1",
            "parcel_id": "mod_a.py::helper",
            "mode": "write",
            "intent": "x" * (1024 * 1024),
            "ensure_parcel": True,
        },
    )
    assert r.status_code == 200


# --- WP3.3 C3 (retired by WP4.2): the `swarm-sync` script IS `swarmsync-serve` -----
# The two tests that lived here proved `app.main` ran the C13 clock assertion
# before serving. WP4.2 deleted `app.main` outright -- both console scripts now
# point at `serve.main`, whose clock-assertion ordering is already proven by
# tests/test_serve.py (test_serve_starts_when_clocks_agree /
# test_serve_refuses_to_start_when_clocks_disagree), so there is no second
# launcher left to hold to the invariant.


# --- WP3.1 (S2): GET /events?limit= is clamped -------------------------------------
#
# Reproduced first: pre-fix, `GET /events?limit=999999999` returned 200 and
# materialized the ENTIRE events table into memory + one JSON body, and
# `limit=-1` did too (SQLite treats a negative LIMIT as "no limit"). Out-of-range
# is now a loud 422 (not a silent clamp): a silently-truncated page would let a
# caller believe it saw everything below `since + limit` and skip events.


def test_events_limit_over_cap_is_422(client):
    from swarmsync.server.app import MAX_EVENTS_LIMIT

    r = client.get("/events", params={"limit": 999_999_999})
    assert r.status_code == 422

    r = client.get("/events", params={"limit": MAX_EVENTS_LIMIT + 1})
    assert r.status_code == 422


def test_events_negative_limit_is_422(client):
    # SQLite's `LIMIT -1` means unlimited -- the same unbounded dump by another door.
    r = client.get("/events", params={"limit": -1})
    assert r.status_code == 422


def test_events_limit_at_cap_and_default_still_work(client, fixture_repo):
    from swarmsync.server.app import MAX_EVENTS_LIMIT

    _index(client, fixture_repo)
    client.post("/lease", json={"agent_id": "a1", "parcel_id": "mod_a.py::helper"})

    assert client.get("/events", params={"limit": MAX_EVENTS_LIMIT}).status_code == 200
    r = client.get("/events")  # default (1000) unchanged
    assert r.status_code == 200
    assert any(e["type"] == "lease_granted" for e in r.json())


def test_events_since_semantics_preserved_with_clamped_limit(client, fixture_repo):
    _index(client, fixture_repo)
    client.post("/lease", json={"agent_id": "a1", "parcel_id": "mod_a.py::helper"})
    client.post("/lease", json={"agent_id": "a2", "parcel_id": "mod_b.py::use_b"})

    all_events = client.get("/events", params={"since": 0, "limit": 10}).json()
    assert len(all_events) >= 2
    watermark = all_events[0]["seq"]
    rest = client.get("/events", params={"since": watermark, "limit": 10}).json()
    assert [e["seq"] for e in rest] == [e["seq"] for e in all_events if e["seq"] > watermark]


def test_events_endpoint_serves_compaction_marker_rows(client):
    """WP3.1 S2: the `events_compacted` marker is outside the frozen EventType
    registry; GET /events must serve it (the widened EventOut response model),
    not 500 on response validation."""
    import time as time_mod

    from swarmsync.blackboard import db as db_mod
    from swarmsync.blackboard import events as events_mod

    conn = db_mod.connect(client.app.state.db_path)
    try:
        events_mod.emit(conn, "heartbeat", "a1", {"lease_id": 1},
                        ts=time_mod.time() - 7200)
        assert events_mod.compact_events(conn) == 1
    finally:
        conn.close()

    r = client.get("/events")
    assert r.status_code == 200
    types = [e["type"] for e in r.json()]
    assert types == [events_mod.EVENTS_COMPACTED]


# --- WP4.5 (A6): typed responses on the raw-row endpoints --------------------------
# GET /parcels and /leases used to return raw `dict(row)` -- the wire shape was
# whatever schema.sql said, unvalidated and invisible in OpenAPI, and the hook
# adapter duck-typed against it. The response models DECLARE that shape; these
# tests pin the exact JSON keys so declaring can never silently become changing.

# The exact wire keys of one GET /leases row == the `leases` schema columns.
LEASE_WIRE_KEYS = {
    "id", "parcel_id", "agent_id", "mode",
    "acquired_at", "ttl_expires_at", "heartbeat_at", "intent", "status",
}

# The exact wire keys of one GET /parcels row == parcel columns + the lease join.
PARCEL_WIRE_KEYS = {
    "id", "path", "symbol", "kind", "territory", "blast_radius",
    "contract_hash", "content_hash", "byte_start", "byte_end",
    "state_summary", "updated_at", "active_leases",
}

# The embedded per-lease projection inside a parcel row's `active_leases`.
ACTIVE_LEASE_WIRE_KEYS = {"lease_id", "agent_id", "mode"}


def test_get_leases_wire_shape_is_the_exact_lease_row(client, fixture_repo):
    _index(client, fixture_repo)
    granted = client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": "mod_a.py::helper", "mode": "write"},
    ).json()
    assert granted["granted"] is True

    rows = client.get("/leases").json()
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == LEASE_WIRE_KEYS
    assert row["id"] == granted["lease_id"]
    assert row["parcel_id"] == "mod_a.py::helper"
    assert row["agent_id"] == "agent-1"
    assert row["mode"] == "write"
    assert row["status"] == "active"
    assert row["intent"] is None  # nullable column serialized, not dropped


def test_get_parcels_wire_shape_is_parcel_columns_plus_lease_join(client, fixture_repo):
    _index(client, fixture_repo)
    granted = client.post(
        "/lease",
        json={"agent_id": "agent-1", "parcel_id": "mod_a.py::helper", "mode": "write"},
    ).json()

    rows = client.get("/parcels").json()
    by_id = {p["id"]: p for p in rows}
    leased = by_id["mod_a.py::helper"]
    for row in rows:
        assert set(row) == PARCEL_WIRE_KEYS
    assert leased["active_leases"] == [
        {"lease_id": granted["lease_id"], "agent_id": "agent-1", "mode": "write"}
    ]
    assert set(leased["active_leases"][0]) == ACTIVE_LEASE_WIRE_KEYS
    # An unleased parcel still carries the key, as an empty list.
    assert by_id["mod_b.py::use_b"]["active_leases"] == []


def _resolve_ref(spec: dict, schema: dict) -> dict:
    """Follow a `$ref` into the spec's components (one level -- all we need)."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return spec["components"]["schemas"][name]
    return schema


def _response_array_item_schema(spec: dict, path: str) -> dict:
    """The resolved item schema of `path`'s 200 application/json array response."""
    schema = spec["paths"][path]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert schema["type"] == "array", f"{path} 200 response is not an array"
    return _resolve_ref(spec, schema["items"])


def test_health_reports_an_operational_snapshot(client, fixture_repo):
    """WP5.1: GET /health is the unauthenticated operational surface -- version,
    the single managed root, db path, active-lease count, last event seq -- so
    `swarmsync status`/`doctor` can see the server is up and what it is bound to,
    even when coordination is silently failing open."""
    from swarmsync import __version__

    _index(client, fixture_repo)
    r = client.post("/lease", json={"agent_id": "a1", "parcel_id": "mod_a.py::helper"})
    assert r.status_code == 200

    resp = client.get("/health")
    assert resp.status_code == 200
    h = resp.json()
    assert h["version"] == __version__
    assert h["active_leases"] == 1
    assert h["last_event_seq"] >= 1  # at least the index/lease events happened
    assert h["db_path"].endswith("blackboard.db")
    assert h["root"]  # the single managed root, resolved like every other endpoint


def test_health_needs_no_token_even_when_auth_is_on(monkeypatch, tmp_path):
    """`/health` must answer without a bearer token: it is what an operator hits
    to discover the server is even reachable, so gating it behind the token an
    operator may be debugging would defeat its purpose. Mirrors the other GETs."""
    monkeypatch.setenv("SWARMSYNC_TOKEN", "s3cret")
    app = create_app(tmp_path / "blackboard.db")
    with TestClient(app) as c:
        resp = c.get("/health")  # no Authorization header
        assert resp.status_code == 200
        assert resp.json()["active_leases"] == 0


def test_openapi_declares_the_health_schema(client):
    """WP5.1 acceptance: `/health` is a declared, snapshot-visible part of the wire
    contract (its shape is what `swarmsync status`/`doctor` depend on)."""
    spec = client.app.openapi()
    schema = spec["paths"]["/health"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    health = _resolve_ref(spec, schema)
    assert set(health["properties"]) == {
        "version",
        "root",
        "db_path",
        "active_leases",
        "last_event_seq",
    }


def test_openapi_declares_leases_and_parcels_response_schemas(client):
    """WP4.5 (A6) schema snapshot: the OpenAPI document must now DECLARE the
    /leases and /parcels response shapes (they were undocumented raw rows).
    Focused property assertions, deliberately not a full-JSON golden."""
    spec = client.app.openapi()

    lease_item = _response_array_item_schema(spec, "/leases")
    assert set(lease_item["properties"]) == LEASE_WIRE_KEYS

    parcel_item = _response_array_item_schema(spec, "/parcels")
    assert set(parcel_item["properties"]) == PARCEL_WIRE_KEYS
    lease_info = _resolve_ref(
        spec, parcel_item["properties"]["active_leases"]["items"]
    )
    assert set(lease_info["properties"]) == ACTIVE_LEASE_WIRE_KEYS
    # The identity triple of the embedded projection is required on the wire.
    assert set(lease_info["required"]) == ACTIVE_LEASE_WIRE_KEYS


# --- WP4.5 (prep C17): GET /events?tail= newest-events mode ------------------------


def test_events_tail_returns_newest_n_in_ascending_order(client, fixture_repo):
    _index(client, fixture_repo)
    client.post("/lease", json={"agent_id": "a1", "parcel_id": "mod_a.py::helper"})
    client.post("/lease", json={"agent_id": "a2", "parcel_id": "mod_b.py::use_b"})
    client.post("/lease", json={"agent_id": "a3", "parcel_id": "mod_c.py::use_c"})

    everything = client.get("/events").json()
    assert len(everything) >= 3

    r = client.get("/events", params={"tail": 2})
    assert r.status_code == 200
    newest_two = r.json()
    assert newest_two == everything[-2:]  # the newest 2, still ascending by seq
    seqs = [e["seq"] for e in newest_two]
    assert seqs == sorted(seqs)


def test_events_tail_larger_than_log_returns_all(client, fixture_repo):
    _index(client, fixture_repo)
    client.post("/lease", json={"agent_id": "a1", "parcel_id": "mod_a.py::helper"})

    everything = client.get("/events").json()
    r = client.get("/events", params={"tail": 500})
    assert r.status_code == 200
    assert r.json() == everything


def test_events_tail_and_since_are_mutually_exclusive_422(client):
    # Even an explicit since=0 counts: the caller named both anchors.
    assert client.get("/events", params={"since": 0, "tail": 5}).status_code == 422
    assert client.get("/events", params={"since": 7, "tail": 5}).status_code == 422


def test_events_tail_clamped_to_the_events_cap(client):
    from swarmsync.server.app import MAX_EVENTS_LIMIT

    assert client.get("/events", params={"tail": 0}).status_code == 422
    assert client.get("/events", params={"tail": -1}).status_code == 422
    assert client.get(
        "/events", params={"tail": MAX_EVENTS_LIMIT + 1}
    ).status_code == 422
    assert client.get(
        "/events", params={"tail": MAX_EVENTS_LIMIT}
    ).status_code == 200
