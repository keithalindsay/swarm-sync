"""Thin HTTP client for the blackboard. DESIGN.md §4.2, §4.3.

Unit U9. Wraps the server endpoints (`server/app.py`, U7) so an agent never
touches SQLite directly -- every read/write goes through the blackboard's HTTP
surface, which is the single-writer connection's only front door.

`BlackboardClient` is deliberately duck-typed over its transport: the
constructor accepts either

  - a plain base-url string, in which case it opens its own `httpx.Client`
    (a real deployment talking to `uvicorn` over the network), or
  - any object that already exposes `.get(url, **kw)` / `.post(url, **kw)`
    returning an httpx-shaped response (`.status_code`, `.json()`) -- in
    particular a `fastapi.testclient.TestClient` (itself an `httpx.Client`
    subclass), so tests can drive the full agent lifecycle in-process against
    a `TestClient`-backed server with no real socket involved. This is the
    literal "against a running TestClient server" shape U9's done-when asks
    for.

This is also the seam DESIGN §2 calls out for swapping in a real Claude Agent
SDK worker later: the wire protocol/methods below stay identical; only
`runner.py`'s "decide the edit" step (today: a scripted mutator) changes.
"""
from __future__ import annotations

import os
from typing import Any, Optional, Protocol, cast

import httpx

# Ordinary blackboard calls (lease, heartbeat, parcel_update, ...) are small SQLite
# writes behind a localhost socket -- milliseconds. 30s is a generous ceiling that
# still fails fast on a dead server, rather than httpx's 5s default which is short
# enough to trip on a merely-busy one (the reaper can stall the event loop for up to
# a 5s busy_timeout all by itself).
DEFAULT_TIMEOUT_SECONDS = 30.0

# `/integrate` is different in kind: it blocks on the integrator's pytest gate, which
# bounds ITSELF at SWARMSYNC_GATE_TIMEOUT (default 600s). The client must therefore
# outlive the gate, or it reports a failure the server is about to turn into a
# successful merge. Mirrors `coordinator.integrator._gate_timeout`'s env contract
# rather than importing it (the agent client must not depend on the coordinator).
INTEGRATE_TIMEOUT_MARGIN_SECONDS = 60.0
DEFAULT_GATE_TIMEOUT_SECONDS = 600.0


def _integrate_timeout() -> float:
    """How long to wait on POST /integrate: the gate's own ceiling plus margin.

    The margin covers the merge, the whole-repo re-index and the rollback that
    bracket the gate inside `integrate()` -- none of which the gate timeout counts.
    """
    raw = os.environ.get("SWARMSYNC_GATE_TIMEOUT")
    gate = DEFAULT_GATE_TIMEOUT_SECONDS
    if raw:
        try:
            parsed = float(raw)
            if parsed > 0:
                gate = parsed
        except ValueError:
            pass
    return gate + INTEGRATE_TIMEOUT_MARGIN_SECONDS


