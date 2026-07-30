"""H2 / H3 -- is the integrator's impact selection CORRECT, and is it AFFORDABLE,
on a real import graph (a clone of `code-learner`: 45 files, 567 parcels, 94
frozen contracts, 252 tests) rather than on `sample_repo`'s 195 lines?

WHAT THIS FILE MEASURED (numbers from this machine, 2026-07-29)
==============================================================

Every number in this docstring is a DATED OBSERVATION against a fixture repo that is
developed separately and keeps growing (it has since passed 800 parcels). None of them
is asserted as an equality; the assertions below are floors and shapes. If a count here
disagrees with a fresh run, the prose is stale -- that is not a swarm-sync defect.

READ THIS FIRST -- ALL THREE DEFECTS BELOW ARE NOW FIXED
--------------------------------------------------------
These tests were written against a tree where all three defects were live, and
the prose below describes what was originally measured. Since then (DEFECT 3 --
the index cap silently disabling graph selection -- is written up in the H3
section, where it was found):

* **DEFECT 1 IS FIXED** (`swarmsync/classifier/graph.py`, commit 54ade0e).
  `_reverse_dep_files` now reaches all 11 test files from `codelearner/db.py`
  (30 dependent files), graph-signal recall on that change went **0.00 -> 1.00**,
  and the repo's frozen-contract count went **86 -> 94** because eight contracts
  whose dependents had been credited to a package `__init__.py` finally cleared
  `FREEZE_THRESHOLD` (`db.py`'s blast_radius went 3 -> 279). The three tests that
  used to CHARACTERIZE this defect have been inverted into REGRESSION GUARDS --
  they now fail if it comes back, and their messages say "DEFECT 1 HAS REGRESSED".
* **DEFECT 2 IS ALSO FIXED** (`coordinator/gate.py::_reverse_dep_files`). The BFS
  now walks at FILE granularity -- any parcel of a file being reached marks the
  whole file, and expansion continues from every parcel of it. Measured on the
  same clone: the modules where the graph lost reverse-dependents went **10 -> 0**
  (it now agrees exactly with this file's independent file-granularity import
  closure, in both directions), and the **4 test files** it had been losing for
  `codelearner/__init__.py` (`test_chunk`, `test_onboard`, `test_rerank`,
  `test_retrieve`) are recalled by the graph rather than by a substring
  coincidence. The precision cost is small and was measured, not assumed: summed
  over all 34 source modules the gate selects **238 -> 240** test files (+0.84%),
  all of it on that one module (9 -> 11); no other module's selection changed at
  all. The test that CHARACTERIZED this defect has been inverted into a
  regression guard, as its own failure message instructed.

H2 -- correctness, AS ORIGINALLY MEASURED: **the gate's union selection held
(recall 1.00 on all three changes measured), but its authoritative signal did
not.** `gate._reverse_dep_files` -- documented as "the authoritative signal, and
the one that catches indirect dependents a textual scan misses" -- silently lost
reverse-dependencies on **8 of 34** source modules, and on `codelearner/db.py`
it lost **all 37 of them**, including all 11 test files. Recall of the graph
signal alone on that change: **0.00** (11 test files fail, 0 selected).

Two independent root causes, each reproduced below on a purpose-built repo where
the gate APPROVED a change that breaks the suite (`run_impact_tests` -> `ok=True`
while the full suite is red):

  DEFECT 1 -- NOW FIXED (`classifier/graph.py::build_graph`, the `ast.ImportFrom`
  branch).
  `from <package> import <submodule>` -- e.g. `from codelearner import db`,
  `from .. import db` -- is resolved as "import the name `db` from the module
  `codelearner`". `db` is not a symbol in `codelearner/__init__.py`, so it falls
  through to the module-granularity fallback and the edge is attributed to
  `codelearner/__init__.py::<module>`. The dependency on `codelearner/db.py`
  is never recorded at all. `codelearner/__init__.py` is 98 bytes (a docstring
  and `__version__`) and is credited with 11 dependent test files; `db.py`,
  745 lines and the single most-imported module in the repo, is credited with
  none. The impact map for those two files is exactly inverted.
  See `test_h2_from_pkg_import_submodule_is_no_longer_a_gate_false_negative`.

  DEFECT 2 -- NOW FIXED (`coordinator/gate.py::_reverse_dep_files`, the BFS). The walk was
  PARCEL-granular and only projected to file granularity at the very end, so a
  file-level chain broke whenever the intermediate module is imported
  by-symbol and the reached symbol is not the imported one. Traced on the real
  repo: the BFS from `chunk/chunker.py` reaches exactly two of
  `cli/commands.py`'s 13 parcels (`<module>` and `cmd_index`), while
  `server/app.py` imports `resolve_index_path`/`_scalar`/`_classify_unresolved`/
  `_embedding_info` from that same file -- none of them in the reached set -- so
  `server/app.py` was never marked as depending on `chunker.py`. The docstring's
  claim ("Every repo file that TRANSITIVELY reverse-depends on a changed `.py`
  file") was therefore stronger than what the code computed. The fix is what this
  paragraph originally proposed: expand at FILE granularity (which is the
  granularity swarm-sync leases at), so any parcel of a reached file puts the
  whole file in the frontier.
  See `test_h2_defect2_file_granular_bfs_keeps_the_chain`.

Neither defect produced a false negative on code-learner itself, and the reason
was luck, not design: the `db.py` loss was covered by the substring backstop
because the 2-character stem `"db"` happens to occur in all 11 test files, and
Defect 2's losses were (then) all non-test files. Both repros below are minimal
repos, not code-learner -- but they use ordinary import forms and an ordinary
refactor, and the second one does not even need a raise: renaming a function is
enough.

After Defect 1 was fixed, Defect 2 was no longer confined to non-test files: it
lost 4 test files for `codelearner/__init__.py` on the real repo. That was still
not a gate false negative -- the stem `"codelearner"` appears in every test file,
so the substring backstop selected all 11 -- but the thing standing between it
and a merged red change was once again a substring coincidence, not the graph.
With the file-granular walk the graph carries those 4 on its own.

The graph signal is nonetheless LOAD-BEARING, not decoration: for
`codelearner/ingest/types.py::content_hash`, 10 test files genuinely fail and the
substring backstop selects only 2. Deleting the graph rule leaves a NON-EMPTY
selection, so the full-suite fallback does not rescue it -- 8 genuinely failing
test files would never run. That is measured in
`test_h2_graph_selection_is_load_bearing`, which asserts the PROPERTY (deleting the
graph rule strands at least one genuinely failing test file) and merely records the
count, since how many tests exist against `ingest/types.py` is the fixture's business.

H3 -- cost: **held, decisively, and in the OPPOSITE direction from the plan's
suspicion.** Re-indexing is 0.7%--2.6% of gate wall-clock on code-learner:

  change                                  total    re-index    pytest    re-index %
  cli/main.py        (2 test files)       5.74 s     151 ms     5.59 s      2.63%
  ingest/types.py   (10 test files)      18.96 s     154 ms    18.81 s      0.81%
  db.py             (11 test files)      18.07 s     134 ms    17.94 s      0.74%
  sample_repo/calc.py (3 test files)      0.252 s     2.3 ms    0.250 s     0.90%

`_reverse_dep_files` costs ~135--155 ms at 45 files / 567 parcels and 2.3 ms at
sample_repo's 7 files. It buys, on the 2-test-file change, 13.5 s of tests not
run (4.33 s of pytest vs a 17.8 s full suite): a **100x** return. Gate totals
vary run to run by 1--3 s under load; the re-index figure does not, so the
fractions above are stable.

The gate interpreter override costs **+26 ms** of pytest boot (3.12: 124 ms vs
3.11: 98 ms) -- **0.48%** of a code-learner gate run, reported separately so it is
never conflated with re-index cost. Two incidental findings from measuring it:

(a) `PYTHONDONTWRITEBYTECODE=1`, which this module sets for the
    bytecode-staleness reason, can be a large observer effect on THIS
    measurement, but only while a venv's site-packages `__pycache__` is
    incomplete. Measured standalone against a cold cache, swarm-sync's 3.11
    pytest boot went 103 ms -> 233 ms under the flag while code-learner's 3.12
    venv was unchanged at 124 ms -- which inverts the sign of the override and
    made an earlier version of the assertion below pass VACUOUSLY. Once the
    `.pyc` files exist the flag costs nothing (105 ms in-test), so the effect is
    transient and cache-state-dependent, NOT a stable property of the 3.11 venv.
    The test measures with the flag removed regardless, because that is what a
    real gate run experiences, and records both variants.
(b) The repo's own heavy imports (tree-sitter, sqlite-vec, ~44 ms) are paid by
    whichever interpreter runs the suite, so they are not part of the override's
    cost at all.

CROSSOVER. Re-index cost is MEASURED LINEAR in `.py` file count (2.02 ms/file,
per-file spread 1.07x across 35->140 files in-test, and 1.93--2.09 ms/file across
35->1120 files in exploration -- see
`test_h3_reindex_cost_is_linear_in_file_count`), and full-suite time also grows
with repo size, so the break-even is not a repo size at all -- it is a
selectivity threshold, and it is scale-invariant to first order:

    break-even = re-index / full-suite = 0.135 / 17.8 = **0.76%**

Impact selection pays for itself as soon as it excludes more than ~0.8% of suite
runtime. It is a net LOSS only when it excludes nothing (the `db.py` case, where
all 11 files are selected), and even then it wastes 0.74% of the gate.

The real cliff is not cost, it was a silent capability loss -- **DEFECT 3, NOW
FIXED** (`coordinator/gate.py`). `indexer.DEFAULT_MAX_INDEX_FILES = 5000` makes
`index_repo` raise `IndexLimitError`, which `_reverse_dep_files` catches with a
bare `except Exception: return set()`. Above 5000 `.py` files the graph signal
DISAPPEARED with no log line and no event, leaving only the substring heuristic
(and, when that matches nothing, the full-suite fallback). Simulated on the real
clone, that took an `ingest/types.py` change from 10 selected test files down to
2 -- the same 8-file gap that `test_h2_graph_selection_is_load_bearing` shows is
a false negative. Extrapolated re-index at that cap is 10.1 s -- still ~0.5% of
the ~2000 s suite a repo that size would have at code-learner's tests-per-file
ratio, so the cap is not protecting against a cost problem. This is an
EXTRAPOLATION from a measured-linear fit at constant edge density; I did not
measure a repo with denser cross-module coupling, so the `build_graph` edge term
could grow faster than files do.

The bare `except` is unchanged -- selection is best-effort and must never fail the
gate -- but the empty set it returns now carries WHY it is empty, so
`run_impact_tests` can tell "nothing depends on this change" from "I could not
compute the dependents" and WIDENS to the whole suite for the second, logging the
reason in both the gate's returned log and swarm-sync's logger. The accepted cost
is that a repo past the cap whose full suite exceeds `SWARMSYNC_GATE_TIMEOUT`
(default 600 s) rejects every merge on a timeout instead of under-testing it: a
visible, trunk-restoring stall in place of an invisible false negative. The test
that CHARACTERIZED this defect has been inverted into a regression guard,
`test_h3_index_cap_no_longer_silently_disables_graph_selection`, and keeps the
`graph=False` control that measures the narrowing it replaced.

HOW GROUND TRUTH IS DERIVED (and why it is not circular)
========================================================
Two independent oracles, neither of which uses `build_graph`:

1. BEHAVIOURAL (the one recall is computed against). Break a symbol with
   `mutators.break_a_test`, run code-learner's real 252-test suite on its own
   3.12 interpreter, and read the set of test FILES that actually fail out of
   pytest's short summary. A test file that fails and was not selected is a
   gate false negative, full stop -- no graph is consulted to decide that.
2. STRUCTURAL (used for the 34-module sweep, which is too big to run
   behaviourally). `_independent_import_edges` below is this file's OWN
   file-granularity import resolver: `ast`, imports only, no call edges, and it
   resolves `from <pkg> import <submodule>` to the SUBMODULE, which is the thing
   `build_graph` gets wrong. It is a different algorithm reaching a different
   answer, not a second call into the code under test.

The gate's SELECTED set is never mirrored or re-derived here either: every
selection figure is read off the actual `subprocess.Popen` argv the gate builds
(`_gate_selection`), so a mistake in this file cannot flatter the gate.

WHAT IS NOT VERIFIED HERE
=========================
* No source fix. Both defects are real and both are reproduced, but
  `swarmsync/**` is deliberately untouched: Agent C was running against the same
  checkout concurrently. The fix is a separate decision. The mutation checks that
  would normally require editing `swarmsync/` are done instead by in-process
  monkeypatch (`_gate_selection(graph=False)` deletes the graph rule for real,
  authoritatively, in argv) plus discrimination controls -- each repro is run a
  second time with the ONE thing changed that is claimed to be the cause, and
  must then be caught.
* Whether either defect has ever let a red change reach trunk in practice. It
  cannot on code-learner (see above), and I did not audit any other repo.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence
from unittest import mock

import pytest

from swarmsync.agent import mutators
from swarmsync.classifier.indexer import IndexLimitError, index_repo
from swarmsync.coordinator import gate
from tests.scale import harness

# --- measurement sink -------------------------------------------------------------
#
# Every number this file produces lands here and is printed by the last test, so a
# run with `-s` is a report rather than a wall of dots. Assertions still carry their
# own numbers in the failure message -- this is in ADDITION to that, never instead.
MEASUREMENTS: dict[str, Any] = {}


def _record(key: str, value: Any) -> Any:
    MEASUREMENTS[key] = value
    return value


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def scale() -> Iterator[harness.ScaleRepo]:
    """The shared clone + blackboard + index (see `harness`'s docstring).

    `PYTHONDONTWRITEBYTECODE` is forced for the whole module: this file restores
    and re-breaks files in the clone within the same clock second, and CPython
    will happily reuse a `__pycache__` entry whose size and mtime-second match,
    which looks exactly like a non-binding test. `config.subprocess_env` copies
    `os.environ`, so setting it here is what makes the GATE's pytest subprocess
    inherit it too.
    """
    previous = os.environ.get("PYTHONDONTWRITEBYTECODE")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        with harness.scale_blackboard() as sr:
            _record("setup_seconds", round(sr.setup_seconds, 3))
            _record("parcels", sr.parcel_count)
            _record("contracts", sr.contract_count)
            yield sr
    finally:
        if previous is None:
            os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        else:
            os.environ["PYTHONDONTWRITEBYTECODE"] = previous


# --- this file's OWN import resolver (ground-truth oracle #2) ----------------------
#
# Deliberately NOT `classifier.graph`. File granularity, imports only, no call
# edges -- and `from <pkg> import <submodule>` resolves to the SUBMODULE, which is
# precisely what `build_graph` gets wrong. Comparing the gate against this is
# comparing two algorithms; comparing it against `build_graph` would only prove
# that a function equals itself.

_SKIP_PARTS = {"__pycache__", "node_modules", "venv"}

# The import FORM each edge came from, so a discrepancy can be attributed to a
# cause rather than just counted.
FORM_PLAIN_IMPORT = "import X"
FORM_FROM_MODULE = "from MOD import name"
FORM_FROM_PKG_SUBMODULE = "from PKG import SUBMODULE"


def _repo_py_files(root: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(p.startswith(".") or p in _SKIP_PARTS for p in rel.parts[:-1]):
            continue
        out.append(rel.as_posix())
    return out


def _dotted_namespace(files: Sequence[str]) -> dict[str, str]:
    namespace: dict[str, str] = {}
    for rel in files:
        dotted = rel[:-3].replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        namespace[dotted] = rel
    return namespace


def _independent_import_edges(
    root: Path, files: Sequence[str]
) -> list[tuple[str, str, str]]:
    """`(importer, imported_file, form)` for every in-repo import, my own way."""
    namespace = _dotted_namespace(files)
    edges: list[tuple[str, str, str]] = []
    for rel in files:
        tree = ast.parse((root / rel).read_text(encoding="utf-8"), filename=rel)
        own_package = rel[:-3].split("/")[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    # `import a.b.c` binds (and executes) a, a.b and a.b.c.
                    for i in range(1, len(parts) + 1):
                        candidate = ".".join(parts[:i])
                        if candidate in namespace:
                            edges.append((rel, namespace[candidate], FORM_PLAIN_IMPORT))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = (
                        own_package[: len(own_package) - (node.level - 1)]
                        if node.level > 1
                        else own_package
                    )
                    dotted = (
                        ".".join([*base, node.module]) if node.module else ".".join(base)
                    )
                else:
                    dotted = node.module or ""
                for alias in node.names:
                    submodule = f"{dotted}.{alias.name}" if dotted else alias.name
                    if submodule in namespace:
                        # THE FORM build_graph mishandles: the alias names a module.
                        edges.append(
                            (rel, namespace[submodule], FORM_FROM_PKG_SUBMODULE)
                        )
                        # ...and importing a submodule ALSO executes the package's
                        # `__init__.py`, so that is a genuine dependency too. Omitting
                        # it made this oracle under-approximate exactly where
                        # `build_graph` over-approximates, which read as the two
                        # disagreeing in both directions when in fact only one of
                        # them was wrong.
                        if dotted in namespace:
                            edges.append((rel, namespace[dotted], FORM_FROM_MODULE))
                    elif dotted in namespace:
                        edges.append((rel, namespace[dotted], FORM_FROM_MODULE))
    return [(a, b, form) for a, b, form in edges if a != b]


def _reverse_map(edges: Sequence[tuple[str, str, str]]) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = {}
    for importer, imported, _form in edges:
        reverse.setdefault(imported, set()).add(importer)
    return reverse


def _independent_dependents(reverse: dict[str, set[str]], changed: str) -> set[str]:
    """Transitive closure of "imports, directly or indirectly", at file granularity."""
    found: set[str] = set()
    stack = [changed]
    while stack:
        current = stack.pop()
        for importer in reverse.get(current, ()):
            if importer not in found:
                found.add(importer)
                stack.append(importer)
    found.discard(changed)
    return found


def _test_files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() for p in (root / "tests").rglob("test_*.py")
    )


# --- reading the gate's REAL selection off its argv --------------------------------


class _RecordedPopen:
    """A `Popen` stand-in that runs nothing and reports success.

    Lets us read the argv `run_impact_tests` built -- i.e. the selection it
    actually made -- without paying for the pytest run. `communicate()` returns
    empty output and `returncode` 0, so `run_impact_tests` returns `(True, "")`;
    callers here only ever look at the recorded `cmd`.
    """

    def __init__(self, sink: list[list[str]], cmd: list[str], **_: Any) -> None:
        sink.append(list(cmd))
        self.returncode = 0
        self.pid = os.getpid()
        self.stdout = None
        self.stderr = None

    def communicate(self, timeout: Optional[float] = None) -> tuple[str, str]:
        return "", ""

    def kill(self) -> None:  # pragma: no cover -- never reached, we never time out
        pass


def _gate_selection(
    repo: Path, changed: list[str], *, graph: bool = True
) -> list[str]:
    """The test files the GATE itself selects for `changed`, read from its argv.

    `graph=False` deletes the dependency-graph rule (`_reverse_dep_files` -> the
    empty set) and returns what is left: the substring backstop, or -- if that
    matches nothing -- the full-suite fallback, which shows up as the bare
    `test_dir` argument. This is the mutation check for that rule, done
    in-process so it cannot disturb anything else using this checkout.
    """
    sink: list[list[str]] = []
    with mock.patch.object(
        gate.subprocess,
        "Popen",
        lambda cmd, **kw: _RecordedPopen(sink, cmd, **kw),
    ):
        if graph:
            gate.run_impact_tests(repo, changed)
        else:
            with mock.patch.object(gate, "_reverse_dep_files", lambda r, c: set()):
                gate.run_impact_tests(repo, changed)
    assert len(sink) == 1, f"expected exactly one gate subprocess, got {sink}"
    cmd = sink[0]
    # Everything after the pytest flags is a path: either selected test files or
    # the bare test_dir fallback.
    return [
        Path(arg).as_posix()
        for arg in cmd
        if arg.endswith(".py") or arg == gate.DEFAULT_TEST_DIR
    ]


def _substring_selection(root: Path, changed: str, tests: Sequence[str]) -> set[str]:
    """The backstop rule alone: does the changed file's bare stem occur in the text?"""
    stem = Path(changed).stem
    if not stem:
        return set()
    return {t for t in tests if stem in (root / t).read_text(encoding="utf-8")}


def _graph_selection(root: Path, changed: str, tests: Sequence[str]) -> set[str]:
    """The dependency-graph rule alone, restricted to test files."""
    return {t for t in gate._reverse_dep_files(root, {changed}) if t in set(tests)}


# --- behavioural ground truth (oracle #1) -----------------------------------------

_FAILING = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:::|\s|$)", re.M)


