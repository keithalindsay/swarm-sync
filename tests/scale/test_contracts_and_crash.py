"""H5 / H6 at realistic scale: frozen-contract detection at 86 contracts, and the
two crash paths -- an agent SIGKILLed mid-edit, and the SERVER SIGKILLed mid-
integration (the least-tested path in the system).

WHY THIS FILE NEEDS ITS OWN APPARATUS
=====================================
`harness.scale_blackboard()` runs agents as THREADS against an in-process
`TestClient` -- there is no socket and nothing to SIGKILL (killing a thread takes
the blackboard down with it, which is not what a crash looks like). So H6 uses a
REAL listening `uvicorn` server in its own OS process (`_live_blackboard` below,
ports 8802/8803) plus real `subprocess` agents -- the pattern `demo/run_demo.py`
+ `demo/_crash_agent.py` already established, reused here rather than reinvented.
H5 and the bonus experiment need no kill, so they use the harness directly.
`harness._prepare_clone` builds every clone; the harness is never modified.

RESULTS UP FRONT (measured 2026-07-29 on this machine)
======================================================
H5  -- HELD. 86 contracts indexed; `change_signature` on
    `codelearner/ingest/python_extract.py::module_qualname` (blast_radius 152)
    emitted exactly ONE `contract_change`, naming that symbol, version 1 -> 2,
    and the dependent task the BROKER scheduled into wave 2 re-read the contract
    from live state and saw the new signature. code-learner's own 252-test suite
    is green on trunk afterwards (18.2 s).
    Finding: the two waves exist only because the dependent is in the SAME FILE
    as the contract. At file granularity a dependent in a *different* file is
    co-schedulable with the contract change and lands in the SAME wave, with
    nothing ordering it after the announcement -- see
    `test_h5_a_cross_file_dependent_is_NOT_ordered_after_the_contract_change`.

H6a -- SPLIT. The crash was genuinely mid-edit (` M codelearner/ingest/types.py`
    uncommitted in the worktree, no commit made) with a LIVE, heartbeat-renewed
    lease. Lease reclamation and trunk isolation HOLD: reaped 30.3 s after the
    kill, and a fresh agent then re-took the parcel and landed the task; trunk's
    HEAD, working tree and reflog never saw the partial edit. Two things did NOT
    hold:
      * "the worktree is cleaned" is **FALSIFIED**. Nothing cleans a SIGKILLed
        agent's worktree: the reaper only touches `leases` (its own docstring says
        so) and the dead process ran no `finally`. 540 KiB survived the reap, the
        reassignment, and stayed registered with git. The only thing that ever
        removes it is `add_worktree`'s S5 prune -- which matches on the SAME agent
        id, while the broker retries under `{task}-attempt-{n+1}`.
      * reclamation took 30.3 s for an agent that asked for an 8 s TTL, because
        `_Heartbeater` renews with no ttl and `POST /heartbeat` then applies
        `leases.DEFAULT_TTL_SECONDS` (30 s). `run_agent(lease_ttl=...)` is honored
        on acquire and silently discarded on renewal, so crash-detection latency
        is the server default no matter what the caller chose.

H6b -- HELD, and the dangerous state was really reached. The server was SIGKILLed
    with the gate's pytest confirmed running (its pid is asserted, then killed
    too), leaving an UN-GATED, test-RED merge on trunk with no verdict and one
    `open_integrations` row -- all asserted BEFORE the restart, so the convergence
    below cannot be vacuous. A restart against the same DB converged in 0.3 s:
    `reset integration <poisoned> -> <trunk_sha_before>`, `unresolved_orphan_count`
    back to 0 on the first attempt, `integrate_orphaned` recorded, the banner in
    the server's own stdout. No stranded orphan; the poisoned commit survives only
    in trunk's reflog and is not an ancestor of trunk.

BONUS (the experiment Agent A could not run) -- CONFIRMED, and it is the
    uncomfortable answer: an agent dispatched with `base_commit=None` DURING the
    un-gated window really does fork its worktree from the poisoned merge commit
    and starts work on top of code that is about to be rolled back off trunk.

Run with `-s` for the measured summary lines the report quotes.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import httpx
import pytest

from swarmsync.agent import mutators
from swarmsync.agent.client import BlackboardClient
from swarmsync.agent.runner import run_agent
from swarmsync.blackboard import db
from swarmsync.coordinator import broker, integrator
from swarmsync.worktree import git_ops
from tests.scale import harness

SWARMSYNC_ROOT = Path(__file__).resolve().parents[2]

# --- H5: a frozen contract with a SAME-FILE dependent -------------------------------
#
# Same file on purpose: at file granularity every task resolves to the file's
# `<module>` parcel, so two tasks in one file are NOT co-schedulable and the BROKER
# itself puts the dependent in a later wave. Two tasks in two files always land in
# the same wave (proved in
# `test_h5_a_cross_file_dependent_is_NOT_ordered_after_the_contract_change`), so a
# same-file dependent is the only way to get a genuine broker-scheduled "later
# wave" to observe the announcement.
#
# Verified against the real index (2026-07-29): `module_qualname` is one of the 86
# frozen contracts, blast_radius 152, signature `module_qualname(rel_path)`, and its
# only same-file caller is `extract` (`mod_qual = module_qualname(rel_path)`).
H5_PATH = "codelearner/ingest/python_extract.py"
H5_SYMBOL = "module_qualname"
H5_CONTRACT = f"{H5_PATH}::{H5_SYMBOL}"
H5_DEPENDENT_SYMBOL = "extract"
# Keyword-only with a default, and the annotations preserved: backward compatible,
# so `tests/test_ingest.py`'s direct `module_qualname(path)` calls keep passing and
# the signature change is measured by the gate rather than masked by a red suite.
H5_NEW_SIG = (
    "def module_qualname(rel_path: str, *, swarmsync_probe: object = None) -> str"
)
H5_NEW_PARAM = "swarmsync_probe"
H5_OLD_CALL = "module_qualname(rel_path)"
H5_NEW_CALL = "module_qualname(rel_path, swarmsync_probe=None)"
TASK_H5_CHANGE = "h5-change-module-qualname"
TASK_H5_DEPENDENT = "h5-fix-extract-call-site"

# --- H6a: the crash target ----------------------------------------------------------
#
# `demo/_crash_agent.py` is reused verbatim (it is a real OS process that hangs
# inside `slow_edit(hang=True)`), so its hardcoded partial-edit body IS the needle.
CRASH_SENTINEL = "CRASH-AGENT-PARTIAL-EDIT-SHOULD-NEVER-REACH-TRUNK"
CRASH_AGENT_SCRIPT = SWARMSYNC_ROOT / "demo" / "_crash_agent.py"
# `harness.BENIGN_EDITS[0]` -- `codelearner/ingest/types.py::content_hash`, whose
# semantics-preserving rewrite Agent A verified against the full 252-test suite. The
# crash and the reassignment therefore target the SAME parcel: the reassignment is a
# real reassignment of the crashed task, and its gate can still go green.
H6A_EDIT = harness.BENIGN_EDITS[0]
H6A_PATH = H6A_EDIT["path"]
H6A_SYMBOL = H6A_EDIT["symbol"]
H6A_PARCEL = f"{H6A_PATH}::<module>"
H6A_TASK = "h6a-crash-mid-edit"
# TTL long enough that the crashed process gets at least one REAL heartbeat in
# before the kill (the runner's default beat is every 5.0s), so the lease is alive
# at kill time because it was being renewed -- not merely because it had not aged
# out yet. Without that the "reaper reclaimed it" claim would be untestable.
H6A_TTL = 8.0

# --- H6b: the server-crash target ---------------------------------------------------
#
# A deliberately test-RED edit (`break_a_test` on Agent A's verified BREAK_TARGET):
# the merge stranded on trunk by the crash is one that WOULD have been rejected, so
# "no un-gated merge is left on trunk" has teeth -- a failed reconciliation leaves
# trunk genuinely red, not merely un-verified.
H6B_SENTINEL = "SWARMSYNC-SCALE-H6B-UNGATED-MERGE-7c41ab"
H6B_PATH = harness.BREAK_TARGET["path"]
H6B_SYMBOL = harness.BREAK_TARGET["symbol"]
H6B_BRANCH = "h6b-agent"
PORT_H6A = 8802
PORT_H6B_CRASH = 8803
PORT_H6B_RESTART = 8802  # free again: H6a's server is torn down inside its own test

# --- bonus: the un-gated-window inheritance probe -----------------------------------
BONUS_SENTINEL = "SWARMSYNC-SCALE-BONUS-POISON-91d0e4"
BONUS_PROBE_AGENT = "bonus-window-probe"


# ===================================================================================
# A REAL server, in a REAL process (H6 only)
# ===================================================================================

# Run as `python -c <this> <db> <port> <reaper_interval> <gate_python>`. It is a
# string rather than a file in the repo because it is test scaffolding, not a
# shipped script -- and it must exist as a separate PROCESS, which is the whole
# point of H6. `harness.gate_interpreter` is reused so this server's gate runs
# code-learner's 3.12 venv exactly like the in-process tests' does; without it every
# gate would fail for environment reasons and every merge would be rejected before
# it could ever be interrupted.
_SERVER_BOOTSTRAP = f"""
import sys
sys.path.insert(0, {str(SWARMSYNC_ROOT)!r})
db_path, port, reaper_interval, gate_python = sys.argv[1:5]
import uvicorn
from tests.scale import harness
from swarmsync.server.app import create_app
interval = None if reaper_interval == "None" else float(reaper_interval)
with harness.gate_interpreter(gate_python):
    app = create_app(db_path, reaper_interval=interval)
    uvicorn.run(app, host="127.0.0.1", port=int(port), log_level="warning")
