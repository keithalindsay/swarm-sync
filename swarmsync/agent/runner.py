"""Agent runner: the full sync protocol lifecycle in a worktree. DESIGN.md §4.3.

Unit U9. Implements the per-agent loop DESIGN §4.3 specifies:

  1. read parcels + events (current state / who's active) -- advisory; the
     real safety net is the CAS lease acquired in step 3.
  2. POST /intent for the task's target parcels (drops a 'planned' pheromone).
  3. POST /lease (write, by default) on each target parcel; on ANY denial,
     back off -- release whatever partial leases were already granted and
     return early rather than editing with an incomplete lock set.
  4. GET /contract for each declared read-dependency (drift detection is a
     later unit's job once there's a broker/plan-time snapshot to diff
     against; this unit fetches the current contract so that snapshot exists).
  5. `git_ops.add_worktree`; apply the edit via a scripted mutator
     (`agent/mutators.py`); heartbeat on a background daemon thread the whole
     time so a hard-killed process (SIGKILL, test case #4) simply stops
     heartbeating with no explicit cleanup needed -- the reaper (U11)
     reclaims the lease once its TTL lapses.
  6. `commit_all`; for each target parcel, re-derive its real `content_hash`
     from the freshly-committed worktree (never trust a self-reported hash --
     DESIGN §5.4/§6 "lying blackboard") and `POST /parcel/update`; then
     `POST /integrate` (submit the branch -- U10's integrator merges it
     serially behind the pytest gate; `run_agent` itself doesn't block on or
     interpret the outcome, it just submits and reports it on `AgentResult`);
     finally `POST /release` every lease this agent holds.

`run_agent(agent_id, client, repo, task, target_parcels, mutator, ...)` drives
one task to completion and returns an `AgentResult` describing what happened,
for the caller (a test, or later the broker/U12) to assert against.

U15 adds `lease_modes`: a per-parcel lease-mode override so a caller (the
broker) can force `"exclusive"` on exactly the target parcels that are
frozen contracts (DESIGN §5.3, test case #3) while leaving every other
target parcel on the task's default `lease_mode`.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

from swarmsync.agent.client import BlackboardClient
from swarmsync.blackboard.parcel_id import split as _split_parcel_id
from swarmsync.classifier.indexer import parse_file
from swarmsync.worktree import git_ops

StrPath = Union[str, Path]

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5.0

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """What happened when `run_agent` drove one task to completion (or backed off)."""

    agent_id: str
    task: str
    status: str  # "done" | "lease_denied" | "error"
    branch: Optional[str] = None
    commit_sha: Optional[str] = None
    lease_ids: dict[str, int] = field(default_factory=dict)
    denied_parcels: list[str] = field(default_factory=list)
    updated_parcels: dict[str, str] = field(default_factory=dict)  # id -> content_hash
    contract_snapshot: dict[str, Optional[dict]] = field(default_factory=dict)
    integrate_result: Optional[dict] = None
    lease_modes_used: dict[str, str] = field(default_factory=dict)  # parcel_id -> mode
    # actually granted -- lets a caller/test confirm a frozen-contract target
    # really was leased `exclusive` (DESIGN §5.3, test case #3, U15), not
    # just whatever `lease_mode` it happened to pass.
    error: Optional[str] = None  # "ExcType: message" summary when status == "error" (C11)
    error_type: Optional[str] = None  # exception class name -- the broker's bounded
    # GitOpsError retry (WP3.6) keys off this, so it must survive the
    # exception object itself not crossing the thread boundary.


class _Heartbeater:
    """Background daemon thread bumping TTL on a set of leases until stopped.

    Runs off the main thread (DESIGN §4.3 step 5) so a hard-killed agent
    process (SIGKILL, test case #4) simply stops heartbeating -- there is
    nothing to explicitly clean up in that scenario; the reaper (U11)
    reclaims the lease once `ttl_expires_at` lapses. Under a normal
    (non-crashed) run, `stop()` is called from `run_agent`'s OUTER `finally` --
    i.e. only once /parcel_update and /integrate have returned -- so the beat
    covers every step that still needs the lease (notably the integrate gate,
    whose runtime is unbounded), and the thread never outlives the call that
    started it.
    """

    def __init__(
        self,
        client: BlackboardClient,
        agent_id: str,
        interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._client = client
        self._agent_id = agent_id
        self._interval = interval
        self._lease_ids: list[int] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def add(self, lease_id: int) -> None:
        self._lease_ids.append(lease_id)

    def start(self) -> None:
        if not self._lease_ids:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            for lease_id in list(self._lease_ids):
                try:
                    self._client.heartbeat(self._agent_id, lease_id)
                except Exception as exc:
                    # A heartbeat failure (e.g. the server went away) must never
                    # crash the background thread -- it just means this beat is
                    # lost and the reaper may reclaim the lease, which is a
                    # legitimate outcome the runner's own protocol handles.
                    # Logged (WP4.4/A8) so a blackboard dying mid-run leaves a
                    # trace instead of silently losing every beat.
                    logger.warning(
                        "heartbeat for lease %s (agent %s) failed: %s -- beat "
                        "lost; the reaper may reclaim the lease at TTL expiry",
                        lease_id,
                        self._agent_id,
                        exc,
                    )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 1.0)


def _cleanup_worktree(
    repo: Path,
    agent_id: str,
    delete_branch: bool = False,
    park_branch: bool = False,
) -> None:
    """Best-effort teardown of this agent's worktree + branch (S5).

    Called from a `finally` after integrate/release on the done path, and on the
    lease_denied path, so a completed/abandoned agent never leaks its
    `.worktrees/<agent_id>` dir or its `<agent_id>` branch -- reruns with the same
    agent_id don't collide or accumulate orphans. Swallows `GitOpsError` (there
    may be nothing to remove -- e.g. lease_denied created no worktree) so cleanup
    never turns into the reason a run fails.

    `delete_branch` defaults to FALSE and must be opted into by the caller, which
    may only do so once the merge has actually LANDED. A landed merge's commits
    live in trunk's history, so dropping the redundant ref loses nothing -- but on
    the merge_rejected/needs_rebase paths the integrator has reset trunk back to
    `pre_merge_sha`, so this branch is the ONLY reference to the agent's commits.
    Deleting it there makes the work unreachable and destroys the rebase-and-
    resubmit path DESIGN §5.5 promises.

    `park_branch` (WP3.5): keeping the branch was NOT enough. Broker attempt ids
    are deterministic (`{task_id}-attempt-{n}`), and `git_ops.add_worktree` runs
    `_prune_stale_worktree` -- which `git branch -D`s the same-named branch --
    at the top of every call, so a re-run of the same task list destroyed the
    "kept" branch before doing anything else. When `park_branch=True` (the
    committed-but-not-landed paths), the branch's tip is additionally parked
    under `rejected/<agent_id>-<UTC ts>` (`git_ops.park_branch`), a namespace
    pruning never deletes, making the preserved-commits contract survive reruns.
    """
    try:
        if park_branch and not delete_branch:
            parked = git_ops.park_branch(repo, agent_id)
            logger.info(
                "run_agent(%s): merge did not land; branch parked as %r -- the "
                "rejected commits stay reachable there even after a re-run of the "
                "same task id prunes branch %r",
                agent_id,
                parked,
                agent_id,
            )
    except git_ops.GitOpsError as exc:
        # e.g. the branch was never created (the failure predates add_worktree);
        # nothing to park, and cleanup must never become the reason a run fails.
        logger.debug(
            "cleanup for agent %s: park_branch failed (%s) -- usually the "
            "branch was never created; continuing",
            agent_id,
            exc,
        )
    try:
        git_ops.remove_worktree(repo, agent_id, delete_branch=delete_branch)
    except git_ops.GitOpsError as exc:
        # Best-effort by design (there may be no worktree to remove, e.g. on
        # the lease_denied path) -- but leave a trace (WP4.4/A8) instead of
        # discarding the failure entirely.
        logger.debug(
            "cleanup for agent %s: remove_worktree failed (%s) -- there may "
            "have been nothing to remove; continuing",
            agent_id,
            exc,
        )


def _state_summary(parcel, task: str) -> str:
    """A lightweight, deterministic note: `kind + signature-ish info + changed-
    line count` (DESIGN §2). This is only the agent's ADVISORY self-report --
    the integrator (U10) regenerates `state_summary` authoritatively on merge,
    per DESIGN §5.4/§6's "lying blackboard is never trusted" rule.
    """
    bits = [str(parcel.kind), f"symbol={parcel.symbol}"]
    if parcel.byte_start is not None and parcel.byte_end is not None:
        bits.append(f"{parcel.byte_end - parcel.byte_start}B")
    bits.append(f"task={task!r}")
    return " ".join(bits)


def run_agent(
    agent_id: str,
    client: BlackboardClient,
    repo: StrPath,
    task: str,
    target_parcels: list[str],
    mutator: Callable[..., None],
    mutator_kwargs: Optional[dict[str, Any]] = None,
    base_commit: Optional[str] = None,
    lease_mode: str = "write",
    lease_modes: Optional[dict[str, str]] = None,
    lease_ttl: Optional[float] = None,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    read_contracts: Optional[list[str]] = None,
) -> AgentResult:
    """Drive one task to completion against a live blackboard (DESIGN §4.3).

    `target_parcels` are parcel ids (e.g. `"mod_a.py::helper"`) this task will
    edit; `mutator` is one of `agent/mutators.py`'s scripted edit functions
    (or a real edit-producing callable later), called as
    `mutator(worktree, **mutator_kwargs)`. `read_contracts` are frozen-contract
    symbols this task merely *reads* (DESIGN §4.3 step 4) -- their current
    signature/version is fetched and returned on the result for the caller to
    diff against a plan-time snapshot.

    `lease_mode` is the uniform default applied to every `target_parcels`
    entry; `lease_modes` (added U15) is an optional `{parcel_id: mode}`
    override for specific parcels, falling back to `lease_mode` for any
    parcel not named in it. This is DESIGN §5.3's enforcement point:
    "to modify [a frozen contract] an agent must take an EXCLUSIVE lease on
    it" is not something a caller has to remember to opt into by hand --
    `coordinator.broker.run` (U15) already knows which of a task's resolved
    parcels are frozen contracts (it computed `frozen_ids` for scheduling
    anyway) and passes exactly those in as `lease_modes={parcel_id:
    "exclusive"}`. The mode actually granted per parcel is recorded on the
    result (`AgentResult.lease_modes_used`) for a caller/test to confirm.

    On any lease denial, releases whatever partial lease set was already
    granted and returns early with `status="lease_denied"` -- no worktree is
    ever created for a task that can't get all of its locks (DESIGN §5.2: a
    misprediction just loses the CAS race and serializes, it never partially
    proceeds).

    C11 failure containment (WP3.6): an `Exception` anywhere in the work phase
    (contract fetch, `add_worktree`, the mutator, `commit_all`, or the HTTP
    calls after them) does NOT raise out of this function -- every lease this
    attempt holds is released best-effort, the worktree is torn down as usual,
    and a `status="error"` result is returned carrying the exception summary
    (`error`) and class name (`error_type`). `KeyboardInterrupt`/`SystemExit`
    still propagate. This covers IN-PROCESS exceptions only, and deliberately
    does not change the crash-recovery story: a SIGKILLed agent process has
    nothing to run an except path, so its leases still leak until TTL expiry /
    the reaper reclaims them (DESIGN §6) -- that remains by design.
    """
    mutator_kwargs = mutator_kwargs or {}
    repo = Path(repo)

    # 1. read current state (advisory -- the CAS lease is the real safety net).
    client.parcels()
    client.events(since=0)

    # 2. declare intent -> drops a 'planned' pheromone + event per target parcel.
    client.intent(agent_id, task, target_parcels)

    # 3. acquire a write-lease (default) on every target parcel; back off on
    # ANY denial rather than holding a partial lock set. `lease_modes` (a
    # per-parcel override, U15) beats `lease_mode` when both name the same
    # parcel -- see this function's own docstring.
    lease_modes = lease_modes or {}
    lease_ids: dict[str, int] = {}
    lease_modes_used: dict[str, str] = {}
    for parcel_id in target_parcels:
        mode = lease_modes.get(parcel_id, lease_mode)
        result = client.lease(agent_id, parcel_id, mode=mode, intent=task, ttl=lease_ttl)
        if not result.get("granted"):
            for held_parcel_id, lease_id in lease_ids.items():
                client.release(agent_id, lease_id)
            # S5: prune any worktree/branch this agent_id leaked on a PRIOR run
            # before backing off, so a later rerun starts clean.
            _cleanup_worktree(repo, agent_id)
            return AgentResult(
                agent_id=agent_id,
                task=task,
                status="lease_denied",
                lease_ids=lease_ids,
                denied_parcels=[parcel_id],
                lease_modes_used=lease_modes_used,
            )
        lease_ids[parcel_id] = result["lease_id"]
        lease_modes_used[parcel_id] = mode

    heartbeater = _Heartbeater(client, agent_id, interval=heartbeat_interval)
    for lease_id in lease_ids.values():
        heartbeater.add(lease_id)
    heartbeater.start()

    # Everything from here holds leases (and soon a worktree); the outer `finally`
    # tears the worktree down after integrate/release -- and on any mid-way failure
    # too -- so nothing leaks for a rerun to collide with (S5). `landed` stays False
    # unless the integrator reports `merged`, so a failure anywhere in here keeps
    # the branch.
    #
    # C11 failure containment (the `except` below): any `Exception` out of this
    # work phase -- the contract fetch, `add_worktree`, the mutator, `commit_all`,
    # or the HTTP calls that follow -- used to propagate to the caller with every
    # acquired lease still ACTIVE until TTL expiry (leaked), and the broker then
    # re-raised it out of `future.result()`, aborting the whole run. Now the
    # except path releases every lease this attempt holds (best-effort) and
    # returns a structured `status="error"` result instead of raising.
    # `KeyboardInterrupt`/`SystemExit` are deliberately NOT masked (Exception,
    # not BaseException). This covers in-process exceptions ONLY and does not
    # change the crash-recovery story: a SIGKILLed agent process runs no except
    # path, so its leases still leak until TTL/reaper reclaim (DESIGN §6) -- by
    # design, and the reaper is exactly the backstop for that case.
    contract_snapshot: dict[str, Optional[dict]] = {}
    landed = False
    commit_sha: Optional[str] = None  # set once commit_all succeeds -- the outer
    # finally parks the branch only when there IS a commit to preserve (WP3.5).
    try:
        # 4. read-dependency contracts (drift detection vs. a plan-time snapshot
        # is the broker's/U12's job; this unit fetches the current state). Inside
        # containment because the leases are already held here: a failing GET must
        # release them like any other work-phase failure.
        for symbol in read_contracts or []:
            contract_snapshot[symbol] = client.contract(symbol)

        # 5. isolated worktree + the scripted (or real) edit.
        #
        # The heartbeat must outlive this block: step 6 posts /parcel/update and
        # then /integrate, whose pytest gate runs for an unbounded time (impact
        # selection falls back to the FULL suite). Stopping the heartbeat here --
        # as this code used to -- left the rest of the run beating zero times
        # against a 30s TTL, so any gate slower than the TTL got this agent's
        # lease reaped mid-flight and re-granted, in write mode, to another agent
        # while this one was still working. It is released explicitly on the
        # success path below and stopped in the outer `finally` regardless.
        worktree = git_ops.add_worktree(repo, agent_id, base_commit)
        mutator(worktree, **mutator_kwargs)
        commit_sha = git_ops.commit_all(worktree, f"{agent_id}: {task}")

        # 6. re-derive each touched parcel's real content_hash from the committed
        # worktree (never trust a self-reported hash) and post it + a
        # deterministic summary; submit the branch to the integrator; release
        # every lease held.
        updated_parcels: dict[str, str] = {}
        for parcel_id in target_parcels:
            path, _symbol = _split_parcel_id(parcel_id)
            fresh_parcels = parse_file(worktree / path, rel_path=path)
            match = next((p for p in fresh_parcels if p.id == parcel_id), None)
            if match is None:
                # The mutator removed/renamed the symbol entirely -- nothing to
                # post for this parcel id; leave it out of updated_parcels rather
                # than posting a stale/None hash.
                continue
            # `parse_file` always populates content_hash; typeshed/model types it
            # Optional (schema allows NULL), so pin the invariant for the str API.
            assert match.content_hash is not None
            client.parcel_update(
                agent_id, parcel_id, match.content_hash, _state_summary(match, task)
            )
            updated_parcels[parcel_id] = match.content_hash

        integrate_result = client.integrate(
            agent_id, branch=agent_id, repo=str(repo), base_commit=base_commit
        )
        # Only a LANDED merge makes this agent's branch redundant; see
        # `_cleanup_worktree`. On merge_rejected/needs_rebase the integrator has
        # reset trunk, so the branch is the only thing still pointing at the work.
        landed = integrate_result.get("status") == "merged"

        for lease_id in lease_ids.values():
            client.release(agent_id, lease_id)

        return AgentResult(
            agent_id=agent_id,
            task=task,
            status="done",
            branch=agent_id,
            commit_sha=commit_sha,
            lease_ids=lease_ids,
            updated_parcels=updated_parcels,
            contract_snapshot=contract_snapshot,
            integrate_result=integrate_result,
            lease_modes_used=lease_modes_used,
        )
    except Exception as exc:
        # C11: contain the failure -- see the comment above the `try`. Release
        # every lease this attempt still holds so siblings/retries can proceed
        # immediately instead of waiting out the TTL. Best-effort: a lease that
        # was already released on the success path just fails its re-release
        # harmlessly, and a release the server refuses (or can't receive) is
        # logged and left to the reaper.
        for parcel_id, lease_id in lease_ids.items():
            try:
                client.release(agent_id, lease_id)
            except Exception as release_exc:
                logger.warning(
                    "run_agent(%s): best-effort release of lease %s (%s) failed "
                    "during error containment: %s -- leaving it to the TTL/reaper",
                    agent_id,
                    lease_id,
                    parcel_id,
                    release_exc,
                )
        summary = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "run_agent(%s): task %r failed in the work phase (%s); leases released, "
            "returning a structured error result",
            agent_id,
            task,
            summary,
        )
        return AgentResult(
            agent_id=agent_id,
            task=task,
            status="error",
            lease_ids=lease_ids,
            contract_snapshot=contract_snapshot,
            lease_modes_used=lease_modes_used,
            error=summary,
            error_type=type(exc).__name__,
        )
    finally:
        # Stopping the beat here (rather than after commit_all) is what keeps this
        # agent's lease alive across /parcel/update and /integrate's unbounded
        # pytest gate -- see the note in the try block above.
        heartbeater.stop()
        # WP3.5: a run that COMMITTED work the merge did not land (merge_rejected,
        # needs_rebase, or an error after commit_all) parks the branch under
        # `rejected/*` so a rerun's deterministic attempt id can't prune away the
        # only reference to those commits. A landed run deletes as before; a run
        # that never committed has nothing worth parking.
        _cleanup_worktree(
            repo,
            agent_id,
            delete_branch=landed,
            park_branch=commit_sha is not None and not landed,
        )