def _pytest_env() -> dict[str, str]:
    env = {
        **os.environ,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for var in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST"):
        env.pop(var, None)
    return env


def _run_suite(
    root: Path, python: Path | str, paths: Optional[Sequence[str]] = None
) -> tuple[float, int, str]:
    """Run a repo's own suite with the gate's flags plus `--tb=no -rfE`, so the
    short summary names every failing/erroring test FILE."""
    cmd = [
        str(python),
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--import-mode=importlib",
        "--tb=no",
        "-rfE",
        *(paths or ["tests"]),
    ]
    started = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(root),
        capture_output=True,
        text=True,
        env=_pytest_env(),
        timeout=1800,
    )
    return time.perf_counter() - started, proc.returncode, proc.stdout + proc.stderr


def _failing_test_files(output: str) -> set[str]:
    return {m for m in _FAILING.findall(output) if m.endswith(".py")}


@contextmanager
def _broken(root: Path, path: str, symbol: str) -> Iterator[None]:
    """Make `symbol` raise, then restore `root` to HEAD on the way out.

    `__pycache__` is cleared on entry AND exit: this file breaks several symbols
    in the same clone inside a few seconds, and a stale `.pyc` whose size and
    mtime-second happen to match is indistinguishable from a test that does not
    bind.
    """

    def clear_pycache() -> None:
        for cache in root.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)

    clear_pycache()
    try:
        mutators.break_a_test(root, path, symbol, message="B-H2-GROUND-TRUTH")
        yield
    finally:
        subprocess.run(
            ["git", "-C", str(root), "checkout", "--", "."],
            check=True,
            capture_output=True,
            text=True,
        )
        clear_pycache()


