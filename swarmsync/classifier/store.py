"""Write classifier output into the blackboard. DESIGN.md §3 (output), §4.2 (POST /index).

Built in Unit U4. This is the glue between the pure classifier (`indexer.py` +
`graph.py`, which only ever compute in-memory `Parcel`/`Contract` model instances)
and the blackboard DB (`blackboard/db.py`, the only module that owns the connection).
`server/app.py`'s `POST /index` endpoint (U7) is expected to call `run_index` here.

Core API:
  run_index(conn, root, threshold=FREEZE_THRESHOLD) -> IndexResult
    1. `index_repo(root)` -> parcels (U2).
    2. `build_graph(parcels, root)` -> graph, then `blast_radius(graph)` (U3).
    3. Write each parcel's computed `blast_radius` back onto the `Parcel` instance
       (U3 returns a separate dict; it does not mutate the parcels it's given).
    4. `extract_contracts(parcels, graph, blast, threshold)` -> contracts (U3), then
       stamp `Parcel.contract_hash` from `graph.signatures[id][1]` (the type_hash) for
       every parcel that made the contract cut, so a parcel row's `contract_hash` and
       the corresponding `contracts.type_hash` row always agree.
    5. `upsert_parcels` / `upsert_contracts`: idempotent `INSERT ... ON CONFLICT DO
       UPDATE`, single transaction each, keyed on `parcels.id` / `contracts.symbol` —
       re-running over an unchanged repo produces the same row count (no duplicates).

Design notes (this unit's own decisions):

- **`state_summary` is intentionally NOT touched by the upsert.** DESIGN §5.4 makes the
  *integrator* (U10) the sole authority that regenerates `state_summary` on merge — the
  classifier populating/re-populating the parcel map on `POST /index` has no opinion
  about an agent's live semantic note, so re-indexing must never clobber it. The column
  is simply omitted from `upsert_parcels`'s `SET` clause on conflict (left out of the
  INSERT'S UPDATE side entirely, not set to NULL) so any existing value survives.
- **Contract `version` bumps only when `type_hash` actually changes** on re-run (i.e. the
  frozen symbol's signature changed), otherwise the existing version is preserved. This
  is a plain SQL `CASE` in the upsert, not a Python read-modify-write, so it stays a
  single atomic statement per row and doesn't race with a concurrent reader. Note this is
  independent of (and simpler than) the *exclusive-lease-gated* `contract_change` event
  flow in DESIGN §5.3 — that's the lease manager's/broker's job (U5/U12) when an agent
  deliberately changes a frozen signature under a lease. This unit only keeps the
  `contracts` table's `version` column honest under repeated `POST /index` calls.
- **No stale-row pruning.** If a file/symbol disappears from the repo between two
  `run_index` calls, its old `parcels`/`contracts` rows are left in place rather than
  deleted. Deleting a parcel row is unsafe to do unconditionally once other units exist
  (`leases`/`pheromone` reference `parcels.id`, and `foreign_keys=ON` per `blackboard/db.py`
  would raise on a referenced row); reconciling that is left to a future unit (the
  integrator already owns "re-index the touched files" on land per DESIGN §5.4, which is
  the natural place to also retire genuinely-deleted parcels). Not exercised by, or
  required by, this unit's done-when.
- **Full rebuild every call**, matching the U3 handoff note: there is no incremental
  parcel-graph diffing here, only `indexer.index_repo`'s file walk is incremental in
  spirit. `run_index` always re-parses every `.py` file under `root`.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from swarmsync.blackboard import db as _db
from swarmsync.blackboard.models import Contract, Parcel
from swarmsync.classifier.graph import (
    FREEZE_THRESHOLD,
    DepGraph,
    blast_radius,
    build_graph,
    extract_contracts,
)
from swarmsync.classifier.indexer import index_repo

StrPath = Union[str, Path]


@dataclass
class IndexResult:
    """Everything one `run_index` call produced, for callers (e.g. the future
    `POST /index` handler, or tests) that want the in-memory objects too, not just
    the DB side effects."""

    parcels: list[Parcel]
    contracts: list[Contract]
    graph: DepGraph


def _parcel_params(p: Parcel) -> dict:
    return {
        "id": p.id,
        "path": p.path,
        "symbol": p.symbol,
        "kind": p.kind,
        "territory": p.territory,
        "blast_radius": p.blast_radius,
        "contract_hash": p.contract_hash,
        "content_hash": p.content_hash,
        "byte_start": p.byte_start,
        "byte_end": p.byte_end,
        "updated_at": p.updated_at,
    }


_UPSERT_PARCEL_SQL = """
INSERT INTO parcels
  (id, path, symbol, kind, territory, blast_radius, contract_hash, content_hash,
   byte_start, byte_end, updated_at)
