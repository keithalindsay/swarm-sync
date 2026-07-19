"""U14/U15 -- End-to-end demo: all five test cases. DESIGN.md §7.

U14 done when (BUILD_PLAN.md): `python demo/run_demo.py` runs >=3 agents on
sample_repo, prints PASS for test cases #1/#2/#4/#5, exits 0; zero same-file
textual collisions reached `integration`; trunk green throughout.

U15 done when (BUILD_PLAN.md): the demo shows an agent changing a frozen
signature (emitting a real `contract_change` event), a dependent agent
observing it, re-reading the contract, and landing a call-site fix with tests
green; test case #3 prints PASS and the full demo exits 0 with all five PASS.
(At file granularity -- the shipping mode -- the change takes a whole-file WRITE
lease, not a symbol-level exclusive lease: the frozen-contract exclusive-upgrade
is parked with symbol mode. Contract DETECTION is granularity-independent and
is what this shot exercises; see SYMBOL_MODE_DESIGN.md.)

S6 note: the five in-process assertions below all read ONE session-scoped
`run_demo(keep=True)` (the `demo_run` fixture) rather than each spinning up its
own ~9s demo run -- the demo is deterministic, so a single run is authoritative
for every angle these tests check. The literal `python demo/run_demo.py`
subprocess done-when stays separate (it uniquely exercises `main()`'s stdout
PASS-printing / real process exit code, which the in-process API never prints).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_DEMO = REPO_ROOT / "demo" / "run_demo.py"

sys.path.insert(0, str(REPO_ROOT / "demo"))
import run_demo  # noqa: E402  (demo/ is not a package; path-inserted above)


@pytest.fixture(scope="session")
def demo_run(tmp_path_factory):
    """Run the whole demo exactly ONCE per test session and hand every
    in-process assertion below the same (result, workdir) pair.

    This fixture is instantiated before the function-scoped `SWARMSYNC_ROOTS`
    autouse fixture in conftest (a session fixture is set up before any
    lower-scoped one it shares a test with), so the managed-root allow-list the
    demo's `/index`+`/integrate` need is pinned here too. The workdir lives
    under the system temp root, so pointing the allow-list at gettempdir()
    covers it -- exactly the operator move conftest documents.
    """
    os.environ["SWARMSYNC_ROOTS"] = tempfile.gettempdir()
    workdir = Path(tmp_path_factory.mktemp("demo-run"))
    result = run_demo.run_demo(workdir=workdir, keep=True)
    return result, workdir


# --- the literal done-when: `python demo/run_demo.py` as a real subprocess ---------


def test_run_demo_script_exits_zero_and_prints_pass_for_all_five_shots():
    # Scrub SWARMSYNC_ROOTS from the child env: the conftest autouse fixture sets it
    # process-wide, which would mask an out-of-the-box break. The documented invocation
    # `python demo/run_demo.py` must work with NO pre-set allow-list (run_demo self-configures).
    env = {k: v for k, v in os.environ.items() if k != "SWARMSYNC_ROOTS"}
    result = subprocess.run(
        [sys.executable, str(RUN_DEMO)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert result.returncode == 0, (
        f"demo exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    out = result.stdout
    assert "PASS: test case #1" in out
    assert "PASS: test case #2" in out
    assert "PASS: test case #3" in out
    assert "PASS: test case #4" in out
    assert "PASS: test case #5" in out
    assert "PASS: overall" in out
    assert "FAIL: test case" not in out
    assert "ALL FIVE TEST CASES PASS" in out


# --- structured, in-process assertions -- all read the ONE `demo_run` fixture ------


def test_run_demo_reports_all_shots_ok_and_touches_at_least_three_agents(demo_run):
    result, _workdir = demo_run

    assert result["all_ok"] is True
    for shot in ("shot1", "shot2", "shot3", "shot4", "shot5", "overall"):
        assert result["results"][shot] is True, f"{shot} did not pass: {result['results']}"


def test_run_demo_lands_at_least_three_distinct_agents_worth_of_commits(demo_run):
    """DESIGN §7's own framing: "a run with >=3 concurrent agents on the sample
    repo." Assert the integration branch's git log actually shows >=3 distinct
    landed merge commits from >=3 distinct branches (agents), not just that the
    script printed PASS."""
    _result, workdir = demo_run

    repo = workdir / "repo"
    log = subprocess.run(
        ["git", "log", "--merges", "--pretty=%s"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    merge_subjects = [line for line in log.stdout.splitlines() if line.strip()]
    # "merge <branch> into integration" -- extract the distinct <branch> names.
    branches = {s.split(" ")[1] for s in merge_subjects if s.startswith("merge ")}
    assert len(branches) >= 3, f"expected >=3 distinct landed agent branches, got {branches!r}"


def test_run_demo_has_zero_textual_merge_conflicts_in_its_event_log(demo_run):
    result, workdir = demo_run
    assert result["all_ok"] is True

    db_path = workdir / "blackboard.db"
    assert db_path.exists()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT payload FROM events WHERE type = 'merge_rejected'"
    ).fetchall()
    conn.close()

    reasons = [json.loads(r["payload"])["reason"] for r in rows if r["payload"]]
    assert "merge_conflict" not in reasons
    # test case #5's own deliberate rejection IS expected, for a real reason.
    assert "tests_failed" in reasons


def test_run_demo_leaves_trunk_test_suite_green_at_the_end(demo_run):
    result, workdir = demo_run
    assert result["all_ok"] is True

    repo = workdir / "repo"
    suite = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert suite.returncode == 0, suite.stdout + suite.stderr


# --- U15: test case #3's own specifics (frozen-contract change + re-plan) ---------


def test_run_demo_shot3_emits_contract_change_and_lands_dependent_fixes(demo_run):
    """U15 done-when, checked directly against the blackboard/git state (not
    just the printed PASS lines): an exclusive-lease signature change on
    `calc.py::add` emits a real `contract_change` event carrying the old/new
    signature, and both real dependents' call-site fixes land on trunk."""
    result, workdir = demo_run
    assert result["all_ok"] is True
    assert result["results"]["shot3"] is True

    repo = workdir / "repo"
    db_path = workdir / "blackboard.db"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    contract_rows = conn.execute(
        "SELECT seq, agent_id, payload FROM events WHERE type = 'contract_change' ORDER BY seq"
    ).fetchall()
    lease_rows = conn.execute(
        "SELECT agent_id, payload FROM events WHERE type = 'lease_granted'"
    ).fetchall()
    merged_rows = conn.execute(
        "SELECT seq, payload FROM events WHERE type = 'merged' ORDER BY seq"
    ).fetchall()
    conn.close()

    assert contract_rows, "expected at least one contract_change event"
    payloads = [json.loads(r["payload"]) for r in contract_rows]
    add_changes = [p for p in payloads if p["symbol"] == "calc.py::add"]
    assert add_changes, f"no contract_change for calc.py::add among {payloads!r}"
    change = add_changes[0]
    assert "rounding" not in change["old_signature"]
    assert "rounding" in change["new_signature"]

    # File granularity (the shipping mode): the signature-change task takes a
    # whole-file WRITE lease on calc.py::<module>, NOT a symbol-level exclusive
    # lease on calc.py::add -- the frozen-contract exclusive-upgrade (DESIGN
    # §5.3) is inert here and parked with symbol mode (SYMBOL_MODE_DESIGN.md).
    # Contract detection below is what still ships, and is what this shot proves.
    change_grants = [
        json.loads(r["payload"]) for r in lease_rows
        if r["agent_id"].startswith("shot3-change-add-signature")
    ]
    assert change_grants, "expected the signature-change agent to acquire a lease"
    assert all(g["parcel_id"] == "calc.py::<module>" for g in change_grants), (
        f"signature-change task should lease the WHOLE FILE, got {change_grants!r}"
    )
    assert all(g["mode"] == "write" for g in change_grants), (
        f"expected a whole-file write lease (exclusive-upgrade is parked), got {change_grants!r}"
    )

    # Ordering: the contract_change event precedes both dependents' `merged`
    # events -- they could only have re-read the NEW contract after it changed.
    dependent_merged_seqs = [
        r["seq"] for r in merged_rows
        if json.loads(r["payload"] or "{}").get("branch", "").startswith(
            ("shot3-fix-formats-call-site", "shot3-fix-api-call-site")
        )
    ]
    assert len(dependent_merged_seqs) >= 2
    assert contract_rows[0]["seq"] < min(dependent_merged_seqs)

    formats_src = (repo / "formats.py").read_text()
    api_src = (repo / "api.py").read_text()
    calc_src = (repo / "calc.py").read_text()
    assert "rounding=2" in formats_src
    assert "rounding=2" in api_src
    assert "rounding" in calc_src

    suite = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert suite.returncode == 0, suite.stdout + suite.stderr