@dataclass
class BehaviouralTruth:
    """Which test FILES actually fail when one symbol is made to raise."""

    path: str
    symbol: str
    failing: set[str]
    suite_seconds: float
    returncode: int


# `ingest/types.py::content_hash` -- the case where the graph rule is load-bearing:
# 10 test files genuinely fail, the substring backstop finds 2, and the surviving
# selection is non-empty so the full-suite fallback does not cover the gap.
GT_LOAD_BEARING = ("codelearner/ingest/types.py", "content_hash")
# `db.py::connect` -- Defect 1 at scale: every test file fails, the graph finds none.
GT_DEFECT_1 = ("codelearner/db.py", "connect")
# `retrieve/lexical.py::escape_fts_query` -- the harness's verified BREAK_TARGET,
# imported the ordinary way (`from .lexical import Hit, search_lexical`). The
# DISCRIMINATION control for Defect 1: same repo, same machine, same oracle, but an
# import form `build_graph` handles -- so the graph must score 1.00 here.
GT_CONTROL = (harness.BREAK_TARGET["path"], harness.BREAK_TARGET["symbol"])


@pytest.fixture(scope="module")
def baseline(scale: harness.ScaleRepo) -> float:
    """Full-suite wall-clock on the UNTOUCHED clone, and proof it is green.

    Two jobs, one 18-second run (shared by H2 and H3 rather than paid twice):
    it is the denominator of H3's crossover, and it is what makes H2's "these
    files fail" attributable to the injected break rather than to pre-existing
    red. If the suite were not green here, every recall figure below would be
    measuring the wrong thing.
    """
    seconds, returncode, log = _run_suite(scale.root, scale.gate_python)
    failing = _failing_test_files(log)
    _record("baseline_suite_seconds", round(seconds, 2))
    _record("baseline_returncode", returncode)
    assert returncode == 0 and not failing, (
        "code-learner's suite is NOT green on the untouched clone "
        f"(rc={returncode}, failing={sorted(failing)}). Every H2 recall figure "
        "attributes failures to the injected break, so this must hold first."
        f"\n{log[-3000:]}"
    )
    return seconds


@pytest.fixture(scope="module")
def ground_truth(
    scale: harness.ScaleRepo, baseline: float
) -> dict[str, BehaviouralTruth]:
    """Behavioural ground truth: which test FILES actually fail, per change."""
    truths: dict[str, BehaviouralTruth] = {}
    for path, symbol in (GT_LOAD_BEARING, GT_DEFECT_1, GT_CONTROL):
        with _broken(scale.root, path, symbol):
            seconds, returncode, log = _run_suite(scale.root, scale.gate_python)
        failing = _failing_test_files(log)
        assert failing, (
            f"breaking {path}::{symbol} made NOTHING fail -- the chosen symbol is "
            "not exercised by the suite, which would make every recall figure "
            f"below vacuous.\n{log[-3000:]}"
        )
        truths[path] = BehaviouralTruth(
            path=path,
            symbol=symbol,
            failing=failing,
            suite_seconds=seconds,
            returncode=returncode,
        )
        _record(
            f"ground_truth[{path}]",
            {"symbol": symbol, "failing": sorted(failing), "n": len(failing)},
        )
    return truths


# =====================================================================================
# H2 -- correctness
# =====================================================================================


def test_h2_graph_selection_never_exceeds_an_independent_import_closure(
    scale: harness.ScaleRepo,
) -> None:
    """The gate's graph is a SUBSET of plain transitive import reachability.

    Worth pinning before any recall claim, because it settles what
    `_reverse_dep_files` is: its call edges buy it nothing beyond import
    reachability at file granularity, so every discrepancy with the independent
    closure below is the graph MISSING something, never the graph knowing more.
    """
    files = _repo_py_files(scale.root)
    reverse = _reverse_map(_independent_import_edges(scale.root, files))
    modules = [f for f in files if f.startswith("codelearner/")]

    over: dict[str, list[str]] = {}
    for module in modules:
        mine = _independent_dependents(reverse, module)
        theirs = gate._reverse_dep_files(scale.root, {module}) - {module}
        extra = sorted(theirs - mine)
        if extra:
            over[module] = extra

    _record("graph_over_approximates_on_modules", over)
    assert not over, (
        "the dependency graph claims reverse-dependents that plain transitive "
        "import reachability does not, so the two oracles disagree in BOTH "
        f"directions and the subset framing below is wrong: {over}"
    )


