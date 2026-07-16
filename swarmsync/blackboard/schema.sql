-- swarm-sync blackboard schema (DESIGN.md §4.1)
-- SQLite in WAL mode: single-writer, concurrent readers, ACID transactions.
-- The `events` table is the append-only pheromone/audit log and is the source of
-- truth for recovery: parcels/leases/pheromone are projections replayable from it.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

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

CREATE TABLE IF NOT EXISTS events (
  seq      INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT,
  type     TEXT NOT NULL,             -- planned|lease_granted|lease_denied|heartbeat|
                                      -- done|released|reaped|contract_change|merged|
                                      -- merge_rejected|reindexed|needs_rebase
  payload  TEXT,                      -- JSON
  ts       REAL NOT NULL
);
