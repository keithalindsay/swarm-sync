"""Shared apparatus for the scale tests. Import this; do not re-implement it.

WHAT THIS IS
============
One reusable fixture that stands up the whole swarm-sync path against a *clone*
of a real repository (`code-learner`), plus the read-only inspection helpers the
hypothesis tests assert on. Setting it up costs a clone + a full index + a
lifespan start, so it is meant to be a **module-scoped** fixture, shared by every
test in one file:

    import pytest
    from tests.scale import harness

    @pytest.fixture(scope="module")
    def scale():
        with harness.scale_blackboard() as sr:
            yield sr

`scale_blackboard()` never touches `/home/keith/projects/code-learner` itself --
it `git clone`s it into a fresh temp dir under `tempfile.gettempdir()`. That is
not optional: the broker force-merges, `git reset --hard`s and rewrites trunk.

WHY THE GATE'S INTERPRETER IS OVERRIDDEN
========================================
`coordinator/gate.py` runs the merged repo's suite with `sys.executable -m
pytest` -- i.e. with *swarm-sync's own* interpreter, which its docstring is
explicit about ("the repo under test has no independent environment of its own
in this prototype"). swarm-sync's venv is Python 3.11.0rc1; code-learner
*requires* 3.12 and needs tree-sitter/sqlite-vec that the swarm-sync venv does
not have. Left alone, EVERY gate run would fail for environment reasons, every
merge would be rejected, and "trunk stayed green" would be true only vacuously.

So `scale_blackboard()` swaps the interpreter the gate spawns (and only that --
impact selection, the subprocess sandbox, the timeout and the group-kill
machinery are the real, unmodified ones) for `code-learner/.venv/bin/python`.
This is a harness adaptation, not a source change, and it is itself a finding:
there is no configuration knob for the gate's interpreter, so today the gate is
structurally unable to test any repo that has its own environment.

PUBLIC INTERFACE
================
Constants::

    CODE_LEARNER_REPO      Path to the real checkout that gets cloned (never written)
    CODE_LEARNER_PYTHON    Path to code-learner's 3.12 venv python (what the gate runs)
    TRUNK                  "integration" -- the branch name run_agent/integrate default to
    PARCEL_COUNT_RANGE     measured sanity range for the index (see MEASURED_* below)
    MEASURED_PARCELS / MEASURED_CONTRACTS / MEASURED_FILES / PLAN_STATED_PARCELS

Context manager::

    scale_blackboard(workdir=None, reaper_interval=None, gate_python=CODE_LEARNER_PYTHON,
                     index=True) -> ContextManager[ScaleRepo]

`ScaleRepo` (what the fixture yields)::

    .root            Path      the clone == the trunk checkout (branch `integration`)
    .base_commit     str       trunk's sha at setup, for Task(base_commit=...)
    .db_path         Path      blackboard sqlite file (pass-through to broker.run)
    .conn            sqlite3   app.state.conn -- out-of-band inspection handle
    .client          BlackboardClient over a fastapi TestClient (in-process, no port)
    .http            the TestClient itself, if you need a raw request
    .parcel_count / .contract_count   int, from POST /index
    .peak_worktrees  int       high-water mark of concurrent .worktrees/ dirs (H7)
    .setup_seconds   float     wall-clock for clone + index + lifespan (report it)

    .watch_hits      list[dict] see `run(watch=...)` below

    .run(tasks, n_agents=4, watch=None, **kw) -> dict[task_id, AgentResult]
                     broker.run with db_path already wired (per-worker connections)
                     and the worktree sampler running. `watch=(relpath, needle)`
                     additionally polls the TRUNK checkout while the run is in
                     flight and appends a record to `.watch_hits` every time
                     `needle` is visible in `relpath` -- i.e. every time trunk is
                     carrying that content. Each record also captures trunk's HEAD
                     sha and whether the needle is in the HEAD COMMIT (not just the
                     working tree), because that is what any worktree cut from
                     trunk during the window would inherit. Read-only: it runs no
                     git write, so it cannot perturb the run it observes.
    .events()            -> list[dict]  every event, seq order, with `data` = parsed payload
    .events_by_type()    -> collections.Counter
    .trunk_log()         -> list[str]   "<sha> <subject>" newest-first, trunk only
    .reflog(ref=TRUNK)   -> list[dict]  {"sha", "selector", "message"} newest-first
    .blob_at(sha, path)  -> str|None    file content at a commit ("" vs None: absent)
    .is_ancestor(sha)    -> bool        is `sha` in trunk's HISTORY (not just its reflog)
    .reflog_hits(needle, path, ref=TRUNK) -> list[str]
                     shas in `ref`'s reflog whose `path` contains `needle`. THE H1
                     assertion: empty means the content never reached trunk at all,
                     as opposed to landing and being reverted.
    .worktrees()         -> list[str]   `git worktree list --porcelain` paths
    .worktree_residue()  -> list[str]   names left under .worktrees/
    .branches()          -> list[str]   local branch names (agent attempts, rejected/*)
    .watch_window_seconds               span over which the watched needle was on trunk
    .suite_green()       -> (bool, str) code-learner's OWN suite on trunk, run with
                     code-learner's 3.12 venv (never swarm-sync's 3.11)
    .file_text(relpath)  -> str         read a file from the trunk checkout

Module-level helpers (the plan's literal `events(conn)` / `trunk_log(repo)` /
`reflog(repo)` / `worktrees(repo)` surface -- same functions the methods call)::

    events(conn) -> list[dict]
    trunk_log(repo, ref=TRUNK) -> list[str]
    reflog(repo, ref=TRUNK) -> list[dict]
    worktrees(repo) -> list[str]

Task builders (thin `broker.Task` factories -- nothing you could not write by
hand, they just keep the mutator kwargs from drifting)::

    edit_task(task_id, path, symbol, new_body, base_commit=None)
    break_task(task_id, path, symbol, message, base_commit=None)
    signature_task(task_id, path, symbol, new_sig, base_commit=None, read_deps=())
    hang_task(task_id, path, symbol, new_body, base_commit=None)   # slow_edit(hang=True)

Event-log analysis (H4, and useful to B/C for the same reason)::

    integrate_spans(events)  -> list[dict] one per integrate_started, with its
                                terminal event resolved via payload `started_seq`
    serialization_violations(events) -> list[str]
                                empty iff every integrate_started reached its own
                                terminal event before the next one began

NOTES FOR THE OTHER SCALE AGENTS (read before you build on this)
================================================================
* **Agents here are THREADS, not processes.** `scale_blackboard()` uses an
  in-process `TestClient` (no socket), and `broker.run` dispatches a wave on a
  `ThreadPoolExecutor`. So there is nothing to SIGKILL: killing a thread would
  take the blackboard down with it, which is not what an agent crash looks like.
  A real "kill an agent mid-edit" or "kill the server mid-integration" test needs
  a REAL listening server plus agents as separate OS processes -- the pattern
  already exists in `demo/_crash_agent.py` (`subprocess.Popen` + `run_agent`
  against `--base-url`) driven by `demo/run_demo.py`. Reuse `_prepare_clone` here
  for the repo and the teardown discipline, but expect to run `serve` yourself.
  Ports: this file needs none; 8801 is reserved for it if that ever changes,
  8802 and 8803 for the other two agents.
* **The gate interpreter override changes gate TIMING**, not just whether it
  works: it spawns code-learner's 3.12 venv, so the gate pays that interpreter's
  import cost (tree-sitter, sqlite-vec) on every run. Any measurement of gate
  wall-clock must say which interpreter produced it, and must not compare a
  code-learner gate run against a `sample_repo` run made with swarm-sync's own
  interpreter without noting the difference.
* Measured for reference on this machine (2026-07-29): `index_repo` over
  code-learner is ~0.05s, `build_graph` ~0.09s, `classifier.store.run_index`
  ~0.15s end to end; code-learner's own 252-test suite is ~18s. So a full
  `scale_blackboard()` setup is ~0.3s and the cost of these tests is entirely in
  the gate's pytest runs.

BENIGN / BREAKING EDIT TARGETS
==============================
`BENIGN_EDITS` and `BREAK_TARGET` below are verified against code-learner's real
import graph and its real suite (see the comments on each). Reuse them rather
than picking new ones blind: a "benign" edit that actually breaks a test, or a
"breaking" edit no selected test exercises, silently turns a hypothesis vacuous.
"""
from __future__ import annotations