def test_h2_graph_selection_keeps_test_dependents_on_the_real_graph(
    scale: harness.ScaleRepo,
) -> None:
    """`_reverse_dep_files` finds `codelearner/db.py`'s dependents. It did not before.

    **This test was inverted when Defect 1 was fixed**, following the instruction its
    own failure message carried. As originally written it asserted the defect: the
    graph lost reverse-dependents on 8 of 34 source modules, and on `db.py` -- imported
    exclusively as `from codelearner import db` -- it found *no* dependent file at all,
    because `build_graph` credited the edge to `codelearner/__init__.py`. It found 29
    after that fix, and 37 -- every file the independent closure knows of, including all
    11 test files -- once Defect 2's parcel-granular walk was fixed too.

    Kept rather than deleted, and still keyed on `db.py` specifically, because that
    file is the sharpest available probe: it is imported only via the form that broke,
    so a regression in `ast.ImportFrom` handling shows up here first and unambiguously.

    This is the structural half of H2: a 34-module sweep is too expensive to run
    behaviourally, so it is measured against this file's own import closure. The
    behavioural half (`test_h2_db_py_...`) confirms the figure by running the suite.
    """
    files = _repo_py_files(scale.root)
    edges = _independent_import_edges(scale.root, files)
    reverse = _reverse_map(edges)
    tests = _test_files(scale.root)
    modules = [f for f in files if f.startswith("codelearner/")]

    lost: dict[str, dict[str, list[str]]] = {}
    for module in modules:
        mine = _independent_dependents(reverse, module)
        theirs = gate._reverse_dep_files(scale.root, {module}) - {module}
        missing = mine - theirs
        if missing:
            lost[module] = {
                "all": sorted(missing),
                "test_files": sorted(m for m in missing if m in tests),
            }

    _record("modules_scanned", len(modules))
    _record(
        "modules_where_graph_loses_dependents",
        {k: {"n_lost": len(v["all"]), "n_test_files_lost": len(v["test_files"])}
         for k, v in lost.items()},
    )

    # The headline: db.py. This assertion was inverted when the fix landed -- the
    # characterization test went red exactly as its author intended it to, and the
    # right response was to invert it rather than silence it.
    db = "codelearner/db.py"
    db_importers = sorted({a for a, b, _ in edges if b == db})
    db_graph = gate._reverse_dep_files(scale.root, {db}) - {db}
    db_mine = _independent_dependents(reverse, db)
    _record(
        "db_py",
        {
            "direct_importers": db_importers,
            "independent_transitive_dependents": len(db_mine),
            "independent_dependent_test_files": sorted(m for m in db_mine if m in tests),
            "graph_dependents": sorted(db_graph),
            "import_forms": sorted({f for a, b, f in edges if b == db}),
        },
    )
    assert db_importers, "nothing imports db.py -- fixture assumption broken"
    assert {f for a, b, f in edges if b == db} == {FORM_FROM_PKG_SUBMODULE}, (
        "db.py is no longer imported exclusively via `from <pkg> import <submodule>`, "
        "so this module no longer isolates Defect 1"
    )
    # Post-fix: the graph must find db.py's dependents, and must not lose the test
    # files among them -- those are what impact selection exists to run.
    assert db_graph, (
        "DEFECT 1 HAS REGRESSED: `_reverse_dep_files` finds NO dependent file for "
        f"{db}, which is imported only as `from codelearner import db`. That form is "
        "being credited to `codelearner/__init__.py` again, which inverts blast_radius "
        "and silently disables contract freezing for every module imported that way."
    )
    lost_tests = sorted(m for m in (db_mine - db_graph) if m in tests)
    assert not lost_tests, (
        f"the graph signal lost dependent TEST files for {db}: {lost_tests}. These "
        "are exactly the tests impact selection would fail to run, which is what makes "
        "a loss a false negative rather than merely an inefficiency."
    )

    # `db.py` used to STILL under-approximate by 8 non-test files after Defect 1 was
    # fixed -- Defect 2's doing: the then parcel-granular BFS severed file-level chains
    # when the intermediate module was imported by-symbol. With the file-granular walk
    # that residue is gone too (measured: 0 lost, on every one of the 34 modules).
    #
    # Asserted on the TEST-file subset rather than on the sweep as a whole on purpose.
    # An earlier version of this asserted `db not in lost` -- that no under-
    # approximation remained at all -- and it failed, correctly: it silently assumed
    # fixing one defect fixed the other. What matters for gate correctness is whether a
    # failing test can go unselected, and the answer for `db.py` is now no.
    residual = lost.get(db, {}).get("all", [])
    _record("db_py_residual_defect2_losses", residual)
    assert not residual, (
        f"{db} is losing dependent files again: {lost.get(db)}. Post-Defect-2 the "
        "graph must agree with the independent import closure exactly."
    )
    assert len(db_mine) >= 26 and len([m for m in db_mine if m in tests]) == len(tests), (
        f"expected db.py to be transitively imported by every test file; got "
        f"{len(db_mine)} dependents, {sorted(m for m in db_mine if m in tests)} tests"
    )

    # Defect 2, traced rather than asserted from counts. The PARCEL-granular walk
    # replicated inline below still reaches only part of cli/commands.py, and
    # server/app.py still imports a different part -- that is a property of the raw
    # graph and has not changed. What changed is that `_reverse_dep_files` no longer
    # walks that way: it expands at FILE granularity, so the chain
    # chunker -> commands -> app holds and app.py IS selected.
    chunker = "codelearner/chunk/chunker.py"
    parcels = index_repo(scale.root)
    from swarmsync.classifier.graph import build_graph  # local: only Defect 2 needs it

    graph = build_graph(parcels, scale.root)
    seeds = {p.id for p in parcels if p.path == chunker}
    seen, frontier = set(seeds), list(seeds)
    while frontier:
        nxt = []
        for pid in frontier:
            for dependent in graph.reverse_edges.get(pid, set()):
                if dependent not in seen:
                    seen.add(dependent)
                    nxt.append(dependent)
        frontier = nxt
    commands = "codelearner/cli/commands.py"
    commands_all = {p.id for p in parcels if p.path == commands}
    commands_hit = seen & commands_all
    app_needs = {
        d
        for pid, deps in graph.edges.items()
        if pid.startswith("codelearner/server/app.py")
        for d in deps
        if d.startswith(commands)
    }
    _record(
        "defect2_trace",
        {
            "commands_parcels_total": len(commands_all),
            "commands_parcels_reached_from_chunker": sorted(commands_hit),
            "parcels_app_imports_from_commands": sorted(app_needs),
            "app_is_selected_for_a_chunker_change": "codelearner/server/app.py"
            in gate._reverse_dep_files(scale.root, {chunker}),
        },
    )
    assert commands_hit and commands_hit != commands_all, (
        "the BFS from chunker.py either misses cli/commands.py entirely or reaches "
        "all of it; either way this no longer demonstrates partial-file reachability "
        f"(reached {len(commands_hit)} of {len(commands_all)})"
    )
    assert app_needs and not (app_needs & commands_hit), (
        "server/app.py now imports at least one cli/commands.py parcel that the "
        "chunker BFS reaches, so Defect 2 no longer manifests on this chain "
        f"(app needs {sorted(app_needs)}, BFS reached {sorted(commands_hit)})"
    )
    assert "codelearner/server/app.py" in gate._reverse_dep_files(
        scale.root, {chunker}
    ), (
        "DEFECT 2 HAS REGRESSED: server/app.py is not a dependent of chunker.py, "
        "though it imports cli/commands.py which imports chunker.py. The reverse-dep "
        "walk has gone back to parcel granularity, which severs a file-level chain "
        "whenever the intermediate module is imported by a symbol the walk did not "
        "happen to reach."
    )


def test_h2_gate_union_selection_has_no_false_negatives(
    scale: harness.ScaleRepo, ground_truth: dict[str, BehaviouralTruth]
) -> None:
    """H2's actual verdict: recall of the gate's UNION selection, against test
    files that were observed to fail. This is the assertion that would catch a
    real correctness bug reaching trunk.

    Precision is reported, not asserted -- over-selection is the documented
    design ("over-selecting a test is always safe, skipping an affected one never
    is") and costs only time.
    """
    report: dict[str, dict[str, Any]] = {}
    false_negatives: dict[str, list[str]] = {}
    for path, truth in ground_truth.items():
        selected = set(_gate_selection(scale.root, [path]))
        if gate.DEFAULT_TEST_DIR in selected:
            # The full-suite fallback: everything runs, so nothing can be missed.
            selected = set(_test_files(scale.root))
        missed = truth.failing - selected
        hit = truth.failing & selected
        report[path] = {
            "symbol": truth.symbol,
            "failing": len(truth.failing),
            "selected": len(selected),
            "recall": round(len(hit) / len(truth.failing), 4),
            "precision": round(len(hit) / len(selected), 4) if selected else None,
            "suite_seconds_when_broken": round(truth.suite_seconds, 2),
        }
        if missed:
            false_negatives[path] = sorted(missed)

    _record("h2_union_precision_recall", report)
    assert not false_negatives, (
        "GATE FALSE NEGATIVE on the real repo: these test files FAILED when the "
        "change was applied and the gate did not select them, so the gate would "
        f"have approved a merge that breaks trunk: {false_negatives}"
    )


