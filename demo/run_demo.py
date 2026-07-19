"""End-to-end demo: the five test cases. DESIGN.md §7.

Built in Unit U14 (test cases #1, #2, #4, #5); test case #3 (frozen-contract
change + dependent re-plan, DESIGN §5.3) was added on top of this file by U15.
All five test cases now print PASS/FAIL and the final summary says so.

This demo drives the SHIPPING product: file-granularity locking. Every lease is
a whole-file `<file>::<module>` lock (symbol granularity is parked -- see
SYMBOL_MODE_DESIGN.md), so `broker.run` is always called at its `mode="file"`
default and every claim printed below is true of file-granularity coordination.

Flow:
  1. copy `sample_repo/` into a fresh temp dir and `git init` it as the shared
     "integration" trunk checkout (`worktree.git_ops.init_repo`).
  2. init the blackboard DB and boot a REAL `uvicorn` server (in a background
     thread, in this same process) -- test case #4 needs an agent that lives
     in a genuinely separate OS process to SIGKILL, which only proves anything
     if the blackboard is reachable over a real socket rather than FastAPI's
     in-process TestClient/ASGI transport. Every test case below (not just #4)
     therefore talks HTTP over a real port, exercising the actual wire protocol.
  3. `POST /index` to populate parcels + contracts from the fresh repo.
  4. run each scripted scenario, printing the event stream and a PASS/FAIL line
     per assertion, then a final summary block.

Test cases exercised here (DESIGN §7):
  #1 THREE agents edit three DIFFERENT files -- `calc.py`'s `sub`,
     `formats.py`'s `money`, `api.py`'s `apply_discount` -- concurrently, each
     under its own whole-file `<file>::<module>` lease (file granularity, the
     shipping default) -> three `merged` events, zero conflicts, all three
     edits present on trunk. Proven with a genuine wall-clock overlap check
     (there is an instant at which all three edits are simultaneously in
     flight, not just "all three eventually ran"), mirroring the same technique
     `tests/test_broker.py` already uses and validates. Three files means three
     distinct whole-file locks, so file granularity is enough for real
     concurrency here -- no symbol-level locking needed.
  #2 a task targeting `calc.py`'s `div` contends against another agent already
     holding calc.py's WHOLE-FILE write-lease (`calc.py::<module>` -- at file
     granularity the whole file is one lock; a stand-in for "another agent is
     already editing this file") -> `lease_denied`, then -- after the holder
     releases (not crashes; that's #4's story) -- `lease_granted` + `merged`
     for the same whole-file parcel. Driven through `broker.run`'s own retry
     loop.
  #4 a REAL subprocess (`demo/_crash_agent.py`) is SIGKILLed while holding a
     whole-file write-lease (`formats.py::<module>`) and mid-`mutators.slow_edit`
     (real, uncommitted work already on disk in its own worktree) -> the reaper
     (running in this process's own live server) reclaims the lease past its
     TTL, emits `reaped`; a fresh agent is dispatched for the same task and
     lands it; trunk never contains the dead process's edit.
  #5 `mutators.break_a_test` deliberately breaks `api.py::summarize`'s
     behavior (under a whole-file `api.py::<module>` lease) -> the integrator's
     impact-selected pytest gate on `test_api.py` fails -> `merge_rejected`,
     the merge commit is reset out, trunk stays green and unchanged.
  #3 (U15) one task changes `calc.py::add`'s frozen signature (`def add(a, b)`
     -> `def add(a, b, rounding=None)`, `mutators.change_signature`) under a
     whole-file write-lease on `calc.py::<module>`. NOTE the frozen-contract
     EXCLUSIVE-lease upgrade (DESIGN §5.3) is INERT at file granularity --
     contracts are symbol ids (`calc.py::add`) while every lease is a file id
     (`calc.py::<module>`), so no target is ever in `frozen_ids` -- and is
     parked alongside symbol mode (SYMBOL_MODE_DESIGN.md). Contract DETECTION,
     though, is granularity-INDEPENDENT and still ships: once the signature
     change lands, the integrator's own before/after `type_hash` diff (U15,
     `coordinator/integrator.py`) emits a real `contract_change` event
     regardless of lease granularity. Because the auto-scheduling that once
     forced the change ahead of its dependents (`co_schedulable`'s frozen
     clause) is inert too, the demo instead dispatches the signature change
     FIRST (its own `broker.run`), lets it LAND and announce `contract_change`,
     THEN dispatches the two real dependents that call `calc.add`
     (`formats.py::total_with_tax`, `api.py::summarize`) in a second
     `broker.run`. Each declares `calc.py::add` as a `read_deps` contract, so
     its `run_agent` call re-reads it (`AgentResult.contract_snapshot`) and
     sees the NEW signature -- fetched from live state AFTER the change landed,
     not a hardcoded guess -- then lands a `mutators.fix_call_site` call-site
     fix (`rounding=2`). The new parameter is optional, so nothing ever breaks;
     the demo proves the NOTIFY + RE-PLAN protocol, not a crash.

OVERALL: zero same-file textual collisions (git merge conflicts) reached
`integration` across the whole run, every landed commit left `sample_repo`'s
test suite green, and all five test case assertions print PASS.
Exit code is 0 iff every check above passed.
"""
from __future__ import annotations