import collections
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from fastapi.testclient import TestClient

from swarmsync.agent import mutators
from swarmsync.agent.client import BlackboardClient
from swarmsync.coordinator import broker, gate
from swarmsync.server.app import create_app

# --- the repo under test ----------------------------------------------------------

CODE_LEARNER_REPO = Path("/home/keith/projects/code-learner")
CODE_LEARNER_PYTHON = CODE_LEARNER_REPO / ".venv" / "bin" / "python"

# `worktree.git_ops.init_repo`'s convention, and the default `into` for both
# `agent.client.integrate` and `coordinator.integrator.integrate`. A clone comes
# in on `master`, so `_prepare_clone` creates this branch explicitly -- without it
# every /integrate would fail on a missing ref.
TRUNK = "integration"

# Measured on 2026-07-29 against code-learner @ 661d91b (`classifier.store.run_index`).
# The execution plan states "380 parcels / 66 contracts / 34 modules"; the first two
# numbers are STALE -- the real index is 567 parcels over 45 files (34 source modules
# + 11 test files). Recorded here rather than quietly asserted away, because a harness
# that "expects ~380" and sees 567 has either indexed the wrong tree or the plan is out
# of date, and those are very different problems.
MEASURED_PARCELS = 567

# Contracts went 86 -> 94 when `build_graph`'s `from <pkg> import <submodule>`
# misattribution was fixed. Those 8 are contracts that SHOULD always have frozen: the
# bug credited their dependents to the package `__init__` instead, so their
# blast_radius never reached FREEZE_THRESHOLD and signature changes to them were
# announced to nobody. `codelearner/db.py` went from blast_radius 3 to 279 -- from
# barely-at-threshold to the highest in the repo, which is what it always was.
# Deliberately a hard number, not a range: it is the observable that moved when the
# defect was fixed, and a silent drift back to 86 would mean the defect returned.
MEASURED_CONTRACTS = 94
MEASURED_FILES = 45
MEASURED_SOURCE_MODULES = 34
MEASURED_TEST_FILES = 11
PLAN_STATED_PARCELS = 380
PLAN_STATED_CONTRACTS = 66

