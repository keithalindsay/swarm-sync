"""Broker: match tasks to parcels, spawn agents, reassign on reap. DESIGN.md §5, §6.

Built in Unit U12. The broker is the piece that decides WHEN task groups run
(concurrently vs. serially), then drives each task through the already-built
per-agent lifecycle (`agent.runner.run_agent`, U9) against a live blackboard
client. It owns NO code-edit truth of its own -- like every other unit, all
coordination stays purely stigmergic (leases + events); the broker only reads
the parcel map/dependency graph to make a scheduling decision and spawns
`run_agent` calls, exactly per this module's own pre-existing docstring
contract.

Task(task_id, targets, mutator, mutator_kwargs=None, read_deps=(), ...)
  A unit of work: `targets` is a list of `(file, symbol_or_None)` hints (a
  task's "target file/symbol hints", the BUILD_PLAN's own wording) -- a
  bare-file hint (`symbol=None`) means "this whole file."

resolve_task(conn, task, mode="file") -> list[parcel_id]
  Maps a task's hints to the parcel ids it will actually take write-leases
  on, using the blackboard's parcel map (populated by `classifier.store.
  run_index` / `POST /index`). `mode="file"` is DESIGN §2's de-risking
  default *enforced lease granularity*: every hint collapses to its file's
  whole-file `"<module>"` interstitial parcel, regardless of which symbol
  was named -- two tasks in the same file share one lock and can never even
  race at symbol granularity. `mode="symbol"` leases the specific named
  symbol (falling back to the file id when no symbol was given or that exact
  symbol parcel doesn't exist -- the classifier's own conservative fallback,
  DESIGN §6).

schedulable(conn, task_a, task_b, mode="file", graph=None, frozen_ids=None) -> bool
  True iff task_a's and task_b's resolved target-parcel sets are pairwise
  `classifier.graph.co_schedulable` (DESIGN §3's parallel-safety relation):
  "two tasks are safely parallel iff their whole target-parcel sets are
  pairwise co-schedulable."

group_schedulable(conn, tasks, ...) -> list[list[Task]]
  Greedily partitions `tasks` (input order preserved) into dispatch "waves":
  each wave is a maximal set of mutually co-schedulable tasks. This is what
  turns the pairwise relation into an actual schedule.

run(conn, repo, tasks, client, n_agents=4, mode="file") -> dict[task_id, AgentResult]
  Dispatch every task to completion: partition into waves, run each wave's
  tasks concurrently (bounded by `n_agents`), retry a task that comes back
  `lease_denied` (contention with a still-active holder, OR the task's
  original agent crashed and hasn't aged out yet) under a fresh agent id with
  backoff -- this is both DESIGN §7 money-shot #2 ("contended parcel
  serializes... waits, then acquires") and the "task whose agent is reaped
  is reassigned and completes" behavior this unit's done-when asks for, via
  the SAME retry loop: `leases.acquire`'s lazy expiry (U5) means a
  reassigned attempt just succeeds once the dead holder's TTL lapses,
  whether or not the reaper has literally flipped the row to `reaped` yet;
  this function still calls `reaper.reap_once` before every attempt anyway,
  purely so the blackboard's event trail carries a real `reaped` event for
  observability (U11's own handoff note explicitly invites this).

Design notes (this unit's own decisions):

- **Frozen-contract targets are auto-upgraded to an EXCLUSIVE lease** (U15,
  DESIGN §5.3, money-shot #3): `run` already computes `frozen_ids` for
  `group_schedulable`'s co-schedulability check when `contract_aware=True`
  (the default); `_run_task_once` reuses that SAME set so any task whose
  resolved target parcel is a frozen contract gets `lease_modes=
  {parcel_id: "exclusive"}` passed to `agent.runner.run_agent` (U15),
  regardless of what `lease_mode` the task/caller otherwise defaults to.
  This is what makes "to change a frozen contract you must take an
  exclusive lease" an enforced runtime invariant rather than a convention a
  scripted mutator/demo has to remember to opt into by hand. Detecting that
  a change actually landed and announcing it (`contract_change`) is a
  separate, later step -- see `coordinator/integrator.py`'s own docstring --
  since only a genuinely landed before/after `type_hash` diff is trustworthy
  (DESIGN §5.4/§6 "lying blackboard").
- **`/integrate` is not internally locked** (server/app.py's own docstring:
  "whatever submits branches... must call this one branch at a time").
  `run_agent` calls it as the very last step of every successful edit, so
  dispatching a wave's tasks concurrently would otherwise let two threads
  race `git checkout <into>` / `git merge` against the SAME shared main
  checkout. `run` wraps whatever `client` it's given in
  `_SerializingIntegrateClient` (this module): every method passes straight
  through except `.integrate()`, which is funneled through one
  `threading.Lock`. Worktree edits (lease/heartbeat/mutate/commit) stay
  genuinely parallel across a wave -- each agent's worktree is its own
  isolated directory (DESIGN §5.1) -- only the shared-trunk merge step
  serializes, which is exactly the invariant DESIGN §5.4 needs.
- **`blackboard/db.py` gained `PRAGMA busy_timeout=5000`** as part of this
  unit (a one-line addition, same shape as U7's `check_same_thread=False`
  fix): U12 is the first unit to genuinely dispatch concurrent writers
  against the one shared connection, and `sqlite3.threadsafety == 3`
  (SQLite compiled "serialized") on this host makes that safe in principle,
  but a losing writer's default behavior on a lock conflict is to raise
  immediately rather than wait -- a real risk once tasks truly run in
  parallel threads. `busy_timeout` makes a loser wait instead of erroring;
  harmless to every earlier unit's (single-threaded) tests.
- **Read-dependencies are fetched, not leased.** DESIGN's prose for
  `resolve_task` says "read-deps -> read-leases," but `agent.runner.
  run_agent` (U9, already built+tested) only ever takes `read_contracts` as
  a plan-time *snapshot fetch* (no lease acquired) -- there is nothing
  downstream of this unit that writes to a read-dependency, so a broker-held
  read-lease would add ceremony with no safety payoff yet. `task.read_deps`
  is threaded straight through to `run_agent`'s `read_contracts` param
  unchanged; a literal read-lease is left for whichever later unit
  (money-shot #3, U15) actually needs to gate on one.
- **Greedy wave partitioning**, not an optimal graph coloring: `tasks` is
  walked in order, each task joining the first existing wave it is
  co-schedulable with everything already in, else starting a new wave. This
  is a schedule, not necessarily the fewest possible waves -- fine for the
  prototype's scale and matches the done-when's own framing ("2 disjoint...
  1 overlapping" -> exactly 2 waves is the only sane answer regardless of
  algorithm).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

from swarmsync.agent.runner import AgentResult, run_agent
from swarmsync.blackboard.models import Parcel
from swarmsync.classifier.graph import DepGraph, build_graph, co_schedulable
from swarmsync.coordinator import reaper

StrPath = Union[str, Path]

MODULE_SYMBOL = "<module>"
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BACKOFF = 0.2


@dataclass
class Task:
    """One unit of work the broker will drive through `run_agent`.

    `targets`: a list of `(file, symbol)` hints this task will edit --
    `symbol=None` means "the whole file" (no finer hint given). `mutator`/
    `mutator_kwargs` are exactly `run_agent`'s own params (a scripted edit
    from `agent/mutators.py`, or any callable of the same shape). `read_deps`
    are frozen-contract symbols/parcel ids this task merely reads (fetched,
    not leased -- see module docstring). `base_commit`, when given, pins
    every attempt's worktree fork point; left `None` (the common case) lets
    each attempt branch from whatever `into`'s HEAD is at dispatch time,
    which is what a later wave wants after an earlier wave has landed.
    """

    task_id: str
    targets: list[tuple[str, Optional[str]]]
    mutator: Callable[..., None]
    mutator_kwargs: dict[str, Any] = field(default_factory=dict)
    read_deps: list[str] = field(default_factory=list)
    base_commit: Optional[str] = None
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


def _module_id(file: str) -> str:
    return f"{file}::{MODULE_SYMBOL}"


def _load_parcel(conn: sqlite3.Connection, parcel_id: str) -> Optional[Parcel]:
    row = conn.execute("SELECT * FROM parcels WHERE id = ?", (parcel_id,)).fetchone()
    return Parcel.model_validate(dict(row)) if row is not None else None


def resolve_task(conn: sqlite3.Connection, task: Task, mode: str = "file") -> list[str]:
    """Map `task`'s target file/symbol hints to concrete, blackboard-known
    parcel ids (DESIGN §3's parcel map is the source of truth -- this never
    invents an id the classifier didn't already emit).

    Raises `ValueError` on an unrecognized `mode`, or if a hinted file has no
    parcel at all in the blackboard yet (i.e. it hasn't been indexed) -- both
    are caller bugs, not races, so they fail loudly rather than silently
    scheduling against a phantom id.
    """
    if mode not in ("file", "symbol"):
        raise ValueError(f"unknown granularity mode: {mode!r}")

    ids: list[str] = []
    for file, symbol in task.targets:
        module_id = _module_id(file)
        if mode == "file":
            candidate = module_id
        elif symbol:
            symbol_id = f"{file}::{symbol}"
            candidate = symbol_id if _load_parcel(conn, symbol_id) is not None else module_id
        else:
            candidate = module_id

        if _load_parcel(conn, candidate) is None:
            raise ValueError(
                f"no parcel {candidate!r} in the blackboard -- has {file!r} been "
                "indexed (POST /index / classifier.store.run_index) yet?"
            )
        if candidate not in ids:
            ids.append(candidate)
    return ids


def load_scheduling_graph(conn: sqlite3.Connection, repo: StrPath) -> tuple[DepGraph, set[str]]:
    """Build a fresh `DepGraph` from the blackboard's CURRENT parcel map (for
    `co_schedulable`'s frozen-contract clause) plus the current set of frozen
    contract symbols. A cheap re-derivation from already-indexed parcels --
    kept separate from `classifier.store.run_index` (which also re-parses
    every file and re-upserts everything) since the broker just needs a
    schedulability answer, not a re-index.
    """
    rows = conn.execute("SELECT * FROM parcels").fetchall()
    parcels = [Parcel.model_validate(dict(r)) for r in rows]
    graph = build_graph(parcels, repo)
    frozen_ids = {
        row["symbol"] for row in conn.execute("SELECT symbol FROM contracts").fetchall()
    }
    return graph, frozen_ids


def _resolved_parcels(conn: sqlite3.Connection, task: Task, mode: str) -> list[Parcel]:
    """Load every parcel a task resolves to. `resolve_task` only returns ids that
    have a live parcel (it raises otherwise), so each load is non-None here; the
    assert pins that invariant for the `co_schedulable(Parcel, Parcel)` contract."""
    parcels: list[Parcel] = []
    for pid in resolve_task(conn, task, mode=mode):
        parcel = _load_parcel(conn, pid)
        assert parcel is not None
        parcels.append(parcel)
    return parcels


def schedulable(
    conn: sqlite3.Connection,
    task_a: Task,
    task_b: Task,
    mode: str = "file",
    graph: Optional[DepGraph] = None,
    frozen_ids: Optional[set[str]] = None,
) -> bool:
    """True iff every parcel `task_a` targets is `co_schedulable` with every
    parcel `task_b` targets (DESIGN §3: "two tasks are safely parallel iff
    their whole target-parcel sets are pairwise co-schedulable")."""
    parcels_a = _resolved_parcels(conn, task_a, mode=mode)
    parcels_b = _resolved_parcels(conn, task_b, mode=mode)
    return all(
        co_schedulable(pa, pb, mode=mode, graph=graph, frozen_ids=frozen_ids)
        for pa in parcels_a
        for pb in parcels_b
    )


def group_schedulable(
    conn: sqlite3.Connection,
    tasks: list[Task],
    mode: str = "file",
    graph: Optional[DepGraph] = None,
    frozen_ids: Optional[set[str]] = None,
) -> list[list[Task]]:
    """Greedily partition `tasks` (input order preserved) into dispatch
    "waves": each wave is a maximal run of mutually co-schedulable tasks. A
    task that conflicts with anything already placed in a wave tries the
    next wave, else starts a new one. This is the schedule `run` dispatches."""
    waves: list[list[Task]] = []
    for task in tasks:
        for wave in waves:
            if all(
                schedulable(conn, task, other, mode=mode, graph=graph, frozen_ids=frozen_ids)
                for other in wave
            ):
                wave.append(task)
                break
        else:
            waves.append([task])
    return waves


class _SerializingIntegrateClient:
    """Wrap a blackboard client so `.integrate()` calls funnel through one
    lock while every other method passes straight through -- see this
    module's own docstring point on `/integrate` not being internally locked.
    """

    def __init__(self, client: Any, lock: threading.Lock) -> None:
        self._client = client
        self._lock = lock

    def integrate(self, *args: Any, **kwargs: Any) -> dict:
        with self._lock:
            return self._client.integrate(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def _run_task_once(
    conn: sqlite3.Connection,
    repo: StrPath,
    client: Any,
    task: Task,
    agent_id: str,
    mode: str,
    frozen_ids: Optional[set[str]] = None,
) -> AgentResult:
    target_parcels = resolve_task(conn, task, mode=mode)
    # DESIGN §5.3 (money-shot #3, U15): any target parcel that is ALREADY a
    # frozen contract (per the SAME `frozen_ids` this call's wave was
    # scheduled against, `load_scheduling_graph`) must be taken under an
    # EXCLUSIVE lease, not whatever `lease_mode` the task/caller happened to
    # default to -- enforced here rather than trusted to the caller, exactly
    # like `resolve_task` itself never invents a parcel id the classifier
    # didn't already emit. Every other target parcel is unaffected.
    lease_modes = (
        {pid: "exclusive" for pid in target_parcels if pid in frozen_ids}
        if frozen_ids
        else None
    )
    return run_agent(
        agent_id=agent_id,
        client=client,
        repo=repo,
        task=task.task_id,
        target_parcels=target_parcels,
        mutator=task.mutator,
        mutator_kwargs=task.mutator_kwargs,
        base_commit=task.base_commit,
        read_contracts=task.read_deps or None,
        lease_modes=lease_modes,
    )


def _run_task_with_retries(
    conn: sqlite3.Connection,
    repo: StrPath,
    client: Any,
    task: Task,
    mode: str,
    retry_backoff: float,
    frozen_ids: Optional[set[str]] = None,
) -> AgentResult:
    """Drive one task to completion, retrying under a fresh agent id on
    `lease_denied` -- contention with a still-live holder (money-shot #2) or
    a crashed original holder that hasn't aged out/been reaped yet (this
    unit's "reassigned and completes" case) look identical from here, and
    both resolve the same way: back off, let `reap_once` run (bookkeeping;
    correctness doesn't depend on it, `leases.acquire`'s lazy expiry does the
    real work), try again under `f"{task.task_id}-attempt-{n}"` (a fresh
    agent id every attempt -- also a valid, unique git branch name, since
    `run_agent`'s branch name convention is exactly `agent_id`).
    """
    result: Optional[AgentResult] = None
    for attempt in range(1, task.max_attempts + 1):
        # Bookkeeping pass (DESIGN §6 / U11's own handoff note): if the
        # previous attempt's agent (or an externally-seeded holder) crashed,
        # this is where that shows up as a real `reaped` event in the
        # blackboard's audit trail before this attempt tries the CAS again.
        reaper.reap_once(conn)
        agent_id = f"{task.task_id}-attempt-{attempt}"
        result = _run_task_once(conn, repo, client, task, agent_id, mode, frozen_ids=frozen_ids)
        if result.status != "lease_denied":
            return result
        if attempt < task.max_attempts:
            time.sleep(retry_backoff)
    assert result is not None
    return result


def run(
    conn: sqlite3.Connection,
    repo: StrPath,
    tasks: list[Task],
    client: Any,
    n_agents: int = 4,
    mode: str = "file",
    contract_aware: bool = True,
    retry_backoff: float = DEFAULT_RETRY_BACKOFF,
) -> dict[str, AgentResult]:
    """Drive every task in `tasks` to completion (DESIGN §5, §6).

    1. Partition `tasks` into co-schedulable waves (`group_schedulable`).
    2. Dispatch each wave's tasks CONCURRENTLY (bounded by `n_agents`) through
       `agent.runner.run_agent`; a wave fully drains (including retries)
       before the next one starts.
    3. Merges are serialized regardless of wave concurrency -- see
       `_SerializingIntegrateClient`.

    Returns `{task_id: AgentResult}` for every task in `tasks`, keyed by
    `task.task_id` (its LAST attempt's result if it needed retries).
    """
    graph: Optional[DepGraph] = None
    frozen_ids: Optional[set[str]] = None
    if contract_aware:
        graph, frozen_ids = load_scheduling_graph(conn, repo)

    waves = group_schedulable(conn, tasks, mode=mode, graph=graph, frozen_ids=frozen_ids)
    serial_client = _SerializingIntegrateClient(client, threading.Lock())

    results: dict[str, AgentResult] = {}
    for wave in waves:
        workers = max(1, min(n_agents, len(wave)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _run_task_with_retries,
                    conn,
                    repo,
                    serial_client,
                    task,
                    mode,
                    retry_backoff,
                    frozen_ids,
                ): task
                for task in wave
            }
            for future, task in futures.items():
                results[task.task_id] = future.result()
    return results