import json
import shutil
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn

from swarmsync import config
from swarmsync.agent import mutators
from swarmsync.agent.client import BlackboardClient
from swarmsync.agent.runner import run_agent
from swarmsync.coordinator import broker
from swarmsync.blackboard import leases as leases_mod
from swarmsync.server.app import create_app
from swarmsync.worktree import git_ops

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_REPO_SRC = REPO_ROOT / "sample_repo"
CRASH_AGENT_SCRIPT = Path(__file__).resolve().with_name("_crash_agent.py")

SHOT_LABELS = {
    "shot1": "test case #1 (three agents on three files land concurrently, clean)",
    "shot2": "test case #2 (contended whole-file parcel serializes)",
    "shot3": "test case #3 (frozen-contract change notifies + dependent re-plans)",
    "shot4": "test case #4 (crash mid-edit is recovered)",
    "shot5": "test case #5 (serial gated integration rejects a bad edit)",
    "overall": "overall (zero collisions, trunk green throughout)",
}
SHOT_ORDER = ["shot1", "shot2", "shot3", "shot4", "shot5", "overall"]


# --- small infra: a real live server + a PASS/FAIL reporter -----------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerThread:
    """A real `uvicorn` server for `app`, running in a background thread.

    Test case #4 needs an agent process this demo can genuinely SIGKILL
    without taking the coordinator down with it -- that only works if agents
    talk to the blackboard over a real socket, so every test case in this
    file (not only #4) is driven over HTTP against this one server.
    """

    def __init__(self, app: Any, host: str = "127.0.0.1", port: Optional[int] = None) -> None:
        self.host = host
        self.port = port if port is not None else _free_port()
        config = uvicorn.Config(app, host=host, port=self.port, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 10.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if getattr(self.server, "started", False):
                return
            time.sleep(0.02)
        raise RuntimeError("uvicorn server did not report started in time")

    def stop(self, timeout: float = 10.0) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=timeout)


class _Reporter:
    """Prints a `[PASS]`/`[FAIL]` line per assertion and ANDs outcomes per shot."""

    def __init__(self) -> None:
        self.results: dict[str, bool] = {}

    def check(self, shot: str, label: str, ok: bool, detail: str = "") -> bool:
        status = "PASS" if ok else "FAIL"
        line = f"  [{status}] {label}"
        if detail and not ok:
            line += f" -- {detail}"
        print(line)
        self.results[shot] = self.results.get(shot, True) and ok
        return ok


# --- setup helpers -----------------------------------------------------------------


def _setup_repo(workdir: Path) -> tuple[Path, str]:
    repo = workdir / "repo"
    shutil.copytree(SAMPLE_REPO_SRC, repo)
    base = git_ops.init_repo(repo)
    return repo, base


def _index(base_url: str, repo: Path) -> None:
    resp = httpx.post(f"{base_url}/index", json={"root": str(repo)})
    resp.raise_for_status()


def _run_full_suite(repo: Path) -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    ok = result.returncode in (0, 5)
    return ok, result.stdout + result.stderr