# Deliberately loose: this is a "did we index the real repo, not an empty dir or
# swarm-sync itself" gate, not a pin on code-learner's exact contents.
PARCEL_COUNT_RANGE = (500, 650)
CONTRACT_COUNT_MIN = 60

# --- edit targets, verified against the real import graph + the real suite ---------

# Two semantics-PRESERVING rewrites. Both were applied to a clone and code-learner's
# full 252-test suite was re-run green (2026-07-29), so a rejection of either is a
# real signal, not a mis-chosen fixture. Both files have genuine reverse-dependents:
#   codelearner/ingest/types.py   -> 18 source files, 10 of 11 test files (transitively)
#   codelearner/cli/render.py     -> cli/commands, cli/main, server/app; 2 test files
BENIGN_EDITS: tuple[dict[str, str], ...] = (
    {
        "path": "codelearner/ingest/types.py",
        "symbol": "content_hash",
        "new_body": "digest = hashlib.sha256(source)\nreturn digest.hexdigest()",
    },
    {
        "path": "codelearner/cli/render.py",
        "symbol": "facts_only",
        "new_body": (
            "kept = [hit for hit in hits if tier_of(hit) <= TIER_RESOLVED]\nreturn kept"
        ),
    },
)

# The breaking target. `codelearner/retrieve/lexical.py` has REAL dependents, verified
# from the import graph rather than assumed: `dense.py`, `fuse.py`, `graph.py`,
# `rerank.py` and `search.py` all import from it (`from .lexical import Hit,
# search_lexical`), and `gate._reverse_dep_files` reaches 7 of the 11 test files from
# it. `escape_fts_query` in particular is called by `search_lexical` (same module) and
# asserted directly by `tests/test_chunk.py::test_escape_fts_query_quotes_every_term`,
# so making it raise fails tests in test_chunk.py, test_rerank.py AND test_retrieve.py
# -- including tests whose own source never names `escape_fts_query`, i.e. genuinely
# indirect dependents (confirmed: 8+ failures on a trial run).
BREAK_TARGET = {
    "path": "codelearner/retrieve/lexical.py",
    "symbol": "escape_fts_query",
}

# A sentinel that appears NOWHERE in code-learner, so "is the broken content in this
# blob?" is an exact string search with no chance of a false positive.
BREAK_SENTINEL = "SWARMSYNC-SCALE-H1-POISON-3f9c1d"

# How often the worktree sampler looks (seconds). Small enough to catch a wave whose
# tasks each live for hundreds of ms, cheap enough to be free.
_SAMPLE_INTERVAL = 0.02