def test_h2_graph_selection_is_load_bearing(
    scale: harness.ScaleRepo, ground_truth: dict[str, BehaviouralTruth]
) -> None:
    """Deleting the graph rule turns a clean change-set into a false negative.

    The mutation check for `_reverse_dep_files`, run against the authoritative
    argv rather than a re-implementation of the selection rule. `ingest/types.py`
    is the case that matters: the substring backstop selects a NON-EMPTY subset,
    so the full-suite fallback never fires and the missing test files are simply
    never run.
    """
    path, symbol = GT_LOAD_BEARING
    truth = ground_truth[path]
    with_graph = set(_gate_selection(scale.root, [path]))
    without_graph = set(_gate_selection(scale.root, [path], graph=False))
    substring = _substring_selection(scale.root, path, _test_files(scale.root))
    graph_only = _graph_selection(scale.root, path, _test_files(scale.root))

    _record(
        "h2_graph_load_bearing",
        {
            "change": f"{path}::{symbol}",
            "failing": sorted(truth.failing),
            "selected_with_graph": sorted(with_graph),
            "selected_without_graph": sorted(without_graph),
            "substring_only": sorted(substring),
            "graph_only": sorted(graph_only),
            "missed_if_graph_deleted": sorted(truth.failing - without_graph),
        },
    )

    assert gate.DEFAULT_TEST_DIR not in without_graph, (
        "deleting the graph rule left an EMPTY selection, so the full-suite "
        "fallback covers the gap and this change cannot demonstrate that the "
        "graph is load-bearing -- pick a change whose stem matches some test text"
    )
    assert truth.failing <= with_graph, (
        f"the graph rule does not even cover this change: failing="
        f"{sorted(truth.failing)} selected={sorted(with_graph)}"
    )
    missed = truth.failing - without_graph
    # ONE stranded failing test file is already the whole finding: the gate would
    # approve a merge that breaks trunk. This used to demand `>= 8` -- the count
    # measured on 2026-07-29 -- and that number is a fact about how many tests the
    # fixture repo happens to have written against `ingest/types.py`, not about
    # whether the graph rule is load-bearing. It fell to 3 when the fixture grew and
    # the substring backstop happened to match more of the new test text, which made
    # a green property look red. The count is still reported in the record above.
    assert missed, (
        "deleting the graph rule stranded NO genuinely failing test file, so the "
        "graph rule is not load-bearing for this change and this mutation check is "
        f"vacuous. failing={sorted(truth.failing)} without_graph={sorted(without_graph)}"
    )


def test_h2_db_py_graph_recall_is_perfect_behaviourally(
    scale: harness.ScaleRepo, ground_truth: dict[str, BehaviouralTruth]
) -> None:
    """Graph-signal recall at scale, confirmed by running the suite -- with the control
    that made the import FORM attributable as the cause.

    **Inverted when Defect 1 was fixed.** Before: `db.py::connect` broke 11 test files
    and the graph signal selected *none* of them (recall 0.00) while
    `retrieve/lexical.py::escape_fts_query`, imported the ordinary way in the same repo
    under the same oracle, scored 1.00. That pairing is what pinned the cause to
    `ast.ImportFrom` rather than to "the graph is bad at something". Both now score
    1.00, and the control is retained because it is what keeps the diagnosis honest if
    this ever regresses.
    """
    tests = _test_files(scale.root)
    scores: dict[str, dict[str, Any]] = {}
    for path, truth in ground_truth.items():
        graph_only = _graph_selection(scale.root, path, tests)
        scores[path] = {
            "symbol": truth.symbol,
            "failing": len(truth.failing),
            "graph_selected": len(graph_only),
            "graph_recall": round(len(truth.failing & graph_only) / len(truth.failing), 4),
            # Of the failing test files the GRAPH signal missed, which ones only the
            # substring backstop is holding up. Parenthesised because `-` binds
            # tighter than `&` and the unbracketed form reads as if it did not.
            "rescued_by_substring": sorted(
                (truth.failing - graph_only)
                & _substring_selection(scale.root, path, tests)
            ),
        }
    _record("h2_graph_only_recall", scores)

    db_path, _ = GT_DEFECT_1
    control_path, _ = GT_CONTROL
    assert scores[db_path]["graph_recall"] == 1.0, (
        "DEFECT 1 HAS REGRESSED behaviourally: the graph signal no longer recalls the "
        f"test files that actually fail when {db_path} breaks ({scores[db_path]}). It "
        "is imported only as `from codelearner import db`, so this is the "
        "`ast.ImportFrom` misattribution returning."
    )
    assert scores[control_path]["graph_recall"] == 1.0, (
        "the control is not clean: the graph signal should have perfect recall for "
        f"{control_path}, which is imported the ordinary way, but scored "
        f"{scores[control_path]}. Without this, the db.py result cannot be "
        "attributed to the import form."
    )
    # Pre-fix, the substring backstop was the ONLY thing covering Defect 1 here, and it
    # did so by luck: the 2-character stem "db" happens to appear in all 11 test files.
    # Post-fix the graph carries it on its own, so nothing needs rescuing -- and that is
    # the assertion, because a non-empty rescue set would mean the graph had gone back
    # to depending on a coincidence about a filename.
    assert scores[db_path]["rescued_by_substring"] == [], (
        "the graph signal is missing failing test files that only the substring "
        f"backstop covers ({scores[db_path]}). On this repo that backstop works by "
        'accident -- the stem "db" occurs in every test filename -- so relying on it '
        "again means the gate is one rename away from a false negative."
    )


# --- the two defects, reproduced on minimal repos where the gate says yes ----------


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


_SYNTH_CONFTEST = """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
"""

# A test file that mentions the changed module's stem and exercises nothing. Its
# only job is to keep the substring backstop's selection NON-EMPTY, so the
# full-suite fallback does not fire and the gate really does run a subset.
_SYNTH_DECOY = """
    \"\"\"Mentions the {stem} module by name; exercises nothing.\"\"\"


    def test_decoy():
        assert True
"""


@contextmanager
def _synthetic_repo() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="swarmsync-b-repro-"))
    try:
        _write(root, "tests/conftest.py", _SYNTH_CONFTEST)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _gate_vs_suite(root: Path) -> tuple[bool, int, set[str], list[str], str]:
    """`(gate_ok, suite_rc, failing_files, gate_selection, gate_log)` for one repo.

    The gate runs on swarm-sync's OWN interpreter here: these repos have no
    dependencies, and pinning the interpreter keeps the reproduction independent
    of the 3.12 override the harness installs for code-learner.
    """
    _seconds, suite_rc, suite_log = _run_suite(root, sys.executable)
    with harness.gate_interpreter(sys.executable):
        selection = _gate_selection(root, ["core.py"])
        ok, gate_log = gate.run_impact_tests(root, ["core.py"])
    return ok, suite_rc, _failing_test_files(suite_log), selection, gate_log


