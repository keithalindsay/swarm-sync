"""Typed rows for the blackboard tables. DESIGN.md §4.1.

Pydantic models mirroring the schema:
  Parcel, Lease, Contract, Pheromone, Intent, Event
Plus the request/response bodies used by the FastAPI endpoints (§4.2):
  LeaseRequest, LeaseResult, IntentBody, HeartbeatBody, ParcelUpdateBody, IntegrateBody

These are the wire contract between agent/client.py and server/app.py — keep them
in sync with schema.sql. Row models use `model_config = ConfigDict(from_attributes=True)`
so they can be built directly from a `sqlite3.Row` (which supports mapping-style
access) via `Model.model_validate(dict(row))`.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

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
    """One row of the append-only pheromone/audit log. Source of truth for replay."""

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
    ttl: Optional[float] = None
    # Opt-in whole-file parcel auto-creation for callers (the hook adapter) that
    # lease arbitrary real files rather than ids resolved from a real index.
    # See `server.leases._ensure_parcel`.
    ensure_parcel: bool = False


class LeaseResult(BaseModel):
    granted: bool
    lease_id: Optional[int] = None
    reason: Optional[str] = None


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
    ttl: Optional[float] = None


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
