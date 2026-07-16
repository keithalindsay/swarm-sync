"""S1 hardening — POST /integrate must serialize concurrent merges. DESIGN §5.4.

The P0 regression guard: fire N `POST /integrate` requests for N independently
committed, file-DISJOINT branches at a live `create_app()`/`TestClient` *at the
same time* and assert the integrator behaves as DESIGN §5.4's "serial
test-gated integrator" promises:

  * every clean branch LANDS (all N come back `merged`, none 500s, none is
    spuriously `merge_rejected`),
  * trunk is never left dirty / mid-merge,
  * no committed work is silently dropped (every branch's file is on trunk),
  * exactly N `merged` events are recorded.

On the OLD unserialized code these concurrent requests race on the single
shared `integration` checkout -- interleaved `git checkout`/`git merge`/`git
reset --hard` and `.git/index.lock` collisions -- so at least one request
bubbles a 500, gets a bogus rejection, drops a committed branch, or leaves
trunk dirty, and one of the asserts below fails. With the process-wide
`integrate_lock` (server/app.py) the merges queue and all N land clean.
"""
from __future__ import annotations

import concurrent.futures
import subprocess
import sys
import threading

import pytest
from starlette.testclient import TestClient

from swarmsync.server.app import create_app
from swarmsync.worktree import git_ops

N_BRANCHES = 6


def _seed_repo(root):
    """A base repo with a tests/ dir, so each branch can add a disjoint file."""
    (root / "seed.py").write_text("def seed():\n    return 0\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_seed.py").write_text(
        "from seed import seed\n\n\ndef test_seed():\n    assert seed() == 0\n",
        encoding="utf-8",
    )


def _make_branch(repo, base, i):
    """An independently committed branch that adds ONLY its own new files.

    File-disjoint by construction (each branch touches feat_<i>.py +
    tests/test_feat_<i>.py and nothing else), so a correctly serialized
    integrator merges every one of them conflict-free -- any failure to land is
    therefore a concurrency defect, not a real merge conflict.
    """
    wt = git_ops.add_worktree(repo, f"agent-{i}", base)
    (wt / f"feat_{i}.py").write_text(
        f"def feat_{i}():\n    return {i}\n", encoding="utf-8"
    )
    (wt / "tests" / f"test_feat_{i}.py").write_text(
        f"from feat_{i} import feat_{i}\n\n\n"
        f"def test_feat_{i}():\n    assert feat_{i}() == {i}\n",
        encoding="utf-8",
    )
    git_ops.commit_all(wt, f"agent-{i}: add feat_{i}")


def test_concurrent_integrate_serializes_and_lands_every_clean_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_repo(repo)
    base = git_ops.init_repo(repo)

    for i in range(N_BRANCHES):
        _make_branch(repo, base, i)

    app = create_app(tmp_path / "blackboard.db")
    with TestClient(app) as client:
        client.post("/index", json={"root": str(repo)})

        # Release all N requests as simultaneously as possible so the OLD
        # (unlocked) handler actually races on the shared checkout.
        start = threading.Barrier(N_BRANCHES)

        def submit(i):
            start.wait()
            return client.post(
                "/integrate",
                json={
                    "agent_id": f"agent-{i}",
                    "branch": f"agent-{i}",
                    "repo": str(repo),
                    "base_commit": base,
                },
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=N_BRANCHES) as ex:
            responses = list(ex.map(submit, range(N_BRANCHES)))

        # (1) No request bubbled a 500 from a git race -- every one returned a
        # structured result.
        for r in responses:
            assert r.status_code == 200, r.text

        # (2) Every clean, file-disjoint branch LANDED. On the old code a race
        # drops merges / spuriously rejects them, so this is the load-bearing
        # assert.
        statuses = sorted(r.json()["status"] for r in responses)
        assert statuses == ["merged"] * N_BRANCHES, statuses

        # (3) Trunk is not dirty and not stuck mid-merge. `--untracked-files=no`
        # ignores pytest's __pycache__ noise while still surfacing any modified
        # tracked file or unmerged (UU) path a race would leave behind.
        porcelain = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        assert porcelain.stdout.strip() == "", porcelain.stdout
        assert not (repo / ".git" / "MERGE_HEAD").exists()

        # (4) No committed work silently dropped -- every branch's file is on
        # trunk with exactly its committed content.
        for i in range(N_BRANCHES):
            assert (repo / f"feat_{i}.py").read_text() == (
                f"def feat_{i}():\n    return {i}\n"
            )

        # (5) Exactly N `merged` events recorded, one per branch.
        events = client.get("/events").json()
        merged = [e for e in events if e["type"] == "merged"]
        assert len(merged) == N_BRANCHES

    # (6) Trunk's own full suite is green after all N landed.
    full = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert full.returncode == 0, full.stdout + full.stderr


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