"""


def _port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _wait_until(
    predicate: Callable[[], bool], timeout: float, poll: float = 0.05
) -> bool:
    """Poll `predicate` until true or `timeout` elapses. Returns whether it held.

    Returned rather than raised so a caller can assert on it with its own message
    -- a bare timeout error hides which condition never arrived.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return predicate()


@dataclass
class LiveBlackboard:
    """One real server process plus the clone it coordinates.

    `conn` is an out-of-band READER on the same DB file (WAL), opened in the test
    process: it keeps working after the server is killed, which is exactly when the
    interesting assertions happen.
    """

    root: Path
    base_commit: str
    db_path: Path
    port: int
    proc: subprocess.Popen
    log_path: Path
    conn: Any
    client: BlackboardClient
    parcel_count: int = 0
    contract_count: int = 0
    boot_seconds: float = 0.0
    _extra_procs: list[subprocess.Popen] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def alive(self) -> bool:
        return self.proc.poll() is None

    def log(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    # --- inspection (all from the DB / git, never from the server) -------------

    def events(self) -> list[dict]:
        return harness.events(self.conn)

    def events_of(self, type_: str, agent_id: Optional[str] = None) -> list[dict]:
        return [
            e
            for e in self.events()
            if e["type"] == type_ and (agent_id is None or e["agent_id"] == agent_id)
        ]

    def wait_for_event(
        self, type_: str, agent_id: Optional[str] = None, timeout: float = 30.0
    ) -> Optional[dict]:
        found: list[dict] = []

        def seen() -> bool:
            hits = self.events_of(type_, agent_id)
            if hits:
                found.append(hits[0])
            return bool(hits)

        _wait_until(seen, timeout)
        return found[0] if found else None

    def trunk_head(self) -> str:
        return git_ops.current_commit(self.root, ref=harness.TRUNK)

    def file_text(self, relpath: str) -> str:
        return (self.root / relpath).read_text(encoding="utf-8")

    def leases(self) -> list[dict]:
        return [
            dict(row)
            for row in self.conn.execute("SELECT * FROM leases ORDER BY id").fetchall()
        ]

    def open_integrations(self) -> list[dict]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM open_integrations ORDER BY started_seq"
            ).fetchall()
        ]

    # --- process control -------------------------------------------------------

    def gate_child_pids(self) -> list[int]:
        """The server's direct children -- i.e. the gate's `pytest`, when one is
        running. `gate.run_impact_tests` starts it with `start_new_session=True`,
        so it is in its OWN process group and does NOT die with the server; it has
        to be captured before the kill and killed explicitly, or it keeps running
        pytest in the trunk checkout while the restart is resetting it."""
        proc = subprocess.run(
            ["ps", "-o", "pid=", "--ppid", str(self.proc.pid)],
            capture_output=True,
            text=True,
        )
        return [int(line) for line in proc.stdout.split() if line.strip().isdigit()]

    def sigkill(self) -> list[int]:
        """SIGKILL the server AND the gate subprocess it spawned. Returns the gate
        pids killed. Uncatchable by design: `integrate`'s `except BaseException`
        rollback must not get a chance to run -- that path is already covered
        in-process, and what H6b tests is the one exit nothing can catch."""
        children = self.gate_child_pids()
        os.kill(self.proc.pid, signal.SIGKILL)
        self.proc.wait(timeout=15.0)
        for pid in children:
            for target in (-pid, pid):  # its own group first (session leader)
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.kill(target, signal.SIGKILL)
        return children

    def track(self, proc: subprocess.Popen) -> subprocess.Popen:
        """Register a helper process so teardown kills it even if a test fails."""
        self._extra_procs.append(proc)
        return proc