# Terminal event types that close an `integrate_started` (they carry `started_seq`).
INTEGRATE_TERMINAL_TYPES = ("merged", "merge_rejected", "integrate_orphaned")


# --- module-level inspection helpers (the plan's literal surface) ------------------


def _git(repo: Path | str, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} in {repo} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def events(conn: sqlite3.Connection) -> list[dict]:
    """Every event, in seq order, read straight off the blackboard.

    Rows are the `events` table's own shape (`seq`, `agent_id`, `type`, `payload`,
    `ts`) with one addition: `data`, the parsed `payload` (`{}` when NULL or
    unparseable). `payload` itself is left as the raw JSON string so this is a
    drop-in for what `GET /events` returns.
    """
    rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    out: list[dict] = []
    for row in rows:
        record = dict(row)
        raw = record.get("payload")
        try:
            record["data"] = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            record["data"] = {}
        out.append(record)
    return out


def trunk_log(repo: Path | str, ref: str = TRUNK) -> list[str]:
    """`<short sha> <subject>` for every commit on `ref`, newest first."""
    out = _git(repo, "log", "--format=%h %s", ref)
    return [line for line in out.splitlines() if line]


def reflog(repo: Path | str, ref: str = TRUNK) -> list[dict]:
    """`ref`'s reflog, newest first: `{"sha", "selector", "message"}` per entry.

    This is the ONLY place that can tell "never landed" from "landed then
    reverted": `git reset --hard` moves the ref back but cannot remove the entry
    recording that it was ever moved forward.
    """
    out = _git(repo, "reflog", "show", "--format=%H%x09%gd%x09%gs", ref, check=False)
    entries: list[dict] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        entries.append(
            {
                "sha": parts[0],
                "selector": parts[1] if len(parts) > 1 else "",
                "message": parts[2] if len(parts) > 2 else "",
            }
        )
    return entries