def test_h2_from_pkg_import_submodule_is_no_longer_a_gate_false_negative() -> None:
    """DEFECT 1, now fixed: `from <pkg> import <submodule>` keeps the dependency, and
    the gate REJECTS a change that breaks the suite.

    **Inverted when the fix landed**, per the instruction this test's own failure
    message carried. As written it reproduced the defect on a minimal repo: the gate
    returned `ok=True` while the suite was red, because `build_graph` attributed
    `from pkg import core` to `pkg/__init__.py` and never to `pkg/core.py`.

    The discrimination control is retained and still matters: the identical repo with
    the identical break, differing only in using `from pkg.core import value`, was
    rejected all along. Both forms must now reject. Keeping both is what would let
    anyone tell a general gate regression (both fail) from this specific defect
    returning (only the submodule form fails).
    """
    results: dict[str, dict[str, Any]] = {}
    for label, mid_import, mid_call in (
        # The defective form: `core` is a submodule of `pkg`, so build_graph
        # attributes the edge to pkg/__init__.py and never to pkg/core.py.
        ("from_pkg_import_submodule", "from pkg import core", "core.value()"),
        # The control: an import form build_graph resolves symbol-precisely.
        ("from_module_import_symbol", "from pkg.core import value", "value()"),
    ):
        with _synthetic_repo() as root:
            _write(root, "pkg/__init__.py", "")
            _write(root, "pkg/core.py", "def value():\n    return 1\n")
            _write(
                root,
                "pkg/mid.py",
                f"""
                {mid_import}


                def compute():
                    return {mid_call} + 1
                """,
            )
            # Genuinely exercises pkg/core.py, and its text never contains "core".
            _write(
                root,
                "tests/test_alpha.py",
                """
                from pkg.mid import compute


                def test_alpha():
                    assert compute() == 2
                """,
            )
            _write(root, "tests/test_decoy.py", _SYNTH_DECOY.format(stem="core"))

            with harness.gate_interpreter(sys.executable):
                clean_ok, _ = gate.run_impact_tests(root, ["pkg/core.py"])
            assert clean_ok, "the untouched synthetic repo must gate green first"

            _write(
                root,
                "pkg/core.py",
                'def value():\n    raise RuntimeError("B-REPRO-1-POISON")\n',
            )
            _seconds, suite_rc, suite_log = _run_suite(root, sys.executable)
            with harness.gate_interpreter(sys.executable):
                selection = _gate_selection(root, ["pkg/core.py"])
                gate_ok, _log = gate.run_impact_tests(root, ["pkg/core.py"])
            failing = _failing_test_files(suite_log)
            results[label] = {
                "graph_dependents": sorted(
                    gate._reverse_dep_files(root, {"pkg/core.py"})
                ),
                "gate_selection": selection,
                "suite_returncode": suite_rc,
                "failing": sorted(failing),
                "gate_ok": gate_ok,
            }

    _record("defect1_repro", results)
    bad, control = results["from_pkg_import_submodule"], results["from_module_import_symbol"]

    assert bad["failing"] == ["tests/test_alpha.py"], (
        f"the reproduction's own premise failed: expected tests/test_alpha.py to "
        f"break, got {bad}"
    )
    assert gate.DEFAULT_TEST_DIR not in bad["gate_selection"], (
        "the gate fell back to the whole suite, so this proves nothing about the graph "
        f"signal either way -- selection was {bad['gate_selection']}"
    )
    assert bad["gate_ok"] is False, (
        "DEFECT 1 HAS REGRESSED: the gate APPROVED a change that breaks the suite. "
        f"({bad}) `from pkg import core` is being attributed to pkg/__init__.py again, "
        "so pkg/core.py's dependents are invisible to impact selection."
    )
    assert "pkg/core.py" in bad["graph_dependents"] or bad["graph_dependents"], (
        f"the graph found no dependents at all for pkg/core.py: {bad}"
    )
    assert control["gate_ok"] is False, (
        "the discrimination control failed: with `from pkg.core import value` the "
        f"gate must reject the very same break, but it returned ok={control['gate_ok']} "
        f"({control}). Without this, the false negative cannot be attributed to the "
        "import form."
    )


def test_h2_defect2_file_granular_bfs_keeps_the_chain() -> None:
    """DEFECT 2, now fixed: the file-granular walk keeps a file-level chain intact and
    the gate REJECTS an ordinary rename that breaks the suite.

    **Inverted when the fix landed**, per the instruction this test's own failure
    message carried. As written it reproduced the defect: the gate returned `ok=True`
    while the suite was red, because the PARCEL-granular BFS reached only
    `mid.py::<module>` and `tests/test_alpha.py` imports `b` from `mid` -- a symbol
    off that chain -- so the test file was never selected.

    Nothing here needs an exotic import form and nothing raises: `value` is simply
    renamed, which is what `mutators.change_signature` and any real agent do
    routinely. `mid.py` then fails to import, so `tests/test_alpha.py` errors at
    collection -- and the gate must run it.

    The discrimination control is retained and still matters: the identical repo whose
    test imports `a` -- the symbol that sat ON the parcel chain -- was rejected all
    along. Both must now reject, and keeping both is what lets anyone tell a general
    gate regression (both fail) from this specific defect returning (only the
    off-chain form fails).
    """
    results: dict[str, dict[str, Any]] = {}
    for label, imported_symbol, call, expected in (
        # `b` is NOT on the parcel chain the old BFS walked, so the file-level
        # dependency test_alpha -> mid -> core used to be lost.
        ("test_imports_off_chain_symbol", "b", "b() == 42", 42),
        # `a` IS on it: same files, same rename, and the chain held even then.
        ("test_imports_on_chain_symbol", "a", "a() == 1", 1),
    ):
        with _synthetic_repo() as root:
            _write(root, "core.py", "def value():\n    return 1\n")
            _write(
                root,
                "mid.py",
                """
                from core import value


                def a():
                    return value()


                def b():
                    return 42
                """,
            )
            _write(
                root,
                "tests/test_alpha.py",
                f"""
                from mid import {imported_symbol}


                def test_alpha():
                    assert {call}
                """,
            )
            _write(root, "tests/test_decoy.py", _SYNTH_DECOY.format(stem="core"))

            with harness.gate_interpreter(sys.executable):
                clean_ok, _ = gate.run_impact_tests(root, ["core.py"])
            assert clean_ok, f"[{label}] untouched synthetic repo must gate green"

            # The change: an ordinary rename. Nothing raises; `mid.py` simply can
            # no longer import the name it asks for.
            _write(root, "core.py", "def valuation():\n    return 1\n")
            _seconds, suite_rc, suite_log = _run_suite(root, sys.executable)
            with harness.gate_interpreter(sys.executable):
                selection = _gate_selection(root, ["core.py"])
                gate_ok, _log = gate.run_impact_tests(root, ["core.py"])
            results[label] = {
                "graph_dependents": sorted(gate._reverse_dep_files(root, {"core.py"})),
                "gate_selection": selection,
                "suite_returncode": suite_rc,
                "failing": sorted(_failing_test_files(suite_log)),
                "gate_ok": gate_ok,
                "expected_value": expected,
            }

    _record("defect2_repro", results)
    bad = results["test_imports_off_chain_symbol"]
    control = results["test_imports_on_chain_symbol"]

    for label, result in results.items():
        assert result["failing"] == ["tests/test_alpha.py"], (
            f"[{label}] the reproduction's premise failed: the rename must break "
            f"tests/test_alpha.py, got {result}"
        )
    # `core.py` is in there because the changed files are returned too (a changed
    # test file must run); `mid.py` is the direct dependent; `tests/test_alpha.py` is
    # the one the parcel-granular walk used to lose.
    assert bad["graph_dependents"] == ["core.py", "mid.py", "tests/test_alpha.py"], (
        "the walk must reach mid.py AND, through every parcel of it, the test file "
        f"that imports an off-chain symbol from it; it reached {bad['graph_dependents']}"
    )
    assert gate.DEFAULT_TEST_DIR not in bad["gate_selection"], (
        f"the gate fell back to the whole suite: {bad['gate_selection']}"
    )
    assert bad["gate_ok"] is False, (
        "DEFECT 2 HAS REGRESSED: the gate APPROVED a rename that breaks the suite, "
        f"because the only dependent test imports an off-chain symbol ({bad}). The "
        "reverse-dep walk is parcel-granular again."
    )
    assert control["gate_ok"] is False, (
        "the discrimination control failed: when the test imports the ON-chain "
        "symbol the gate must reject the very same rename, but it returned "
        f"ok={control['gate_ok']} ({control}). Without this, the false negative "
        "cannot be attributed to parcel-granular reachability."
    )


# =====================================================================================
# H3 -- cost
# =====================================================================================


@dataclass
class GateSplit:
    label: str
    total_seconds: float
    reindex_seconds: float
    selected: list[str]
    ok: bool

    @property
    def pytest_seconds(self) -> float:
        return self.total_seconds - self.reindex_seconds

    @property
    def reindex_fraction(self) -> float:
        return self.reindex_seconds / self.total_seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_s": round(self.total_seconds, 3),
            "reindex_ms": round(self.reindex_seconds * 1000, 1),
            "pytest_s": round(self.pytest_seconds, 3),
            "reindex_pct": round(self.reindex_fraction * 100, 2),
            "n_selected": len(self.selected),
            "ok": self.ok,
        }


@contextmanager
def _reindex_timer() -> Iterator[list[float]]:
    """Time every `_reverse_dep_files` call the gate makes, without touching it.

    A wrapper around the real function, installed on `gate`'s own name for the
    duration -- so what is measured is the actual re-index + graph build the gate
    performs on that invocation, not a separate call made alongside it.
    """
    elapsed: list[float] = []
    real = gate._reverse_dep_files

    def timed(repo: Path, changed: set[str]) -> set[str]:
        started = time.perf_counter()
        try:
            return real(repo, changed)
        finally:
            elapsed.append(time.perf_counter() - started)

    with mock.patch.object(gate, "_reverse_dep_files", timed):
        yield elapsed


def _timed_gate(label: str, repo: Path, changed: list[str], python: Path | str) -> GateSplit:
    selected = _gate_selection(repo, changed)
    with harness.gate_interpreter(python):
        with _reindex_timer() as elapsed:
            started = time.perf_counter()
            ok, _log = gate.run_impact_tests(repo, changed)
            total = time.perf_counter() - started
    assert len(elapsed) == 1, f"expected one _reverse_dep_files call, saw {elapsed}"
    return GateSplit(label, total, elapsed[0], selected, ok)


