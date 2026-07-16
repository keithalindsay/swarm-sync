"""Serial, test-gated integrator. DESIGN.md §5.4, §5.5.

Unit U10. Every merge goes through `integrate()` -- called one branch at a time
by whatever drives it (a test, the demo harness, or later `POST /integrate` /
the broker, U12) -- so trunk (the `into` branch, "integration" by convention
per `worktree.git_ops.init_repo`) is never poisoned by a partial edit.

integrate(conn, repo, branch, base_commit=None, into="integration", ...) -> IntegrateResult
  1. **Optimistic re-check** (DESIGN §5.5, opt-in via `expected_read_deps`): if the
     caller passes `{parcel_or_contract_id: expected_hash}` snapshotted at
     plan-time, compare it against the blackboard's *current* `content_hash`
     (parcels) / `type_hash` (contracts). A mismatch means a read-dependency
     shifted mid-work -> **no merge attempted**, `needs_rebase`, event emitted,
     bounce back to the agent. (There is no persisted "declared read-deps per
     branch" table yet -- `agent/runner.py`'s `read_contracts`/`contract_snapshot`
     lives only on the in-memory `AgentResult` -- so this step is a capability
     the caller opts into by passing the snapshot it already has, not something
     `integrate` can reconstruct on its own.)
  2. `git_ops.merge_branch(repo, branch, into)` -- `--no-ff`. Because the
     scheduler only ever runs file-/span-disjoint work concurrently, a clean
     merge is the expected case; a **textual conflict is treated as a hard
     signal of touch-set misprediction**: reject, emit `merge_rejected` with
     the conflicted paths, do not retry or auto-resolve (`merge_branch` already
     aborts the failed merge itself, so `into` is untouched).
  3. Run `run_impact_tests` -- pytest restricted to test files that plausibly
     exercise the changed modules (impact selection), falling back to the full
     suite under `test_dir` when selection is uncertain (nothing matched).
     Green -> land: emit `merged`. Red -> `git_ops.reset_hard(repo, pre_merge_sha)`
     to undo the just-landed merge commit, emit `merge_rejected` with the
     captured pytest log, trunk (`into`) is left exactly as it was pre-merge.

  ATOMICITY (S1 hardening): once the merge commit is on trunk, EVERYTHING after
  it -- the pytest gate, re-index, `state_summary` regen, contract detection --
  runs inside one guard. Any failure there (a later GitOpsError, a parse/
  re-index crash, a bad symbol) `git reset --hard`s trunk back to `pre_merge_sha`
  so it is left byte-identical, and returns a STRUCTURED `merge_rejected`
  (reason `integration_error`) instead of bubbling a 500 that would strand a
  half-integrated, un-reindexed merge on trunk. The success-path events
  (`merged`/`contract_change`/`reindexed`) are emitted only after that guarded
  block succeeds, so a rolled-back merge never leaves a dangling `merged` event
  in the log the projection tables replay from.
  4. On land only: re-index the repo (`classifier.store.run_index`, same
     pipeline `POST /index` uses) so `blast_radius`/`content_hash`/contracts
     stay current, then **authoritatively regenerate `state_summary`**
     (`regenerate_summary`) for every parcel the branch actually touched --
     never trust the agent's self-reported note (DESIGN §5.4/§6 "lying
     blackboard"). Emits `reindexed` with the touched parcel ids.
  5. **Frozen-contract change detection** (DESIGN §5.3, money-shot #3, U15):
     bracketing step 4's re-index, this snapshots `contracts.type_hash` for
     every symbol whose file this branch touched, BEFORE re-indexing, and
     compares it against the same symbols' type_hash AFTER. Any symbol whose
     type_hash genuinely changed emits a `contract_change` event (old/new
     signature + version) -- the "coordinator" side of DESIGN §5.3's "an
     agent...emit[s] a contract_change event" (an agent's own self-report
     would be exactly the "lying blackboard" DESIGN §5.4/§6 rejects; this is
     the one place a real, LANDED before/after diff is known for certain).
     Dependents (`agent/runner.py`'s `read_contracts` snapshot, or a live
     poll of `GET /events`) observe this event and re-read `GET /contract/
     {symbol}` to see the change, per DESIGN §4.3/§5.3.

Serialization is the CALLER's responsibility: this module holds no lock of its
own (matching `git_ops.merge_branch`, which is documented the same way) --
whatever drives `integrate()` must call it for one branch at a time, since each
call mutates `into`'s shared working tree and the shared blackboard connection
in place. A single-writer SQLite connection plus a single-threaded call
sequence is exactly the "one merge in flight at a time" DESIGN §5.4 asks for;
an `asyncio.Lock`/single-worker-queue wrapper (for a real async server) is a
thin shell around this function, not a separate implementation.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from swarmsync.blackboard.models import Parcel
from swarmsync.classifier.graph import DepGraph, build_graph
from swarmsync.classifier.indexer import index_repo
from swarmsync.classifier.store import run_index
from swarmsync.server import events as events_mod
from swarmsync.worktree import git_ops

StrPath = Union[str, Path]

DEFAULT_TEST_DIR = "tests"


@dataclass
class IntegrateResult:
    """Everything one `integrate()` call decided, for callers/tests to assert on."""

    status: str  # "merged" | "merge_rejected" | "needs_rebase"
    branch: str
    into: str
    merged_commit: Optional[str] = None
    conflicts: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    reindexed_parcels: list[str] = field(default_factory=list)
    test_log: str = ""
    stale_deps: list[str] = field(default_factory=list)
    reason: Optional[str] = None
    contract_changes: list[str] = field(default_factory=list)  # symbols whose
    # frozen signature genuinely changed on THIS merge (DESIGN §5.3, U15) --
    # each also has a `contract_change` event emitted, see module docstring.


def _check_read_deps(
    conn, expected_read_deps: dict[str, str]
) -> list[str]:
    """Compare `{id: expected_hash}` against current `parcels.content_hash` /
    `contracts.type_hash`. Returns the ids whose current hash no longer matches
    what the branch's agent saw at plan time (DESIGN §5.5)."""
    stale: list[str] = []
    for target_id, expected_hash in expected_read_deps.items():
        row = conn.execute(
            "SELECT content_hash AS h FROM parcels WHERE id = ?", (target_id,)
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT type_hash AS h FROM contracts WHERE symbol = ?", (target_id,)
            ).fetchone()
        current_hash = row["h"] if row is not None else None
        if current_hash != expected_hash:
            stale.append(target_id)
    return stale