VALUES
  (:id, :path, :symbol, :kind, :territory, :blast_radius, :contract_hash, :content_hash,
   :byte_start, :byte_end, :updated_at)
ON CONFLICT(id) DO UPDATE SET
  path=excluded.path,
  symbol=excluded.symbol,
  kind=excluded.kind,
  territory=excluded.territory,
  blast_radius=excluded.blast_radius,
  contract_hash=excluded.contract_hash,
  content_hash=excluded.content_hash,
  byte_start=excluded.byte_start,
  byte_end=excluded.byte_end,
  updated_at=excluded.updated_at
"""
# state_summary is deliberately absent from both the column list and the ON CONFLICT
# SET clause above -- see module docstring. A brand-new row still gets state_summary's
# schema default (NULL) since it's simply omitted from the INSERT's column list too.


def upsert_parcels(conn: sqlite3.Connection, parcels: list[Parcel]) -> None:
    """Idempotently write `parcels` into the `parcels` table, one transaction for
    the whole batch. Existing rows (matched by `id`) are updated in place; new ids
    are inserted. Never touches `state_summary`."""
    if not parcels:
        return
    # ONE IMMEDIATE transaction on THIS connection (db.transaction): a rollback
    # here can only ever undo this batch, never a concurrent single-statement
    # writer -- that writer lives on its own per-request connection (S4). See
    # db.transaction's docstring for why BEGIN IMMEDIATE + the no-nesting guard.
    with _db.transaction(conn):
        conn.executemany(_UPSERT_PARCEL_SQL, [_parcel_params(p) for p in parcels])


_UPSERT_CONTRACT_SQL = """
INSERT INTO contracts (symbol, signature, type_hash, frozen, version)
VALUES (:symbol, :signature, :type_hash, :frozen, :version)
ON CONFLICT(symbol) DO UPDATE SET
  signature=excluded.signature,
  type_hash=excluded.type_hash,
  frozen=excluded.frozen,
  version=CASE
    WHEN contracts.type_hash != excluded.type_hash THEN contracts.version + 1
    ELSE contracts.version
  END
"""


def upsert_contracts(conn: sqlite3.Connection, contracts: list[Contract]) -> None:
    """Idempotently write `contracts` into the `contracts` table, one transaction
    for the whole batch. A symbol whose `type_hash` changed since the last run has
    its `version` bumped; an unchanged symbol keeps its existing version."""
    if not contracts:
        return
    with _db.transaction(conn):
        conn.executemany(
            _UPSERT_CONTRACT_SQL,
            [
                {
                    "symbol": c.symbol,
                    "signature": c.signature,
                    "type_hash": c.type_hash,
                    "frozen": c.frozen,
                    "version": c.version,
                }
                for c in contracts
            ],
        )


def run_index(
    conn: sqlite3.Connection, root: StrPath, threshold: int = FREEZE_THRESHOLD
) -> IndexResult:
    """Run the full classifier pipeline over `root` and (re)populate the
    blackboard's `parcels` + `contracts` tables. DESIGN §3 output / §4.2 `POST /index`.

    Safe to call repeatedly against the same `conn`/`root`: rows are upserted keyed
    on `parcels.id` / `contracts.symbol`, so re-running over an unchanged repo leaves
    row counts unchanged (no duplicates) and only refreshes the mutable columns.
    """
    parcels = index_repo(root)
    graph = build_graph(parcels, root)
    blast = blast_radius(graph)

    for p in parcels:
        p.blast_radius = blast.get(p.id, 0)

    contracts = extract_contracts(parcels, graph, blast, threshold=threshold)
    contract_type_hash = {c.symbol: c.type_hash for c in contracts}
    for p in parcels:
        if p.id in contract_type_hash:
            p.contract_hash = contract_type_hash[p.id]

    upsert_parcels(conn, parcels)
    upsert_contracts(conn, contracts)

    return IndexResult(parcels=parcels, contracts=contracts, graph=graph)