class _HttpLike(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...
    def post(self, url: str, **kwargs: Any) -> Any: ...


class BlackboardClient:
    """HTTP client for every DESIGN §4.2 endpoint, matching `server/app.py` 1:1."""

    def __init__(self, http: "_HttpLike | str", timeout: Optional[float] = None) -> None:
        if isinstance(http, str):
            # httpx.Client satisfies the duck-typed `.get(url, **kw)`/`.post(url, **kw)`
            # protocol at runtime; its nominal signatures differ (keyword-only params),
            # so cast rather than let mypy reject the structurally-compatible transport.
            #
            # An explicit timeout is REQUIRED: httpx's default is 5s, which is shorter
            # than most things this client waits on. `/integrate` is the extreme case --
            # it blocks on the pytest gate, whose own ceiling defaults to 600s -- so with
            # the default, any gate slower than 5 seconds raised ReadTimeout in the agent
            # WHILE THE SERVER WENT ON AND LANDED THE MERGE. The agent then reports
            # failure and tears down, believing its work was lost, while trunk has it:
            # the blackboard-vs-git divergence this system exists to prevent. Every test
            # drives a TestClient (no socket, no timeout), which hid it completely.
            # `integrate()` additionally overrides this per-request; see below.
            self._http: _HttpLike = cast(
                _HttpLike,
                httpx.Client(
                    base_url=http,
                    timeout=DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout,
                ),
            )
            self._owns_http = True
        else:
            self._http = http
            self._owns_http = False

    def close(self) -> None:
        if self._owns_http:
            self._http.close()  # type: ignore[attr-defined]

    def __enter__(self) -> "BlackboardClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- reads ---------------------------------------------------------------

    def parcels(self) -> list[dict]:
        r = self._http.get("/parcels")
        r.raise_for_status()
        return r.json()

    def leases(self) -> list[dict]:
        r = self._http.get("/leases")
        r.raise_for_status()
        return r.json()

    def events(self, since: int = 0, limit: int = 1000) -> list[dict]:
        r = self._http.get("/events", params={"since": since, "limit": limit})
        r.raise_for_status()
        return r.json()

    def contract(self, symbol: str) -> Optional[dict]:
        """`GET /contract/{symbol}` -> the frozen signature/version dict, or
        `None` if `symbol` isn't (or is no longer) a frozen contract (404)."""
        r = self._http.get(f"/contract/{symbol}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # --- writes ----------------------------------------------------------------

    def intent(self, agent_id: str, task: str, target_parcels: list[str]) -> dict:
        r = self._http.post(
            "/intent",
            json={
                "agent_id": agent_id,
                "task": task,
                "target_parcels": list(target_parcels),
            },
        )
        r.raise_for_status()
        return r.json()

    def lease(
        self,
        agent_id: str,
        parcel_id: str,
        mode: str = "write",
        intent: Optional[str] = None,
        ttl: Optional[float] = None,
        ensure_parcel: bool = False,
    ) -> dict:
        """`POST /lease` -> `{"granted": bool, "lease_id": int|None, "reason": str|None}`.

        `ensure_parcel=True` asks the server to auto-create a coarse whole-file
        parcel when the id is unknown, so a file the classifier never indexed is
        still coordinated instead of ungated (see `server.leases._ensure_parcel`).
        """
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "parcel_id": parcel_id,
            "mode": mode,
        }
        if intent is not None:
            body["intent"] = intent
        if ttl is not None:
            body["ttl"] = ttl
        if ensure_parcel:
            body["ensure_parcel"] = True
        r = self._http.post("/lease", json=body)
        r.raise_for_status()
        return r.json()

    def heartbeat(
        self, agent_id: str, lease_id: int, ttl: Optional[float] = None
    ) -> bool:
        """`POST /heartbeat` -> renew the lease's TTL. `ttl` (seconds) overrides
        the server's default renewal window when given -- the hook keepalive (S5)
        passes its own long TTL here so a renewed lease keeps the long window,
        not the short server default."""
        body: dict[str, Any] = {"agent_id": agent_id, "lease_id": lease_id}
        if ttl is not None:
            body["ttl"] = ttl
        r = self._http.post("/heartbeat", json=body)
        r.raise_for_status()
        return bool(r.json()["ok"])

    def release(self, agent_id: str, lease_id: int) -> bool:
        r = self._http.post(
            "/release", json={"agent_id": agent_id, "lease_id": lease_id}
        )
        r.raise_for_status()
        return bool(r.json()["ok"])

    def parcel_update(
        self,
        agent_id: str,
        parcel_id: str,
        content_hash: str,
        state_summary: Optional[str] = None,
    ) -> dict:
        r = self._http.post(
            "/parcel/update",
            json={
                "agent_id": agent_id,
                "parcel_id": parcel_id,
                "content_hash": content_hash,
                "state_summary": state_summary,
            },
        )
        r.raise_for_status()
        return r.json()

    def integrate(
        self,
        agent_id: str,
        branch: str,
        repo: str,
        base_commit: Optional[str] = None,
        into: str = "integration",
    ) -> dict:
        """`POST /integrate` -> the integrator's response dict (U10:
        `coordinator.integrator.IntegrateResult`, serialized as JSON --
        `{"status": "merged"|"merge_rejected"|"needs_rebase", ...}`).

        `repo` is the filesystem path to the git repo `branch` lives in --
        required since U10's integrator needs it to actually run `git merge`
        + pytest. Deliberately does NOT `raise_for_status()`: the integrator's
        rejection/needs-rebase outcomes are ordinary 200 responses (only a
        real plumbing error is a non-2xx), so callers (see `runner.run_agent`)
        should inspect `result["status"]` rather than the HTTP status alone.
        """
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "branch": branch,
            "repo": repo,
            "into": into,
        }
        if base_commit is not None:
            body["base_commit"] = base_commit
        # This one request must outlive the server's pytest gate. Timing out here does
        # NOT cancel the merge -- the server keeps going and lands it -- so a client
        # that gives up early doesn't abort the work, it just stops being told the
        # outcome, and reports failure for a merge that succeeded.
        #
        # Only for a transport we built: an injected one (a TestClient, or a caller's
        # own httpx.Client) owns its timeout policy, and a TestClient has no socket to
        # time out at all -- passing `timeout` to it is a documented no-op that only
        # earns a StarletteDeprecationWarning.
        kwargs: dict[str, Any] = {"json": body}
        if self._owns_http:
            kwargs["timeout"] = _integrate_timeout()
        r = self._http.post("/integrate", **kwargs)
        try:
            result = r.json()
        except ValueError:
            result = {"detail": r.text}
        result["_status_code"] = r.status_code
        return result
