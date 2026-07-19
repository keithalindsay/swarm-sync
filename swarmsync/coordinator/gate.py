"""The integrator's pytest gate: impact-selected test runs with subprocess
containment. DESIGN.md §5.4 step 3.

`run_impact_tests` is the only entry point. Everything else here is its
apparatus: dependency-graph test selection (`_reverse_dep_files`), the gate
timeout (`_gate_timeout` / SWARMSYNC_GATE_TIMEOUT), and the kill/drain
machinery (`_kill_process_group`, `_close_streams`) that guarantees a hung
gate cannot wedge the caller -- which holds the ONE global `integrate_lock`.

Split out of `coordinator.integrator` (WP4.3): the gate runs just-merged,
agent-authored code in a subprocess, and none of that machinery reads or
writes the blackboard or trunk. `integrator.integrate` consumes only the
`(ok, log)` verdict.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Union

from swarmsync import config
from swarmsync.classifier.graph import build_graph
from swarmsync.classifier.indexer import index_repo

StrPath = Union[str, Path]

DEFAULT_TEST_DIR = "tests"

# Wall-clock ceiling for the pytest gate. The gate executes just-merged, agent-
# authored test code of unbounded runtime while `app.post_integrate` holds the ONE
# global `integrate_lock`, so an infinite loop (or a test that blocks on input, a
# socket, a dead network mount) in any agent's branch does not merely fail that
# merge -- it wedges integration for every other agent, permanently, with trunk
# left carrying the un-gated merge commit. A timeout converts that from "the
# coordinator is dead until someone restarts it" into an ordinary rejection.
# Override per-deployment via SWARMSYNC_GATE_TIMEOUT (seconds).
# WP4.2: the value (and its parse/fallback) now lives in `swarmsync.config`;
# these names survive as aliases because `coordinator.integrator` re-exports
# them as part of its public surface (and tests drive them there).
DEFAULT_GATE_TIMEOUT_SECONDS = config.DEFAULT_GATE_TIMEOUT_SECONDS

# How long to wait for a killed gate's output after the group kill. Short by design:
# by this point the verdict (rejected) is already decided and the log is a nicety, so
# there is no reason to let an escaped descendant holding the pipes delay the caller
# -- which holds the global integrate_lock.
_DRAIN_TIMEOUT_SECONDS = 5.0


# WP4.2: the single env read for the gate timeout is `config.gate_timeout()`.
_gate_timeout = config.gate_timeout


def _reverse_dep_files(repo: Path, changed_py: set[str]) -> set[str]:
    """Every repo file that TRANSITIVELY reverse-depends on a changed `.py` file,
    via the classifier's real import/call dependency graph.

    This is the correctness core of impact selection: a test that exercises the
    changed code only *indirectly* (it imports module M, which imports the changed
    module C -- and the test's own source never names C) is a genuine dependent
    a textual scan of the test source cannot see. We re-index the (already
    merged) repo on disk, build the dep graph, seed a BFS at every parcel whose
    file is a changed file, walk `reverse_edges` to the transitive dependent set,
    and map those parcel ids back to their files. Returns an empty set on any
    failure (a broken repo, etc.) -- the substring heuristic + full-suite fallback
    in `run_impact_tests` still backstop selection, so this only ever ADDS
    coverage, never removes.
    """
    if not changed_py:
        return set()
    try:
        parcels = index_repo(repo)
        graph = build_graph(parcels, repo)
    except Exception:  # noqa: BLE001 -- selection is best-effort; never fail the gate here
        return set()
    changed_parcel_ids = {p.id for p in parcels if p.path in changed_py}
    affected: set[str] = set()
    queue: deque[str] = deque(changed_parcel_ids)
    while queue:
        pid = queue.popleft()
        for dependent in graph.reverse_edges.get(pid, set()):
            if dependent not in affected:
                affected.add(dependent)
                queue.append(dependent)
    return {
        graph.parcels_by_id[a].path for a in affected if a in graph.parcels_by_id
    }


def _close_streams(proc: subprocess.Popen) -> None:
    """Drop our read ends of a killed gate's pipes. Best-effort: this runs only on
    the already-failing timeout path and must not raise into it."""
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the gate's whole process group, falling back to the direct child.

    Best-effort by design: the process (or group) may already be gone, and a gate
    that has timed out must never turn its own cleanup into an exception that
    escapes into `integrate`'s error path.

    NEVER signals our OWN process group. The gate is spawned with
    `start_new_session=True` so it leads its own group, and this SIGKILLs that group.
    But if that ever stops holding -- a refactor drops the flag, a platform ignores it,
    someone reuses this helper for a plainly-spawned child -- then `getpgid(child)`
    IS our group, and a gate timeout would SIGKILL the server itself: the coordinator
    dies to reap a hanging test. Observed for real: a mutation run that flipped
    `start_new_session` to False killed the harness that was running it. The child is
    still killed directly in that case, so the timeout still works; it just stops
    taking us with it.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    if pgid is not None and pgid != os.getpgrp():
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def run_impact_tests(
    repo: StrPath, changed_files: list[str], test_dir: str = DEFAULT_TEST_DIR
) -> tuple[bool, str]:
    """Run pytest restricted to tests reachable from `changed_files` (DESIGN §5.4
    step 3): a CONSERVATIVE over-selection with a full-suite fallback.

    A test file is selected if EITHER:
      - the dependency graph shows it transitively reverse-depends on a changed
        file (`_reverse_dep_files`) -- the authoritative signal, and the one that
        catches indirect dependents a textual scan misses; OR
      - the changed file's module stem appears as a token in the test's source
        (a bare-stem substring backstop for edges the classifier can't see --
        dynamic dispatch / string imports, DESIGN §6 "classifier miss":
        over-selecting a test is always safe, skipping an affected one never is).
    Selecting the UNION keeps this a strict over-approximation: it can only run
    more tests, never fewer -- it never skips an affected test.
    If nothing matches -- selection is genuinely uncertain (no changed `.py`
    files, or no test relates to the change) -- fall back to the whole `test_dir`
    rather than risk a silent skip. If `test_dir` doesn't exist under `repo` at
    all, falls back further to running pytest across the whole repo.

    Returns `(ok, combined_stdout_stderr_log)`. Uses `sys.executable -m pytest`
    so it runs against whichever Python/venv is already running this process
    (the repo under test has no independent environment of its own in this
    prototype).
    """
    repo = Path(repo)
    tests_root = repo / test_dir

    # S3 security: sandbox the gate's pytest run against the untrusted agent branch
    # we just merged. `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` + `-p no:cacheprovider`
    # stop third-party pytest plugins / conftest side effects on $PATH from being
    # auto-loaded and running arbitrary code inside our process's environment;
    # `--import-mode=importlib` avoids mutating `sys.path`/polluting the parent's
    # module namespace via legacy prepend-import. These narrow WHAT the gate can do
    # without changing whether a genuinely passing/failing suite passes/fails.
    base_cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--import-mode=importlib",
    ]

    if not tests_root.exists():
        cmd = base_cmd
    else:
        changed_py = {f for f in changed_files if f.endswith(".py")}
        changed_stems = {Path(f).stem for f in changed_py}
        # Authoritative dependency-graph reverse-deps (transitive), plus the
        # substring backstop -- their union is the conservative over-select.
        affected_files = _reverse_dep_files(repo, changed_py)
        selected: list[str] = []
        for test_file in sorted(tests_root.rglob("test_*.py")):
            rel_posix = test_file.relative_to(repo).as_posix()
            try:
                text = test_file.read_text(encoding="utf-8")
            except OSError:
                continue
            hit_graph = rel_posix in affected_files
            hit_substr = any(stem and stem in text for stem in changed_stems)
            if hit_graph or hit_substr:
                selected.append(str(test_file.relative_to(repo)))
        if selected:
            cmd = [*base_cmd, *selected]
        else:
            # Selection uncertain (nothing matched, or no changed .py files) --
            # full-suite fallback per DESIGN §5.4.
            cmd = [*base_cmd, test_dir]

    env = config.subprocess_env(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    timeout = _gate_timeout()
    # `start_new_session=True` puts the gate in its own process GROUP so a timeout
    # can kill the whole tree. `subprocess.run(timeout=...)` alone kills only the
    # direct child -- pytest spawns (xdist workers, subprocesses under test), and
    # those orphans would keep running, holding the repo and the CPU, after we'd
    # already reported the merge rejected.
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        # Draining the pipes must ALSO be bounded. `communicate()` waits for EOF on
        # stdout/stderr, not for the direct child to die -- and EOF only arrives once
        # EVERY holder of the write end has exited. A grandchild that re-`setsid`s
        # (`subprocess.Popen(..., start_new_session=True)`, a double-forking daemon)
        # is in its own process group, so the killpg above never reaches it, and it
        # keeps the inherited pipe open. An unbounded drain here therefore blocks for
        # that descendant's whole lifetime while `post_integrate` holds the global
        # `integrate_lock` -- reinstating the exact permanent wedge this timeout
        # exists to prevent. Reachable whenever the merged branch turns pytest's
        # fd-capture off (`addopts = -s`), which the agent-authored repo controls.
        try:
            stdout, stderr = proc.communicate(timeout=_DRAIN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # A descendant escaped the group kill and still holds the pipes. Abandon
            # the output rather than wait on it; the verdict does not depend on it.
            stdout, stderr = "", ""
            _close_streams(proc)
        return False, (
            f"{stdout}{stderr}\n"
            f"swarm-sync: test gate exceeded {timeout:.0f}s and was killed "
            f"(SWARMSYNC_GATE_TIMEOUT to change). Treating as a gate FAILURE: a "
            f"branch whose tests do not terminate cannot be shown to keep trunk green."
        )
    # pytest exit code 5 == "no tests were collected" -- e.g. a repo/fixture with
    # no test suite yet, or an impact-selection pass that (correctly) found no
    # test touches this change. Nothing to gate on is not a rejection reason.
    ok = returncode in (0, 5)
    log = stdout + stderr
    return ok, log