SAMPLE_REPO = Path(harness.__file__).resolve().parents[2] / "sample_repo"


def test_h3_the_reindex_timer_actually_measures_reverse_dep_files() -> None:
    """Binding check for the instrumentation, before any number produced with it.

    A timer that silently measured nothing would report a beautiful re-index
    fraction for the wrong reason. Inject a known delay into
    `_reverse_dep_files` and require the split to move by it.
    """
    delay = 0.75
    real = gate._reverse_dep_files

    def slow(repo: Path, changed: set[str]) -> set[str]:
        time.sleep(delay)
        return real(repo, changed)

    with mock.patch.object(gate, "_reverse_dep_files", slow):
        slowed = _timed_gate("sample_repo+delay", SAMPLE_REPO, ["calc.py"], sys.executable)
    plain = _timed_gate("sample_repo", SAMPLE_REPO, ["calc.py"], sys.executable)

    _record(
        "h3_timer_binding_check",
        {"injected_delay_s": delay, "with_delay": slowed.as_dict(), "without": plain.as_dict()},
    )
    assert slowed.reindex_seconds >= delay, (
        f"a {delay}s delay injected into _reverse_dep_files did not show up in the "
        f"measured re-index time ({slowed.reindex_seconds:.3f}s) -- the instrumentation "
        "is not measuring what it claims and every H3 number is suspect"
    )
    assert plain.reindex_seconds < delay / 2, (
        f"the undelayed run also reported {plain.reindex_seconds:.3f}s, so the timer "
        "is not sensitive to the injected delay specifically"
    )


def test_h3_reindex_is_a_small_fraction_of_gate_wall_clock(
    scale: harness.ScaleRepo,
) -> None:
    """H3 as stated: gate wall-clock is dominated by running tests, not re-indexing.

    Three code-learner changes plus `sample_repo` for contrast. The primary
    assertion is the hypothesis verbatim (`re-index < pytest`), with no invented
    threshold. The 10% ceiling below is a regression tripwire set at ~3x the worst
    figure measured (3.03%), not a pass condition anything was tuned to.
    """
    splits = [
        # Few dependents: 2 of 11 test files. The case impact selection exists for.
        _timed_gate(
            "code-learner cli/main.py (few dependents)",
            scale.root,
            ["codelearner/cli/main.py"],
            scale.gate_python,
        ),
        # Many dependents: 10 of 11 test files.
        _timed_gate(
            "code-learner ingest/types.py (many dependents)",
            scale.root,
            ["codelearner/ingest/types.py"],
            scale.gate_python,
        ),
        # The worst case for cost: selection excludes nothing, so the re-index is
        # pure overhead.
        _timed_gate(
            "code-learner db.py (selects every test file)",
            scale.root,
            ["codelearner/db.py"],
            scale.gate_python,
        ),
        # sample_repo, 195 lines, on swarm-sync's own interpreter.
        _timed_gate(
            "sample_repo calc.py (3.11, swarm-sync's own python)",
            SAMPLE_REPO,
            ["calc.py"],
            sys.executable,
        ),
    ]
    _record("h3_gate_splits", {s.label: s.as_dict() for s in splits})

    dominated = [s.label for s in splits if s.reindex_seconds >= s.pytest_seconds]
    assert not dominated, (
        "H3 FALSIFIED for these changes -- re-indexing cost at least as much as the "
        f"tests it gated: {[ (s.label, s.as_dict()) for s in splits if s.label in dominated ]}"
    )
    over = {s.label: s.as_dict() for s in splits if s.reindex_fraction > 0.10}
    assert not over, (
        "re-index is now more than 10% of gate wall-clock (worst previously measured: "
        f"3.03%): {over}"
    )


def test_h3_impact_selection_pays_for_itself(
    scale: harness.ScaleRepo, baseline: float
) -> None:
    """The crossover, stated as what it actually is: a selectivity threshold, not a
    repo size.

    Break-even = re-index seconds / full-suite seconds. Below that share of the
    suite excluded, the re-index costs more than it saves. `baseline` is the same
    full-suite run H2 uses as its green check -- same clone, same interpreter,
    same flags -- so the ratio compares like with like.
    """
    full_seconds = baseline
    few = _timed_gate(
        "few", scale.root, ["codelearner/cli/main.py"], scale.gate_python
    )
    break_even = few.reindex_seconds / full_seconds
    saved = full_seconds - few.pytest_seconds

    _record(
        "h3_crossover",
        {
            "full_suite_seconds": round(full_seconds, 2),
            "reindex_seconds": round(few.reindex_seconds, 4),
            "gate_pytest_seconds_for_2_files": round(few.pytest_seconds, 2),
            "test_seconds_saved": round(saved, 2),
            "return_multiple": round(saved / few.reindex_seconds, 1),
            "break_even_fraction_of_suite": round(break_even, 5),
            "break_even_pct": round(break_even * 100, 3),
        },
    )
    assert saved > few.reindex_seconds, (
        "impact selection is a NET LOSS even on a 2-test-file change: it saved "
        f"{saved:.2f}s of tests and cost {few.reindex_seconds:.3f}s to compute"
    )
    assert break_even < 0.05, (
        "the break-even share of the suite that selection must exclude to pay for "
        f"itself is now {break_even * 100:.2f}% (was 0.80%); at that point impact "
        "selection stops being obviously worth it"
    )


