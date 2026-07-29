-- swarm-sync blackboard schema (DESIGN.md §4.1)
-- SQLite in WAL mode: single-writer, concurrent readers, ACID transactions.
-- The SQLite tables ARE the state of record. The `events` table is the
-- append-only pheromone/audit log -- observability, NOT a recovery source:
-- state writes and event emits are separate autocommit statements, several
-- mutations (run_index, _ensure_parcel, pheromone decay) emit no event at all,
-- and crash recovery reads the `open_integrations` projection (WP3.2), never a
-- log replay.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Database-level facts, one row per key. Keys in use:
--   schema_version -- stamped by blackboard/db.py `init_db`; init_db REFUSES to
--                     open a DB at any other version (see db.SCHEMA_VERSION for
--                     the version history and the refuse-plus-rotate policy).
--   managed_root   -- the repo root this DB file was first bound to (see
--                     db.bind_managed_root); reusing a DB against a different
--                     root would silently mix root-relative parcel ids.
-- Additive -- CREATE TABLE IF NOT EXISTS needs no migration.
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parcels (
  id            TEXT PRIMARY KEY,     -- "path::qualified.name" or "path::<module>"
  path          TEXT NOT NULL,
  symbol        TEXT,                 -- NULL for interstitial (module-glue) parcels
  kind          TEXT,                 -- function|method|class|module
  territory     INTEGER,
  blast_radius  INTEGER NOT NULL DEFAULT 0,
  contract_hash TEXT,                 -- signature/type hash if frozen, else NULL
  content_hash  TEXT,                 -- sha256 of current source slice
  byte_start    INTEGER,
  byte_end      INTEGER,
  state_summary TEXT,                 -- live note of what this parcel now does
  updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  parcel_id      TEXT NOT NULL REFERENCES parcels(id),
  agent_id       TEXT NOT NULL,
  mode           TEXT NOT NULL,       -- read|write|exclusive
  acquired_at    REAL NOT NULL,
  ttl_expires_at REAL NOT NULL,
  heartbeat_at   REAL NOT NULL,
  intent         TEXT,
  status         TEXT NOT NULL        -- active|released|reaped
);
CREATE INDEX IF NOT EXISTS idx_leases_active ON leases(parcel_id, status);
-- The reaper's sweep (`UPDATE leases SET status='reaped' WHERE status='active'
-- AND ttl_expires_at<=?`) matched no index and full-scanned the never-pruned
-- leases table every interval (finding C4). This index makes it a range scan
-- over just the active, past-TTL rows instead. Additive -- CREATE INDEX IF NOT
-- EXISTS needs no migration.
CREATE INDEX IF NOT EXISTS idx_leases_reap ON leases(status, ttl_expires_at);

CREATE TABLE IF NOT EXISTS contracts (
  symbol    TEXT PRIMARY KEY,
  signature TEXT NOT NULL,
  type_hash TEXT NOT NULL,
  frozen    INTEGER NOT NULL DEFAULT 1,
  version   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS pheromone (
  parcel_id  TEXT NOT NULL REFERENCES parcels(id),
  agent_id   TEXT NOT NULL,
  kind       TEXT NOT NULL,           -- planned|touched|done
  strength   REAL NOT NULL,           -- decays toward 0 over time
  updated_at REAL NOT NULL,
  PRIMARY KEY (parcel_id, agent_id, kind)
);

CREATE TABLE IF NOT EXISTS intents (
  agent_id       TEXT NOT NULL,
  task           TEXT NOT NULL,
  target_parcels TEXT NOT NULL,       -- JSON array of parcel ids
  declared_at    REAL NOT NULL,
  PRIMARY KEY (agent_id, task)
);

-- Projection of `integrate_started` events that have not yet reached a terminal
-- verdict (merged | merge_rejected | integrate_orphaned). WP3.2, finding C3:
-- startup crash-recovery (`coordinator.integrator.reconcile_orphaned_integrations`)
-- reads THIS table -- O(open rows, in practice 0 or 1) -- instead of replaying the
-- unbounded, heartbeat-dominated event log through a fixed window. Rows are
-- INSERTed in the SAME transaction as the `integrate_started` emit and DELETEd in
-- the same transaction as the terminal emit, so the table is exactly the set of
-- integrates that died (or are still running) without a verdict; at startup --
-- reconciliation runs before serving, integrate is serialized in-process -- any
-- row present IS an orphan.
CREATE TABLE IF NOT EXISTS open_integrations (
  started_seq      INTEGER PRIMARY KEY,  -- seq of the integrate_started event
  repo             TEXT NOT NULL,
  branch           TEXT NOT NULL,
  into_branch      TEXT NOT NULL,        -- the `into` trunk ("into" is an SQL keyword)
  trunk_sha_before TEXT NOT NULL,        -- what to reset `into` back to
  ts               REAL NOT NULL,
  -- How many times startup reconciliation has TRIED and failed to roll this orphan
  -- back (schema v3). A failed rollback keeps the row -- deleting it stranded an
  -- un-gated merge on trunk with nothing left that could ever detect it -- so the
  -- retry needs a bound, or a genuinely dead repo would be retried on every boot
  -- forever, hold its `integrate_started` event out of compaction permanently, and
  -- sit in /health's orphan count with no way to clear it. See
  -- `coordinator.integrator.MAX_RECONCILE_ATTEMPTS`.
  reconcile_attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
  seq      INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT,
  type     TEXT NOT NULL,             -- planned|lease_granted|lease_denied|heartbeat|
                                      -- done|released|reaped|contract_change|merged|
                                      -- merge_rejected|reindexed|needs_rebase
  payload  TEXT,                      -- JSON
  ts       REAL NOT NULL
);
