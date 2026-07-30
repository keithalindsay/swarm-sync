"""The test gate must not narrow SILENTLY when its dependency graph is unavailable.

THE DEFECT THESE GUARD
======================
`classifier.indexer.DEFAULT_MAX_INDEX_FILES = 5000` makes `index_repo` raise
`IndexLimitError`, and `gate._reverse_dep_files` catches a bare `Exception` and
returns the empty set. The bare `except` is correct and stays -- selection is
best-effort and must never fail the gate -- but the empty set it produced was
indistinguishable from a genuine "nothing reverse-depends on this change", so
above the cap the authoritative signal DISAPPEARED and selection fell back to the
substring heuristic with no log line, no event and no change in the verdict's
shape. Measured on a real 45-file repo with the cap lowered, an
`ingest/types.py` change went from 10 selected test files to 2 -- the same 8-file
gap `tests/scale/test_impact_selection.py::test_h2_graph_selection_is_load_bearing`
shows is a false negative. An operator whose repo grew past 5000 `.py` files could
not tell impact selection had stopped working.

The fix: an unavailable graph WIDENS the run to the whole suite and says so, in
swarm-sync's logger and in the gate's own returned log.

WHAT EACH TEST IS FOR, INCLUDING THE ONES THAT PIN THE DANGEROUS DIRECTION
=========================================================================
A fix here has two opposite ways to be wrong, and both are worse than the defect,
so both are pinned rather than assumed:

  * WIDENING TOO LITTLE -- the defect itself.
    `test_an_unavailable_graph_widens_to_the_full_suite` (with its
    graph-available discrimination control).
  * MAKING THE GATE FAIL rather than merely widen. Letting `IndexLimitError`
    propagate, or returning `ok=False` when the graph is missing, turns a
    best-effort optimisation into an outage: every merge rejected, for a reason
    that has nothing to do with the branch being merged.
    `test_an_unavailable_graph_does_not_fail_the_gate` and
    `test_reverse_dep_files_never_raises_whatever_indexing_does`.
  * WIDENING ALWAYS, which is vacuously safe and destroys impact selection.
    Two controls, both of which are real states rather than contrivances:
    `test_a_genuinely_empty_graph_answer_does_not_widen` (a merge that DELETES a
    file -- the changed path has no parcels, so the graph legitimately answers
    "nothing", and the substring backstop legitimately narrows) and
    `test_an_unparseable_file_does_not_widen` (an ordinary broken repo, which is
    the case the bare `except`'s own docstring used to cite -- `index_repo` skips
    and logs one bad file rather than aborting, so no exception, so no widening).

Selection is read off the argv the gate actually builds, never re-derived here, so
a mistake in this file cannot flatter the gate.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional
from unittest import mock

import pytest

from swarmsync.classifier.indexer import DEFAULT_MAX_INDEX_FILES, IndexLimitError
from swarmsync.coordinator import gate


# --- reading the gate's REAL selection off its argv ---------------------------------


class _RecordedPopen:
    """A `Popen` stand-in that runs nothing and reports success, so a test can read
    the selection `run_impact_tests` made without paying for a pytest run."""

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


def _selection(repo: Path, changed: list[str]) -> list[str]:
    """The paths the gate passes to pytest: selected test files, or the bare
    `test_dir` that means "the whole suite"."""
    sink: list[list[str]] = []
    with mock.patch.object(
        gate.subprocess, "Popen", lambda cmd, **kw: _RecordedPopen(sink, cmd, **kw)
    ):
        gate.run_impact_tests(repo, changed)
    assert len(sink) == 1, f"expected exactly one gate subprocess, got {sink}"
    return [
        Path(arg).as_posix()
        for arg in sink[0]
        if arg.endswith(".py") or arg == gate.DEFAULT_TEST_DIR
    ]


def _over_cap(*_a: Any, **_kw: Any) -> Any:
    """`index_repo` on a repo bigger than `DEFAULT_MAX_INDEX_FILES`.

    Patched in rather than built for real: materialising 5001 `.py` files per test
    would dominate this module's runtime, and what is under test is the gate's
    reaction to the raise, not the counting that produces it (which
    `tests/test_security.py::test_index_repo_caps_file_count` already covers).
    """
    raise IndexLimitError("index walk of <repo> exceeded max_files=5000 (simulated)")


# --- the repo ----------------------------------------------------------------------


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repo where the graph signal is LOAD-BEARING, so a silent narrowing is
    observable as lost coverage rather than only as a different argv.

    `tests/test_mid.py` exercises `base.py` only indirectly (it imports `mid`,
    which imports `base`) and its source never spells "base", so the substring
    backstop cannot see it and only the dependency graph can. `tests/test_base.py`
    does spell it, which is what keeps the substring selection NON-EMPTY -- without
    that, the pre-existing full-suite fallback would cover the gap for the wrong
    reason and none of this would be measuring the widening.
    """
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "base.py").write_text("def core():\n    return 1\n", encoding="utf-8")
    (root / "mid.py").write_text(
        "from base import core\n\n\ndef use():\n    return core()\n", encoding="utf-8"
    )
    (root / "tests" / "test_mid.py").write_text(
        "from mid import use\n\n\ndef test_mid():\n    assert use() == 1\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_base.py").write_text(
        "from base import core\n\n\ndef test_base():\n    assert core() == 1\n",
        encoding="utf-8",
    )
    return root