def test_h3_reindex_cost_is_linear_in_file_count() -> None:
    """The measured fit the 5000-file extrapolation rests on.

    `N` self-contained copies of code-learner's package: file count scales, edge
    density per file does not. If ms/file were to climb, the extrapolation in this
    module's docstring would be wrong and should not be quoted.

    Includes its own discrimination control: the same flatness check is run over a
    deliberately quadratic series and must reject it, so a check that could never
    fail is not passing for free.
    """

    def per_file_spread(samples: Sequence[tuple[int, float]]) -> float:
        rates = [seconds / files for files, seconds in samples]
        return max(rates) / min(rates)

    quadratic = [(n, 1e-5 * n * n) for n in (35, 70, 140)]
    assert per_file_spread(quadratic) > 1.6, (
        "the flatness check cannot detect quadratic growth, so it proves nothing "
        f"about the measured series (spread {per_file_spread(quadratic):.2f})"
    )

    workdir = Path(tempfile.mkdtemp(prefix="swarmsync-b-scale-"))
    samples: list[tuple[int, float]] = []
    detail: list[dict[str, Any]] = []
    try:
        for copies in (1, 2, 4):
            root = workdir / f"r{copies}"
            root.mkdir()
            for i in range(copies):
                shutil.copytree(
                    harness.CODE_LEARNER_REPO / "codelearner",
                    root / f"pkg{i}" / "codelearner",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                (root / f"pkg{i}" / "__init__.py").write_text("", encoding="utf-8")
            n_files = len(_repo_py_files(root))
            best = min(
                _time_reverse_deps(root, "pkg0/codelearner/db.py") for _ in range(3)
            )
            samples.append((n_files, best))
            detail.append(
                {
                    "copies": copies,
                    "py_files": n_files,
                    "reverse_dep_seconds": round(best, 4),
                    "ms_per_file": round(best * 1000 / n_files, 3),
                }
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    spread = per_file_spread(samples)
    ms_per_file = sum(s / f for f, s in samples) / len(samples) * 1000
    _record(
        "h3_scaling",
        {
            "samples": detail,
            "per_file_spread": round(spread, 3),
            "mean_ms_per_file": round(ms_per_file, 3),
            "extrapolated_seconds_at_index_cap_5000_files": round(
                ms_per_file * 5000 / 1000, 2
            ),
        },
    )
    assert spread < 1.6, (
        "re-index cost per file is NOT flat across a 4x file-count range "
        f"(spread {spread:.2f}), so the linear extrapolation to the 5000-file "
        f"index cap is not supported: {detail}"
    )


def _time_reverse_deps(root: Path, changed: str) -> float:
    started = time.perf_counter()
    gate._reverse_dep_files(root, {changed})
    return time.perf_counter() - started


def test_h3_gate_interpreter_override_cost_is_separable_and_small(
    scale: harness.ScaleRepo,
) -> None:
    """The harness's 3.12 override changes gate TIMING, so quantify it separately
    from re-index cost rather than letting the two be conflated.

    Measured as pytest's boot-and-collect-nothing floor on each interpreter --
    what the gate pays before a single test of the repo runs.

    Two things this had to get right, both of which changed the answer:

    * `PYTHONDONTWRITEBYTECODE` is REMOVED from the subprocess environment here,
      even though the rest of this module sets it -- because a real gate run does
      not set it. It can be a large observer effect, but only while a venv's
      site-packages `__pycache__` is incomplete: measured standalone against a
      cold cache, swarm-sync's 3.11 floor went 103 ms -> 233 ms under the flag
      while code-learner's 3.12 venv stayed at 124 ms, which inverts the sign of
      the override and made an earlier version of the assertion below pass
      vacuously. Once the `.pyc` files exist the flag is free, so the `_nopyc`
      figures recorded below will normally look identical to the main ones -- that
      is the cache being warm, not the effect being absent. Both are reported so
      the difference is never invisible.
    * The two interpreters are measured INTERLEAVED, so a slow patch of machine
      (this file copies code-learner's package around a few tests earlier) cannot
      land entirely on one of them and manufacture a difference.
    """

    def once(python: Path | str, repo: Path, env: dict[str, str]) -> float:
        started = time.perf_counter()
        subprocess.run(
            [
                str(python),
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--import-mode=importlib",
                "tests",
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        return time.perf_counter() - started

    def floors(repo: Path, *, dont_write_bytecode: bool) -> tuple[float, float]:
        env = _pytest_env()
        if dont_write_bytecode:
            env["PYTHONDONTWRITEBYTECODE"] = "1"
        else:
            env.pop("PYTHONDONTWRITEBYTECODE", None)
        mine: list[float] = []
        theirs: list[float] = []
        for i in range(5):
            mine.append(once(sys.executable, repo, env))
            theirs.append(once(scale.gate_python, repo, env))
            if i == 0:  # first pass is warm-up for both, symmetrically
                mine.clear()
                theirs.clear()
        return min(mine), min(theirs)

    empty = Path(tempfile.mkdtemp(prefix="swarmsync-b-empty-"))
    try:
        (empty / "tests").mkdir()
        own, override = floors(empty, dont_write_bytecode=False)
        own_nopyc, override_nopyc = floors(empty, dont_write_bytecode=True)
    finally:
        shutil.rmtree(empty, ignore_errors=True)

    # The repo's own heavy imports (tree-sitter, sqlite-vec) are paid by whichever
    # interpreter runs the suite, so they are NOT part of the override's cost --
    # measured here only so the report can say so.
    started = time.perf_counter()
    subprocess.run(
        [str(scale.gate_python), "-c", "import codelearner.retrieve.search"],
        cwd=str(scale.root),
        capture_output=True,
        text=True,
        env=_pytest_env(),
        timeout=300,
    )
    repo_import = time.perf_counter() - started

    gate_total = _timed_gate(
        "few", scale.root, ["codelearner/cli/main.py"], scale.gate_python
    ).total_seconds
    added = override - own

    _record(
        "h3_interpreter_override",
        {
            "swarmsync_3_11_pytest_boot_ms": round(own * 1000, 1),
            "codelearner_3_12_pytest_boot_ms": round(override * 1000, 1),
            "override_added_ms": round(added * 1000, 1),
            "repo_own_heavy_import_ms_paid_by_either_interpreter": round(
                repo_import * 1000, 1
            ),
            "as_pct_of_a_code_learner_gate_run": round(
                abs(added) / gate_total * 100, 3
            ),
            "observer_effect_PYTHONDONTWRITEBYTECODE": {
                "swarmsync_3_11_pytest_boot_ms": round(own_nopyc * 1000, 1),
                "codelearner_3_12_pytest_boot_ms": round(override_nopyc * 1000, 1),
                "override_added_ms": round((override_nopyc - own_nopyc) * 1000, 1),
                "note": (
                    "swarm-sync's 3.11 venv has an incomplete site-packages "
                    "__pycache__, so forbidding .pyc writes makes it recompile on "
                    "every start; code-learner's 3.12 venv is unaffected. Measured "
                    "this way the override looks free, which is an artifact."
                ),
            },
        },
    )
    # `abs`, deliberately: the first version of this assertion was `added < 5%` and
    # it passed VACUOUSLY when a measurement artifact made `added` negative. A
    # difference in either direction that large would mean the override is
    # distorting every H3 number, so both directions must trip it.
    assert abs(added) < 0.05 * gate_total, (
        "the gate-interpreter override now shifts gate wall-clock by more than 5% "
        f"of a code-learner gate run ({added * 1000:.0f}ms of {gate_total:.2f}s); "
        "H3's re-index figures would need restating against it"
    )
    assert own > 0.01 and override > 0.01, (
        f"a pytest boot floor of {own * 1000:.0f}ms / {override * 1000:.0f}ms is "
        "implausibly fast -- the subprocess probably failed instead of running"
    )


def test_h3_index_cap_no_longer_silently_disables_graph_selection(
    scale: harness.ScaleRepo,
) -> None:
    """Above `DEFAULT_MAX_INDEX_FILES` the graph signal is still unavailable, but the
    gate now WIDENS to the whole suite instead of narrowing in silence.

    **Inverted when the fix landed** (`coordinator/gate.py`), following the same
    convention as Defects 1 and 2 above. As written this test asserted the defect:
    `index_repo` raises `IndexLimitError`, `_reverse_dep_files` catches bare
    `Exception` and returns the empty set, and selection degraded to the substring
    heuristic with no log line, no event and no difference in the gate's verdict
    shape -- so the operator of a repo that had grown past the cap could not tell
    impact selection had stopped working. Measured here on the real clone, that took
    this change from 10 selected test files to 2, the same 8-file gap
    `test_h2_graph_selection_is_load_bearing` shows is a false negative.

    The bare `except` is unchanged and still catches everything -- selection must
    never fail the gate. What changed is that the empty set now carries WHY it is
    empty, so "no dependents" and "could not compute dependents" are no longer the
    same answer.

    The narrowing the fix replaces is measured here as the discrimination control
    (`graph=False`), not merely described: it must still be a strict, non-empty
    subset, because if it were empty the pre-existing full-suite fallback would
    produce the same widened argv for an entirely different reason and this test
    would prove nothing.
    """
    changed = "codelearner/ingest/types.py"
    before = gate._reverse_dep_files(scale.root, {changed})
    assert before, "fixture assumption broken: this change has graph dependents"

    def over_cap(root: Any, **kwargs: Any) -> Any:
        raise IndexLimitError(
            f"index walk of {root} exceeded max_files=5000 (simulated)"
        )

    with mock.patch.object(gate, "index_repo", over_cap):
        after = gate._reverse_dep_files(scale.root, {changed})
        selection_after = _gate_selection(scale.root, [changed])
        _ok, gate_log = gate.run_impact_tests(scale.root, [changed])
    selection_before = _gate_selection(scale.root, [changed])
    # What the gate USED to fall back to when the graph vanished.
    selection_substring_only = _gate_selection(scale.root, [changed], graph=False)

    _record(
        "h3_index_cap",
        {
            "max_index_files": 5000,
            "graph_dependents_normally": len(before),
            "graph_dependents_over_cap": len(after),
            "gate_selection_normally": selection_before,
            "gate_selection_over_cap": selection_after,
            "selection_the_old_silent_narrowing_would_have_used": (
                selection_substring_only
            ),
            "test_files_the_old_narrowing_lost": sorted(
                set(selection_before) - set(selection_substring_only)
            ),
            "gate_log_announces_widening": "WIDENED" in gate_log,
        },
    )
    assert after == set(), (
        f"expected the empty set once index_repo raises, got {sorted(after)}"
    )
    assert set(selection_substring_only) < set(selection_before) and (
        selection_substring_only
    ), (
        "the substring backstop no longer narrows non-emptily on this change, so this "
        "repo cannot distinguish widening from the pre-existing full-suite fallback "
        f"({selection_before} -> {selection_substring_only})"
    )
    assert selection_after == [gate.DEFAULT_TEST_DIR], (
        "THE INDEX-CAP DEFECT HAS REGRESSED: with the dependency graph unavailable "
        f"the gate selected {selection_after} instead of widening to the whole suite. "
        f"It is back to reading 'I could not compute the dependents' as 'there are no "
        f"dependents', which on this change silently drops "
        f"{sorted(set(selection_before) - set(selection_substring_only))}."
    )
    assert "WIDENED" in gate_log, (
        f"the gate widened without saying so in its own log: {gate_log!r}"
    )


def test_zz_report() -> None:
    """Print every measurement this module produced. Run with `-s` to read it.

    Named to sort last. Asserts only that the earlier tests actually recorded
    their numbers -- a report of nothing would be a silently vacuous run.
    """
    import json

    print("\n" + "=" * 78)
    print("H2 / H3 -- IMPACT SELECTION AT CODE-LEARNER SCALE")
    print("=" * 78)
    for key in sorted(MEASUREMENTS):
        print(f"\n--- {key}")
        print(json.dumps(MEASUREMENTS[key], indent=2, default=str))

    required = {
        "h2_union_precision_recall",
        "h2_graph_only_recall",
        "h2_graph_load_bearing",
        "defect1_repro",
        "defect2_repro",
        "h3_gate_splits",
        "h3_crossover",
        "h3_scaling",
        "h3_interpreter_override",
        "h3_index_cap",
    }
    missing = sorted(required - set(MEASUREMENTS))
    assert not missing, (
        f"these measurements were never recorded, so the run was partial: {missing}"
    )