def worktrees(repo: Path | str) -> list[str]:
    """Every worktree git currently knows about (`git worktree list --porcelain`).

    The first entry is always the main checkout, so a clean run returns exactly
    one path: the trunk checkout itself.
    """
    out = _git(repo, "worktree", "list", "--porcelain")
    return [
        line.split(" ", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def is_ancestor(repo: Path | str, sha: str, ref: str = TRUNK) -> bool:
    """Whether `sha` is in `ref`'s history. False for a commit that only ever
    existed in the reflog -- which is exactly how a rolled-back merge looks."""
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, ref],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def blob_at(repo: Path | str, sha: str, path: str) -> Optional[str]:
    """`path`'s content at commit `sha`, or None if it did not exist there."""
    proc = subprocess.run(
        ["git", "show", f"{sha}:{path}"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def reflog_hits(
    repo: Path | str, needle: str, path: str, ref: str = TRUNK
) -> list[str]:
    """Shas in `ref`'s reflog whose version of `path` contains `needle`.

    Empty means the content named by `needle` was NEVER on `ref`, at any point --
    the strong form of "the gate rejected it". Non-empty with a clean current
    trunk means it landed and was reverted, which is a different (much larger)
    blast radius and is NOT what the README claims.
    """
    hits: list[str] = []
    for entry in reflog(repo, ref):
        content = blob_at(repo, entry["sha"], path)
        if content is not None and needle in content:
            hits.append(entry["sha"])
    return hits


# --- event-log analysis (H4) -------------------------------------------------------


def integrate_spans(event_rows: Sequence[dict]) -> list[dict]:
    """One record per `integrate_started`, with its terminal event resolved.

    Pairing is by the terminal event's `started_seq` payload field -- NOT by
    branch/repo name, which a reused broker attempt id would make ambiguous
    (`integrator.integrate` records `started_seq` for exactly this reason).
    `{"start_seq", "branch", "end_seq", "end_type"}`; `end_seq`/`end_type` are
    None for an integrate that never reached a verdict.
    """
    starts: dict[int, dict] = {}
    order: list[int] = []
    for row in event_rows:
        if row["type"] == "integrate_started":
            starts[row["seq"]] = {
                "start_seq": row["seq"],
                "branch": row["data"].get("branch"),
                "end_seq": None,
                "end_type": None,
            }
            order.append(row["seq"])
        elif row["type"] in INTEGRATE_TERMINAL_TYPES:
            start_seq = row["data"].get("started_seq")
            span = starts.get(start_seq) if start_seq is not None else None
            if span is not None and span["end_seq"] is None:
                span["end_seq"] = row["seq"]
                span["end_type"] = row["type"]
    return [starts[s] for s in order]


def serialization_violations(event_rows: Sequence[dict]) -> list[str]:
    """Every place the event log shows two integrations overlapping. Empty == serial.

    Walks the log in seq order with a single "currently open" slot. A second
    `integrate_started` arriving while one is open is an overlap; a terminal event
    naming a `started_seq` that is not the open one is a mis-pairing. Both are
    reported rather than raised so a caller can assert on the whole list.

    Deliberately structural: it proves NO TWO INTEGRATIONS INTERLEAVE from
    ordering alone, never from the absence of corruption (trunk could be
    accidentally fine after a race).
    """
    problems: list[str] = []
    open_seq: Optional[int] = None
    for row in event_rows:
        if row["type"] == "integrate_started":
            if open_seq is not None:
                problems.append(
                    f"integrate_started seq={row['seq']} "
                    f"(branch={row['data'].get('branch')!r}) began while "
                    f"seq={open_seq} was still open"
                )
            open_seq = row["seq"]
        elif row["type"] in INTEGRATE_TERMINAL_TYPES:
            start_seq = row["data"].get("started_seq")
            if start_seq is None:
                continue  # e.g. the pre-start needs_rebase path: no start to close
            if open_seq is None:
                problems.append(
                    f"{row['type']} seq={row['seq']} closes started_seq={start_seq} "
                    "but no integrate was open"
                )
            elif start_seq != open_seq:
                problems.append(
                    f"{row['type']} seq={row['seq']} closes started_seq={start_seq} "
                    f"but the open integrate was seq={open_seq}"
                )
                continue
            open_seq = None
    return problems


# --- task builders ----------------------------------------------------------------


def edit_task(
    task_id: str,
    path: str,
    symbol: str,
    new_body: str,
    base_commit: Optional[str] = None,
    **task_kwargs: Any,
) -> broker.Task:
    return broker.Task(
        task_id=task_id,
        targets=[(path, symbol)],
        mutator=mutators.edit_function_body,
        mutator_kwargs={"path": path, "symbol": symbol, "new_body": new_body},
        base_commit=base_commit,
        **task_kwargs,
    )


def break_task(
    task_id: str,
    path: str,
    symbol: str,
    message: str = BREAK_SENTINEL,
    base_commit: Optional[str] = None,
    **task_kwargs: Any,
) -> broker.Task:
    """`break_a_test`: rewrite `symbol` to `raise RuntimeError(message)`.

    `message` doubles as the reflog search needle, so keep it unique.
    """
    return broker.Task(
        task_id=task_id,
        targets=[(path, symbol)],
        mutator=mutators.break_a_test,
        mutator_kwargs={"path": path, "symbol": symbol, "message": message},
        base_commit=base_commit,
        **task_kwargs,
    )


def signature_task(
    task_id: str,
    path: str,
    symbol: str,
    new_sig: str,
    base_commit: Optional[str] = None,
    read_deps: Sequence[str] = (),
    **task_kwargs: Any,
) -> broker.Task:
    return broker.Task(
        task_id=task_id,
        targets=[(path, symbol)],
        mutator=mutators.change_signature,
        mutator_kwargs={"path": path, "symbol": symbol, "new_sig": new_sig},
        base_commit=base_commit,
        read_deps=list(read_deps),
        **task_kwargs,
    )


def hang_task(
    task_id: str,
    path: str,
    symbol: str,
    new_body: str,
    base_commit: Optional[str] = None,
    **task_kwargs: Any,
) -> broker.Task:
    """`slow_edit(hang=True)`: writes the edit to disk, THEN blocks forever, so a
    SIGKILL during the hang leaves genuine uncommitted work in the worktree."""
    return broker.Task(
        task_id=task_id,
        targets=[(path, symbol)],
        mutator=mutators.slow_edit,
        mutator_kwargs={
            "path": path,
            "symbol": symbol,
            "new_body": new_body,
            "hang": True,
        },
        base_commit=base_commit,
        **task_kwargs,
    )


# --- gate interpreter override ------------------------------------------------------


class _InterpreterShim:
    """`sys` with `executable` replaced. Everything else falls through to the real
    module, so this cannot quietly break some other `sys` use in `gate.py`."""

    def __init__(self, real: Any, executable: str) -> None:
        self._real = real
        self.executable = executable

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@contextlib.contextmanager
def gate_interpreter(python: Path | str) -> Iterator[None]:
    """Make `gate.run_impact_tests` spawn `python` instead of `sys.executable`.

    See this module's docstring for why this is required rather than optional.
    Restores the real `sys` on the way out, always.
    """
    real = gate.sys
    gate.sys = _InterpreterShim(real, str(python))  # type: ignore[assignment]
    try:
        yield
    finally:
        gate.sys = real  # type: ignore[assignment]


# --- the fixture object -------------------------------------------------------------


@dataclass
class ScaleRepo:
    """Everything one scale run needs. See the module docstring for the interface."""

    root: Path
    base_commit: str
    db_path: Path
    conn: sqlite3.Connection
    client: BlackboardClient
    http: TestClient
    gate_python: Path
    parcel_count: int = 0
    contract_count: int = 0
    peak_worktrees: int = 0
    setup_seconds: float = 0.0
    watch_hits: list[dict] = dataclasses_field(default_factory=list)

    # --- driving the broker ---------------------------------------------------

    def run(
        self,
        tasks: list[broker.Task],
        n_agents: int = 4,
        watch: Optional[tuple[str, str]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """`broker.run` with `db_path` wired (per-worker connections, WP4.6/A7),
        the concurrent-worktree sampler running, and -- if `watch=(relpath,
        needle)` -- a read-only trunk watcher (see the module docstring)."""
        kwargs.setdefault("db_path", self.db_path)
        with self._sampling(), self._watching(watch):
            return broker.run(
                self.conn, self.root, tasks, self.client, n_agents=n_agents, **kwargs
            )

    @property
    def watch_window_seconds(self) -> float:
        """Wall-clock span over which the watched needle was visible on trunk."""
        if len(self.watch_hits) < 2:
            return 0.0
        return self.watch_hits[-1]["monotonic"] - self.watch_hits[0]["monotonic"]

    @contextlib.contextmanager
    def _watching(self, watch: Optional[tuple[str, str]]) -> Iterator[None]:
        if watch is None:
            yield
            return
        relpath, needle = watch
        target = self.root / relpath
        stop = threading.Event()

        def poll() -> None:
            while not stop.wait(_SAMPLE_INTERVAL):
                try:
                    on_disk = needle in target.read_text(encoding="utf-8")
                except OSError:
                    continue
                if not on_disk:
                    continue
                # Only now (cheap path first) ask git where trunk points and
                # whether the COMMIT -- not merely the working tree -- carries it.
                head = _git(self.root, "rev-parse", TRUNK, check=False).strip()
                blob = blob_at(self.root, TRUNK, relpath) or "" if head else ""
                self.watch_hits.append(
                    {
                        "monotonic": time.monotonic(),
                        "head_sha": head,
                        "in_worktree": True,
                        "in_head_commit": needle in blob,
                    }
                )

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2.0)

    @contextlib.contextmanager
    def _sampling(self) -> Iterator[None]:
        stop = threading.Event()
        wt_dir = self.root / ".worktrees"

        def sample() -> None:
            while not stop.wait(_SAMPLE_INTERVAL):
                try:
                    live = sum(1 for p in wt_dir.iterdir() if p.is_dir())
                except OSError:
                    live = 0
                if live > self.peak_worktrees:
                    self.peak_worktrees = live

        thread = threading.Thread(target=sample, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2.0)

    # --- inspection ------------------------------------------------------------

    def events(self) -> list[dict]:
        return events(self.conn)

    def events_by_type(self) -> collections.Counter:
        return collections.Counter(row["type"] for row in self.events())

    def trunk_log(self, ref: str = TRUNK) -> list[str]:
        return trunk_log(self.root, ref)

    def reflog(self, ref: str = TRUNK) -> list[dict]:
        return reflog(self.root, ref)

    def blob_at(self, sha: str, path: str) -> Optional[str]:
        return blob_at(self.root, sha, path)

    def is_ancestor(self, sha: str, ref: str = TRUNK) -> bool:
        return is_ancestor(self.root, sha, ref)

    def reflog_hits(self, needle: str, path: str, ref: str = TRUNK) -> list[str]:
        return reflog_hits(self.root, needle, path, ref)

    def worktrees(self) -> list[str]:
        return worktrees(self.root)

    def worktree_residue(self) -> list[str]:
        wt_dir = self.root / ".worktrees"
        if not wt_dir.exists():
            return []
        return sorted(p.name for p in wt_dir.iterdir())

    def file_text(self, relpath: str) -> str:
        return (self.root / relpath).read_text(encoding="utf-8")

    def branches(self) -> list[str]:
        out = _git(self.root, "branch", "--format=%(refname:short)")
        return [line.strip() for line in out.splitlines() if line.strip()]

    # --- the repo's OWN suite, on ITS OWN interpreter ---------------------------

    def suite_green(self, timeout: float = 900.0) -> tuple[bool, str]:
        """Run code-learner's full suite on the trunk checkout with code-learner's
        3.12 venv (NEVER swarm-sync's 3.11), using the same pytest flags the gate
        uses. Returns `(ok, log_tail)`.

        pytest exit 5 ("no tests collected") is NOT counted as green here -- unlike
        in the gate, where nothing-to-gate-on is legitimately not a rejection
        reason. If this call collects no tests, the check has silently stopped
        being a check.
        """
        cmd = [
            str(self.gate_python),
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--import-mode=importlib",
            "tests",
        ]
        env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        for var in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST"):
            env.pop(var, None)
        proc = subprocess.run(
            cmd,
            cwd=str(self.root),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        log = (proc.stdout + proc.stderr)[-6000:]
        return proc.returncode == 0, log


# --- setup / teardown ----------------------------------------------------------------


def _prepare_clone(workdir: Path) -> tuple[Path, str]:
    """Clone code-learner into `workdir` and make it a swarm-sync-shaped trunk.

    `--no-hardlinks` so nothing we do can reach back into the source object store.
    A repo-local commit identity (never global) and `.worktrees/` in `.gitignore`
    mirror what `git_ops.init_repo` sets up for the fixtures, so the broker's
    worktrees never show up as untracked changes in a merge.
    """
    root = workdir / "code-learner"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(CODE_LEARNER_REPO), str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(root, "checkout", "-q", "-b", TRUNK)
    _git(root, "config", "user.email", "swarm-sync@example.local")
    _git(root, "config", "user.name", "swarm-sync")
    _git(root, "config", "commit.gpgsign", "false")
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".worktrees/" not in existing:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        gitignore.write_text(existing + ".worktrees/\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "swarm-sync scale harness: trunk base")
    base = _git(root, "rev-parse", "HEAD").strip()
    return root, base


def _assert_gate_tests_the_clone(root: Path, python: Path) -> None:
    """Prove the gate's pytest run imports the CLONE's `codelearner`, not the
    editable install pointing at the real checkout.

    code-learner's venv has an editable install of `/home/keith/projects/code-learner`.
    If that shadowed the clone, the gate would test the pristine source no matter
    what the agents wrote -- every merge would pass, and every "trunk green"
    assertion would be measuring the wrong tree. This is cheap; skipping it is not.
    """
    proc = subprocess.run(
        [str(python), "-c", "import codelearner; print(codelearner.__file__)"],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    resolved = proc.stdout.strip()
    if proc.returncode != 0 or not resolved.startswith(str(root)):
        raise AssertionError(
            "the gate's interpreter does not import the CLONE's codelearner "
            f"(got {resolved!r}, expected something under {root}); every gate "
            "verdict would be about the wrong tree.\n"
            f"stderr: {proc.stderr.strip()[-500:]}"
        )


def _teardown_worktrees(root: Path) -> None:
    """Remove every non-main worktree, prune, and delete `.worktrees/` residue."""
    try:
        registered = worktrees(root)
    except Exception:  # noqa: BLE001 -- teardown must never raise
        registered = []
    for path in registered[1:]:
        _git(root, "worktree", "remove", "--force", "--", path, check=False)
    _git(root, "worktree", "prune", check=False)
    shutil.rmtree(root / ".worktrees", ignore_errors=True)


@contextlib.contextmanager
def scale_blackboard(
    workdir: Optional[Path] = None,
    reaper_interval: Optional[float] = None,
    gate_python: Path = CODE_LEARNER_PYTHON,
    index: bool = True,
) -> Iterator[ScaleRepo]:
    """Stand up a blackboard bound to a fresh clone of code-learner.

    `reaper_interval=None` (the default) disables the background reap/decay loop,
    matching `tests/test_broker.py`: the broker's own retry loop drives
    `reap_once` explicitly, and a real-time loop racing the test body only adds
    nondeterminism. Pass a float if you specifically need the background reaper
    (H6's lease reclamation).

    `index=False` skips `POST /index` (and therefore the parcel-count assertions)
    for a caller that wants to index by hand.

    Teardown ALWAYS runs, in order: stop the sampler, exit the TestClient
    lifespan (which closes the blackboard connection), remove every worktree,
    delete the temp dir, restore the gate interpreter and `SWARMSYNC_ROOTS`.
    """
    t0 = time.perf_counter()
    owns_workdir = workdir is None
    # Under gettempdir() on purpose: `tests/conftest.py`'s autouse fixture points
    # SWARMSYNC_ROOTS there for every test, and `POST /index` + `POST /integrate`
    # 403 any path outside the managed roots.
    base_dir = Path(tempfile.mkdtemp(prefix="swarmsync-scale-")) if owns_workdir else Path(workdir)
    previous_roots = os.environ.get("SWARMSYNC_ROOTS")
    # Set it here rather than relying on the autouse conftest fixture: a
    # MODULE-scoped fixture is built before any function-scoped one, so at setup
    # time the conftest monkeypatch has not been applied yet. Same value it uses,
    # so the two never disagree.
    os.environ["SWARMSYNC_ROOTS"] = tempfile.gettempdir()

    root: Optional[Path] = None
    scale: Optional[ScaleRepo] = None
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(gate_interpreter(gate_python))
        root, base = _prepare_clone(base_dir)
        _assert_gate_tests_the_clone(root, gate_python)

        db_path = base_dir / "blackboard.db"
        app = create_app(db_path, reaper_interval=reaper_interval)
        http = stack.enter_context(TestClient(app))
        client = BlackboardClient(http)

        parcels = contracts = 0
        if index:
            resp = http.post("/index", json={"root": str(root)})
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            parcels, contracts = payload["parcels"], payload["contracts"]
            low, high = PARCEL_COUNT_RANGE
            assert low <= parcels <= high, (
                f"indexed {parcels} parcels from {root}, expected {low}..{high} "
                f"(measured {MEASURED_PARCELS} for code-learner @ 661d91b; the "
                f"execution plan's {PLAN_STATED_PARCELS} is stale). Did we index "
                "the right tree?"
            )
            assert contracts >= CONTRACT_COUNT_MIN, (
                f"only {contracts} frozen contracts (expected >= {CONTRACT_COUNT_MIN}; "
                f"measured {MEASURED_CONTRACTS})"
            )

        scale = ScaleRepo(
            root=root,
            base_commit=base,
            db_path=db_path,
            conn=app.state.conn,
            client=client,
            http=http,
            gate_python=Path(gate_python),
            parcel_count=parcels,
            contract_count=contracts,
        )
        scale.setup_seconds = time.perf_counter() - t0
        yield scale
    finally:
        # Closes the TestClient lifespan (reaper stopped, app.state.conn closed)
        # and restores the gate interpreter, in reverse order of entry.
        with contextlib.suppress(Exception):
            stack.close()
        if root is not None:
            with contextlib.suppress(Exception):
                _teardown_worktrees(root)
        if owns_workdir:
            shutil.rmtree(base_dir, ignore_errors=True)
        if previous_roots is None:
            os.environ.pop("SWARMSYNC_ROOTS", None)
        else:
            os.environ["SWARMSYNC_ROOTS"] = previous_roots


__all__ = [
    "BENIGN_EDITS",
    "BREAK_SENTINEL",
    "BREAK_TARGET",
    "CODE_LEARNER_PYTHON",
    "CODE_LEARNER_REPO",
    "CONTRACT_COUNT_MIN",
    "INTEGRATE_TERMINAL_TYPES",
    "MEASURED_CONTRACTS",
    "MEASURED_FILES",
    "MEASURED_PARCELS",
    "MEASURED_SOURCE_MODULES",
    "MEASURED_TEST_FILES",
    "PARCEL_COUNT_RANGE",
    "PLAN_STATED_CONTRACTS",
    "PLAN_STATED_PARCELS",
    "ScaleRepo",
    "TRUNK",
    "blob_at",
    "break_task",
    "edit_task",
    "events",
    "gate_interpreter",
    "hang_task",
    "integrate_spans",
    "is_ancestor",
    "reflog",
    "reflog_hits",
    "scale_blackboard",
    "serialization_violations",
    "signature_task",
    "trunk_log",
    "worktrees",
]