def test_the_fixture_isolates_the_graph_signal(repo: Path) -> None:
    """Premise check, so nothing below can pass for an accidental reason.

    If `test_mid.py` ever spelled "base", the substring backstop would select it
    and every assertion about the graph signal in this module would be vacuous.
    """
    text = (repo / "tests" / "test_mid.py").read_text(encoding="utf-8")
    assert "base" not in text, (
        "tests/test_mid.py now names the changed module, so the substring backstop "
        "covers it and this module no longer isolates the graph signal"
    )
    assert "tests/test_mid.py" in gate._reverse_dep_files(repo, {"base.py"}), (
        "the graph does not reach the indirectly-dependent test file, so there is no "
        "coverage for an unavailable graph to lose"
    )


# --- the defect: an unavailable graph must widen, not narrow -------------------------


def test_an_unavailable_graph_widens_to_the_full_suite(repo: Path) -> None:
    """The headline. Above the index cap the gate runs the WHOLE suite.

    Three selections, so the widening is attributable rather than merely present:
    with the graph (narrowed, and correct), with the graph unavailable (must be
    the whole suite), and -- the discrimination control -- what the substring
    backstop ALONE would have selected, which is the silent narrowing this
    replaces. That control is what makes the middle result meaningful: if the
    substring selection were empty, the pre-existing full-suite fallback would
    produce the same argv for a completely different reason.
    """
    with_graph = _selection(repo, ["base.py"])
    with mock.patch.object(gate, "index_repo", _over_cap):
        over_cap = _selection(repo, ["base.py"])
    with mock.patch.object(gate, "_reverse_dep_files", lambda r, c: set()):
        substring_only = _selection(repo, ["base.py"])

    assert sorted(with_graph) == ["tests/test_base.py", "tests/test_mid.py"], (
        f"the graph-available selection is not the narrowed one this test assumes: "
        f"{with_graph}"
    )
    assert substring_only == ["tests/test_base.py"], (
        "the substring backstop no longer narrows on this repo, so 'widening' and "
        f"'narrowing' are indistinguishable here: {substring_only}"
    )
    assert over_cap == [gate.DEFAULT_TEST_DIR], (
        "THE DEFECT IS BACK: with the dependency graph unavailable the gate did not "
        f"widen to the whole suite, it selected {over_cap}. An empty reverse-dep set "
        "is being read as 'nothing depends on this change' when it actually means "
        "'I could not compute the dependents', so a repo past "
        "DEFAULT_MAX_INDEX_FILES silently tests less."
    )


# A widening an operator cannot see is only half a fix, and there are two channels
# because there are two readers: the returned log is what `integrator` puts in the
# merge verdict's `test_log` (the operator staring at a slow merge), and the logger
# is what a running coordinator's stderr shows (the operator staring at a slow
# system). They are asserted in SEPARATE tests on purpose. A single test covering
# both cannot tell "widened but silent" from "did not widen at all" -- a mutation
# run that dropped only the widening failed the combined test through its gate-log
# assertion, which made the logger half look unpinned. The logger is also the ONE
# signal that does not depend on the widening decision at all, since it comes from
# `_reverse_dep_files`, so it is tested against that function directly.
#
# An event would be a third channel and is deliberately NOT used: `coordinator.gate`
# is split out precisely so nothing in it reads or writes the blackboard, and it
# holds no connection to emit through.


