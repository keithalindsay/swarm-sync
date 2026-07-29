"""U1 — Blackboard DB + schema. DESIGN.md §4.1.

Done when:
  - init_db(tmp) creates all 6 tables
  - PRAGMA journal_mode returns 'wal'
  - a second init_db on the same file is a no-op (idempotent, no errors, no data loss)

WP3.4 adds the schema-version gate + managed-root binding tests:
  - fresh DBs are stamped `schema_version = SCHEMA_VERSION` in `meta`
  - legacy DBs (app tables, no stamp) and wrong-version DBs are REFUSED
  - `bind_managed_root` pins a DB to one repo root and refuses a different one
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from swarmsync.blackboard import db
from swarmsync.blackboard.models import Event, Lease, Parcel


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}


def test_init_db_creates_all_six_tables(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        names = _table_names(conn)
        assert set(db.EXPECTED_TABLES) <= names
        assert len(db.EXPECTED_TABLES) == 6
    finally:
        conn.close()


def test_journal_mode_is_wal(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_foreign_keys_enabled(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_second_init_db_is_idempotent_no_op(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn1 = db.init_db(dbfile)
    now = time.time()
    conn1.execute(
        "INSERT INTO parcels (id, path, symbol, kind, blast_radius, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("a.py::foo", "a.py", "foo", "function", 0, now),
    )
    conn1.close()

    # Re-running init_db against the same file must not error and must not wipe
    # or duplicate existing data / schema objects.
    conn2 = db.init_db(dbfile)
    try:
        names = _table_names(conn2)
        assert set(db.EXPECTED_TABLES) <= names

        rows = conn2.execute("SELECT * FROM parcels").fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == "a.py::foo"

        # calling init_db a third time still doesn't error or duplicate
        conn3 = db.init_db(dbfile)
        try:
            rows3 = conn3.execute("SELECT * FROM parcels").fetchall()
            assert len(rows3) == 1
        finally:
            conn3.close()
    finally:
        conn2.close()


def test_connect_without_init_on_existing_db_reuses_schema(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    conn.close()

    conn2 = db.connect(dbfile)
    try:
        names = _table_names(conn2)
        assert set(db.EXPECTED_TABLES) <= names
        assert conn2.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn2.close()


def test_row_factory_is_sqlite_row(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        conn.execute(
            "INSERT INTO events (agent_id, type, payload, ts) VALUES (?, ?, ?, ?)",
            ("agent-1", "planned", "{}", time.time()),
        )
        row = conn.execute("SELECT * FROM events").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["type"] == "planned"
        assert row["agent_id"] == "agent-1"
    finally:
        conn.close()


def test_reset_removes_db_file(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    conn.close()
    assert dbfile.exists()

    db.reset(dbfile)
    assert not dbfile.exists()

    # reset on an already-absent file must not raise
    db.reset(dbfile)


def test_events_seq_autoincrements(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        cur1 = conn.execute(
            "INSERT INTO events (agent_id, type, payload, ts) VALUES (?, ?, ?, ?)",
            ("a1", "planned", None, time.time()),
        )
        cur2 = conn.execute(
            "INSERT INTO events (agent_id, type, payload, ts) VALUES (?, ?, ?, ?)",
            ("a1", "lease_granted", None, time.time()),
        )
        assert cur2.lastrowid > cur1.lastrowid
    finally:
        conn.close()


def test_leases_reference_parcels_foreign_key_enforced(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        now = time.time()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO leases "
                "(parcel_id, agent_id, mode, acquired_at, ttl_expires_at, heartbeat_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("does.not.exist", "agent-1", "write", now, now + 30, now, "active"),
            )
    finally:
        conn.close()


def test_models_validate_from_sqlite_rows(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        now = time.time()
        conn.execute(
            "INSERT INTO parcels (id, path, symbol, kind, blast_radius, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("a.py::foo", "a.py", "foo", "function", 2, now),
        )
        row = conn.execute("SELECT * FROM parcels WHERE id='a.py::foo'").fetchone()
        parcel = Parcel.model_validate(dict(row))
        assert parcel.id == "a.py::foo"
        assert parcel.kind == "function"
        assert parcel.blast_radius == 2

        conn.execute(
            "INSERT INTO leases "
            "(parcel_id, agent_id, mode, acquired_at, ttl_expires_at, heartbeat_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("a.py::foo", "agent-1", "write", now, now + 30, now, "active"),
        )
        lrow = conn.execute("SELECT * FROM leases").fetchone()
        lease = Lease.model_validate(dict(lrow))
        assert lease.mode == "write"
        assert lease.status == "active"

        conn.execute(
            "INSERT INTO events (agent_id, type, payload, ts) VALUES (?, ?, ?, ?)",
            ("agent-1", "lease_granted", "{}", now),
        )
        erow = conn.execute("SELECT * FROM events").fetchone()
        event = Event.model_validate(dict(erow))
        assert event.type == "lease_granted"
    finally:
        conn.close()


# --------------------------------------------------------------------------
# WP3.4 — schema version gate (finding C7)
# --------------------------------------------------------------------------


def _make_legacy_db(dbfile) -> None:
    """Build a pre-`meta` (schema v1) DB: application tables exist, no stamp.

    Uses the real schema script and then drops `meta`, so the fixture stays in
    lockstep with the DDL instead of hand-copying it.
    """
    raw = sqlite3.connect(str(dbfile))
    try:
        raw.executescript(db.SCHEMA)
        raw.executescript("DROP TABLE meta;")
    finally:
        raw.close()


def test_init_db_stamps_fresh_db_with_current_schema_version(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert row is not None
        assert row["value"] == str(db.SCHEMA_VERSION)
        assert db.SCHEMA_VERSION == 3
    finally:
        conn.close()


def test_second_init_db_does_not_alter_the_version_stamp(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    db.init_db(dbfile).close()
    conn = db.init_db(dbfile)  # must not raise, must not re-stamp/duplicate
    try:
        rows = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchall()
        assert [r["value"] for r in rows] == [str(db.SCHEMA_VERSION)]
    finally:
        conn.close()


def test_init_db_refuses_legacy_db_without_version_stamp(tmp_path):
    """Finding C7: pre-meta DBs used to be silently accepted (CREATE IF NOT
    EXISTS masked the difference) and then stranded by any schema change."""
    dbfile = tmp_path / "legacy.db"
    _make_legacy_db(dbfile)

    with pytest.raises(db.SchemaVersionError) as exc:
        db.init_db(dbfile)
    msg = str(exc.value)
    assert "--fresh" in msg  # the remedy is named
    # the refusal must not have half-upgraded the file: still no meta table
    raw = sqlite3.connect(str(dbfile))
    try:
        assert (
            raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            is None
        )
    finally:
        raw.close()


def test_init_db_refuses_legacy_db_with_meta_but_no_version_row(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    conn.execute("DELETE FROM meta WHERE key='schema_version'")
    conn.close()

    with pytest.raises(db.SchemaVersionError):
        db.init_db(dbfile)


@pytest.mark.parametrize("stamp", ["1", "2", "4"])
def test_init_db_refuses_version_mismatch_older_and_newer(tmp_path, stamp):
    """The gate covers both directions: an older stamp AND a newer one (a DB
    written by future code must not be silently downgraded either)."""
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (stamp,))
    conn.close()

    with pytest.raises(db.SchemaVersionError) as exc:
        db.init_db(dbfile)
    msg = str(exc.value)
    assert f"v{stamp}" in msg
    assert f"v{db.SCHEMA_VERSION}" in msg
    assert "--fresh" in msg


def test_init_db_on_empty_file_is_treated_as_fresh(tmp_path):
    """`connect()` (and sqlite generally) may leave a zero-byte file behind;
    that must count as fresh, not legacy."""
    dbfile = tmp_path / "blackboard.db"
    dbfile.touch()
    conn = db.init_db(dbfile)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert row["value"] == str(db.SCHEMA_VERSION)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# WP3.4 — managed-root binding (finding U8)
# --------------------------------------------------------------------------


def test_bind_managed_root_first_call_stores_the_root(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        assert db.stored_managed_root(conn) is None
        db.bind_managed_root(conn, "/repo/alpha")
        assert db.stored_managed_root(conn) == "/repo/alpha"
    finally:
        conn.close()


def test_bind_managed_root_same_root_is_a_noop(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        db.bind_managed_root(conn, "/repo/alpha")
        db.bind_managed_root(conn, "/repo/alpha")  # must not raise
        db.bind_managed_root(conn, "/repo/alpha")
        rows = conn.execute(
            "SELECT value FROM meta WHERE key='managed_root'"
        ).fetchall()
        assert [r["value"] for r in rows] == ["/repo/alpha"]
    finally:
        conn.close()


def test_bind_managed_root_different_root_raises_naming_both_and_remedies(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        db.bind_managed_root(conn, "/repo/alpha")
        with pytest.raises(db.ManagedRootMismatchError) as exc:
            db.bind_managed_root(conn, "/repo/beta")
        msg = str(exc.value)
        assert "/repo/alpha" in msg  # the original binding
        assert "/repo/beta" in msg  # the offending root
        assert "--fresh" in msg  # remedy 1: rotate
        assert "original root" in msg  # remedy 2: point back at the bound root
        # the failed bind must not have overwritten the stored root
        assert db.stored_managed_root(conn) == "/repo/alpha"
    finally:
        conn.close()


def test_bind_managed_root_survives_reconnect(tmp_path):
    """The binding is a property of the DB FILE, not the connection."""
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    db.bind_managed_root(conn, "/repo/alpha")
    conn.close()

    conn2 = db.init_db(dbfile)
    try:
        assert db.stored_managed_root(conn2) == "/repo/alpha"
        with pytest.raises(db.ManagedRootMismatchError):
            db.bind_managed_root(conn2, "/repo/beta")
    finally:
        conn2.close()


def test_bind_managed_root_second_binder_loses_and_sees_the_winners_root(tmp_path):
    """Two connections, sequential: INSERT OR IGNORE + read-back means the first
    bind wins and a second one for a different root raises, seeing the winner's.

    Deliberately NOT threaded -- this pins the INSERT OR IGNORE semantics, not the
    race. (It was previously named `..._concurrent_first_bind_one_winner`, which
    promised a concurrency test its body never performed.)"""
    dbfile = tmp_path / "blackboard.db"
    conn_a = db.init_db(dbfile)
    conn_b = db.connect(dbfile)
    try:
        db.bind_managed_root(conn_a, "/repo/alpha")
        with pytest.raises(db.ManagedRootMismatchError):
            db.bind_managed_root(conn_b, "/repo/beta")
        assert db.stored_managed_root(conn_b) == "/repo/alpha"
    finally:
        conn_a.close()
        conn_b.close()
