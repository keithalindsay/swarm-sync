"""End-to-end demo: the five money shots. DESIGN.md §7.

Built in Unit U14 (money shots #1, #2, #4, #5); money-shot #3 (frozen-contract
change + dependent re-plan, DESIGN §5.3) was added on top of this file by U15.
All five money shots now print PASS/FAIL and the final summary says so.

Flow:
  1. copy `sample_repo/` into a fresh temp dir and `git init` it as the shared
     "integration" trunk checkout (`worktree.git_ops.init_repo`).
  2. init the blackboard DB and boot a REAL `uvicorn` server (in a background
     thread, in this same process) -- money-shot #4 needs an agent that lives
     in a genuinely separate OS process to SIGKILL, which only proves anything
     if the blackboard is reachable over a real socket rather than FastAPI's
     in-process TestClient/ASGI transport. Every money shot below (not just #4)
     therefore talks HTTP over a real port, exercising the actual wire protocol.
  3. `POST /index` to populate parcels + contracts from the fresh repo.
  4. run each scripted scenario, printing the event stream and a PASS/FAIL line
     per assertion, then a final summary block.

Money shots exercised here (DESIGN §7):
  #1 two agents edit different functions (`calc.py`'s `sub`/`mul`) in the SAME
     file concurrently, symbol-mode leased -> two `merged` events, zero
     conflicts, both edits present on trunk. Proven with a genuine wall-clock
     overlap check (not just "both eventually ran"), mirroring the same
     technique `tests/test_broker.py` already uses and validates.
  #2 a third task (`calc.py`'s `div`) contends against an externally-held
     write-lease (a stand-in for "another agent is already editing this
     parcel") -> `lease_denied`, then -- after the holder releases (not
     crashes; that's #4's story) -- `lease_granted` + `merged` for the same
     parcel. Driven through the SAME `broker.run` call as #1, matching U12's
     own handoff note for this unit.
  #4 a REAL subprocess (`demo/_crash_agent.py`) is SIGKILLed while holding a
     write-lease and mid-`mutators.slow_edit` (real, uncommitted work already
     on disk in its own worktree) -> the reaper (running in this process's own
     live server) reclaims the lease past its TTL, emits `reaped`; a fresh
     agent is dispatched for the same task and lands it; trunk never contains
     the dead process's edit.
  #5 `mutators.break_a_test` deliberately breaks `api.py::summarize`'s
     behavior -> the integrator's impact-selected pytest gate on `test_api.py`
     fails -> `merge_rejected`, the merge commit is reset out, trunk stays
     green and unchanged.
  #3 (U15) one task changes `calc.py::add`'s frozen signature (`def add(a, b)`
     -> `def add(a, b, rounding=None)`, `mutators.change_signature`) under an
     EXCLUSIVE lease -- auto-enforced by `coordinator.broker.run` (U15) since
     `calc.py::add` is already a registered frozen contract (blast_radius >=
     FREEZE_THRESHOLD across `formats.py`/`api.py`/their tests, per U13's own
     fixture design). Once that lands, the integrator's own before/after
     `type_hash` diff (U15, `coordinator/integrator.py`) emits a real
     `contract_change` event. Two more tasks -- real dependents that already
     call `calc.add` (`formats.py::total_with_tax`, `api.py::summarize`) --
     declare `calc.py::add` as a `read_deps` contract, so their `run_agent`
     call re-reads it (`AgentResult.contract_snapshot`) and sees the NEW
     signature, then land a `mutators.fix_call_site` call-site fix
     (`rounding=2`) via the SAME `broker.run` call -- `co_schedulable`'s
     frozen-contract clause (DESIGN §3, built in U3/U12) already forces the
     signature-change task into its own earlier wave, so the two dependents
     only ever see the contract AFTER it changed. Tests stay green throughout
     (the new parameter is optional, so nothing ever actually breaks -- the
     demo proves the NOTIFY + RE-PLAN protocol, not a crash).

OVERALL: zero same-file textual collisions (git merge conflicts) reached
`integration` across the whole run, every landed commit left `sample_repo`'s
test suite green, and all five money-shot assertions print PASS.
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

from swarmsync.agent import mutators
from swarmsync.agent.client import BlackboardClient
from swarmsync.agent.runner import run_agent
from swarmsync.coordinator import broker
from swarmsync.server import leases as leases_mod
from swarmsync.server.app import create_app
from swarmsync.worktree import git_ops

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_REPO_SRC = REPO_ROOT / "sample_repo"
CRASH_AGENT_SCRIPT = Path(__file__).resolve().with_name("_crash_agent.py")

SHOT_LABELS = {
    "shot1": "money-shot #1 (concurrent disjoint edits land clean)",
    "shot2": "money-shot #2 (contended parcel serializes)",
    "shot3": "money-shot #3 (frozen-contract change notifies + dependent re-plans)",
    "shot4": "money-shot #4 (crash mid-edit is recovered)",
    "shot5": "money-shot #5 (serial gated integration rejects a bad edit)",
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

    Money-shot #4 needs an agent process this demo can genuinely SIGKILL
    without taking the coordinator down with it -- that only works if agents
    talk to the blackboard over a real socket, so every money shot in this
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


# --- money shots #1 + #2: driven through ONE broker.run() call ---------------------

_timeline_lock = threading.Lock()


def _timed_edit(worktree, path, symbol, new_body, label, timeline, hold=0.0):
    """`edit_function_body`, with (label, "start"/"end", monotonic-clock) entries
    recorded around it so the demo can PROVE two edits' wall-clock windows
    genuinely overlapped, not just "both eventually landed" (same technique
    `tests/test_broker.py` already uses)."""
    with _timeline_lock:
        timeline.append((label, "start", time.monotonic()))
    if hold:
        time.sleep(hold)
    mutators.edit_function_body(worktree, path, symbol, new_body)
    with _timeline_lock:
        timeline.append((label, "end", time.monotonic()))


def _run_shots_1_and_2(conn, repo: Path, client: BlackboardClient, reporter: _Reporter) -> None:
    # Money-shot #2's "already write-leased" holder: a stand-in for a fourth
    # agent already mid-edit on calc.py::div when our task tries for it. Long
    # TTL (it must NOT merely time out -- that's shot #4's story) + an explicit
    # release on a delay simulates "the holder finishes and releases".
    holder = leases_mod.acquire(
        conn, "calc.py::div", "manual-editor", mode="write", ttl=20.0, intent="manual-div-edit"
    )
    assert holder.granted, "test setup: holder lease on calc.py::div must be granted"

    def _release_holder_later(delay: float) -> None:
        time.sleep(delay)
        leases_mod.release(conn, holder.lease_id, "manual-editor")

    releaser = threading.Thread(target=_release_holder_later, args=(1.5,), daemon=True)
    releaser.start()

    timeline: list[tuple[str, str, float]] = []

    task_sub = broker.Task(
        task_id="shot1-sub",
        targets=[("calc.py", "sub")],
        mutator=_timed_edit,
        mutator_kwargs={
            "path": "calc.py",
            "symbol": "sub",
            "new_body": "result = a - b\nreturn result",
            "label": "sub",
            "timeline": timeline,
            "hold": 0.5,
        },
    )
    task_mul = broker.Task(
        task_id="shot1-mul",
        targets=[("calc.py", "mul")],
        mutator=_timed_edit,
        mutator_kwargs={
            "path": "calc.py",
            "symbol": "mul",
            "new_body": "product = a * b\nreturn product",
            "label": "mul",
            "timeline": timeline,
            "hold": 0.5,
        },
    )
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

    results = broker.run(
        conn, repo, [task_sub, task_mul, task_div], client,
        n_agents=3, mode="symbol", retry_backoff=0.4,
    )
    releaser.join(timeout=5.0)

    events = client.events(since=0)

    # --- shot #1 -----------------------------------------------------------
    sub_ok = results["shot1-sub"].status == "done" and (
        results["shot1-sub"].integrate_result or {}
    ).get("status") == "merged"
    mul_ok = results["shot1-mul"].status == "done" and (
        results["shot1-mul"].integrate_result or {}
    ).get("status") == "merged"
    reporter.check("shot1", "agent A (sub) and agent B (mul) both landed merged", sub_ok and mul_ok)

    by_label: dict[str, dict[str, float]] = {}
    for label, kind, ts in timeline:
        by_label.setdefault(label, {})[kind] = ts
    overlapped = (
        "sub" in by_label and "mul" in by_label
        and by_label["sub"]["start"] < by_label["mul"]["end"]
        and by_label["mul"]["start"] < by_label["sub"]["end"]
    )
    reporter.check(
        "shot1", "sub's and mul's edits genuinely overlapped in wall-clock time (real concurrency)", overlapped
    )

    calc_src = (repo / "calc.py").read_text()
    both_present = "result = a - b" in calc_src and "product = a * b" in calc_src
    reporter.check("shot1", "both edits present on trunk (integration branch)", both_present)

    conflict_events = [e for e in events if e["type"] == "merge_rejected"]
    reporter.check(
        "shot1", "zero textual merge conflicts reached integration for this wave",
        not any(json.loads(e["payload"] or "{}").get("reason") == "merge_conflict" for e in conflict_events),
    )

    # --- shot #2 -------------------------------------------------------------
    div_ok = results["shot2-div"].status == "done" and (
        results["shot2-div"].integrate_result or {}
    ).get("status") == "merged"
    reporter.check("shot2", "the contended task (div) eventually landed merged", div_ok)

    div_denied = [
        e for e in events
        if e["type"] == "lease_denied" and json.loads(e["payload"] or "{}").get("parcel_id") == "calc.py::div"
    ]
    reporter.check("shot2", "a real lease_denied was observed against calc.py::div", len(div_denied) >= 1)

    div_granted = [
        e for e in events
        if e["type"] == "lease_granted" and json.loads(e["payload"] or "{}").get("parcel_id") == "calc.py::div"
        and e["agent_id"] != "manual-editor"
    ]
    reporter.check(
        "shot2", "the contending agent later acquired the SAME parcel", len(div_granted) >= 1
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


# --- money shot #3 (U15): frozen-contract change + dependent re-plan ----------------

CONTRACT_SYMBOL = "calc.py::add"
NEW_ADD_SIGNATURE = "def add(a, b, rounding=None)"


def _run_shot3(conn, repo: Path, client: BlackboardClient, reporter: _Reporter) -> None:
    """DESIGN §5.3/§7 money-shot #3: `calc.py::add` is sample_repo's registered
    frozen contract (U13's fixture design; `blast_radius >= FREEZE_THRESHOLD`
    across `formats.py`, `api.py`, and their own test suites). One task
    changes its signature; two more -- REAL dependents that already call
    `calc.add` -- fix their call sites to match. All three run through one
    `broker.run` call (symbol-mode leasing, same as shots #1/#2):
    `co_schedulable`'s frozen-contract clause (DESIGN §3) forces the
    signature-change task into its own wave ahead of the two dependents
    (`load_scheduling_graph`'s `frozen_ids` already contains `calc.py::add`,
    and `formats.py::total_with_tax` / `api.py::summarize` are real
    call-graph dependents of it), and the broker (U15) auto-upgrades the
    signature-change task's lease to `exclusive` because it targets a known
    frozen contract -- nobody has to remember to ask for that by hand.

    The new parameter (`rounding=None`, unused by `add`'s own body) is
    deliberately backward-compatible: `sample_repo/tests/test_calc.py` calls
    `add(2, 3)` directly and must keep passing on the signature-change task's
    OWN impact-selected test gate (which only re-checks tests reachable from
    `calc.py`, i.e. before either dependent has fixed anything) -- this shot
    proves the NOTIFY + RE-PLAN protocol, not a crash-and-recover story
    (that is money-shot #4's job).
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

    results = broker.run(
        conn, repo, [task_change, task_fix_formats, task_fix_api], client,
        n_agents=3, mode="symbol",
    )
    events = client.events(since=0)

    change_result = results["shot3-change-add-signature"]
    change_landed = change_result.status == "done" and (
        change_result.integrate_result or {}
    ).get("status") == "merged"
    reporter.check(
        "shot3", "the signature-change task landed merged under an exclusive lease",
        change_landed and change_result.lease_modes_used.get(CONTRACT_SYMBOL) == "exclusive",
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


# --- money shot #4: real SIGKILL + reaper recovery ----------------------------------


def _run_shot4(
    repo: Path, base_url: str, client: BlackboardClient, reporter: _Reporter
) -> None:
    base_commit = git_ops.current_commit(repo)
    crash_agent_id = "crash-agent"
    task_id = "shot4-percent"
    parcel_id = "formats.py::percent"
    ttl = 3.0
    # Trunk's formats.py right before this shot's crash-agent starts -- NOT
    # sample_repo's static on-disk source: money-shot #3 (U15) legitimately
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


# --- money shot #5: a test-breaking edit is rejected -----------------------------


def _run_shot5(repo: Path, client: BlackboardClient, reporter: _Reporter) -> None:
    base_commit = git_ops.current_commit(repo)
    original_api = (repo / "api.py").read_text()

    result = run_agent(
        agent_id="shot5-agent",
        client=client,
        repo=repo,
        task="shot5-break-summarize",
        target_parcels=["api.py::summarize"],
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

        print("\n=== money shot #1 + #2: concurrent disjoint edits + contended parcel ===")
        _run_shots_1_and_2(conn, repo, client, reporter)

        print("\n=== money shot #3: frozen-contract change + dependent re-plan ===")
        _run_shot3(conn, repo, client, reporter)

        print("\n=== money shot #4: crash mid-edit is recovered ===")
        _run_shot4(repo, server.base_url, client, reporter)

        print("\n=== money shot #5: serial gated integration rejects a bad edit ===")
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
    result = run_demo()

    print("\n" + "=" * 72)
    print("RESULTS")
    for key in SHOT_ORDER:
        ok = result["results"].get(key, False)
        print(f"  {'PASS' if ok else 'FAIL'}: {SHOT_LABELS[key]}")
    print("=" * 72)
    if result["all_ok"]:
        print("ALL FIVE MONEY SHOTS PASS (DESIGN §7): concurrent disjoint edits land "
              "clean, a contended parcel serializes, a frozen-contract change notifies "
              "its dependents and they re-plan, a crash mid-edit is recovered, and a "
              "test-breaking edit is rejected by the gated integrator.")
    else:
        print("AT LEAST ONE MONEY SHOT FAILED.")

    return 0 if result["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