def test_the_swallowed_failure_is_logged_with_its_cause(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`_reverse_dep_files` still swallows everything, but no longer in silence.

    Asserted against `_reverse_dep_files` itself rather than through the gate, so
    it stays a guard on the swallowing site whatever the gate later decides to do
    about it. `exc_info` is required, not optional: without the chained exception
    the operator learns that indexing failed but not which cap, which file, or
    which error -- and the whole complaint about the defect was that the cause was
    unrecoverable.
    """
    with caplog.at_level(logging.WARNING, logger="swarmsync.coordinator.gate"):
        with mock.patch.object(gate, "index_repo", _over_cap):
            result = gate._reverse_dep_files(repo, {"base.py"})

    assert result == set()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, (
        "the graph became unavailable and NOTHING was logged at WARNING -- this is "
        "the silence the defect was about"
    )
    assert any("UNAVAILABLE" in r.getMessage() for r in warnings), (
        f"the WARNING does not say the graph was unavailable: "
        f"{[r.getMessage() for r in warnings]}"
    )
    assert any(r.exc_info for r in warnings), (
        "the WARNING carries no exception info, so the underlying cause (which cap, "
        "which file, which error) is unrecoverable from the log"
    )


def test_the_widening_is_announced_in_the_gate_log(repo: Path) -> None:
    """The merge verdict itself has to say the gate widened, and what to do.

    Three separate things, because a bare "widened" line leaves the operator with
    a slow gate and nothing to act on: that it widened, the cap that caused it
    (with its value, so a grep finds the constant), and the knob the widening can
    push the run past.
    """
    with mock.patch.object(gate, "index_repo", _over_cap):
        ok, log = gate.run_impact_tests(repo, ["base.py"])

    assert ok is True, log
    assert "WIDENED" in log, (
        f"the gate widened silently -- its own log does not say so: {log!r}"
    )
    assert "DEFAULT_MAX_INDEX_FILES" in log and str(DEFAULT_MAX_INDEX_FILES) in log, (
        "the log must name the cap that caused this and its value, or an operator "
        f"has a slow gate and nothing to act on: {log!r}"
    )
    assert "SWARMSYNC_GATE_TIMEOUT" in log, (
        "the log must warn about the knob the widening can push the gate past: "
        f"{log!r}"
    )


def test_an_ordinary_gate_run_says_nothing_about_widening(repo: Path) -> None:
    """The control for both loudness tests: a healthy gate run is quiet.

    Without this, prepending the notice unconditionally would satisfy every
    assertion above while telling the operator nothing -- a banner on every merge
    is the same as no banner.
    """
    ok, log = gate.run_impact_tests(repo, ["base.py"])
    assert ok is True, log
    assert "WIDENED" not in log and "impact selection" not in log, (
        f"a normal gate run is announcing a widening that did not happen: {log!r}"
    )


def test_the_widened_run_actually_runs_the_test_the_narrowing_would_have_skipped(
    repo: Path
) -> None:
    """The same claim, behaviourally: pytest really runs both tests.

    Reading argv proves what the gate INTENDED. This proves it happened, on the
    real subprocess, with `1 passed` vs `2 passed` as the discriminator -- so the
    widening cannot be a cosmetic argv change that pytest interprets differently.
    """
    with mock.patch.object(gate, "index_repo", _over_cap):
        widened_ok, widened_log = gate.run_impact_tests(repo, ["base.py"])
    with mock.patch.object(gate, "_reverse_dep_files", lambda r, c: set()):
        narrow_ok, narrow_log = gate.run_impact_tests(repo, ["base.py"])

    assert "2 passed" in widened_log, (
        f"the widened gate did not run both test files:\n{widened_log}"
    )
    assert "1 passed" in narrow_log, (
        "the control did not narrow to one test file, so the comparison above is "
        f"not measuring the widening:\n{narrow_log}"
    )
    assert widened_ok is True and narrow_ok is True


# --- the dangerous direction: widening must not become FAILING -----------------------


def test_an_unavailable_graph_does_not_fail_the_gate(repo: Path) -> None:
    """A missing graph must cost TIME, never a verdict.

    The tempting fix is to reject when the gate cannot compute impact -- "we could
    not test this properly, so no". On a repo permanently past the index cap that
    rejects every merge from every agent forever, for a reason that has nothing to
    do with the branch. Impact selection is an optimisation; its failure must
    degrade to the un-optimised behaviour, not to an outage.
    """
    with mock.patch.object(gate, "index_repo", _over_cap):
        ok, log = gate.run_impact_tests(repo, ["base.py"])
    assert ok is True, (
        "an unavailable dependency graph turned into a gate FAILURE on a repo whose "
        f"suite is green. That is an outage, not a safety measure:\n{log}"
    )


def test_reverse_dep_files_never_raises_whatever_indexing_does(repo: Path) -> None:
    """`_reverse_dep_files` must swallow everything, and answer a `set`.

    `integrate` calls the gate while holding the ONE global `integrate_lock` with
    an un-gated merge already on trunk, so an exception escaping here is not a
    failed merge -- it is `integrate`'s error path. Checked for the cap error and
    for an arbitrary unrelated exception, because a fix that special-cases
    `IndexLimitError` and lets everything else through would pass the first alone.
    """
    for boom in (_over_cap, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))):
        with mock.patch.object(gate, "index_repo", boom):
            result = gate._reverse_dep_files(repo, {"base.py"})
        assert isinstance(result, set) and result == set(), result


# --- the other dangerous direction: widening must not become UNCONDITIONAL -----------


def test_a_genuinely_empty_graph_answer_does_not_widen(repo: Path) -> None:
    """A merge that DELETES a file: the graph answers "nothing", truthfully.

    `base.py`'s path is in the change set but not on disk, so it has no parcels
    and no reverse-dependents -- an empty answer that IS an answer. The substring
    backstop still selects `tests/test_base.py`, and the gate must narrow to it.
    If this widened, the fix would be "always run everything", which is safe and
    useless: it deletes impact selection.
    """
    (repo / "base.py").unlink()
    (repo / "mid.py").unlink()
    (repo / "tests" / "test_mid.py").unlink()

    assert gate._reverse_dep_files(repo, {"base.py"}) == set(), (
        "a deleted file no longer produces an empty reverse-dep set, so this test "
        "is not exercising the genuinely-empty case"
    )
    selection = _selection(repo, ["base.py"])
    assert selection == ["tests/test_base.py"], (
        "the gate widened to the whole suite for a graph answer that was genuinely "
        f"empty rather than unavailable, which removes impact selection: {selection}"
    )


def test_an_unparseable_file_does_not_widen(repo: Path) -> None:
    """The ORDINARY broken repo stays quiet, which is the case the bare `except`
    was written for.

    An agent merges a file with a syntax error. `index_repo` skips-and-logs that
    one file and keeps walking, so nothing raises, so the graph still answers and
    the gate still narrows. This is the path that must NOT be treated as a
    capability loss -- if it were, every merge that briefly breaks a file would
    run the full suite.
    """
    (repo / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    selection = _selection(repo, ["base.py"])
    assert sorted(selection) == ["tests/test_base.py", "tests/test_mid.py"], (
        "one unparseable file in the repo made the gate widen. `index_repo` skips it "
        f"per-file and does not raise, so the graph is available: {selection}"
    )


# --- the failure mode this fix deliberately accepts ---------------------------------


def test_a_widened_run_that_times_out_says_the_widening_may_be_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accepted cost, made diagnosable.

    Widening can push a big repo's suite past `SWARMSYNC_GATE_TIMEOUT`, and the
    gate then rejects with "a branch whose tests do not terminate" -- which would
    be a misdiagnosis: the branch is fine, the gate ran the whole suite because it
    could not index the repo. The rejection is still the right verdict (trunk is
    restored by `integrator._reject_and_reset`, so nothing lands half-merged), but
    it has to name the real cause.

    This test hangs forever if the gate timeout is ever removed, so it is its own
    proof of that too.
    """
    root = tmp_path / "hangrepo"
    (root / "tests").mkdir(parents=True)
    (root / "base.py").write_text("def core():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(600)\n", encoding="utf-8"
    )
    monkeypatch.setenv("SWARMSYNC_GATE_TIMEOUT", "2")

    started = time.monotonic()
    with mock.patch.object(gate, "index_repo", _over_cap):
        ok, log = gate.run_impact_tests(root, ["base.py"])
    elapsed = time.monotonic() - started

    assert ok is False and "exceeded" in log, log
    assert elapsed < 60, f"the gate was not killed at its timeout ({elapsed:.1f}s)"
    assert "WIDENED" in log and "may be" in log, (
        "a timeout on a WIDENED run reads as 'your tests do not terminate' with no "
        f"hint that the gate widened, which sends the operator after the wrong bug: "
        f"{log!r}"
    )

    # Control: the identical hang WITHOUT the widening must not claim it widened,
    # or the note above is boilerplate on every timeout and carries no information.
    started = time.monotonic()
    ok, plain_log = gate.run_impact_tests(root, ["base.py"])
    assert time.monotonic() - started < 60
    assert ok is False and "exceeded" in plain_log, plain_log
    assert "WIDENED" not in plain_log, (
        f"an ordinary timeout claims the run was widened: {plain_log!r}"
    )
