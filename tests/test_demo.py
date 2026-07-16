"""U14/U15 -- End-to-end demo: all five money shots. DESIGN.md §7.

U14 done when (BUILD_PLAN.md): `python demo/run_demo.py` runs >=3 agents on
sample_repo, prints PASS for money-shots #1/#2/#4/#5, exits 0; zero same-file
textual collisions reached `integration`; trunk green throughout.

U15 done when (BUILD_PLAN.md): the demo shows an agent changing a frozen
signature (exclusive lease + `contract_change` event), a dependent agent
observing it, re-reading the contract, and landing a call-site fix with tests
green; money-shot #3 prints PASS and the full demo exits 0 with all five PASS.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_DEMO = REPO_ROOT / "demo" / "run_demo.py"

sys.path.insert(0, str(REPO_ROOT / "demo"))
import run_demo  # noqa: E402  (demo/ is not a package; path-inserted above)


# --- the literal done-when: `python demo/run_demo.py` as a real subprocess ---------


def test_run_demo_script_exits_zero_and_prints_pass_for_all_five_shots():
    result = subprocess.run(
        [sys.executable, str(RUN_DEMO)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"demo exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    out = result.stdout
    assert "PASS: money-shot #1" in out
    assert "PASS: money-shot #2" in out
    assert "PASS: money-shot #3" in out
    assert "PASS: money-shot #4" in out
    assert "PASS: money-shot #5" in out
    assert "PASS: overall" in out
    assert "FAIL: money-shot" not in out
    assert "ALL FIVE MONEY SHOTS PASS" in out


# --- structured, in-process assertions via run_demo.run_demo() ---------------------


def test_run_demo_reports_all_shots_ok_and_touches_at_least_three_agents(tmp_path):
    result = run_demo.run_demo(workdir=tmp_path / "demo-run", keep=True)

    assert result["all_ok"] is True
    for shot in ("shot1", "shot2", "shot3", "shot4", "shot5", "overall"):
        assert result["results"][shot] is True, f"{shot} did not pass: {result['results']}"


def test_run_demo_lands_at_least_three_distinct_agents_worth_of_commits(tmp_path):
    """DESIGN §7's own framing: "a run with >=3 concurrent agents on the sample
    repo." Assert the integration branch's git log actually shows >=3 distinct
    landed merge commits from >=3 distinct branches (agents), not just that the
    script printed PASS."""
    workdir = tmp_path / "demo-run-2"
    run_demo.run_demo(workdir=workdir, keep=True)

    repo = workdir / "repo"
    log = subprocess.run(
        ["git", "log", "--merges", "--pretty=%s"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    merge_subjects = [line for line in log.stdout.splitlines() if line.strip()]
    # "merge <branch> into integration" -- extract the distinct <branch> names.
    branches = {s.split(" ")[1] for s in merge_subjects if s.startswith("merge ")}
    assert len(branches) >= 3, f"expected >=3 distinct landed agent branches, got {branches!r}"


def test_run_demo_has_zero_textual_merge_conflicts_in_its_event_log(tmp_path):
    workdir = tmp_path / "demo-run-3"
    result = run_demo.run_demo(workdir=workdir, keep=True)
    assert result["all_ok"] is True

    db_path = workdir / "blackboard.db"
    assert db_path.exists()

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT payload FROM events WHERE type = 'merge_rejected'"
    ).fetchall()
    conn.close()

    reasons = [json.loads(r["payload"])["reason"] for r in rows if r["payload"]]
    assert "merge_conflict" not in reasons
    # money-shot #5's own deliberate rejection IS expected, for a real reason.
    assert "tests_failed" in reasons


def test_run_demo_leaves_trunk_test_suite_green_at_the_end(tmp_path):
    workdir = tmp_path / "demo-run-4"
    result = run_demo.run_demo(workdir=workdir, keep=True)
    assert result["all_ok"] is True

    repo = workdir / "repo"
    suite = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert suite.returncode == 0, suite.stdout + suite.stderr


# --- U15: money-shot #3's own specifics (frozen-contract change + re-plan) ---------


def test_run_demo_shot3_emits_contract_change_and_lands_dependent_fixes(tmp_path):
    """U15 done-when, checked directly against the blackboard/git state (not
    just the printed PASS lines): an exclusive-lease signature change on
    `calc.py::add` emits a real `contract_change` event carrying the old/new
    signature, and both real dependents' call-site fixes land on trunk."""
    workdir = tmp_path / "demo-run-shot3"
    result = run_demo.run_demo(workdir=workdir, keep=True)
    assert result["all_ok"] is True
    assert result["results"]["shot3"] is True

    repo = workdir / "repo"
    db_path = workdir / "blackboard.db"

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    contract_rows = conn.execute(
        "SELECT seq, agent_id, payload FROM events WHERE type = 'contract_change' ORDER BY seq"
    ).fetchall()
    lease_rows = conn.execute(
        "SELECT payload FROM events WHERE type = 'lease_granted'"
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

    # The signature-change task really did take an EXCLUSIVE lease on the
    # frozen contract (DESIGN §5.3), auto-enforced by coordinator.broker.run.
    exclusive_grants = [
        json.loads(r["payload"]) for r in lease_rows
        if json.loads(r["payload"] or "{}").get("parcel_id") == "calc.py::add"
        and json.loads(r["payload"] or "{}").get("mode") == "exclusive"
    ]
    assert exclusive_grants, "expected calc.py::add to be leased in exclusive mode"

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