def _start_server(
    db_path: Path, port: int, log_path: Path, reaper_interval: Optional[float]
) -> tuple[subprocess.Popen, Any]:
    env = {
        **os.environ,
        # Same value `tests/conftest.py` and the harness use: /index and /integrate
        # 403 any path outside the managed roots, and both clones live under it.
        "SWARMSYNC_ROOTS": tempfile.gettempdir(),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    log = log_path.open("wb")
    # Its own session: teardown can kill the whole group without depending on the
    # server having exited cleanly. The log handle is handed back so the caller can
    # close it in its own `finally` (the server's stdout is evidence -- the
    # reconciliation banner H6b asserts on comes from it).
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _SERVER_BOOTSTRAP,
            str(db_path),
            str(port),
            str(reaper_interval),
            str(harness.CODE_LEARNER_PYTHON),
        ],
        cwd=str(SWARMSYNC_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc, log


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(timeout=10.0)


@contextlib.contextmanager
def _live_blackboard(
    port: int,
    reaper_interval: float = 0.5,
    workdir: Optional[Path] = None,
    db_path: Optional[Path] = None,
    index: bool = True,
) -> Iterator[LiveBlackboard]:
    """A real uvicorn blackboard on `port`, coordinating a fresh code-learner clone.

    Teardown ALWAYS runs and always kills: a leaked listening server on 8802/8803
    breaks the next run. `workdir`/`db_path` let the H6b restart re-open the SAME
    clone and the SAME DB the crashed server was using -- that reuse is the whole
    point of the test.
    """
    t0 = time.perf_counter()
    owns_workdir = workdir is None
    base_dir = (
        Path(tempfile.mkdtemp(prefix="swarmsync-scale-live-")) if owns_workdir else workdir
    )
    assert _port_is_free(port), (
        f"port {port} is already bound -- a previous run leaked a server; kill it "
        "before trusting anything this test says"
    )

    root: Optional[Path] = None
    base = ""
    if owns_workdir:
        root, base = harness._prepare_clone(base_dir)
    else:
        root = base_dir / "code-learner"
        # Suppressed on purpose: `test_h6b_a_failed_rollback...` deliberately boots a
        # server whose repo has been moved away, which is exactly the transient this
        # retry bound exists for. `base_commit` is unused by those callers.
        with contextlib.suppress(Exception):
            base = git_ops.current_commit(root, ref=harness.TRUNK)
    db_file = db_path if db_path is not None else base_dir / "blackboard.db"
    log_path = base_dir / f"server-{port}.log"

    proc, log_handle = _start_server(db_file, port, log_path, reaper_interval)
    conn = None
    client: Optional[BlackboardClient] = None
    live: Optional[LiveBlackboard] = None
    try:
        client = BlackboardClient(f"http://127.0.0.1:{port}", timeout=120.0)

        def up() -> bool:
            if proc.poll() is not None:
                raise AssertionError(
                    f"the server exited during startup (rc={proc.returncode}):\n"
                    f"{log_path.read_text(errors='replace')[-3000:]}"
                )
            try:
                client.health()  # type: ignore[union-attr]
                return True
            except Exception:
                return False

        assert _wait_until(up, timeout=60.0, poll=0.1), (
            "the server never became reachable:\n"
            f"{log_path.read_text(errors='replace')[-3000:]}"
        )
        conn = db.connect(db_file)
        live = LiveBlackboard(
            root=root,
            base_commit=base,
            db_path=db_file,
            port=port,
            proc=proc,
            log_path=log_path,
            conn=conn,
            client=client,
            boot_seconds=time.perf_counter() - t0,
        )
        if index:
            r = httpx.post(
                f"http://127.0.0.1:{port}/index", json={"root": str(root)}, timeout=120.0
            )
            r.raise_for_status()
            payload = r.json()
            live.parcel_count = payload["parcels"]
            live.contract_count = payload["contracts"]
            low, high = harness.PARCEL_COUNT_RANGE
            assert low <= live.parcel_count <= high, payload
        yield live
    finally:
        for extra in live._extra_procs if live is not None else []:
            _kill_process_tree(extra)
        _kill_process_tree(proc)
        with contextlib.suppress(Exception):
            log_handle.close()
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
        if root is not None and owns_workdir:
            with contextlib.suppress(Exception):
                harness._teardown_worktrees(root)
        if owns_workdir:
            shutil.rmtree(base_dir, ignore_errors=True)


def _dir_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            with contextlib.suppress(OSError):
                total += (Path(dirpath) / name).stat().st_size
    return total


def _worktree_dirty_files(worktree: Path) -> list[str]:
    """`git status --porcelain` inside a worktree: the proof that uncommitted work
    is really sitting there. Deliberately reads git's own view rather than
    comparing file bytes -- "the edit is on disk" and "git considers it uncommitted
    work in this worktree" are different claims, and H6a needs the second."""
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


# ===================================================================================
# H5 -- contract detection at 86 contracts
# ===================================================================================


@pytest.fixture(scope="module")
def scale():
    """The expensive setup ONCE for H5: clone + blackboard + 567-parcel index."""
    with harness.scale_blackboard() as sr:
        yield sr


@pytest.fixture(scope="module")
def h5_wave(scale):
    """Dispatch H5's two tasks in ONE `broker.run` and hand back the results.

    The broker partitions them itself: same file -> not co-schedulable -> wave 1 is
    the signature change, wave 2 is the dependent. Wave 2's tasks are dispatched
    only after wave 1 has fully drained (`broker.run`'s own contract), which is
    what makes "the dependent saw the announcement" a real ordering claim.
    `base_commit=None` on the dependent is deliberate: it forks from trunk's HEAD
    at dispatch time, i.e. from the landed contract change.
    """
    change = harness.signature_task(
        TASK_H5_CHANGE,
        path=H5_PATH,
        symbol=H5_SYMBOL,
        new_sig=H5_NEW_SIG,
        base_commit=scale.base_commit,
    )
    dependent = broker.Task(
        task_id=TASK_H5_DEPENDENT,
        targets=[(H5_PATH, H5_DEPENDENT_SYMBOL)],
        mutator=mutators.fix_call_site,
        mutator_kwargs={
            "path": H5_PATH,
            "symbol": H5_DEPENDENT_SYMBOL,
            "old": H5_OLD_CALL,
            "new": H5_NEW_CALL,
        },
        read_deps=[H5_CONTRACT],
        base_commit=None,
    )
    graph, frozen_ids = broker.load_scheduling_graph(scale.conn, scale.root)
    waves = broker.group_schedulable(
        scale.conn, [change, dependent], graph=graph, frozen_ids=frozen_ids
    )
    t0 = time.perf_counter()
    results = scale.run([change, dependent], n_agents=2)
    elapsed = time.perf_counter() - t0
    yield scale, results, waves, elapsed
    changes = [e for e in scale.events() if e["type"] == "contract_change"]
    print(
        "\n--- H5 summary ---"
        f"\nparcels / contracts:   {scale.parcel_count} / {scale.contract_count}"
        f"\nwaves the broker made: {[[t.task_id for t in w] for w in waves]}"
        f"\ntwo-wave wall-clock:   {elapsed:.1f}s (two serialized real gates)"
        f"\ncontract_change:       {[e['data']['symbol'] for e in changes]}"
        f"\nevents by type:        {dict(sorted(scale.events_by_type().items()))}"
    )


def test_h5_the_contract_is_really_frozen_before_we_touch_it(scale):
    """Precondition. If `module_qualname` were not one of the 86 frozen contracts,
    a `contract_change` could never fire and H5 would pass or fail for reasons that
    have nothing to do with contract detection."""
    assert scale.contract_count == harness.MEASURED_CONTRACTS
    row = scale.conn.execute(
        "SELECT symbol, signature, version, frozen FROM contracts WHERE symbol = ?",
        (H5_CONTRACT,),
    ).fetchone()
    assert row is not None, f"{H5_CONTRACT} is not a frozen contract"
    assert row["frozen"] == 1
    assert row["version"] == 1
    assert H5_NEW_PARAM not in row["signature"], row["signature"]
    # ...and the dependent really depends on it, read off the source, not assumed.
    assert H5_OLD_CALL in scale.file_text(H5_PATH)


def test_h5_the_broker_itself_scheduled_the_dependent_into_a_later_wave(h5_wave):
    """The ordering claim, from the broker's own partition -- not from the test
    having called `run` twice. Wave 2 is dispatched only after wave 1 drains."""
    _scale, _results, waves, _elapsed = h5_wave
    assert [[t.task_id for t in w] for w in waves] == [
        [TASK_H5_CHANGE],
        [TASK_H5_DEPENDENT],
    ], waves


def test_h5_contract_change_is_emitted_naming_the_right_symbol(h5_wave):
    """THE H5 rule: the landed signature change announces itself, once, for the
    symbol that actually changed, with both signatures and a bumped version."""
    scale, results, _waves, _elapsed = h5_wave
    assert results[TASK_H5_CHANGE].status == "done", results[TASK_H5_CHANGE]
    assert (
        results[TASK_H5_CHANGE].integrate_result["status"] == "merged"
    ), results[TASK_H5_CHANGE].integrate_result

    # Exactly one, and it belongs to the signature change's branch. This doubles as
    # the NEGATIVE control: the dependent's merge in wave 2 rewrote a body in the
    # SAME file (`extract`'s call site) without touching a signature, and it emitted
    # nothing -- so `contract_change` is keyed on a real type_hash diff rather than
    # firing for any merge that happens to touch a file containing contracts.
    changes = [e for e in scale.events() if e["type"] == "contract_change"]
    assert len(changes) == 1, [e["data"] for e in changes]
    payload = changes[0]["data"]
    assert payload["branch"].startswith(TASK_H5_CHANGE), payload
    assert payload["symbol"] == H5_CONTRACT, payload
    assert H5_NEW_PARAM not in payload["old_signature"], payload
    assert H5_NEW_PARAM in payload["new_signature"], payload
    assert payload["old_version"] == 1 and payload["new_version"] == 2, payload
    # The integrator reported it on the result too, not only in the log.
    assert results[TASK_H5_CHANGE].integrate_result["contract_changes"] == [H5_CONTRACT]


def test_h5_the_later_wave_dependent_received_the_change_via_read_deps(h5_wave):
    """The dependent's OWN re-read (`read_deps` -> `GET /contract/{symbol}`) shows
    the NEW signature, and it happened AFTER the announcement.

    Ordering is asserted against the dependent's `lease_granted` -- the first thing
    its attempt does that appears in the log -- rather than its merge, because
    "planned against the new contract" is the claim; comparing against the merge
    would be true even if it had planned before the change landed.
    """
    scale, results, _waves, _elapsed = h5_wave
    dependent = results[TASK_H5_DEPENDENT]
    assert dependent.status == "done", dependent
    assert dependent.integrate_result["status"] == "merged", dependent.integrate_result

    snapshot = dependent.contract_snapshot.get(H5_CONTRACT)
    assert snapshot is not None, dependent.contract_snapshot
    assert H5_NEW_PARAM in snapshot["signature"], snapshot
    assert snapshot["version"] == 2, snapshot

    events = scale.events()
    change_seq = next(e["seq"] for e in events if e["type"] == "contract_change")
    dependent_lease_seq = next(
        e["seq"]
        for e in events
        if e["type"] == "lease_granted" and (e["agent_id"] or "").startswith(TASK_H5_DEPENDENT)
    )
    assert change_seq < dependent_lease_seq, (change_seq, dependent_lease_seq)

    # ...and the dependent's fix is on trunk, so it really adapted its call site.
    assert H5_NEW_CALL in scale.file_text(H5_PATH)
    assert H5_NEW_SIG.rstrip(":") + ":" in scale.file_text(H5_PATH)


def test_h5_trunk_is_still_green_after_the_contract_change(h5_wave):
    """A contract change that notifies correctly but breaks trunk has failed
    differently. code-learner's OWN 252-test suite, on its own 3.12 venv, on trunk
    as the broker left it."""
    scale, _results, _waves, _elapsed = h5_wave
    ok, log = scale.suite_green()
    assert ok, f"trunk is NOT green after the contract change:\n{log}"


def test_h5_a_cross_file_dependent_is_NOT_ordered_after_the_contract_change(scale):
    """FINDING, asserted so it cannot rot: at file granularity the broker will
    happily dispatch a contract change and a dependent in ANOTHER file in the SAME
    wave -- concurrently, with nothing ordering the dependent after the
    announcement it is supposed to react to.

    `co_schedulable`'s frozen-contract clause exists for exactly this, but it can
    never fire here: contracts are extracted for function/class parcels while file
    granularity resolves every target to its `<module>` parcel, so no target is
    ever in `frozen_ids` (`broker`'s own docstring says the upgrade is INERT). A
    same-file dependent (the wave above) separates only because the two tasks share
    one whole-file lock -- a lock accident, not contract awareness.
    """
    change = harness.signature_task(
        "probe-change", path=H5_PATH, symbol=H5_SYMBOL, new_sig=H5_NEW_SIG
    )
    cross_file = harness.edit_task(
        "probe-cross-file-dependent",
        path="codelearner/ingest/indexer.py",  # imports python_extract's contract
        symbol="index_repo",
        new_body="return None",
    )
    graph, frozen_ids = broker.load_scheduling_graph(scale.conn, scale.root)
    waves = broker.group_schedulable(
        scale.conn, [change, cross_file], graph=graph, frozen_ids=frozen_ids
    )
    assert len(waves) == 1, waves  # one wave == dispatched concurrently
    # And the reason: the resolved targets are `<module>` parcels, which are never
    # frozen contracts, so the clause that would have separated them is inert.
    assert broker.resolve_task(scale.conn, change) == [f"{H5_PATH}::<module>"]
    assert not (set(broker.resolve_task(scale.conn, change)) & frozen_ids)
    assert H5_CONTRACT in frozen_ids  # the symbol IS frozen -- just never leased
    # The dependency itself is real (this is a genuine dependent, not a stranger):
    # indexer.py imports the contract's module and the graph carries the edge.
    assert "python_extract" in scale.file_text("codelearner/ingest/indexer.py")
    assert any(
        dep.startswith(H5_PATH)
        for dep in graph.edges.get("codelearner/ingest/indexer.py::index_repo", set())
    ), graph.edges.get("codelearner/ingest/indexer.py::index_repo")


# ===================================================================================
# H6a -- an agent SIGKILLed mid-edit
# ===================================================================================


def test_h6a_agent_sigkilled_mid_edit(capsys):
    """Kill a REAL agent process while it hangs inside `slow_edit(hang=True)`, at
    real scale, and check every clause of the plan's H6a separately.

    The apparatus is `demo/_crash_agent.py` (unmodified) against a real listening
    server, because a thread cannot be SIGKILLed without taking the blackboard with
    it. Sequenced so the crash is provably MID-EDIT and provably mid-LEASE:

      1. wait for `lease_granted` (the agent really holds the write lease),
      2. wait for a real `heartbeat` from that agent -- so the lease is alive
         because it is being RENEWED, not merely because it has not aged out,
      3. prove the partial edit is on disk in the worktree AND that git sees it as
         uncommitted work there, and that no commit was made,
      4. only then SIGKILL, and only if the process is still running.
    """
    with _live_blackboard(PORT_H6A, reaper_interval=0.5) as live:
        pre_trunk_head = live.trunk_head()
        pre_trunk_text = live.file_text(H6A_PATH)
        agent_id = "h6a-crash-agent"
        worktree = live.root / ".worktrees" / agent_id

        proc = live.track(
            subprocess.Popen(
                [
                    sys.executable,
                    str(CRASH_AGENT_SCRIPT),
                    "--base-url", live.base_url,
                    "--repo", str(live.root),
                    "--agent-id", agent_id,
                    "--task", H6A_TASK,
                    "--parcel", H6A_PARCEL,
                    "--path", H6A_PATH,
                    "--symbol", H6A_SYMBOL,
                    "--base-commit", live.base_commit,
                    "--ttl", str(H6A_TTL),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        )

        granted = live.wait_for_event("lease_granted", agent_id=agent_id, timeout=60.0)
        assert granted is not None, "the crash agent never acquired its write lease"
        lease_id = granted["data"]["lease_id"]

        beat = live.wait_for_event("heartbeat", agent_id=agent_id, timeout=H6A_TTL + 20.0)
        assert beat is not None, (
            "no heartbeat arrived before the TTL, so the lease was never actively "
            "renewed and 'the reaper reclaimed a LIVE lease' would be untestable"
        )
        # FINDING (measured here, not assumed): `run_agent(lease_ttl=8)` is honored on
        # ACQUIRE but not on RENEWAL -- `_Heartbeater` calls `client.heartbeat(agent,
        # lease_id)` with no ttl, so `POST /heartbeat` applies the server default
        # (`leases.DEFAULT_TTL_SECONDS`, 30s). After the first beat the crashed
        # agent's parcel therefore stays locked for 30s, not the 8s the agent asked
        # for -- crash-detection latency is set by the server default, whatever the
        # caller chose. Asserted so the reclaim time below is explained rather than
        # tuned around. (Also why the demo never saw it: its ttl=3.0 lapses before
        # the runner's first 5.0s beat, so no renewal ever happens there.)
        lease_before_kill = next(
            row for row in live.leases() if row["id"] == lease_id
        )
        assert lease_before_kill["status"] == "active", lease_before_kill
        renewed_window = lease_before_kill["ttl_expires_at"] - beat["ts"]
        # INVERTED when the finding was fixed, exactly as the original assertion
        # predicted it would be. As written it pinned the bug: `_Heartbeater` called
        # `client.heartbeat(agent, lease_id)` with no ttl, so the server applied its
        # 30 s default and `run_agent(lease_ttl=8.0)` was honored on the acquire and
        # silently discarded from the first beat onward. Crash-detection latency was
        # the server default whatever the caller asked for.
        #
        # A window is asserted rather than equality because the beat timestamp and the
        # server's own clock read are not the same instant.
        assert abs(renewed_window - H6A_TTL) < 2.0, (
            "the renewal is not honoring the requested lease_ttl -- `_Heartbeater` is "
            f"dropping its ttl again, so the server default is back. Asked for "
            f"{H6A_TTL}s, renewed window was {renewed_window}s "
            f"({'looks like the 30s server default' if renewed_window > 20 else 'unexpected'})."
        )
        assert lease_before_kill["ttl_expires_at"] > time.time(), (
            "the lease was already expired at kill time, so the reap below would "
            "prove nothing about a crash"
        )

        # --- the crash is genuinely mid-edit: real uncommitted work, no commit ---
        assert worktree.is_dir(), f"no worktree at {worktree}"
        partial = (worktree / H6A_PATH).read_text(encoding="utf-8")
        assert CRASH_SENTINEL in partial, "the hanging mutator never wrote its edit"
        dirty = _worktree_dirty_files(worktree)
        assert dirty == [f" M {H6A_PATH}"], dirty
        assert git_ops.current_commit(worktree) == live.base_commit, (
            "the agent committed before hanging -- the kill would not be mid-edit"
        )
        assert proc.poll() is None, "the crash agent exited on its own; nothing to kill"

        # --- the real SIGKILL ---------------------------------------------------
        kill_at = time.monotonic()
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=15.0)

        reaped = live.wait_for_event("reaped", agent_id=agent_id, timeout=90.0)
        assert reaped is not None, "the reaper never reclaimed the crashed agent's lease"
        reclaim_seconds = time.monotonic() - kill_at
        assert reaped["data"]["lease_id"] == lease_id, reaped
        assert reaped["data"]["parcel_id"] == H6A_PARCEL, reaped
        lease_row = next(row for row in live.leases() if row["id"] == lease_id)
        assert lease_row["status"] == "reaped", lease_row

        # --- trunk is untouched -------------------------------------------------
        assert live.trunk_head() == pre_trunk_head
        assert live.file_text(H6A_PATH) == pre_trunk_text
        assert CRASH_SENTINEL not in live.file_text(H6A_PATH)
        assert harness.reflog_hits(live.root, CRASH_SENTINEL, H6A_PATH) == []
        assert live.events_of("integrate_started") == []
        assert integrator.unresolved_orphan_count(live.conn) == 0

        # --- FALSIFIED: nothing cleans the dead agent's worktree ----------------
        # The reaper only touches `leases` (its own docstring), and the SIGKILLed
        # process ran no `finally`. So the worktree survives the reap, still
        # registered with git, still carrying the partial edit.
        leaked_bytes = _dir_bytes(worktree)
        assert worktree.is_dir(), "worktree was cleaned -- H6a's third clause HOLDS"
        assert CRASH_SENTINEL in (worktree / H6A_PATH).read_text(encoding="utf-8")
        assert str(worktree) in harness.worktrees(live.root), harness.worktrees(live.root)
        assert agent_id in [p.name for p in (live.root / ".worktrees").iterdir()]

        # ...and a fresh agent CAN still take the parcel: the lease recovered even
        # though the worktree did not.
        retry_agent = f"{H6A_TASK}-attempt-2"
        result = run_agent(
            agent_id=retry_agent,
            client=live.client,
            repo=live.root,
            task=H6A_TASK,
            target_parcels=[H6A_PARCEL],
            mutator=mutators.edit_function_body,
            mutator_kwargs={
                "path": H6A_PATH,
                "symbol": H6A_SYMBOL,
                "new_body": H6A_EDIT["new_body"],
            },
            base_commit=live.base_commit,
        )
        assert result.status == "done", result
        assert result.integrate_result["status"] == "merged", result.integrate_result
        assert H6A_EDIT["new_body"].splitlines()[0] in live.file_text(H6A_PATH)
        # The reassignment landed, and the crashed agent's worktree is STILL there:
        # the broker's next attempt uses a NEW agent id, so `add_worktree`'s S5
        # prune (which only matches the same name) never touches it.
        assert worktree.is_dir(), (
            "the reassignment cleaned the crashed worktree -- then the leak is "
            "self-healing and this finding needs re-stating"
        )

        # The ONLY thing that ever removes it: a re-run under the SAME agent id.
        pruned_path = git_ops.add_worktree(live.root, agent_id, live.base_commit)
        try:
            assert CRASH_SENTINEL not in (pruned_path / H6A_PATH).read_text(
                encoding="utf-8"
            ), "add_worktree did not prune the stale worktree"
            # CONTROL for the uncommitted-work prober used above: on a freshly cut
            # worktree it reports nothing, so the ` M <path>` it reported at kill
            # time was a real difference, not something it says about any worktree.
            assert _worktree_dirty_files(pruned_path) == []
        finally:
            git_ops.remove_worktree(live.root, agent_id)

        with capsys.disabled():
            print(
                "\n--- H6a summary ---"
                f"\nlease reclaimed after kill:  {reclaim_seconds:.1f}s "
                f"(agent asked for ttl {H6A_TTL}s; the heartbeat had widened it to "
                f"{renewed_window:.0f}s -- the server default)"
                f"\nuncommitted work at kill:    {dirty} in {worktree.name}"
                f"\nleaked worktree after reap:  {leaked_bytes / 1024:.0f} KiB, "
                f"still registered with git"
                f"\ntrunk moved:                 no ({pre_trunk_head[:8]} throughout)"
                f"\nreassignment landed:         {result.integrate_result['status']}"
            )


# ===================================================================================
# H6b -- the SERVER SIGKILLed mid-integration (the least-tested path)
# ===================================================================================


def test_h6b_server_sigkilled_mid_integration_reconciles(capsys):
    """Kill the server INSIDE the un-gated window, restart it against the same DB,
    and see whether reconciliation converges.

    Agent A proved the precondition: `integrator.integrate` merges to trunk BEFORE
    it gates, so for the gate's whole duration (14-20 s here) trunk's HEAD carries
    an unverified merge. This test enters that window deliberately, kills the
    server with SIGKILL (uncatchable -- `integrate`'s own `except BaseException`
    rollback must not get to run), and asserts the state that leaves behind BEFORE
    restarting, so the convergence assertions afterwards cannot be vacuous.

    The branch is prepared with `git_ops` + a mutator directly rather than by a
    full agent: H6b is about the integrator's crash window, and a real agent would
    add a lease, a heartbeat thread and a client-side exception that have nothing
    to do with it (the agent protocol under crash is H6a's job).
    """
    workdir = Path(tempfile.mkdtemp(prefix="swarmsync-scale-h6b-"))
    try:
        root, base = harness._prepare_clone(workdir)
        db_file = workdir / "blackboard.db"
        crashed_head = ""
        started_seq: Optional[int] = None

        with _live_blackboard(
            PORT_H6B_CRASH, workdir=workdir, db_path=db_file, index=True
        ) as live:
            # A branch whose merge WOULD be rejected: if reconciliation fails to
            # roll it back, trunk is left genuinely red, not merely un-verified.
            worktree = git_ops.add_worktree(root, H6B_BRANCH, base)
            mutators.break_a_test(
                worktree, H6B_PATH, H6B_SYMBOL, message=H6B_SENTINEL
            )
            git_ops.commit_all(worktree, f"{H6B_BRANCH}: un-gated merge probe")
            assert live.trunk_head() == base
            assert H6B_SENTINEL not in live.file_text(H6B_PATH)

            outcome: dict[str, Any] = {}

            def submit() -> None:
                try:
                    outcome["result"] = live.client.integrate(
                        H6B_BRANCH, branch=H6B_BRANCH, repo=str(root), base_commit=base
                    )
                except Exception as exc:  # the server dies under us -- expected
                    outcome["error"] = f"{type(exc).__name__}: {exc}"

            submitter = threading.Thread(target=submit, daemon=True)
            submitter.start()

            # --- enter the window: the un-gated merge is ON trunk ---------------
            assert _wait_until(lambda: live.trunk_head() != base, timeout=120.0), (
                "trunk never moved, so the integrate never merged and there was no "
                "window to crash inside"
            )
            crashed_head = live.trunk_head()
            in_head_commit = H6B_SENTINEL in (
                harness.blob_at(root, harness.TRUNK, H6B_PATH) or ""
            )
            in_worktree = H6B_SENTINEL in live.file_text(H6B_PATH)
            open_rows = live.open_integrations()
            assert len(open_rows) == 1, open_rows
            started_seq = open_rows[0]["started_seq"]
            assert open_rows[0]["trunk_sha_before"] == base, open_rows[0]
            assert in_head_commit and in_worktree, (in_head_commit, in_worktree)
            # Wait until the GATE has actually started, and prove it: `integrate`
            # merges and then calls `run_impact_tests`, which spawns pytest as a
            # direct child. Killing as soon as trunk moves would land in the much
            # narrower merge->gate gap and this test would never exercise a crash
            # *inside* the 14-20s gate window, which is the window that matters.
            gate_pids: list[int] = []

            def gate_running() -> bool:
                gate_pids[:] = live.gate_child_pids()
                return bool(gate_pids)

            assert _wait_until(gate_running, timeout=60.0, poll=0.02), (
                "the gate's pytest never started, so the kill below would not be "
                "inside the gate window"
            )
            assert live.trunk_head() == crashed_head, "trunk moved again mid-gate"

            # --- the real, uncatchable kill -------------------------------------
            killed_gate_pids = live.sigkill()
            assert not live.alive()
            submitter.join(timeout=30.0)

        # --- what the crash left behind (asserted BEFORE any restart) ----------
        conn = db.connect(db_file)
        try:
            stranded = integrator.unresolved_orphan_count(conn)
            events = harness.events(conn)
            terminal = [
                e
                for e in events
                if e["type"] in harness.INTEGRATE_TERMINAL_TYPES
                and e["data"].get("started_seq") == started_seq
            ]
            assert stranded == 1, (
                "the crash left NO open_integrations row, so nothing recorded the "
                "un-gated merge and there is nothing for a restart to reconcile"
            )
            assert terminal == [], terminal
            assert git_ops.current_commit(root, ref=harness.TRUNK) == crashed_head
            assert crashed_head != base
            assert H6B_SENTINEL in (root / H6B_PATH).read_text(encoding="utf-8")
        finally:
            conn.close()

        # --- restart against the SAME DB and the SAME clone --------------------
        restart_at = time.monotonic()
        restart_wall = time.time()
        with _live_blackboard(
            PORT_H6B_RESTART, workdir=workdir, db_path=db_file, index=False
        ) as restarted:
            # Two numbers, because they answer different questions: `boot_to_serving`
            # includes interpreter + import startup (~0.27s of the ~0.3-0.6s measured
            # here), while `reconcile_seconds` is how long after launch the rollback
            # was actually recorded -- reconciliation runs in the lifespan BEFORE the
            # server serves, so /health answering at all already means it finished.
            boot_to_serving = time.monotonic() - restart_at
            assert integrator.unresolved_orphan_count(restarted.conn) == 0, (
                "STRANDED ORPHAN: the restart did not resolve the open integrate -- "
                f"{restarted.open_integrations()}"
            )
            assert restarted.trunk_head() == base, (
                "trunk was NOT rolled back: an un-gated merge is stranded on trunk "
                f"at {restarted.trunk_head()[:8]}"
            )
            assert H6B_SENTINEL not in restarted.file_text(H6B_PATH)
            assert H6B_SENTINEL not in (
                harness.blob_at(root, harness.TRUNK, H6B_PATH) or ""
            )
            # The poisoned merge is not in trunk's HISTORY either -- only its reflog.
            assert not harness.is_ancestor(root, crashed_head)
            assert harness.reflog_hits(root, H6B_SENTINEL, H6B_PATH) == [crashed_head]

            orphaned = restarted.events_of("integrate_orphaned")
            assert len(orphaned) == 1, [e["data"] for e in orphaned]
            reconcile_seconds = orphaned[0]["ts"] - restart_wall
            record = orphaned[0]["data"]
            assert record["started_seq"] == started_seq, record
            assert record["error"] is None, record
            assert record["attempts"] == 1, record
            # `reconcile_orphaned_integrations` records its action under
            # `reconciliation` in the event payload (`action` is the key on the
            # RETURNED record only) -- assert the event, since that is the durable
            # audit trail an operator actually has after the restart.
            assert record["reconciliation"].startswith("reset "), record
            assert base[:8] in record["reconciliation"], record
            assert crashed_head[:8] in record["reconciliation"], record
            assert record["trunk_sha_before"] == base, record
            # The operator-visible trail, from the server's own stdout.
            assert "reconciled orphaned integrate" in restarted.log(), restarted.log()[-2000:]

            with capsys.disabled():
                print(
                    "\n--- H6b summary ---"
                    f"\nun-gated merge on trunk at kill: {crashed_head[:8]} "
                    f"(in HEAD commit: {in_head_commit}, on disk: {in_worktree})"
                    f"\ngate pids alive at kill:         {gate_pids} (killed: {killed_gate_pids})"
                    f"\nopen_integrations after crash:   1 (started_seq={started_seq})"
                    f"\nrestart -> serving again in:     {boot_to_serving:.1f}s"
                    f"\nrestart -> orphan resolved in:   {reconcile_seconds:.1f}s"
                    f"\nreconciliation:                  {record['reconciliation']}"
                    f"\nsubmitter saw:                   {outcome.get('error') or outcome.get('result')}"
                )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_h6b_a_failed_rollback_keeps_the_orphan_row_until_the_attempt_bound(capsys):
    """The other half of the H6b path: what happens when the rollback itself FAILS.

    Same crash as above, then the repo is moved out from under the restart (the
    transient `MAX_RECONCILE_ATTEMPTS` exists for: an unmounted share, a permissions
    blip, a checkout mid-restore). Each boot must KEEP the `open_integrations` row --
    deleting it is the P0 `docs/AUDIT.md` found, because the row is the only thing
    that can still detect the un-gated merge -- and count the attempt, until the
    bound abandons it LOUDLY.

    Asserted end to end: attempts 1..4 keep the row and record `FAILED`; attempt 5
    (`integrator.MAX_RECONCILE_ATTEMPTS`) abandons it with a message naming the sha
    to reset by hand; and then -- with the repo restored -- trunk really is still
    carrying the un-gated merge with no row left to find it, which is the honest
    consequence of abandoning, not a bug.
    """
    workdir = Path(tempfile.mkdtemp(prefix="swarmsync-scale-h6b-retry-"))
    try:
        root, base = harness._prepare_clone(workdir)
        db_file = workdir / "blackboard.db"
        moved = workdir / "code-learner-moved-away"

        with _live_blackboard(
            PORT_H6B_CRASH, workdir=workdir, db_path=db_file, index=True
        ) as live:
            worktree = git_ops.add_worktree(root, H6B_BRANCH, base)
            mutators.break_a_test(worktree, H6B_PATH, H6B_SYMBOL, message=H6B_SENTINEL)
            git_ops.commit_all(worktree, f"{H6B_BRANCH}: un-gated merge probe")

            def submit() -> None:
                with contextlib.suppress(Exception):
                    live.client.integrate(
                        H6B_BRANCH, branch=H6B_BRANCH, repo=str(root), base_commit=base
                    )

            submitter = threading.Thread(target=submit, daemon=True)
            submitter.start()
            assert _wait_until(lambda: live.trunk_head() != base, timeout=120.0)
            crashed_head = live.trunk_head()
            assert _wait_until(lambda: bool(live.gate_child_pids()), timeout=60.0, poll=0.02)
            live.sigkill()
            submitter.join(timeout=30.0)

        # The repo becomes unreachable. `git worktree remove` first so the moved
        # checkout does not leave git admin files pointing at a path that no longer
        # exists once it is moved back.
        git_ops.remove_worktree(root, H6B_BRANCH, delete_branch=False)
        os.rename(root, moved)

        actions: list[str] = []
        for boot in range(1, integrator.MAX_RECONCILE_ATTEMPTS + 1):
            port = PORT_H6B_RESTART if boot % 2 else PORT_H6B_CRASH
            with _live_blackboard(
                port, workdir=workdir, db_path=db_file, index=False
            ) as retry:
                events = retry.events_of("integrate_orphaned")
                assert len(events) == boot, [e["data"] for e in events]
                record = events[-1]["data"]
                actions.append(record["reconciliation"])
                assert record["attempts"] == boot, record
                assert record["error"] is not None, record
                rows = retry.open_integrations()
                if boot < integrator.MAX_RECONCILE_ATTEMPTS:
                    # KEPT, and counted: the next boot must be able to retry.
                    assert record["reconciliation"] == "FAILED", record
                    assert len(rows) == 1, rows
                    assert rows[0]["reconcile_attempts"] == boot, rows[0]
                    assert integrator.unresolved_orphan_count(retry.conn) == 1
                else:
                    # ABANDONED at the bound -- loudly, naming the manual remedy.
                    assert record["reconciliation"].startswith("ABANDONED"), record
                    assert base[:8] in record["reconciliation"], record
                    assert "by hand" in record["reconciliation"], record
                    assert rows == [], rows
                    assert integrator.unresolved_orphan_count(retry.conn) == 0

        # The consequence, stated plainly: with the repo back, trunk STILL carries
        # the un-gated merge and nothing in the blackboard can find it any more.
        os.rename(moved, root)
        assert git_ops.current_commit(root, ref=harness.TRUNK) == crashed_head
        assert H6B_SENTINEL in (root / H6B_PATH).read_text(encoding="utf-8")
        with _live_blackboard(
            PORT_H6B_RESTART, workdir=workdir, db_path=db_file, index=False
        ) as final:
            assert integrator.unresolved_orphan_count(final.conn) == 0
            assert final.trunk_head() == crashed_head, (
                "an abandoned orphan was rolled back anyway -- the bound no longer "
                "means what this test says it means"
            )

        with capsys.disabled():
            print(
                "\n--- H6b retry-bound summary ---"
                f"\nMAX_RECONCILE_ATTEMPTS:      {integrator.MAX_RECONCILE_ATTEMPTS}"
                f"\nper-boot reconciliation:     {actions}"
                f"\nafter abandonment:           trunk still at {crashed_head[:8]} "
                "(un-gated), zero rows left to detect it"
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_h6b_the_orphan_detector_is_not_vacuous():
    """MUTATION/CONTROL CHECK for the two probes H6b's verdict rests on.

    `unresolved_orphan_count` returning 0 for everything -- or `reflog_hits`
    finding the sentinel in anything -- would make "reconciliation converged"
    meaningless. So: a hand-written `open_integrations` row must COUNT, its
    deletion must clear the count, and a needle nothing ever wrote must not be
    found in a clean repo's reflog.
    """
    with _live_blackboard(PORT_H6A, reaper_interval=None, index=False) as live:
        assert integrator.unresolved_orphan_count(live.conn) == 0
        live.conn.execute(
            "INSERT INTO open_integrations "
            "(started_seq, repo, branch, into_branch, trunk_sha_before, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (999999, str(live.root), "probe", harness.TRUNK, live.base_commit, time.time()),
        )
        assert integrator.unresolved_orphan_count(live.conn) == 1
        live.conn.execute("DELETE FROM open_integrations WHERE started_seq = 999999")
        assert integrator.unresolved_orphan_count(live.conn) == 0
        assert harness.reflog_hits(live.root, H6B_SENTINEL, H6B_PATH) == []
        assert harness.reflog_hits(live.root, "swarm-sync scale harness", ".gitignore") == []


# ===================================================================================
# BONUS -- does an agent really INHERIT the poisoned commit? (Agent A could not run this)
# ===================================================================================


def test_bonus_a_worktree_cut_during_the_ungated_window_inherits_the_poison():
    """Agent A proved trunk's HEAD carries an un-gated merge for the whole gate
    window, and that `add_worktree` cuts from HEAD when `base_commit is None` --
    but every task in its wave PINNED `base_commit`, so nothing confirmed an agent
    actually inherits the poison. This runs the missing experiment.

    A breaking task is dispatched in a background thread; the moment trunk's HEAD
    moves (the un-gated merge), a SECOND agent is dispatched with
    `base_commit=None` -- the broker's own default, and what a later wave uses.
    Its mutator records what the worktree it was given actually contains, then
    RAISES: `run_agent` contains that as `status="error"` and never integrates, so
    this probe cannot race the in-flight merge it is observing. The fork point is
    the question; a second concurrent integrate is not part of it.

    A negative result here would be genuinely reassuring and is reported either way.
    """
    with harness.scale_blackboard() as sr:
        break_path = harness.BREAK_TARGET["path"]
        observed: dict[str, Any] = {}

        class _ProbeDone(RuntimeError):
            pass

        def probe_mutator(worktree, path: str, symbol: str, new_body: str) -> None:
            observed["worktree_head"] = git_ops.current_commit(worktree)
            observed["poison_in_worktree"] = BONUS_SENTINEL in (
                Path(worktree) / break_path
            ).read_text(encoding="utf-8")
            observed["trunk_head_then"] = git_ops.current_commit(
                sr.root, ref=harness.TRUNK
            )
            raise _ProbeDone("probe complete -- deliberately not integrating")

        breaker = harness.break_task(
            "bonus-breaking-task",
            path=break_path,
            symbol=harness.BREAK_TARGET["symbol"],
            message=BONUS_SENTINEL,
            base_commit=sr.base_commit,
        )
        wave: dict[str, Any] = {}

        def run_breaker() -> None:
            wave["results"] = sr.run([breaker], n_agents=1)

        thread = threading.Thread(target=run_breaker, daemon=True)
        thread.start()
        try:
            entered = _wait_until(
                lambda: git_ops.current_commit(sr.root, ref=harness.TRUNK)
                != sr.base_commit,
                timeout=120.0,
                poll=0.02,
            )
            assert entered, "trunk never carried the un-gated merge; nothing to inherit"
            poisoned_head = git_ops.current_commit(sr.root, ref=harness.TRUNK)

            # A DIFFERENT file's parcel, so this probe never contends with the
            # breaking task's lease -- the experiment is about the fork point.
            probe_target = harness.BENIGN_EDITS[1]
            result = run_agent(
                agent_id=BONUS_PROBE_AGENT,
                client=sr.client,
                repo=sr.root,
                task="bonus-window-probe",
                target_parcels=[f"{probe_target['path']}::<module>"],
                mutator=probe_mutator,
                mutator_kwargs={
                    "path": probe_target["path"],
                    "symbol": probe_target["symbol"],
                    "new_body": probe_target["new_body"],
                },
                base_commit=None,  # THE experiment: fork from whatever HEAD is
            )
        finally:
            thread.join(timeout=180.0)

        assert result.error_type == "_ProbeDone", result
        assert observed, "the probe mutator never ran"
        # THE finding: the worktree was cut from the poisoned merge commit itself.
        assert observed["worktree_head"] == poisoned_head, observed
        assert observed["poison_in_worktree"] is True, observed
        assert observed["trunk_head_then"] == poisoned_head, observed

        # ...and the breaking task was rejected, so the commit the probe forked from
        # is one that no longer exists on trunk at all.
        results = wave["results"]
        assert results["bonus-breaking-task"].integrate_result["status"] == (
            "merge_rejected"
        ), results["bonus-breaking-task"].integrate_result
        assert git_ops.current_commit(sr.root, ref=harness.TRUNK) == sr.base_commit
        assert not harness.is_ancestor(sr.root, poisoned_head)
        print(
            "\n--- bonus summary ---"
            f"\nprobe worktree forked from:  {observed['worktree_head'][:8]} "
            f"(the un-gated merge)"
            f"\npoison visible to the agent: {observed['poison_in_worktree']}"
            f"\nthat commit's fate:          rejected, reset off trunk, "
            f"ancestor of trunk: {harness.is_ancestor(sr.root, poisoned_head)}"
        )
