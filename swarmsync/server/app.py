"""FastAPI app wiring the blackboard endpoints. DESIGN.md §4.2.

Built in Unit U7. This is pure wiring: every endpoint below is a thin HTTP shell
around a function some earlier unit already built and tested in isolation
(`classifier.store.run_index`, `server.leases.acquire/heartbeat/release`,
`server.events.emit/tail/drop_pheromone`). U7 adds no new coordination logic of
its own -- it only decides the request/response shapes and talks to the
blackboard. S4 connection model: schema is bootstrapped once (`db.init_db`), but
each request handler runs on its OWN per-request connection (`get_conn`, via
`db.connect`) so WAL delivers real concurrent readers + a single writer instead
of every request serializing on one shared handle; the background reaper holds a
separate dedicated connection. `app.state.conn` survives only as an out-of-band
inspection handle for tests/tools (and the broker's single-writer entry point).

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
  GET  /events?since={seq}    -> tail the event log (or ?tail={n}: newest n, asc)
  POST /integrate             -> submit branch to the serial integrator (U10)

`POST /integrate` (U10) calls straight into `coordinator.integrator.integrate`
-- merge `body.branch` into `body.into` (default `"integration"`) behind the
impact-selected pytest gate, land + re-index + regenerate `state_summary` on
green, reject + roll back on a conflict or a red test run. S1 hardening: the
endpoint now serializes itself behind a process-wide `asyncio.Lock`
(`app.state.integrate_lock`) and runs the blocking merge in a threadpool, so
at most ONE merge touches the shared `into` checkout at a time even when many
clients POST /integrate concurrently -- callers no longer have to serialize
externally (the broker's own lock, U12, is now belt-and-suspenders).

The background reaper + pheromone-decay loop (U11) is wired into the app's
lifespan: `create_app`'s lifespan starts `coordinator.reaper.run(conn,
interval=reaper_interval, half_life=pheromone_half_life)` as an
`asyncio.create_task` right after startup; on shutdown it signals the reaper's
stop event and AWAITS the task (WP3.1 P2 -- so an in-flight pass finishes before
its connection closes; cancel is only the past-timeout fallback).
Pass `reaper_interval=None` to `create_app` to disable the background loop
entirely (e.g. a test that wants a fully quiet blackboard and drives
`reaper.reap_once`/`decay_once` itself instead).

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

import argparse
import asyncio
import dataclasses
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from swarmsync.blackboard import db
from swarmsync.blackboard.models import (
    Contract,
    Event,
    HeartbeatBody,
    IntegrateBody,
    IntentBody,
    Lease,
    LeaseRequest,
    LeaseResult,
    ParcelUpdateBody,
    ParcelWithLeases,
)
from swarmsync.classifier.graph import FREEZE_THRESHOLD
from swarmsync.classifier.indexer import IndexLimitError
from swarmsync.classifier.store import run_index
from swarmsync.coordinator import integrator
from swarmsync.coordinator import reaper as reaper_mod
from swarmsync.blackboard import events as events_mod
from swarmsync.blackboard import leases as leases_mod

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


class EventOut(Event):
    """`GET /events` response row. Identical to `blackboard.models.Event` except
    `type` is widened to `str`: the log legitimately contains maintenance rows
    (`events.EVENTS_COMPACTED`) whose type is outside the frozen EventType
    registry (models.py is owned by a parallel work package -- see
    `server.events.EVENTS_COMPACTED`), and FastAPI re-validates the response
    against this model, so the Literal would 500 any page containing one."""

    type: str  # type: ignore[assignment]  # deliberate widening of the Literal


# --- WP3.1 (finding S2): GET /events?limit= clamp ---------------------------------
# The endpoint previously accepted ANY limit -- `?limit=999999999` materialized the
# whole (unboundedly growing) events table into memory and one JSON response, and a
# NEGATIVE limit did too (SQLite treats `LIMIT -1` as "no limit"). Out-of-range
# values are now a 422 validation error, NOT a silent clamp: a caller that asked
# for a million rows and silently got 1000 would believe it saw everything below
# `since + 1_000_000` and skip ahead, silently dropping events; a loud 422 matches
# how every other malformed field here already fails (e.g. LeaseRequest.ttl) and
# tells the caller to page with `since` instead. Default stays 1000, unchanged.

MAX_EVENTS_LIMIT = 1000

# --- WP3.1 (P2): graceful-reaper-shutdown ceiling, in seconds ---------------------
# How long lifespan shutdown waits for the reaper to finish its in-flight pass and
# exit after the stop event is set, before falling back to task cancellation. A
# pass is a handful of SQLite statements whose worst case is a few 5s busy-timeout
# waits, so 30s is generous; past it the worker is presumed wedged and cancel is
# the least-bad option.
REAPER_SHUTDOWN_TIMEOUT = 30.0


# --- WP3.3 (finding S5): request body-size cap -----------------------------------
# Nothing bounded request bodies before uvicorn/Starlette buffer them: a client that
# clears the token gate (or any client, when no token is set) could POST an arbitrarily
# large body and have it read fully into memory before validation rejects it. The cap
# is generous (10 MB default -- every legitimate body here is a small JSON document)
# and env-tunable without a code change.

DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_BODY_BYTES_ENV = "SWARMSYNC_MAX_BODY_BYTES"


def _max_body_bytes() -> int:
    """Body cap, read from the env per request (test-friendly); unset/garbage
    values fall back to the generous default."""
    raw = os.environ.get(MAX_BODY_BYTES_ENV)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_BODY_BYTES


def get_conn(request: Request):
    """FastAPI dependency: a fresh blackboard connection scoped to THIS request.

    S4 connection model. Previously every request handler shared one process-wide
    `app.state.conn`, which serialized all DB work on a single SQLite handle
    (throwing away WAL's concurrent readers) and, worse, let any single-statement
    writer get folded into -- and rolled back with -- whatever explicit
    transaction another handler happened to have open on that shared connection
    (SQLite has exactly one transaction per connection). Now each request opens its
    OWN connection and closes it on the way out (the canonical FastAPI + SQLite
    per-request pattern). Concurrent requests therefore run on independent
    connections: WAL delivers real reader concurrency + a single writer, and
    `store.run_index`'s BEGIN/COMMIT on one request's connection can never swallow
    another request's writer.

    A generator dependency: the connection is created here, yielded to the handler,
    and closed in the `finally` even on error. FastAPI runs a sync generator
    dependency's setup/teardown around the handler *sequentially* (never
    concurrently with the handler), so the same connection safely moves between
    threadpool workers within one request -- `connect()` uses
    `check_same_thread=False` and SQLite's serialized threading mode makes that
    sequential hand-off safe. It is never touched by two threads at once.
    """
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


# --- S3 security: token auth on mutating routes ----------------------------------
# The token gates WHO may reach the blackboard at all; it is the trust boundary.
# `agent_id` stays the in-session coordination identity and is NOT derived from the
# token (see DESIGN §4.3 -- agents coordinate by agent_id, the operator gates access
# by token). When `SWARMSYNC_TOKEN` is UNSET there is no auth: dev/test/demo and every
# pre-S3 test keep working with no Authorization header. When SET, every *mutating*
# (POST) route requires `Authorization: Bearer <token>`, compared in constant time
# with `hmac.compare_digest`. Read-only GET routes stay open (they mutate nothing).


def require_token(request: Request) -> None:
    """FastAPI dependency guarding a mutating route. No-op when SWARMSYNC_TOKEN is
    unset; otherwise requires a matching `Authorization: Bearer <token>` header."""
    token = os.environ.get("SWARMSYNC_TOKEN")
    if not token:
        return
    header = request.headers.get("Authorization", "")
    scheme, _, provided = header.partition(" ")
    # Constant-time compare so a wrong token leaks no timing signal about the secret.
    if scheme != "Bearer" or not hmac.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


# --- S3 security: managed-root allow-list for filesystem paths -------------------
# `POST /index` (root) and `POST /integrate` (repo) take a caller-supplied filesystem
# path. Left unchecked, a client could point the classifier walk / git merge at any
# path on the host (or escape a symlink). We realpath the path (resolving symlinks)
# and require it to live under one of the managed roots: `SWARMSYNC_ROOTS`
# (os.pathsep-separated), defaulting to the server's launch cwd.


class MultiRootError(ValueError):
    """Raised at startup when more than one managed root is configured."""


def _managed_roots() -> list[str]:
    raw = os.environ.get("SWARMSYNC_ROOTS")
    if raw:
        candidates = [r for r in raw.split(os.pathsep) if r]
    else:
        candidates = [os.getcwd()]
    return [os.path.realpath(r) for r in candidates]


def check_single_root() -> str:
    """One server coordinates ONE repo. Enforce that, loudly, at startup.

    Parcel ids are `<relpath>::<symbol>` -- RELATIVE to the indexed root, with no repo
    qualifier anywhere in the id or the schema. So two roots that both contain (say)
    `utils.py` produce the SAME id `utils.py::helper` for two different files:
    `upsert_parcels` overwrites one repo's rows with the other's, a write lease on that
    id locks BOTH repos' files, and `integrate`'s re-index clobbers the other root's
    parcels wholesale. Silently, in every case.

    Multi-root is therefore not a supported mode -- it is data corruption with a
    plural-looking config. `SWARMSYNC_ROOTS` is an allow-list bounding which paths the
    one coordinated repo may touch, not a way to coordinate several. Fixing it properly
    means putting the root in the parcel identity -- a schema change, and the migration
    policy is refuse+rotate (`db.SCHEMA_VERSION`, WP3.4: a version-bumped DB refuses to
    open with `--fresh` named as the remedy; there is deliberately no in-place migration
    framework) -- so until someone wants that, refusing to start is the honest
    behaviour: a config that cannot work should not appear to.

    Returns the single managed root. Raises MultiRootError otherwise.
    """
    roots = _managed_roots()
    if len(roots) > 1:
        raise MultiRootError(
            "swarm-sync coordinates ONE repo per server, but "
            f"{len(roots)} managed roots are configured: {os.pathsep.join(roots)}.\n"
            "Parcel ids are relative to the root and carry no repo qualifier, so two "
            "roots sharing a filename (utils.py, __init__.py, conftest.py -- i.e. "
            "always) collide on the same parcel id: their rows overwrite each other "
            "and a lease on one repo's file locks the other's.\n"
            "Set SWARMSYNC_ROOTS (or --root) to exactly one repo, and run a second "
            "server on another port for a second repo."
        )
    return roots[0]


def _validate_managed_path(path: str) -> str:
    """Realpath `path` and require it under a managed root; return the realpath.

    Rejects both plainly-outside paths and symlink escapes (the symlink target is
    what `realpath` resolves to, so a link that points outside is caught here)."""
    real = os.path.realpath(path)
    for root in _managed_roots():
        if real == root or real.startswith(root + os.sep):
            return real
    raise HTTPException(
        status_code=403,
        detail=(
            f"path {path!r} resolves outside the managed roots (set SWARMSYNC_ROOTS "
            "to the directories the server is allowed to index/merge)"
        ),
    )


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
    the app under uvicorn) -- and that lifespan is also where the reaper +
    pheromone-decay loop (U11, `coordinator.reaper.run`) is started as a
    background `asyncio` task and cleanly stopped (stop event + await, WP3.1 P2)
    on shutdown.

    `reaper_interval=None` disables the background loop entirely (no task is
    created) -- useful for a test that wants full manual control over when
    reap/decay passes happen instead of a real-time loop racing the test body.
    """
    db_path = str(db_path)
    # One-time schema bootstrap. The returned connection is kept ONLY as a
    # persistent, out-of-band inspection handle (tests read `app.state.conn`
    # directly; the broker, U12, still drives its own ThreadPoolExecutor against a
    # single caller-supplied connection by design). Request handlers do NOT use it
    # -- they each open their own per-request connection via `get_conn` (S4).
    conn = db.init_db(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Refuse to serve a multi-root config at all. Checked here (not in create_app)
        # so it is evaluated when the server actually starts, against the environment
        # it will actually run under -- and so tests/tools that build an app object
        # without serving it are unaffected. See `check_single_root`.
        managed_root = check_single_root()

        # U8/WP3.4: bind this DB to its repo. Parcel ids are root-relative, so
        # reusing a DB file against a DIFFERENT root silently mixes two repos'
        # parcel maps (rows overwrite, leases conflate). First boot stores the
        # root in `meta`; a later boot with another root refuses to start --
        # `ManagedRootMismatchError` names both roots and the remedies
        # (`swarmsync-serve --fresh`, or point the server back at the original).
        # Same loud-refusal posture as MultiRootError above. Runs on its OWN
        # short-lived connection, not `app.state.conn`: the bind gates the DB
        # FILE, and pre-existing tests legitimately re-enter one app object
        # whose previous lifespan teardown already closed the shared handle
        # (reconciliation below tolerates that; a startup gate must too --
        # loudly for a wrong root, never for a recycled app object).
        bind_conn = db.connect(db_path)
        try:
            db.bind_managed_root(bind_conn, managed_root)
        finally:
            bind_conn.close()

        # BEFORE serving anything: roll trunk back out of any integrate that died
        # mid-flight. `integrate` merges to trunk before it knows the verdict, and a
        # SIGKILL/OOM in that window (up to the 600s gate ceiling) cannot be caught in
        # process -- it leaves an UN-GATED merge on trunk with no event and no
        # rollback, silently falsifying "trunk is always test-green" forever. This is
        # the only thing that can detect it, and it must run before a new integrate can
        # build on the poisoned trunk. Never raises; a repo that moved or vanished must
        # not stop the server booting.
        try:
            for record in integrator.reconcile_orphaned_integrations(conn):
                print(
                    f"swarm-sync: reconciled orphaned integrate "
                    f"{record['branch']!r} -> {record['into']!r} in {record['repo']!r}: "
                    f"{record['action']}"
                    + (f" (error: {record['error']})" if record["error"] else ""),
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001 -- startup must never be blocked
            print(f"swarm-sync: startup reconciliation failed: {exc!r}", flush=True)

        task = None
        reaper_conn = None
        reaper_stop: Optional[asyncio.Event] = None
        if reaper_interval is not None:
            # The reaper is a long-lived background task on the event loop thread;
            # give it its OWN connection so it never shares a handle with a request
            # handler or the inspection connection. WAL lets its periodic writes
            # (reaped rows + events) run concurrently with reader requests.
            reaper_conn = db.connect(db_path)
            reaper_stop = asyncio.Event()
            task = asyncio.create_task(
                reaper_mod.run(
                    reaper_conn,
                    interval=reaper_interval,
                    half_life=pheromone_half_life,
                    stop=reaper_stop,
                )
            )
        yield
        if task is not None and reaper_stop is not None:
            # WP3.1 P2: deterministic shutdown, NOT task.cancel(). Cancelling only
            # interrupts the AWAIT on `asyncio.to_thread`, never the worker thread
            # inside it -- run() would return while `reap_once`/`decay_once` was
            # still executing on `reaper_conn`, and the close() below would yank the
            # connection out from under that thread (`ProgrammingError: Cannot
            # operate on a closed database` into a discarded future; closing a
            # handle mid-`sqlite3_step` is documented SQLite misuse and has
            # segfaulted the test suite). Instead: signal the stop event, then WAIT
            # for run() to finish its in-flight pass and exit cleanly -- only after
            # that do the connections close. The timeout is generous (worst-case
            # pass is a few 5s busy_timeout waits, not 30s); cancellation survives
            # only as the past-timeout last resort for a wedged worker.
            reaper_stop.set()
            try:
                await asyncio.wait_for(task, timeout=REAPER_SHUTDOWN_TIMEOUT)
            except asyncio.TimeoutError:
                # wait_for already cancelled the task and awaited that cancellation;
                # nothing more can be done safely -- log and fall through to close.
                print(
                    "swarm-sync: reaper did not stop within "
                    f"{REAPER_SHUTDOWN_TIMEOUT}s; cancelled",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                # The reaper is hardened to never die on a transient error (finding
                # C4), but if the task ever stored ANY exception, awaiting it
                # re-raises here. Aborting teardown would leak the connections
                # below. Log and press on so they always close.
                print(f"swarm-sync: reaper task exited with error: {exc!r}", flush=True)
        if reaper_conn is not None:
            reaper_conn.close()
        conn.close()

    app = FastAPI(title="swarm-sync blackboard", lifespan=lifespan)
    app.state.conn = conn
    app.state.db_path = db_path
    # P0 (S1): a process-wide lock that serializes POST /integrate so at most
    # ONE merge touches the shared `into` checkout at a time. FastAPI runs the
    # sync route handlers on Starlette's threadpool, so without this two
    # concurrent /integrate requests would race on the same working tree (dual
    # `git checkout`/`git merge`, interleaved `git reset --hard`) and silently
    # corrupt trunk or drop a committed branch. An asyncio.Lock (held in the
    # async handler while the blocking merge runs in a threadpool) makes waiters
    # queue without tying up threadpool workers -- exactly DESIGN §5.4's "serial
    # test-gated integrator: trunk is never poisoned by a partial edit".
    app.state.integrate_lock = asyncio.Lock()

    # --- WP3.3 (S5): reject oversized request bodies with 413 ---------------------
    # Declared-length check: when Content-Length is present (every real client here
    # -- the hook adapter, httpx, requests, TestClient -- sends it for a body), a
    # too-large request is rejected up front, before the framework buffers a byte of
    # the body. Chunked/absent-length bodies PASS THROUGH deliberately: counting a
    # stream means re-plumbing the receive channel for a transfer shape none of our
    # clients use, and the S3 token gate already bounds WHO can reach the mutating
    # routes at all. Stated trade-off, not an oversight.
    #
    # Deliberately a PURE-ASGI wrapper, not `@app.middleware("http")`: the decorator
    # form wraps every request in a BaseHTTPMiddleware anyio task group, whose
    # cancel-scope teardown fires the py3.11 `Task.cancel(msg=)` deprecation pair on
    # EVERY request (the suite went 3 -> ~1800 warnings) and adds per-request task
    # overhead. A raw ASGI callable inspects the already-parsed header list with no
    # task machinery at all.
    class _BodySizeCapMiddleware:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def __call__(self, scope, receive, send) -> None:
            if scope["type"] == "http":
                declared: Optional[int] = None
                for name, value in scope.get("headers", []):
                    if name == b"content-length":
                        try:
                            declared = int(value)
                        except ValueError:
                            declared = None  # malformed: let the server stack handle it
                        break
                if declared is not None:
                    cap = _max_body_bytes()
                    if declared > cap:
                        response = JSONResponse(
                            status_code=413,
                            content={
                                "detail": (
                                    f"request body of {declared} bytes exceeds the "
                                    f"{cap}-byte cap (raise {MAX_BODY_BYTES_ENV} if "
                                    "this is legitimate)"
                                )
                            },
                        )
                        await response(scope, receive, send)
                        return
            await self.inner(scope, receive, send)

    app.add_middleware(_BodySizeCapMiddleware)

    # --- POST /index ---------------------------------------------------------

    @app.post("/index", dependencies=[Depends(require_token)])
    def post_index(body: IndexBody, conn=Depends(get_conn)):
        real_root = _validate_managed_path(body.root)
        try:
            result = run_index(conn, real_root, threshold=body.threshold)
        except IndexLimitError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        return {
            "root": body.root,
            "parcels": len(result.parcels),
            "contracts": len(result.contracts),
        }

    # --- GET /parcels ----------------------------------------------------------

    # WP4.5 (A6): `response_model` declares the wire shape this endpoint has
    # ALWAYS returned (raw parcel columns + the active-lease join) -- previously
    # implicit in this handler's dict-building, duck-typed against by the hook
    # adapter, and absent from the OpenAPI schema. The JSON is byte-identical;
    # only the contract is now written down and response-validated.
    @app.get("/parcels", response_model=list[ParcelWithLeases])
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

    # WP4.5 (A6): `blackboard.models.Lease` matches the `leases` schema columns
    # field-for-field (verified against schema.sql), so declaring it changes no
    # bytes on the wire -- it just makes the previously implicit contract typed,
    # validated, and visible in OpenAPI.
    @app.get("/leases", response_model=list[Lease])
    def get_leases(conn=Depends(get_conn)):
        now = time.time()
        rows = conn.execute(
            "SELECT * FROM leases WHERE status = 'active' AND ttl_expires_at > ? "
            "ORDER BY id",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- POST /intent --------------------------------------------------------

    @app.post("/intent", dependencies=[Depends(require_token)])
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

    @app.post("/lease", response_model=LeaseResult, dependencies=[Depends(require_token)])
    def post_lease(body: LeaseRequest, conn=Depends(get_conn)):
        kwargs: dict = {
            "mode": body.mode,
            "intent": body.intent,
            "ensure_parcel": body.ensure_parcel,
        }
        if body.ttl is not None:
            kwargs["ttl"] = body.ttl
        return leases_mod.acquire(conn, body.parcel_id, body.agent_id, **kwargs)

    # --- POST /heartbeat ---------------------------------------------------------

    @app.post("/heartbeat", dependencies=[Depends(require_token)])
    def post_heartbeat(body: HeartbeatBody, conn=Depends(get_conn)):
        kwargs: dict = {}
        if body.ttl is not None:
            kwargs["ttl"] = body.ttl
        ok = leases_mod.heartbeat(conn, body.lease_id, body.agent_id, **kwargs)
        return {"ok": ok}

    # --- POST /release -----------------------------------------------------------

    @app.post("/release", dependencies=[Depends(require_token)])
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

    @app.post("/parcel/update", dependencies=[Depends(require_token)])
    def post_parcel_update(body: ParcelUpdateBody, conn=Depends(get_conn)):
        now = time.time()

        # 404 for a genuinely unknown parcel stays a 404 (distinct from "you don't
        # hold the lease"); check existence before the ownership gate so the two
        # failure modes don't collapse into one.
        if conn.execute(
            "SELECT 1 FROM parcels WHERE id = ? LIMIT 1", (body.parcel_id,)
        ).fetchone() is None:
            raise HTTPException(
                status_code=404, detail=f"no parcel {body.parcel_id!r}"
            )

        # C5: previously the UPDATE was keyed on parcel_id ONLY and `body.agent_id`
        # merely labelled the emitted event/pheromone -- never checked -- so ANY
        # client could overwrite the content_hash/state_summary of a parcel that
        # ANOTHER agent held a write lease on. `_check_read_deps` compares plan-time
        # snapshots against exactly this content_hash column, so a rogue or stale
        # update spuriously bounces the innocent holder with `needs_rebase`, or
        # clears a snapshot a later merge then validates against state that never
        # landed. Require that the caller holds an ACTIVE, UNEXPIRED write/exclusive
        # lease on this parcel before mutating it.
        #
        # Liveness is evaluated on SQLite's OWN clock via `leases._NOW_SQL` -- the
        # same expression `leases.heartbeat` uses -- rather than a Python-side
        # `time.time()` bound before the statement serializes. A stale Python `now`
        # would answer "was the lease alive when I read the clock?", and the row
        # could lapse in the gap before the statement runs (busy_timeout wait,
        # threadpool queueing, GIL preemption), letting an already-expired lease pass
        # the gate -- the C13 clock class of bug. `_NOW_SQL` is a module constant,
        # never caller input, so the interpolation is not an injection surface.
        owns = conn.execute(
            f"""
            SELECT 1 FROM leases
            WHERE parcel_id = :parcel_id
              AND agent_id = :agent_id
              AND mode IN ('write', 'exclusive')
              AND status = 'active'
              AND ttl_expires_at > {leases_mod._NOW_SQL}
            LIMIT 1
            """,  # noqa: S608 - _NOW_SQL is a module constant, never caller input
            {"parcel_id": body.parcel_id, "agent_id": body.agent_id},
        ).fetchone()
        if owns is None:
            # Name the actual current holder (if any) so the caller can act -- e.g.
            # back off and rebase rather than retry blindly.
            holder = conn.execute(
                f"""
                SELECT agent_id FROM leases
                WHERE parcel_id = :parcel_id
                  AND mode IN ('write', 'exclusive')
                  AND status = 'active'
                  AND ttl_expires_at > {leases_mod._NOW_SQL}
                ORDER BY ttl_expires_at DESC
                LIMIT 1
                """,  # noqa: S608 - _NOW_SQL is a module constant, never caller input
                {"parcel_id": body.parcel_id},
            ).fetchone()
            if holder is not None:
                reason = (
                    f"parcel {body.parcel_id!r} is write-leased by "
                    f"{holder['agent_id']!r}, not {body.agent_id!r}"
                )
            else:
                reason = (
                    f"{body.agent_id!r} holds no active write lease on "
                    f"{body.parcel_id!r}"
                )
            return {"ok": False, "parcel_id": body.parcel_id, "reason": reason}

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

    # --- GET /events?since= | ?tail= ------------------------------------------------

    @app.get("/events", response_model=list[EventOut])
    def get_events(
        # WP4.5 (prep C17): `since` is now Optional so an EXPLICIT `?since=` can
        # be told apart from the default -- omitting it still means "from seq 0",
        # so every existing caller's wire behavior is unchanged.
        since: Optional[int] = None,
        # WP4.5: newest-first window. `?tail=N` returns the newest N events (in
        # ascending seq order, same as every other page) -- the "what happened
        # recently" read the agent runner's read-the-world step needs, without
        # paging the whole log forward from 0. Mutually exclusive with `since`:
        # the two name incompatible anchors (a forward page vs. a newest window),
        # and silently preferring one would misread the caller's intent -- same
        # loud-422 posture as the limit clamp below. Bounded by the same cap.
        tail: Optional[int] = Query(default=None, ge=1, le=MAX_EVENTS_LIMIT),
        # WP3.1 S2: bounded surface -- see the MAX_EVENTS_LIMIT comment above.
        # `since` semantics are untouched (any int, page forward from that seq).
        limit: int = Query(default=1000, ge=0, le=MAX_EVENTS_LIMIT),
        conn=Depends(get_conn),
    ):
        if tail is not None:
            if since is not None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "`since` and `tail` are mutually exclusive: page forward "
                        "with since=, or take the newest window with tail=, not both"
                    ),
                )
            return events_mod.tail_newest(conn, tail)
        return events_mod.tail(conn, since_seq=since if since is not None else 0, limit=limit)

    # --- POST /integrate -----------------------------------------------------------

    @app.post("/integrate", dependencies=[Depends(require_token)])
    async def post_integrate(
        body: IntegrateBody, request: Request, conn=Depends(get_conn)
    ):
        # Reject a repo path outside the managed roots before any git process runs.
        real_repo = _validate_managed_path(body.repo)
        # U10: the serial test-gated integrator. S1 hardening: this endpoint is
        # now the serialization point. Every merge is funneled through the
        # process-wide `integrate_lock` (above) so at most one branch touches
        # the shared `into` checkout at a time -- concurrent /integrate requests
        # queue on the lock instead of racing on trunk's working tree. The
        # actual merge is blocking (subprocess git + pytest), so it runs in a
        # threadpool via `run_in_threadpool` while the coroutine holds the lock;
        # waiters simply await the lock without consuming threadpool workers.
        async with request.app.state.integrate_lock:
            result = await run_in_threadpool(
                integrator.integrate,
                conn,
                real_repo,
                body.branch,
                base_commit=body.base_commit,
                into=body.into,
                agent_id=body.agent_id,
            )
        return dataclasses.asdict(result)

    return app


def main(argv: Optional[list[str]] = None) -> None:
    """Launch the blackboard under uvicorn (the `swarm-sync` console script).

    S3 hardening: binds to 127.0.0.1 by DEFAULT (was 0.0.0.0 -- the blackboard
    holds no auth by default and gates real editing sessions, so it must not be
    reachable from the network unless the operator deliberately opts in with
    `--host`). Mirrors `server/serve.py`'s argparse surface (--host/--port/--db).

    WP3.3 C3: this launcher (the `swarm-sync` console script) previously skipped the
    C13 clock assertion that `swarmsync-serve` runs, so the exact same server started
    via the other entry point silently forwent the double-lease clock guard. The
    import is LAZY (inside this function) because `serve` imports `app` at module
    level -- a top-level import here would be a genuine import cycle.
    """
    import uvicorn

    from swarmsync.server.serve import assert_clock_agreement

    parser = argparse.ArgumentParser(
        prog="swarm-sync", description="swarm-sync blackboard server"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="bind port (default: 8000)"
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("SWARM_SYNC_DB", "blackboard.db"),
        help="blackboard SQLite path (default: $SWARM_SYNC_DB or blackboard.db)",
    )
    args = parser.parse_args(argv)

    # C13: verify the SQLite-vs-Python clock invariant before serving anything --
    # same guard, same ordering, as `serve.main`.
    assert_clock_agreement()

    app = create_app(args.db)
    uvicorn.run(app, host=args.host, port=args.port)
