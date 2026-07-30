"""The integrator's pytest gate: impact-selected test runs with subprocess
containment. DESIGN.md §5.4 step 3.

`run_impact_tests` is the only entry point. Everything else here is its
apparatus: dependency-graph test selection (`_reverse_dep_files`), the gate
timeout (`_gate_timeout` / SWARMSYNC_GATE_TIMEOUT), the interpreter the gate
spawns (`resolve_python` / SWARMSYNC_GATE_PYTHON), and the kill/drain
machinery (`_kill_process_group`, `_close_streams`) that guarantees a hung
gate cannot wedge the caller -- which holds the ONE global `integrate_lock`.

Split out of `coordinator.integrator` (WP4.3): the gate runs just-merged,
agent-authored code in a subprocess, and none of that machinery reads or
writes the blackboard or trunk. `integrator.integrate` consumes only the
`(ok, log)` verdict.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Optional, Union

from swarmsync import config
from swarmsync.classifier.graph import build_graph
from swarmsync.classifier.indexer import DEFAULT_MAX_INDEX_FILES, index_repo

logger = logging.getLogger(__name__)

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

# The single env read for the gate's interpreter is `config.gate_python()`.
_gate_python = config.gate_python


def resolve_python() -> str:
    """The interpreter the gate spawns pytest with: `SWARMSYNC_GATE_PYTHON`, else
    `sys.executable` (the interpreter running swarm-sync itself).

    Why the default is resolved HERE rather than in `config`: it is not a
    constant. `config`'s accessors return fixed defaults, and caching "whatever
    interpreter is running" at import time in the bottom layer would freeze it --
    so `config.gate_python()` is deliberately RAW (None when unset, like
    `lease_ttl()`), and this function supplies the default by reading THIS
    module's `sys` at call time. That also keeps the pre-existing test seam
    (`tests/scale/harness.gate_interpreter`, which swaps `gate.sys`) working,
    though with the env knob in place that shim is no longer necessary.
    """
    return _gate_python() or sys.executable


class _AffectedFiles(set[str]):
    """The reverse-dependency answer, plus whether it IS an answer.

    `_reverse_dep_files` used to return a bare `set()` for two states a caller
    must not confuse: "the graph says nothing reverse-depends on this change"
    (a real, empty answer) and "the graph could not be built, so I have no idea"
    (no answer at all). Both were `set()`, so `run_impact_tests` narrowed
    identically in each case -- see that function's WIDENING note.

    Why a `set` subclass rather than a second return value: `_reverse_dep_files`
    is a long-standing seam. `coordinator.integrator` re-exports it,
    `tests/scale/test_impact_selection.py` replaces it with `lambda r, c: set()`
    to mutation-test the graph rule and wraps it with a timing shim that returns
    its result verbatim, and several tests call it directly and do set algebra on
    what comes back. A tuple return would break every one of those; a subclass of
    `set[str]` keeps the published contract (`-> set[str]`, and `== set()` still
    holds) while carrying the one extra bit out of band. A caller that gets a
    PLAIN set -- because something patched this function -- sees
    `unavailable_reason` absent via `getattr(..., None)`, i.e. "available", which
    is the correct reading of a stub that answered without failing.
    """

    unavailable_reason: Optional[str] = None

    @classmethod
    def unavailable(cls, reason: str) -> "_AffectedFiles":
        out = cls()
        out.unavailable_reason = reason
        return out


def _reverse_dep_files(repo: Path, changed_py: set[str]) -> set[str]:
    """Every repo file that TRANSITIVELY reverse-depends on a changed `.py` file,
    via the classifier's real import/call dependency graph.

    This is the correctness core of impact selection: a test that exercises the
    changed code only *indirectly* (it imports module M, which imports the changed
    module C -- and the test's own source never names C) is a genuine dependent
    a textual scan of the test source cannot see. We re-index the (already
    merged) repo on disk, build the dep graph, and walk `reverse_edges` out from
    the changed files.

    NEVER RAISES, and that is deliberate: selection is best-effort and must not
    turn a gate into an outage, so every failure below is swallowed. But it is no
    longer swallowed SILENTLY, and the empty set it returns on failure is no
    longer indistinguishable from a genuine "nothing depends on this":

      * the failure is logged at WARNING with the exception chained
        (`exc_info`), so a repo that has quietly outgrown
        `indexer.DEFAULT_MAX_INDEX_FILES` says so on every gate run rather than
        never; and
      * the returned set carries `unavailable_reason` (see `_AffectedFiles`), so
        `run_impact_tests` can WIDEN to the whole suite instead of narrowing on
        the substring backstop alone.

    Every exception that can reach that handler is a capability loss, not an
    answer, which is why they are treated alike. Working through them:
    `index_repo` raises `IndexLimitError` past its file-count OR wall-clock cap
    (the file cap is the one a growing repo hits); a single pathological source
    file can raise `RecursionError`/`ValueError` out of `ast.parse` past
    `index_repo`'s and `build_graph`'s per-file
    `OSError`/`SyntaxError`/`UnicodeDecodeError` guards; and `MemoryError` on a
    huge tree. In none of those is "no dependents" the right answer.

    The genuinely quiet cases are the ones that never reach the handler at all,
    and they are unchanged: an empty `changed_py` (nothing was asked), a changed
    file with no parcels, a changed file nothing imports, and -- the one worth
    naming -- the ORDINARY broken repo, because an unparseable file is
    skipped-and-logged per-file INSIDE `index_repo` rather than aborting the
    walk. So a syntax error an agent just merged still yields a real, narrowed
    answer and no widening; only a wholesale inability to index widens. That
    matters, because "broken repo" is the common case and the one this docstring
    used to cite as the reason for the bare `except`.

    THE WALK IS FILE-GRANULAR, and that is the whole point. It used to be
    parcel-granular -- seed every parcel of a changed file, walk parcel->parcel
    reverse edges, and project to files only at the very end -- which severed
    file-level chains: reaching *some* parcel of module M does not put M's OTHER
    parcels in the frontier, so anything importing M by a different symbol was
    never reached. Traced on a real 45-file repo: the walk from
    `chunk/chunker.py` reached 2 of `cli/commands.py`'s 13 parcels (`<module>`,
    `cmd_index`) while `server/app.py` imports four *different* symbols from that
    same file -- so `app.py` was not a dependent of `chunker.py` as far as the
    gate was concerned, though it plainly is one. It cost reverse-dependents on
    10 of 34 modules, including 4 dependent TEST files for the package
    `__init__.py`; those escaped being a live false negative only because the
    substring backstop happened to match, which is a coincidence about filenames
    and not a guarantee.

    So: any parcel of a file being affected marks the WHOLE file affected, and
    expansion continues from every parcel of that file. File granularity is not
    an approximation bolted on here either -- it is the granularity swarm-sync
    leases and schedules at (`graph.check_file_granularity`), and the granularity
    this function's own return type and its caller's selection both use. The
    price is over-selection, which is the documented, safe direction: the gate
    may run a test it did not need to, never skip one it did.

    The changed files themselves are INCLUDED in the result. A file is not a
    reverse-dependent of itself, so this is deliberate: when the change set
    contains a test file, that test is affected by definition and must run, and
    the substring backstop only catches it by accident (it looks for the stem in
    the file's TEXT, so `tests/test_payments.py` is matched only if something in
    it happens to spell "test_payments"). The old walk picked such a file up only
    when it happened to have an intra-file call edge, which is not a property
    anyone should be relying on. For a changed non-test file this adds nothing:
    `run_impact_tests` only ever asks about `test_*.py` paths.
    """
    if not changed_py:
        return _AffectedFiles()
    try:
        parcels = index_repo(repo)
        graph = build_graph(parcels, repo)
    except Exception as exc:  # noqa: BLE001 -- selection is best-effort; never fail the gate here
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "swarm-sync: impact selection's dependency graph is UNAVAILABLE for %s "
            "(%s). Reverse-dependents are UNKNOWN, not empty, so the test gate will "
            "widen to the whole suite instead of trusting the substring backstop. If "
            "this repo has grown past classifier.indexer.DEFAULT_MAX_INDEX_FILES "
            "(%d) .py files, this is permanent until that cap is raised.",
            repo,
            reason,
            DEFAULT_MAX_INDEX_FILES,
            exc_info=True,
        )
        return _AffectedFiles.unavailable(reason)
    parcels_by_file: dict[str, set[str]] = {}
    for parcel in parcels:
        parcels_by_file.setdefault(parcel.path, set()).add(parcel.id)

    # `visited` starts at the changed files so the walk cannot loop back through
    # them (a cycle would otherwise re-expand a file already done). They are also
    # seeded into `affected` -- see the docstring: a changed test file must run.
    visited: set[str] = set(changed_py)
    affected: set[str] = {f for f in changed_py if f in parcels_by_file}
    queue: deque[str] = deque(affected)
    while queue:
        current_file = queue.popleft()
        for pid in parcels_by_file.get(current_file, ()):
            for dependent in graph.reverse_edges.get(pid, set()):
                target = graph.parcels_by_id.get(dependent)
                if target is None or target.path in visited:
                    continue
                visited.add(target.path)
                affected.add(target.path)
                queue.append(target.path)
    return _AffectedFiles(affected)


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

    WIDENING WHEN THE GRAPH IS UNAVAILABLE. The authoritative signal above can be
    unavailable rather than empty -- most realistically because the repo has more
    than `indexer.DEFAULT_MAX_INDEX_FILES` (5000) `.py` files, so `index_repo`
    raises `IndexLimitError` on every gate run. `_reverse_dep_files` swallows that
    (it must never fail the gate) and used to return the same bare `set()` it
    returns for "nothing depends on this change", so selection collapsed to the
    substring backstop with NO log line, NO event and no change in this
    function's verdict shape. Measured on a real repo with the cap lowered, an
    `ingest/types.py` change went from 10 selected test files to 2 (13 to 2 when
    re-measured on a later state of that same repo) -- the same gap that
    `tests/scale/test_impact_selection.py::test_h2_graph_selection_is_load_bearing`
    demonstrates is a false negative -- and an operator had nothing to look at.
    So when the graph is unavailable this now runs the WHOLE `test_dir` and says
    so in the returned log.

    The widening's marginal cost is smaller than it sounds, and was measured on
    that repo for the same change: 34.9 s graph-selected (13 of 14 test files)
    vs 36.7 s widened, i.e. +5%. The 2.4 s the silent narrowing took was not a
    cheap gate, it was 2 of 13 test files. Whether that holds on a repo actually
    past the cap is NOT measured -- see the class of failure named below.

    That trade is deliberate and it is not free: on a repo big enough to trip the
    cap the full suite may exceed `SWARMSYNC_GATE_TIMEOUT`, in which case every
    merge is rejected as a timeout instead of being under-tested. That is the
    failure this chooses, for two reasons. A rejection is recoverable and
    `integrator._reject_and_reset` restores trunk byte-for-byte, so nothing bad
    lands and nothing is left half-merged; an under-tested merge is neither
    recoverable nor visible. And it is consistent with the rest of this module,
    where "we could not test this" must never read as "this is fine" (see the
    unusable-interpreter path below). The timeout message names the widening when
    it applies, so the stall is diagnosable rather than mysterious.

    Returns `(ok, combined_stdout_stderr_log)`. Runs `<interpreter> -m pytest`,
    where the interpreter is `SWARMSYNC_GATE_PYTHON` if set and `sys.executable`
    (this process's own Python) otherwise -- see `resolve_python`. Point the knob
    at the target repo's own venv when the repo has an environment of its own;
    with the default, a repo needing a different Python version or dependencies
    swarm-sync's venv lacks fails the gate for environment reasons on every
    merge, which is indistinguishable from a gate that works.
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
    python = resolve_python()
    base_cmd = [
        python,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--import-mode=importlib",
    ]

    # Prepended to whatever log this call returns, so a degraded gate is visible in
    # the merge verdict itself and not only in swarm-sync's logger. Empty on the
    # normal path.
    notice = ""
    if not tests_root.exists():
        cmd = base_cmd
    else:
        changed_py = {f for f in changed_files if f.endswith(".py")}
        changed_stems = {Path(f).stem for f in changed_py}
        # Authoritative dependency-graph reverse-deps (transitive), plus the
        # substring backstop -- their union is the conservative over-select.
        affected_files = _reverse_dep_files(repo, changed_py)
        # `getattr`, not an attribute access: the annotated return type here is the
        # published `set[str]`, and the test seams that replace this function hand
        # back a PLAIN set. Absent means "the graph answered" -- see `_AffectedFiles`.
        graph_unavailable: Optional[str] = getattr(
            affected_files, "unavailable_reason", None
        )
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
        if graph_unavailable is not None:
            # The authoritative signal did not answer. Narrowing on the substring
            # backstop alone would be a SILENT loss of coverage; widening is a
            # visible, recoverable loss of time. See the WIDENING note above.
            cmd = [*base_cmd, test_dir]
            notice = (
                f"swarm-sync: impact selection WIDENED this gate run to the whole "
                f"'{test_dir}' suite. The dependency graph could not be built "
                f"({graph_unavailable}), so reverse-dependents are UNKNOWN rather "
                f"than empty. The substring backstop alone would have selected "
                f"{len(selected)} test file(s) and would have skipped any dependent "
                f"it does not textually name. If this repo has grown past "
                f"classifier.indexer.DEFAULT_MAX_INDEX_FILES "
                f"({DEFAULT_MAX_INDEX_FILES}) .py files then EVERY gate run from now "
                f"on runs the full suite and may exceed SWARMSYNC_GATE_TIMEOUT; raise "
                f"that cap to restore impact selection.\n"
            )
        elif selected:
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
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        # A misconfigured SWARMSYNC_GATE_PYTHON (typo, deleted venv, a path that
        # is not executable) cannot even be spawned. Report it as a REJECTION with
        # the reason named, rather than letting OSError escape into
        # `integrate`'s error path while it holds the global integrate lock: the
        # verdict "we could not test this" must never read as "this is fine", and
        # an operator staring at rejected merges needs the knob's name in the log.
        return False, (
            f"swarm-sync: could not start the test gate's interpreter {python!r} "
            f"({exc}). Set SWARMSYNC_GATE_PYTHON to a python that can run this "
            f"repo's tests, or unset it to use swarm-sync's own ({sys.executable}). "
            f"Treating as a gate FAILURE: a branch whose tests never ran cannot be "
            f"shown to keep trunk green."
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
        # Name the widening in the timeout message when it applies. This is the one
        # place the widening's chosen failure mode actually bites, and "your tests do
        # not terminate" would be a misdiagnosis of "the gate ran your entire suite
        # because it could not index the repo".
        widened_note = (
            ""
            if not notice
            else (
                " NOTE: this run had been WIDENED to the whole suite because impact "
                "selection's dependency graph was unavailable, so the timeout may be "
                "the widening rather than a non-terminating test -- fix the indexing "
                "failure named above, or raise SWARMSYNC_GATE_TIMEOUT."
            )
        )
        return False, (
            f"{notice}{stdout}{stderr}\n"
            f"swarm-sync: test gate exceeded {timeout:.0f}s and was killed "
            f"(SWARMSYNC_GATE_TIMEOUT to change). Treating as a gate FAILURE: a "
            f"branch whose tests do not terminate cannot be shown to keep trunk "
            f"green.{widened_note}"
        )
    # pytest exit code 5 == "no tests were collected" -- e.g. a repo/fixture with
    # no test suite yet, or an impact-selection pass that (correctly) found no
    # test touches this change. Nothing to gate on is not a rejection reason.
    ok = returncode in (0, 5)
    log = notice + stdout + stderr
    return ok, log
