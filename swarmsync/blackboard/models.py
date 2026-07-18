"""Typed rows for the blackboard tables. DESIGN.md §4.1.

Pydantic models mirroring the schema:
  Parcel, Lease, Contract, Pheromone, Intent, Event
Plus the request/response bodies used by the FastAPI endpoints (§4.2):
  LeaseRequest, LeaseResult, IntentBody, HeartbeatBody, ParcelUpdateBody, IntegrateBody
  ParcelLeaseInfo, ParcelWithLeases (the `GET /parcels` row: parcel columns +
  the active-lease join -- WP4.5's declaration of the previously implicit shape)

These are the wire contract between agent/client.py and server/app.py — keep them
in sync with schema.sql. Row models use `model_config = ConfigDict(from_attributes=True)`
so they can be built directly from a `sqlite3.Row` (which supports mapping-style
access) via `Model.model_validate(dict(row))`.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from swarmsync.blackboard.db import BUSY_TIMEOUT_SECONDS

# --- TTL bounds (C9) ---------------------------------------------------------------
# A lease TTL must be STRICTLY positive. A `ttl <= 0` makes
# `ttl_expires_at = now + ttl` land in the past, so the lease is granted AND already
# expired: `server.leases.acquire`'s CAS predicate (`ttl_expires_at > now`) treats the
# born-dead row as non-blocking, so a SECOND agent is ALSO immediately granted -- two
# writers on one parcel, both told they hold the lock. The ceiling guards the
# symmetric hazard: a huge TTL is an effectively permanent lease the reaper never
# fires on. Enforced as pydantic field constraints on the wire bodies below, so
# `POST /lease {"ttl": 0}` is a 422 rather than a silent double-grant.
LEASE_TTL_MAX_SECONDS = 86400.0  # 24h

# Defense-in-depth floor (C13): a TTL should comfortably exceed the SQLite
# `busy_timeout` (see blackboard/db.py) -- >= 2x it -- so the heartbeat liveness
# predicate can never be raced by a lock wait that shrinks the live window toward
# request latency. DERIVED from the busy_timeout constant rather than hardcoded, so
# retuning the timeout cannot silently strand this floor at a stale multiple. NOT
# enforced on the wire: the hook's own keepalive tests deliberately use sub-second
# TTLs to exercise renewal quickly, so a hard floor would be a false constraint
# here. Callers that can (the hook adapter) warn below it.
LEASE_TTL_FLOOR_SECONDS = 2 * BUSY_TIMEOUT_SECONDS

# --- literals mirroring the CHECK-by-convention columns in schema.sql -------------

LeaseMode = Literal["read", "write", "exclusive"]
LeaseStatus = Literal["active", "released", "reaped"]
ParcelKind = Literal["function", "method", "class", "module"]
PheromoneKind = Literal["planned", "touched", "done"]
EventType = Literal[
    "planned",
    "lease_granted",
    "lease_denied",
    "heartbeat",
    "done",
    "released",
    "reaped",
    "contract_change",
    "merged",
    "merge_rejected",
    "reindexed",
    "needs_rebase",  # U10: optimistic re-check (DESIGN §5.5) found a stale read-dep
    # R5: `integrate` mutates trunk BEFORE it knows the verdict (merge, then gate,
    # then keep-or-reset). That window was in-memory only, so a crash inside it left
    # an un-gated merge on trunk with nothing recording that it was ever provisional.
    # `integrate_started` is written before the merge and carries the sha to roll back
    # to; a start with no terminal event is an orphan, which startup reconciliation
    # resets out and records as `integrate_orphaned`.
    "integrate_started",
    "integrate_orphaned",
    # WP3.5 (C14): the integrator's post-land re-index retires `parcels` rows whose
    # file a landed merge deleted/renamed (`parcel_retired`, why=file_deleted) and
    # `contracts` rows whose symbol no longer exists (`contract_retired`,
    # why=symbol_deleted). Dependents observe these instead of polling a ghost row
    # that would otherwise never change again.
    "parcel_retired",
    "contract_retired",
    # WP3.1 (S2): one marker per non-empty compaction pass, carrying the pruned
    # count + seq range (`server.events.compact_events`). Registered here so the
    # maintenance row is a first-class citizen of the log; `GET /events` keeps its
    # widened `EventOut` anyway as defense against future registry-external rows.
    "events_compacted",
]


# --- row models, one per table in schema.sql --------------------------------------


class Parcel(BaseModel):
    """A leasable unit: a top-level def/async def/class/method, or a module
    interstitial bucket for code not inside a named symbol."""

    model_config = ConfigDict(from_attributes=True)

    id: str  # "path::qualified.name" or "path::<module>"
    path: str
    symbol: Optional[str] = None
    kind: Optional[ParcelKind] = None
    territory: Optional[int] = None
    blast_radius: int = 0
    contract_hash: Optional[str] = None
    content_hash: Optional[str] = None
    byte_start: Optional[int] = None
    byte_end: Optional[int] = None
    state_summary: Optional[str] = None
    updated_at: float


class Lease(BaseModel):
    """A CAS-acquired hold on a parcel. DESIGN.md §5.2."""

    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None  # autoincrement; None before insert
    parcel_id: str
    agent_id: str
    mode: LeaseMode
    acquired_at: float
    ttl_expires_at: float
    heartbeat_at: float
    intent: Optional[str] = None
    status: LeaseStatus


class ParcelLeaseInfo(BaseModel):
    """One active lease as embedded in a `GET /parcels` row's `active_leases`
    (WP4.5, A6). NOT a full `Lease`: the endpoint deliberately projects just the
    identity triple a reader needs to see who holds a parcel -- and the lease's
    row id is exposed under the key `lease_id` (not `id`), matching the wire
    shape the hook adapter already duck-types against. Field names here ARE the
    wire contract; renaming one is a breaking API change."""

    lease_id: int
    agent_id: str
    mode: LeaseMode


class ParcelWithLeases(Parcel):
    """`GET /parcels` response row (WP4.5, A6): every `parcels` column exactly
    as in `Parcel`, plus the endpoint's `active_leases` join -- the currently
    active, unexpired leases on this parcel. Declares the shape the endpoint has
    always returned; the JSON is unchanged."""

    active_leases: list[ParcelLeaseInfo] = Field(default_factory=list)


class Contract(BaseModel):
    """A frozen interface surface. DESIGN.md §3 step 5."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    signature: str
    type_hash: str
    frozen: int = 1
    version: int = 1


