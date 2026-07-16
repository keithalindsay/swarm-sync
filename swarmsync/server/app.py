"""FastAPI app wiring the blackboard endpoints. DESIGN.md §4.2.

Built in Unit U7. This is pure wiring: every endpoint below is a thin HTTP shell
around a function some earlier unit already built and tested in isolation
(`classifier.store.run_index`, `server.leases.acquire/heartbeat/release`,
`server.events.emit/tail/drop_pheromone`). U7 adds no new coordination logic of
its own -- it only decides the request/response shapes and talks to the one
blackboard connection (`db.init_db`), preserving the single-writer invariant.

Endpoints (all JSON):
  POST /index                 -> run classifier over repo, populate parcels + contracts
  GET  /parcels               -> live parcel map (+ current active-lease status)
  GET  /leases                -> active (status='active', unexpired) leases
  POST /intent                -> declare intent, drop 'planned' pheromone + event
  POST /lease                 -> atomic CAS acquire (server/leases.acquire)
  POST /heartbeat             -> bump lease TTL
  POST /release               -> release a lease
  GET  /contract/{symbol}     -> frozen signature + version (404 if unknown)
  POST /parcel/update         -> agent posts new content_hash + state_summary on done
  GET  /events?since={seq}    -> tail the event log
  POST /integrate             -> submit branch to the serial integrator (U10)

`POST /integrate` (U10) calls straight into `coordinator.integrator.integrate`
-- merge `body.branch` into `body.into` (default `"integration"`) behind the
impact-selected pytest gate, land + re-index + regenerate `state_summary` on
green, reject + roll back on a conflict or a red test run. Not internally
locked (see `integrator.py`'s own docstring): whatever submits branches (the
broker, U12) must call this one branch at a time itself.

DESIGN §4.2's "Background (startup): reaper + pheromone decay run as asyncio
tasks" is now wired (U11): `create_app`'s lifespan starts
`coordinator.reaper.run(conn, interval=reaper_interval, half_life=
pheromone_half_life)` as an `asyncio.create_task` right after startup and
cancels + awaits it on shutdown. Pass `reaper_interval=None` to `create_app`
to disable the background loop entirely (e.g. a test that wants a fully quiet
blackboard and drives `reaper.reap_once`/`decay_once` itself instead).

`create_app(db_path)` is the testable factory (mirrors `blackboard.db.init_db`'s
own "pass a path, get a fresh handle" shape): it opens/inits the blackboard at
`db_path` immediately (so a caller can hit endpoints on the returned app without
needing to enter it as an ASGI lifespan context first) and registers a lifespan
that closes that connection on shutdown for callers that do use
`with TestClient(app) as client: ...` or run it under uvicorn.

`main()` launches uvicorn (referenced by the `swarm-sync` console script),
building a fresh app rooted at `$SWARM_SYNC_DB` (default `blackboard.db` in the
current directory) -- deliberately NOT a module-level `app = create_app()`,
since that would create/open a DB file as a side effect of merely importing
this module (e.g. from a test).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from swarmsync.blackboard import db
from swarmsync.blackboard.models import (
    Contract,
    Event,
    HeartbeatBody,
    IntegrateBody,
    IntentBody,
    LeaseRequest,
    LeaseResult,
    ParcelUpdateBody,
)
from swarmsync.classifier.graph import FREEZE_THRESHOLD
from swarmsync.classifier.store import run_index
from swarmsync.coordinator import integrator
from swarmsync.coordinator import reaper as reaper_mod
from swarmsync.server import events as events_mod
from swarmsync.server import leases as leases_mod

StrPath = Union[str, Path]


# --- request bodies not already defined in blackboard.models ----------------------
# (IntentBody/LeaseRequest/LeaseResult/HeartbeatBody/ParcelUpdateBody/IntegrateBody
# already live in blackboard.models per U1 -- these two are new for this unit and
# follow the exact same "flat body matching the endpoint's DESIGN §4.2 row" shape.)


class IndexBody(BaseModel):
    root: str
    threshold: int = FREEZE_THRESHOLD


class ReleaseBody(BaseModel):
    agent_id: str
    lease_id: int


def get_conn(request: Request):
    """FastAPI dependency: the one shared blackboard connection for this app."""
    return request.app.state.conn


def create_app(
    db_path: StrPath = "blackboard.db",
    reaper_interval: Optional[float] = reaper_mod.DEFAULT_INTERVAL,
    pheromone_half_life: float = reaper_mod.DEFAULT_HALF_LIFE,
) -> FastAPI:
    """Build a fresh FastAPI app wired to the blackboard at `db_path`.

    Opens/initializes the DB immediately (not deferred to lifespan startup) so
    the returned app is immediately usable via `TestClient(app)` without
    requiring the `with` context-manager form. A lifespan is still registered
    so `conn` is closed cleanly on shutdown for callers that do use it (or run
    the app under uvicorn) -- and, per DESIGN §4.2's "Background (startup)"
    note, that lifespan is also where the reaper + pheromone-decay loop
    (U11, `coordinator.reaper.run`) is started as a background `asyncio` task
    and cleanly cancelled on shutdown.

    `reaper_interval=None` disables the background loop entirely (no task is
    created) -- useful for a test that wants full manual control over when
    reap/decay passes happen instead of a real-time loop racing the test body.
    """
    conn = db.init_db(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if reaper_interval is not None:
            task = asyncio.create_task(
                reaper_mod.run(
                    conn, interval=reaper_interval, half_life=pheromone_half_life
                )
            )
        yield
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        conn.close()

    app = FastAPI(title="swarm-sync blackboard", lifespan=lifespan)
    app.state.conn = conn

    # --- POST /index ---------------------------------------------------------

    @app.post("/index")
    def post_index(body: IndexBody, conn=Depends(get_conn)):
        result = run_index(conn, body.root, threshold=body.threshold)
        return {
            "root": body.root,
            "parcels": len(result.parcels),
            "contracts": len(result.contracts),
        }

    # --- GET /parcels ----------------------------------------------------------

    @app.get("/parcels")
    def get_parcels(conn=Depends(get_conn)):
        now = time.time()
        parcel_rows = conn.execute("SELECT * FROM parcels ORDER BY id").fetchall()
        lease_rows = conn.execute(
            "SELECT * FROM leases WHERE status = 'active' AND ttl_expires_at > ?",
            (now,),
        ).fetchall()

        leases_by_parcel: dict[str, list[dict]] = {}
        for lr in lease_rows:
            leases_by_parcel.setdefault(lr["parcel_id"], []).append(
                {"lease_id": lr["id"], "agent_id": lr["agent_id"], "mode": lr["mode"]}
            )

        return [
            {**dict(row), "active_leases": leases_by_parcel.get(row["id"], [])}
            for row in parcel_rows
        ]

    # --- GET /leases -------------------------------------------------------------

    @app.get("/leases")
    def get_leases(conn=Depends(get_conn)):
        now = time.time()
        rows = conn.execute(
            "SELECT * FROM leases WHERE status = 'active' AND ttl_expires_at > ? "
            "ORDER BY id",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- POST /intent --------------------------------------------------------

    @app.post("/intent")
    def post_intent(body: IntentBody, conn=Depends(get_conn)):
        now = time.time()
        conn.execute(
            """
            INSERT INTO intents (agent_id, task, target_parcels, declared_at)
            VALUES (:agent_id, :task, :target_parcels, :now)
            ON CONFLICT(agent_id, task) DO UPDATE SET
              target_parcels = excluded.target_parcels,
              declared_at = excluded.declared_at
            """,
            {
                "agent_id": body.agent_id,
                "task": body.task,
                "target_parcels": json.dumps(body.target_parcels),
                "now": now,
            },
        )
        # DESIGN §4.3 step 2: declaring intent drops a 'planned' pheromone on each
        # target parcel (a dedup hint for other agents) plus one `planned` event.
        for parcel_id in body.target_parcels:
            events_mod.drop_pheromone(
                conn, parcel_id, body.agent_id, "planned", 1.0, ts=now
            )
        seq = events_mod.emit(
            conn,
            "planned",
            body.agent_id,
            {"task": body.task, "target_parcels": body.target_parcels},
            ts=now,
        )
        return {
            "agent_id": body.agent_id,
            "task": body.task,
            "target_parcels": body.target_parcels,
            "declared_at": now,
            "event_seq": seq,
        }

    # --- POST /lease -----------------------------------------------------------

    @app.post("/lease", response_model=LeaseResult)
    def post_lease(body: LeaseRequest, conn=Depends(get_conn)):
        kwargs: dict = {"mode": body.mode, "intent": body.intent}
        if body.ttl is not None:
            kwargs["ttl"] = body.ttl
        return leases_mod.acquire(conn, body.parcel_id, body.agent_id, **kwargs)

    # --- POST /heartbeat ---------------------------------------------------------

    @app.post("/heartbeat")
    def post_heartbeat(body: HeartbeatBody, conn=Depends(get_conn)):
        ok = leases_mod.heartbeat(conn, body.lease_id, body.agent_id)
        return {"ok": ok}

    # --- POST /release -----------------------------------------------------------

    @app.post("/release")
    def post_release(body: ReleaseBody, conn=Depends(get_conn)):
        ok = leases_mod.release(conn, body.lease_id, body.agent_id)
        return {"ok": ok}

    # --- GET /contract/{symbol} --------------------------------------------------

    @app.get("/contract/{symbol:path}", response_model=Contract)
    def get_contract(symbol: str, conn=Depends(get_conn)):
        row = conn.execute(
            "SELECT * FROM contracts WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"no contract for symbol {symbol!r}"
            )
        return Contract.model_validate(dict(row))

    # --- POST /parcel/update -------------------------------------------------------

    @app.post("/parcel/update")
    def post_parcel_update(body: ParcelUpdateBody, conn=Depends(get_conn)):
        now = time.time()
        cur = conn.execute(
            """
            UPDATE parcels
            SET content_hash = :content_hash,
                state_summary = COALESCE(:state_summary, state_summary),
                updated_at = :now
            WHERE id = :parcel_id
            """,
            {
                "content_hash": body.content_hash,
                "state_summary": body.state_summary,
                "now": now,
                "parcel_id": body.parcel_id,
            },
        )
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=404, detail=f"no parcel {body.parcel_id!r}"
            )
        # DESIGN §4.1 schema comment: pheromone.kind is planned|touched|done -- this
        # is literally the 'done' signal (DESIGN §4.3 step 6), so drop it here.
        events_mod.drop_pheromone(
            conn, body.parcel_id, body.agent_id, "done", 1.0, ts=now
        )
        seq = events_mod.emit(
            conn,
            "done",
            body.agent_id,
            {
                "parcel_id": body.parcel_id,
                "content_hash": body.content_hash,
                "state_summary": body.state_summary,
            },
            ts=now,
        )
        return {"ok": True, "parcel_id": body.parcel_id, "event_seq": seq}

    # --- GET /events?since= --------------------------------------------------------

    @app.get("/events", response_model=list[Event])
    def get_events(since: int = 0, limit: int = 1000, conn=Depends(get_conn)):
        return events_mod.tail(conn, since_seq=since, limit=limit)

    # --- POST /integrate -----------------------------------------------------------

    @app.post("/integrate")
    def post_integrate(body: IntegrateBody, conn=Depends(get_conn)):
        # U10: the serial test-gated integrator. Not internally locked (see
        # coordinator/integrator.py's own docstring) -- callers of this endpoint
        # (the broker, U12) are responsible for submitting one branch at a time;
        # this single shared blackboard connection + a synchronous handler is
        # what makes "one merge in flight at a time" hold for the prototype.
        result = integrator.integrate(
            conn,
            body.repo,
            body.branch,
            base_commit=body.base_commit,
            into=body.into,
            agent_id=body.agent_id,
        )
        return dataclasses.asdict(result)

    return app


def main() -> None:
    import uvicorn

    app = create_app(os.environ.get("SWARM_SYNC_DB", "blackboard.db"))
    uvicorn.run(app, host="0.0.0.0", port=8000)