def _reverse_dep_files(repo: Path, changed_py: set[str]) -> set[str]:
    """Every repo file that TRANSITIVELY reverse-depends on a changed `.py` file,
    via the classifier's real import/call dependency graph.

    This is the correctness core of impact selection: a test that exercises the
    changed code only *indirectly* (it imports module M, which imports the changed
    module C -- and the test's own source never names C) is a genuine dependent
    the old bare-stem-substring scan silently skipped. We re-index the (already
    merged) repo on disk, build the dep graph, seed a BFS at every parcel whose
    file is a changed file, walk `reverse_edges` to the transitive dependent set,
    and map those parcel ids back to their files. Returns an empty set on any
    failure (a broken repo, etc.) -- the substring heuristic + full-suite fallback
    below still backstop selection, so this only ever ADDS coverage, never removes.
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
        (the old bare-stem heuristic, KEPT as a backstop for edges the classifier
        can't see -- dynamic dispatch / string imports, DESIGN §6 "classifier
        miss": over-selecting a test is always safe, skipping an affected one is
        the bug we're fixing).
    Selecting the UNION is a strict over-approximation of the old behavior, so it
    can only run more tests, never fewer -- it never skips an affected test.
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

    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    result = subprocess.run(
        cmd, cwd=str(repo), capture_output=True, text=True, env=env
    )
    # pytest exit code 5 == "no tests were collected" -- e.g. a repo/fixture with
    # no test suite yet, or an impact-selection pass that (correctly) found no
    # test touches this change. Nothing to gate on is not a rejection reason.
    ok = result.returncode in (0, 5)
    log = result.stdout + result.stderr
    return ok, log


def regenerate_summary(parcel: Parcel, graph: Optional[DepGraph] = None) -> str:
    """The integrator's AUTHORITATIVE `state_summary` (DESIGN §2, §5.4/§6): a
    deterministic `kind + signature-ish info + blast_radius` note computed from
    the freshly re-indexed parcel -- never the agent's self-reported one.

    `graph` (from the same `run_index` call that produced `parcel`) lets this
    include the parcel's real signature when it has one (top-level function/
    class); omitted for parcels `run_index` didn't compute a signature for
    (methods, module/class glue).
    """
    bits = [str(parcel.kind), f"symbol={parcel.symbol}"]
    if parcel.byte_start is not None and parcel.byte_end is not None:
        bits.append(f"{parcel.byte_end - parcel.byte_start}B")
    if graph is not None and parcel.id in graph.signatures:
        bits.append(f"sig={graph.signatures[parcel.id][0]}")
    bits.append(f"blast_radius={parcel.blast_radius}")
    bits.append("(integrator-verified)")
    return " ".join(bits)


def integrate(
    conn,
    repo: StrPath,
    branch: str,
    base_commit: Optional[str] = None,
    into: str = "integration",
    agent_id: Optional[str] = None,
    expected_read_deps: Optional[dict[str, str]] = None,
    test_dir: str = DEFAULT_TEST_DIR,
    threshold: Optional[int] = None,
) -> IntegrateResult:
    """Serially merge `branch` into `into` behind the pytest gate. DESIGN §5.4/§5.5.

    Not internally locked -- see module docstring: callers must invoke this for
    one branch at a time. `base_commit` (the branch's own fork point, e.g. what
    `worktree.git_ops.add_worktree` cut it from) is used to compute the
    touched-file diff for impact-test selection / re-indexing when given --
    that isolates exactly the files this branch's own commits changed. If
    omitted, falls back to diffing against `into`'s HEAD right before this
    call's merge, which can over-report files the branch never touched but
    trunk has since moved past.
    """
    repo = Path(repo)
    now = time.time()

    # --- step 1: optimistic re-check (DESIGN §5.5), opt-in -----------------
    if expected_read_deps:
        stale = _check_read_deps(conn, expected_read_deps)
        if stale:
            events_mod.emit(
                conn,
                "needs_rebase",
                agent_id,
                {
                    "branch": branch,
                    "into": into,
                    "stale_deps": stale,
                    "base_commit": base_commit,
                },
                ts=now,
            )
            return IntegrateResult(
                status="needs_rebase",
                branch=branch,
                into=into,
                stale_deps=stale,
                reason=f"stale read-dependencies: {stale}",
            )

    pre_merge_sha = git_ops.current_commit(repo, ref=into)

    def _reject_and_reset(
        reason_code: str,
        reason: str,
        *,
        result_fields: Optional[dict] = None,
        **payload_extra,
    ) -> IntegrateResult:
        """Roll `into` back to its exact pre-merge state and return a STRUCTURED
        merge_rejected (DESIGN §5.4 "leave trunk untouched on reject").

        S1 atomicity contract: used for any failure at or AFTER the merge --
        a GitOpsError, a parse/re-index crash, a bad ref -- so trunk is left
        byte-identical to `pre_merge_sha` and the caller gets a rejection object
        instead of a bubbled-out 500 sitting on top of a half-integrated trunk.
        `git reset --hard pre_merge_sha` is a no-op when the merge never landed
        (conflict aborted, error before the commit) and undoes the merge commit
        when it did -- either way trunk ends at `pre_merge_sha`.

        `payload_extra` are extra fields for the emitted event; `result_fields`
        are extra fields for the returned `IntegrateResult` (e.g. the pytest
        `changed_files`/`test_log` on a red-gate rejection).
        """
        rollback_error: Optional[str] = None
        try:
            git_ops.reset_hard(repo, pre_merge_sha, branch=into)
        except git_ops.GitOpsError as rexc:  # pragma: no cover - catastrophic git failure
            rollback_error = str(rexc)
        payload = {"branch": branch, "into": into, "reason": reason_code}
        payload.update(payload_extra)
        full_reason = reason
        if rollback_error is not None:
            payload["rollback_error"] = rollback_error
            full_reason = f"{reason} (WARNING: trunk rollback also failed: {rollback_error})"
        events_mod.emit(conn, "merge_rejected", agent_id, payload, ts=now)
        return IntegrateResult(
            status="merge_rejected",
            branch=branch,
            into=into,
            reason=full_reason,
            **(result_fields or {}),
        )

    # --- step 2: serialized merge -------------------------------------------
    try:
        ok, conflicts = git_ops.merge_branch(repo, branch, into=into)
    except git_ops.GitOpsError as exc:
        # A non-conflict merge failure (bad ref, dirty tree, git missing):
        # merge_branch already aborted any in-progress merge, so trunk is at
        # pre_merge_sha -- surface it as a structured rejection, not a 500.
        return _reject_and_reset("merge_error", f"git merge failed: {exc}")
    if not ok:
        events_mod.emit(
            conn,
            "merge_rejected",
            agent_id,
            {
                "branch": branch,
                "into": into,
                "reason": "merge_conflict",
                "conflicts": conflicts,
            },
            ts=now,
        )
        return IntegrateResult(
            status="merge_rejected",
            branch=branch,
            into=into,
            conflicts=conflicts,
            reason="textual merge conflict (touch-set misprediction)",
        )

    # From here the merge commit is ON trunk (`into`). Everything below --
    # diffing, the pytest gate, re-index, summary regen, contract detection --
    # runs inside one try/except so ANY post-merge failure rolls trunk back to
    # `pre_merge_sha` (byte-identical) and returns a structured merge_rejected,
    # never a 500 leaving a half-integrated, un-reindexed merge on trunk. The
    # success-path events (`merged`/`contract_change`/`reindexed`) are emitted
    # only AFTER this whole block succeeds, so a rolled-back merge never leaves
    # a dangling `merged` event in the log the projection tables replay from.
    try:
        # Diff against the branch's OWN fork point (`base_commit`) when known --
        # that isolates exactly the files *this branch's commits* touched. Falling
        # back to `pre_merge_sha` (trunk's head right before this merge) is only a
        # best-effort approximation for a caller that didn't pass `base_commit`:
        # it can over-report files the branch never touched but trunk has since
        # moved past (the branch's own tree looks "different" there too).
        diff_base = base_commit if base_commit is not None else pre_merge_sha
        changed = git_ops.changed_files(repo, branch, diff_base)

        # --- step 3: impact-selected pytest gate --------------------------------
        tests_ok, test_log = run_impact_tests(repo, changed, test_dir=test_dir)
        if not tests_ok:
            return _reject_and_reset(
                "tests_failed",
                "impact tests failed",
                changed_files=changed,
                test_log=test_log[-4000:],  # keep the event payload bounded
                result_fields={"changed_files": changed, "test_log": test_log},
            )

        merged_commit = git_ops.current_commit(repo, ref=into)

        # --- step 4: re-index + authoritative state_summary regen ---------------
        changed_set = set(changed)

        # Frozen-contract change detection (DESIGN §5.3, money-shot #3, U15):
        # snapshot every contract whose symbol lives in a file this branch
        # touched BEFORE re-indexing, so it can be diffed against the same
        # symbols' post-re-index state below. Restricted to `changed_set` since
        # `run_index` re-parses the WHOLE repo every call (per `classifier.store`'s
        # own "no incremental diffing" note) -- an unrelated file's contract rows
        # cannot have changed from THIS branch's edit, so there is nothing to
        # gain (and a real false-positive risk to avoid) by comparing those too.
        before_contracts = {
            row["symbol"]: (row["signature"], row["type_hash"], row["version"])
            for row in conn.execute(
                "SELECT symbol, signature, type_hash, version FROM contracts"
            ).fetchall()
            if row["symbol"].split("::", 1)[0] in changed_set
        }

        index_kwargs = {} if threshold is None else {"threshold": threshold}
        index_result = run_index(conn, repo, **index_kwargs)
        touched_parcels = [p for p in index_result.parcels if p.path in changed_set]

        reindexed_ids: list[str] = []
        for parcel in touched_parcels:
            summary = regenerate_summary(parcel, index_result.graph)
            conn.execute(
                "UPDATE parcels SET state_summary = ?, updated_at = ? WHERE id = ?",
                (summary, now, parcel.id),
            )
            reindexed_ids.append(parcel.id)

        # Diff post-re-index contract state against the pre-re-index snapshot
        # above. A symbol whose `type_hash` genuinely changed -> a frozen
        # signature really did change on this landed merge -> a
        # `contract_change` (old/new signature + version) so a dependent watching
        # `GET /events` (or holding a plan-time `read_contracts` snapshot,
        # `agent/runner.py`) can observe it and re-plan (DESIGN §5.3/§4.3). The
        # events are collected here and emitted below with the rest of the
        # success-path events, once the atomic block is known to have succeeded.
        contract_changes: list[str] = []
        contract_change_payloads: list[dict] = []
        for row in conn.execute(
            "SELECT symbol, signature, type_hash, version FROM contracts"
        ).fetchall():
            symbol = row["symbol"]
            before = before_contracts.get(symbol)
            if before is None or before[1] == row["type_hash"]:
                continue  # unknown before this merge, or genuinely unchanged
            contract_changes.append(symbol)
            contract_change_payloads.append(
                {
                    "symbol": symbol,
                    "branch": branch,
                    "into": into,
                    "old_signature": before[0],
                    "new_signature": row["signature"],
                    "old_version": before[2],
                    "new_version": row["version"],
                }
            )
    except Exception as exc:  # noqa: BLE001 -- deliberate: any post-merge failure
        # A failure AFTER the merge landed (a later GitOpsError, a parse/re-index
        # crash, a bad symbol, etc.): roll trunk back to byte-identical
        # pre_merge_sha and reject structurally rather than 500 with the merge
        # still sitting on trunk. No `merged` event was emitted yet, so the log
        # stays consistent (only the merge_rejected below).
        return _reject_and_reset(
            "integration_error", f"post-merge integration failed: {exc!r}"
        )

    # --- success: the whole atomic block landed. Emit the trail now. --------
    events_mod.emit(
        conn,
        "merged",
        agent_id,
        {
            "branch": branch,
            "into": into,
            "merged_commit": merged_commit,
            "changed_files": changed,
        },
        ts=now,
    )
    for payload in contract_change_payloads:
        events_mod.emit(conn, "contract_change", agent_id, payload, ts=now)
    events_mod.emit(
        conn,
        "reindexed",
        agent_id,
        {"branch": branch, "into": into, "parcels": reindexed_ids},
        ts=now,
    )

    return IntegrateResult(
        status="merged",
        branch=branch,
        into=into,
        merged_commit=merged_commit,
        changed_files=changed,
        reindexed_parcels=reindexed_ids,
        contract_changes=contract_changes,
        test_log=test_log,
    )