def _wait_for_event(
    client: BlackboardClient,
    event_type: str,
    agent_id: Optional[str] = None,
    timeout: float = 10.0,
    poll: float = 0.1,
) -> Optional[dict]:
    """Poll `GET /events` until an event of `event_type` (optionally filtered by
    `agent_id`) shows up, or give up after `timeout` seconds and return `None`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for ev in client.events(since=0):
            if ev["type"] != event_type:
                continue
            if agent_id is not None and ev["agent_id"] != agent_id:
                continue
            return ev
        time.sleep(poll)
    return None


def _print_event_stream(client: BlackboardClient) -> None:
    print("\n=== event stream (heartbeats omitted) ===")
    for ev in client.events(since=0):
        if ev["type"] == "heartbeat":
            continue
        payload = ev.get("payload")
        agent = ev["agent_id"] if ev["agent_id"] is not None else "-"
        print(f"  seq={ev['seq']:>4}  {ev['type']:<16} agent={agent:<28} {payload}")


# --- test case #1: three agents on three DIFFERENT files, concurrently -------------

_timeline_lock = threading.Lock()

# Whole-file lock ids (file granularity, the shipping default) for the parcels
# this demo contends on directly.
CALC_MODULE = "calc.py::<module>"

# Test case #1's three concurrent tasks -- one per file, each a real,
# behavior-preserving edit its own test suite still passes:
#   calc.py::sub   -> body rewritten, still `a - b`   (test_sub: sub(5,3)==2)
#   formats.py::money -> body rewritten, same output  (test_money: money(12.5)=="$12.50")
#   api.py::apply_discount -> body rewritten, same    (test_apply_discount: ...==$180.00)
SHOT1_EDITS = [
    ("shot1-calc", "calc.py", "sub", "result = a - b\nreturn result", "calc.py::sub body"),
    ("shot1-formats", "formats.py", "money",
     'formatted = f"{currency}{amount:.2f}"\nreturn formatted', "formats.py::money body"),
    ("shot1-api", "api.py", "apply_discount",
     "discount = calc.mul(price, discount_pct / 100)\n"
     "return formats.money(calc.sub(price, discount))", "api.py::apply_discount body"),
]


def _timed_edit(worktree, path, symbol, new_body, label, timeline, hold=0.0):
    """`edit_function_body`, with (label, "start"/"end", monotonic-clock) entries
    recorded around it so the demo can PROVE the concurrent edits' wall-clock
    windows genuinely overlapped, not just "all eventually landed" (same
    technique `tests/test_broker.py` already uses)."""
    with _timeline_lock:
        timeline.append((label, "start", time.monotonic()))
    if hold:
        time.sleep(hold)
    mutators.edit_function_body(worktree, path, symbol, new_body)
    with _timeline_lock:
        timeline.append((label, "end", time.monotonic()))


def _run_shot1(conn, repo: Path, client: BlackboardClient, reporter: _Reporter) -> None:
    """Three agents edit three DIFFERENT files at once. At file granularity each
    file is one whole-file lock, so three files means three disjoint locks and
    the three tasks land in a SINGLE co-schedulable wave -- genuinely
    concurrent, zero contention, zero collisions."""
    timeline: list[tuple[str, str, float]] = []

    tasks = [
        broker.Task(
            task_id=task_id,
            targets=[(path, symbol)],
            mutator=_timed_edit,
            mutator_kwargs={
                "path": path,
                "symbol": symbol,
                "new_body": new_body,
                "label": task_id,
                "timeline": timeline,
                # A shared hold so all three edit-windows line up in wall-clock
                # time -- there must be an instant when all three are in flight.
                "hold": 0.5,
            },
        )
        for task_id, path, symbol, new_body, _desc in SHOT1_EDITS
    ]

    # No mode= -> file granularity (the shipping default). Three files, one wave.
    results = broker.run(conn, repo, tasks, client, n_agents=3, retry_backoff=0.4)

    events = client.events(since=0)

    all_merged = all(
        results[task_id].status == "done"
        and (results[task_id].integrate_result or {}).get("status") == "merged"
        for task_id, *_ in SHOT1_EDITS
    )
    reporter.check(
        "shot1", "all three agents (calc.py, formats.py, api.py) landed merged", all_merged
    )

    # Real concurrency: there is a single instant at which all three edit windows
    # are simultaneously open -- i.e. the latest start precedes the earliest end.
    by_label: dict[str, dict[str, float]] = {}
    for label, kind, ts in timeline:
        by_label.setdefault(label, {})[kind] = ts
    have_all = all(
        task_id in by_label and "start" in by_label[task_id] and "end" in by_label[task_id]
        for task_id, *_ in SHOT1_EDITS
    )
    overlapped = have_all and (
        max(by_label[t]["start"] for t, *_ in SHOT1_EDITS)
        < min(by_label[t]["end"] for t, *_ in SHOT1_EDITS)
    )
    reporter.check(
        "shot1",
        "all three edits were simultaneously in flight (real wall-clock concurrency)",
        overlapped,
    )

    calc_src = (repo / "calc.py").read_text()
    formats_src = (repo / "formats.py").read_text()
    api_src = (repo / "api.py").read_text()
    all_present = (
        "result = a - b" in calc_src
        and "formatted = f" in formats_src
        and "return formats.money(calc.sub(price, discount))" in api_src
    )
    reporter.check("shot1", "all three edits present on trunk (integration branch)", all_present)

    # Three whole-file locks, all disjoint -> exactly one dispatch wave.
    lease_parcels = {
        pid
        for task_id, *_ in SHOT1_EDITS
        for pid in results[task_id].lease_modes_used
    }
    reporter.check(
        "shot1",
        "each agent held its own whole-file lock (three disjoint <module> parcels)",
        lease_parcels == {"calc.py::<module>", "formats.py::<module>", "api.py::<module>"},
    )

    conflict_events = [e for e in events if e["type"] == "merge_rejected"]
    reporter.check(
        "shot1", "zero textual merge conflicts reached integration for this wave",
        not any(json.loads(e["payload"] or "{}").get("reason") == "merge_conflict" for e in conflict_events),
    )


# --- test case #2: a contended whole-file parcel serializes ------------------------


def _run_shot2(conn, repo: Path, client: BlackboardClient, reporter: _Reporter) -> None:
    """A task wanting calc.py's `div` contends against another agent already
    holding calc.py's WHOLE-FILE write-lease. At file granularity every lease is
    a `<file>::<module>` lock, so "the div task is blocked" is literally "the
    whole calc.py file is locked by someone else". The task is denied, backs
    off, and lands once the holder releases -- serialization, proven end to end.
    """
    # The "already editing calc.py" holder: a stand-in for another agent mid-edit
    # somewhere in calc.py when our div task tries for it. Long TTL (it must NOT
    # merely time out -- that's shot #4's story) + an explicit release on a delay
    # simulates "the holder finishes and releases".
    holder = leases_mod.acquire(
        conn, CALC_MODULE, "manual-editor", mode="write", ttl=20.0, intent="manual-calc-edit"
    )
    assert holder.granted, f"test setup: holder lease on {CALC_MODULE} must be granted"

    def _release_holder_later(delay: float) -> None:
        time.sleep(delay)
        leases_mod.release(conn, holder.lease_id, "manual-editor")

    releaser = threading.Thread(target=_release_holder_later, args=(1.5,), daemon=True)
    releaser.start()

    task_div = broker.Task(
        task_id="shot2-div",
        targets=[("calc.py", "div")],
        mutator=mutators.edit_function_body,
        mutator_kwargs={
            "path": "calc.py",
            "symbol": "div",
            "new_body": "quotient = a / b\nreturn quotient",
        },
        max_attempts=20,
    )

    results = broker.run(conn, repo, [task_div], client, n_agents=1, retry_backoff=0.4)
    releaser.join(timeout=5.0)

    events = client.events(since=0)

    div_ok = results["shot2-div"].status == "done" and (
        results["shot2-div"].integrate_result or {}
    ).get("status") == "merged"
    reporter.check("shot2", "the contended task (div) eventually landed merged", div_ok)

    div_denied = [
        e for e in events
        if e["type"] == "lease_denied" and json.loads(e["payload"] or "{}").get("parcel_id") == CALC_MODULE
    ]
    reporter.check(
        "shot2", "a real lease_denied was observed against calc.py's whole-file lock", len(div_denied) >= 1
    )

    div_granted = [
        e for e in events
        if e["type"] == "lease_granted" and json.loads(e["payload"] or "{}").get("parcel_id") == CALC_MODULE
        and e["agent_id"] != "manual-editor"
    ]
    reporter.check(
        "shot2", "the contending agent later acquired the SAME whole-file parcel", len(div_granted) >= 1
    )
    if div_denied and div_granted:
        reporter.check(
            "shot2", "ordering: lease_denied happened before the later lease_granted",
            div_denied[0]["seq"] < div_granted[-1]["seq"],
        )

    div_merged = [
        e for e in events
        if e["type"] == "merged" and json.loads(e["payload"] or "{}").get("branch", "").startswith("shot2-div")
    ]
    reporter.check("shot2", "a merged event landed for the contended parcel's branch", len(div_merged) >= 1)


# --- test case #3 (U15): frozen-contract change + dependent re-plan ----------------

CONTRACT_SYMBOL = "calc.py::add"
NEW_ADD_SIGNATURE = "def add(a, b, rounding=None)"


def _run_shot3(conn, repo: Path, client: BlackboardClient, reporter: _Reporter) -> None:
    """DESIGN §5.3/§7 test case #3: `calc.py::add` is sample_repo's registered
    frozen contract (U13's fixture design; `blast_radius >= FREEZE_THRESHOLD`
    across `formats.py`, `api.py`, and their own test suites). One task changes
    its signature; two more -- REAL dependents that already call `calc.add` --
    re-read the changed contract and fix their call sites to match.

    File-granularity note: the frozen-contract EXCLUSIVE-lease upgrade and
    `co_schedulable`'s frozen clause (both DESIGN §5.3/§3) are INERT here --
    contracts are symbol ids (`calc.py::add`) but every lease is a whole-file id
    (`calc.py::<module>`), so no target is ever in `frozen_ids`. Both are parked
    with symbol mode (SYMBOL_MODE_DESIGN.md). What still SHIPS -- and is what
    this shot proves -- is contract DETECTION, which is granularity-independent:
    the integrator's own before/after `type_hash` diff (`coordinator/
    integrator.py`) emits a real `contract_change` on the landed merge no matter
    the lease granularity.

    Since nothing auto-orders the change ahead of its dependents at file
    granularity, the demo sequences it explicitly: dispatch the signature change
    in its OWN `broker.run` first, let it LAND and announce `contract_change`,
    THEN dispatch the two dependents in a second `broker.run`. Each dependent
    declares `calc.py::add` as a `read_deps` contract, so it re-reads the
    contract from LIVE state (after the change landed and was re-indexed) and
    sees the NEW signature before fixing its own call site.

    The new parameter (`rounding=None`, unused by `add`'s own body) is
    deliberately backward-compatible: `sample_repo/tests/test_calc.py` calls
    `add(2, 3)` directly and must keep passing on the signature-change task's
    OWN impact-selected test gate -- this shot proves the NOTIFY + RE-PLAN
    protocol, not a crash-and-recover story (that is test case #4's job).
    """
    task_change = broker.Task(
        task_id="shot3-change-add-signature",
        targets=[("calc.py", "add")],
        mutator=mutators.change_signature,
        mutator_kwargs={"path": "calc.py", "symbol": "add", "new_sig": NEW_ADD_SIGNATURE},
    )
    task_fix_formats = broker.Task(
        task_id="shot3-fix-formats-call-site",
        targets=[("formats.py", "total_with_tax")],
        mutator=mutators.fix_call_site,
        mutator_kwargs={
            "path": "formats.py",
            "symbol": "total_with_tax",
            "old": "add(amount, mul(amount, tax_rate))",
            "new": "add(amount, mul(amount, tax_rate), rounding=2)",
        },
        read_deps=[CONTRACT_SYMBOL],
    )
    task_fix_api = broker.Task(
        task_id="shot3-fix-api-call-site",
        targets=[("api.py", "summarize")],
        mutator=mutators.fix_call_site,
        mutator_kwargs={
            "path": "api.py",
            "symbol": "summarize",
            "old": "calc.add(a, b)",
            "new": "calc.add(a, b, rounding=2)",
        },
        read_deps=[CONTRACT_SYMBOL],
    )

    # Wave 1: the signature change lands and announces `contract_change` FIRST
    # (its own broker.run -- see docstring on why the demo orders this by hand
    # at file granularity). Wave 2: the two dependents, now that the contract
    # has actually changed on trunk, re-read it and land their call-site fixes.
    change_results = broker.run(conn, repo, [task_change], client, n_agents=1)
    dependent_results = broker.run(
        conn, repo, [task_fix_formats, task_fix_api], client, n_agents=2
    )
    results = {**change_results, **dependent_results}
    events = client.events(since=0)

    change_result = results["shot3-change-add-signature"]
    change_landed = change_result.status == "done" and (
        change_result.integrate_result or {}
    ).get("status") == "merged"
    # At file granularity the change takes a whole-file WRITE lease on
    # calc.py::<module> (the exclusive-upgrade is parked/inert -- see docstring).
    reporter.check(
        "shot3",
        "the signature-change task landed merged, holding calc.py's whole-file write-lease",
        change_landed and change_result.lease_modes_used.get(CALC_MODULE) == "write",
    )

    contract_change_events = [
        e for e in events
        if e["type"] == "contract_change"
        and json.loads(e["payload"] or "{}").get("symbol") == CONTRACT_SYMBOL
    ]
    reporter.check(
        "shot3", "a real contract_change event fired for calc.py::add",
        len(contract_change_events) >= 1,
    )
    if contract_change_events:
        payload = json.loads(contract_change_events[0]["payload"])
        reporter.check(
            "shot3", "contract_change carries the OLD signature (no rounding param)",
            "rounding" not in (payload.get("old_signature") or ""),
        )
        reporter.check(
            "shot3", "contract_change carries the NEW signature (rounding param present)",
            "rounding" in payload.get("new_signature", ""),
        )

    fmt_result = results["shot3-fix-formats-call-site"]
    api_result = results["shot3-fix-api-call-site"]
    dependents_landed = all(
        r.status == "done" and (r.integrate_result or {}).get("status") == "merged"
        for r in (fmt_result, api_result)
    )
    reporter.check("shot3", "both dependent call-site fixes landed merged", dependents_landed)

    # The dependent's OWN re-read (DESIGN §4.3 step 4, `read_contracts`) must
    # reflect the NEW signature -- proof this is a genuine re-plan against
    # live state, not a hardcoded edit that happened to match. (The
    # classifier's stored `signature` is a bare `name(params...)` -- no `def`
    # prefix/colon -- so this checks for the new param, not a literal match
    # against `NEW_ADD_SIGNATURE`, which is `change_signature`'s own input
    # format.)
    reread_ok = all(
        "rounding" in ((r.contract_snapshot.get(CONTRACT_SYMBOL) or {}).get("signature") or "")
        for r in (fmt_result, api_result)
    )
    reporter.check(
        "shot3", "both dependents re-read the contract and saw the NEW signature",
        reread_ok,
    )

    if contract_change_events and dependents_landed:
        dependent_merged_seqs = [
            e["seq"] for e in events
            if e["type"] == "merged"
            and json.loads(e["payload"] or "{}").get("branch", "") in (
                fmt_result.branch, api_result.branch,
            )
        ]
        reporter.check(
            "shot3", "ordering: contract_change happened before either dependent's merge",
            bool(dependent_merged_seqs)
            and contract_change_events[0]["seq"] < min(dependent_merged_seqs),
        )

    formats_src = (repo / "formats.py").read_text()
    api_src = (repo / "api.py").read_text()
    reporter.check(
        "shot3", "trunk's formats.py call site was fixed to the new signature",
        "rounding=2" in formats_src,
    )
    reporter.check(
        "shot3", "trunk's api.py call site was fixed to the new signature",
        "rounding=2" in api_src,
    )

    ok, log = _run_full_suite(repo)
    reporter.check(
        "shot3", "sample_repo tests are green after the contract change + re-plan",
        ok, detail=log[-1500:],
    )


# --- test case #4: real SIGKILL + reaper recovery ----------------------------------


def _run_shot4(
    repo: Path, base_url: str, client: BlackboardClient, reporter: _Reporter
) -> None:
    base_commit = git_ops.current_commit(repo)
    crash_agent_id = "crash-agent"
    task_id = "shot4-percent"
    # File granularity: the crash agent holds calc.py's sibling -- formats.py's
    # WHOLE-FILE lock -- while it edits `percent`. Symbol-level parcels are never
    # leased (parked); every lease in this demo is a `<file>::<module>` lock.
    parcel_id = "formats.py::<module>"
    ttl = 3.0
    # Trunk's formats.py right before this shot's crash-agent starts -- NOT
    # sample_repo's static on-disk source: test case #3 (U15) legitimately
    # edits formats.py::total_with_tax earlier in the run, so the static
    # source and trunk's actual pre-this-shot content can genuinely differ.
    # What must hold is narrower and order-independent: whatever trunk looked
    # like right before the crash agent touched it is EXACTLY what it looks
    # like right after the reap -- the dead process's edit never landed.
    pre_shot4_formats = (repo / "formats.py").read_text()

    proc = subprocess.Popen(
        [
            sys.executable, str(CRASH_AGENT_SCRIPT),
            "--base-url", base_url,
            "--repo", str(repo),
            "--agent-id", crash_agent_id,
            "--task", task_id,
            "--parcel", parcel_id,
            "--path", "formats.py",
            "--symbol", "percent",
            "--base-commit", base_commit,
            "--ttl", str(ttl),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        granted = _wait_for_event(client, "lease_granted", agent_id=crash_agent_id, timeout=15.0)
        reporter.check(
            "shot4", "the crash-agent subprocess genuinely acquired its write-lease",
            granted is not None,
        )

        # Give the (now-hanging) mutator a moment to actually write its edit to
        # disk -- proves the crash happens mid-edit, not before it started.
        time.sleep(0.6)
        still_running = proc.poll() is None
        reporter.check("shot4", "the subprocess was still alive (hung) right before the kill", still_running)

        # --- the real kill ---------------------------------------------------
        proc.kill()  # SIGKILL
        proc.wait(timeout=10.0)

        reaped = _wait_for_event(
            client, "reaped", agent_id=crash_agent_id, timeout=ttl + 15.0
        )
        reporter.check(
            "shot4", "the reaper reclaimed the crashed agent's lease (reaped event)", reaped is not None
        )

        trunk_source = (repo / "formats.py").read_text()
        reporter.check(
            "shot4",
            "integration branch never contains the crashed agent's partial edit",
            trunk_source == pre_shot4_formats,
        )

        try:
            git_ops.remove_worktree(repo, crash_agent_id)
        except Exception:
            pass  # best-effort cleanup; not required for correctness

        result = run_agent(
            agent_id="crash-agent-retry-1",
            client=client,
            repo=repo,
            task=task_id,
            target_parcels=[parcel_id],
            mutator=mutators.edit_function_body,
            mutator_kwargs={
                "path": "formats.py",
                "symbol": "percent",
                "new_body": (
                    'if total == 0:\n'
                    '    return "0.0%"\n'
                    'ratio = div(part, total) * 100\n'
                    'return f"{ratio:.1f}%"'
                ),
            },
            base_commit=base_commit,
        )
        landed = result.status == "done" and (result.integrate_result or {}).get("status") == "merged"
        reporter.check("shot4", "the task was reassigned to a fresh agent and landed merged", landed)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10.0)


# --- test case #5: a test-breaking edit is rejected -----------------------------


def _run_shot5(repo: Path, client: BlackboardClient, reporter: _Reporter) -> None:
    base_commit = git_ops.current_commit(repo)
    original_api = (repo / "api.py").read_text()

    result = run_agent(
        agent_id="shot5-agent",
        client=client,
        repo=repo,
        task="shot5-break-summarize",
        # Whole-file lock (file granularity) -- see test case #4's note.
        target_parcels=["api.py::<module>"],
        mutator=mutators.break_a_test,
        mutator_kwargs={"path": "api.py", "symbol": "summarize"},
        base_commit=base_commit,
    )
    rejected = result.status == "done" and (result.integrate_result or {}).get("status") == "merge_rejected"
    reporter.check("shot5", "the test-breaking edit was rejected by the integrator", rejected)

    trunk_after = (repo / "api.py").read_text()
    reporter.check("shot5", "trunk api.py is byte-identical to before the rejected attempt", trunk_after == original_api)

    ok, log = _run_full_suite(repo)
    reporter.check("shot5", "the full sample_repo test suite stays green after the rejection", ok, detail=log[-1500:])


# --- orchestration -----------------------------------------------------------------


def run_demo(workdir: Optional[Path] = None, keep: bool = False) -> dict[str, Any]:
    """Run the whole demo end to end. Returns a dict with `workdir`, per-shot
    `results` (bool), and `all_ok`. Cleans up its own temp workdir unless
    `keep=True` (or `workdir` was supplied by the caller)."""
    reporter = _Reporter()
    own_workdir = workdir is None
    workdir = Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="swarm-sync-demo-"))
    # SWARMSYNC_ROOTS (the /index + /integrate managed-roots allow-list) defaults to
    # the server's cwd, but the demo builds its repo under a temp workdir. Self-configure
    # the allow-list to that workdir so standalone `python demo/run_demo.py` works out of
    # the box; an explicitly-set SWARMSYNC_ROOTS (e.g. from a test) still wins.
    os.environ.setdefault("SWARMSYNC_ROOTS", os.path.realpath(workdir))
    server: Optional[_ServerThread] = None

    try:
        print(f"[demo] workdir: {workdir}")
        repo, base = _setup_repo(workdir)
        print(f"[demo] sample_repo copied + git-initialized at {repo} (base={base[:8]})")

        db_path = workdir / "blackboard.db"
        app = create_app(db_path, reaper_interval=0.5, pheromone_half_life=60.0)
        server = _ServerThread(app)
        server.start()
        print(f"[demo] blackboard server live at {server.base_url}")

        _index(server.base_url, repo)
        conn = app.state.conn
        client = BlackboardClient(server.base_url)

        print("\n=== test case #1: three agents on three files land concurrently ===")
        _run_shot1(conn, repo, client, reporter)

        print("\n=== test case #2: a contended whole-file parcel serializes ===")
        _run_shot2(conn, repo, client, reporter)

        print("\n=== test case #3: frozen-contract change + dependent re-plan ===")
        _run_shot3(conn, repo, client, reporter)

        print("\n=== test case #4: crash mid-edit is recovered ===")
        _run_shot4(repo, server.base_url, client, reporter)

        print("\n=== test case #5: serial gated integration rejects a bad edit ===")
        _run_shot5(repo, client, reporter)

        _print_event_stream(client)

        print("\n=== overall ===")
        ok, log = _run_full_suite(repo)
        reporter.check("overall", "sample_repo test suite is green at the end of the run", ok, detail=log[-1500:])

        events = client.events(since=0)
        textual_collisions = [
            e for e in events
            if e["type"] == "merge_rejected"
            and json.loads(e["payload"] or "{}").get("reason") == "merge_conflict"
        ]
        reporter.check(
            "overall", "zero same-file textual collisions reached the integration branch",
            not textual_collisions,
        )

        return {"workdir": str(workdir), "results": reporter.results, "all_ok": all(reporter.results.values())}
    finally:
        if server is not None:
            server.stop()
        if own_workdir and not keep:
            shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    config.require_python()  # U1: fail fast on an unsupported interpreter
    result = run_demo()

    print("\n" + "=" * 72)
    print("RESULTS")
    for key in SHOT_ORDER:
        ok = result["results"].get(key, False)
        print(f"  {'PASS' if ok else 'FAIL'}: {SHOT_LABELS[key]}")
    print("=" * 72)
    if result["all_ok"]:
        print("ALL FIVE TEST CASES PASS (DESIGN §7): three agents on three files land "
              "clean and concurrently, a contended whole-file parcel serializes, a "
              "frozen-contract change notifies its dependents and they re-plan, a crash "
              "mid-edit is recovered, and a test-breaking edit is rejected by the gated "
              "integrator -- all under file-granularity locking, the shipping default.")
    else:
        print("AT LEAST ONE TEST CASE FAILED.")

    return 0 if result["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