class Pheromone(BaseModel):
    """A decaying signal of activity on a parcel. DESIGN.md §4.1."""

    model_config = ConfigDict(from_attributes=True)

    parcel_id: str
    agent_id: str
    kind: PheromoneKind
    strength: float
    updated_at: float


class Intent(BaseModel):
    """An agent's declared plan to touch a set of parcels for a task."""

    model_config = ConfigDict(from_attributes=True)

    agent_id: str
    task: str
    target_parcels: str  # JSON array of parcel ids, stored as TEXT
    declared_at: float


class Event(BaseModel):
    """One row of the append-only pheromone/audit log. Audit and observability --
    NOT a replay source of truth: the SQLite tables are the state of record, and
    crash recovery reads the `open_integrations` projection (see events.py's
    honesty note and DESIGN §4.1)."""

    model_config = ConfigDict(from_attributes=True)

    seq: Optional[int] = None  # autoincrement; None before insert
    agent_id: Optional[str] = None
    type: EventType
    payload: Optional[str] = None  # JSON
    ts: float


# --- endpoint request/response bodies (DESIGN.md §4.2) ----------------------------


class LeaseRequest(BaseModel):
    agent_id: str
    parcel_id: str
    mode: LeaseMode = "write"
    intent: Optional[str] = None
    # C9: a TTL, when supplied, must be strictly positive and within the ceiling.
    # None means "use the server's default window" (server.leases.DEFAULT_TTL_SECONDS).
    ttl: Optional[float] = Field(default=None, gt=0, le=LEASE_TTL_MAX_SECONDS)
    # Opt-in whole-file parcel auto-creation for callers (the hook adapter) that
    # lease arbitrary real files rather than ids resolved from a real index.
    # See `server.leases._ensure_parcel`.
    ensure_parcel: bool = False


class LeaseResult(BaseModel):
    granted: bool
    lease_id: Optional[int] = None
    reason: Optional[str] = None
    # On a DENY, the identity + expiry of the conflicting active lease that best
    # explains the denial, so a caller (the hook adapter) can name the holder and say
    # when it frees WITHOUT a second /leases round-trip. Both stay None on the granted
    # path and on any deny where no single holder is identifiable (e.g. a race in which
    # the blocker released between the CAS and the lookup). See server.leases.acquire.
    holder: Optional[str] = None  # conflicting write/exclusive (or blocking read) agent_id
    holder_ttl_expires_at: Optional[float] = None  # that lease's ttl_expires_at (epoch s)


class IntentBody(BaseModel):
    agent_id: str
    task: str
    target_parcels: list[str]


class HeartbeatBody(BaseModel):
    agent_id: str
    lease_id: int
    # Optional TTL (seconds) to renew with; None -> the server's default window.
    # The hook keepalive (S5) sends its own long TTL so a renewed lease keeps the
    # long window instead of collapsing back to the short server default.
    # C9: same bounds as LeaseRequest -- a renewal with ttl <= 0 would push the lease
    # into the past and revive the double-lease `heartbeat`'s liveness guard prevents.
    ttl: Optional[float] = Field(default=None, gt=0, le=LEASE_TTL_MAX_SECONDS)


class ParcelUpdateBody(BaseModel):
    agent_id: str
    parcel_id: str
    content_hash: str
    state_summary: Optional[str] = None


class IntegrateBody(BaseModel):
    agent_id: str
    branch: str
    repo: str  # filesystem path to the git repo `branch` lives in (U10 needs it to merge)
    base_commit: Optional[str] = None
    into: str = "integration"
    # WP4.6 (A1): the submitting agent's plan-time read-dependency snapshot,
    # `{parcel_or_contract_id: expected_hash}`, forwarded verbatim to
    # `coordinator.integrator.integrate(expected_read_deps=...)` (DESIGN §5.5).
    # The integrator compares each id against the blackboard's CURRENT
    # `parcels.content_hash` (parcels are checked first) or, for an id with no
    # parcels row, `contracts.type_hash`; any mismatch means a read-dependency
    # shifted between plan and submit -> the verdict is `needs_rebase` and NO
    # merge is attempted. Optional and opt-in: omitted/None skips the check
    # entirely (every pre-WP4.6 caller's wire behavior, unchanged).
    expected_read_deps: Optional[dict[str, str]] = None
