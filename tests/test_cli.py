"""The `swarmsync` operator/agent CLI (WP5.1, U2/U5).

Commands are driven through `cli.run(parsed_args, client, out)` against a
TestClient-backed `BlackboardClient` -- the same injection seam the agent tests
use -- so the assertions exercise the real HTTP surface, not mocks. `main()` is
tested separately for its parser and its unreachable-server path.
"""
from __future__ import annotations

import io
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from swarmsync import __version__, cli
from swarmsync.agent.client import BlackboardClient
from swarmsync.server.app import create_app


@pytest.fixture()
def bb(tmp_path):
    """A live blackboard (TestClient) plus a raw handle to seed it with."""
    app = create_app(tmp_path / "blackboard.db")
    with TestClient(app) as tc:
        yield tc


def _seed_lease(tc, agent_id, parcel_id):
    r = tc.post(
        "/lease",
        json={"agent_id": agent_id, "parcel_id": parcel_id, "ensure_parcel": True},
    )
    assert r.status_code == 200 and r.json()["granted"], r.text


def _run(tc, argv):
    """Parse `argv` through the real parser and dispatch against a client wrapping
    `tc`. Returns (exit_code, stdout_text)."""
    args = cli._build_parser().parse_args(argv)
    out = io.StringIO()
    code = cli.run(args, BlackboardClient(tc), out)
    return code, out.getvalue()


# --- status ------------------------------------------------------------------------


def test_status_reports_up_version_root_and_active_holds(bb):
    _seed_lease(bb, "agent-a", "payments.py::<module>")

    code, text = _run(bb, ["status"])

    assert code == 0
    assert "UP" in text
    assert __version__ in text  # version comes straight from /health
    assert "1 active" in text
    # the hold is listed with its holder
    assert "payments.py::<module>" in text
    assert "agent-a" in text


def test_status_works_against_an_empty_blackboard(bb):
    code, text = _run(bb, ["status"])
    assert code == 0
    assert "UP" in text
    assert "0 active" in text


# --- holds -------------------------------------------------------------------------


def test_holds_lists_every_active_hold(bb):
    _seed_lease(bb, "agent-a", "payments.py::<module>")
    _seed_lease(bb, "agent-b", "ledger.py::post")

    code, text = _run(bb, ["holds"])

    assert code == 0
    assert "payments.py::<module>" in text and "agent-a" in text
    assert "ledger.py::post" in text and "agent-b" in text


def test_holds_says_so_when_nothing_is_held(bb):
    code, text = _run(bb, ["holds"])
    assert code == 0
    assert "no active holds" in text


# --- free (the work-discovery surface, U5) -----------------------------------------


def test_free_marks_held_and_free_paths_and_exits_nonzero_if_any_held(bb):
    # A SYMBOL-level hold must make the whole FILE read as held (path granularity).
    _seed_lease(bb, "agent-a", "payments.py::charge")

    code, text = _run(bb, ["free", "payments.py", "ledger.py"])

    assert code == 1  # at least one requested path is held -> non-zero for `&&` gating
    assert "HELD  payments.py  by agent-a" in text
    assert "FREE  ledger.py" in text


def test_free_exits_zero_when_all_requested_paths_are_free(bb):
    _seed_lease(bb, "agent-a", "payments.py::<module>")

    code, text = _run(bb, ["free", "ledger.py", "billing.py"])

    assert code == 0
    assert "FREE  ledger.py" in text and "FREE  billing.py" in text
    assert "HELD" not in text


def test_free_names_all_holders_of_a_contested_file(bb):
    _seed_lease(bb, "agent-a", "payments.py::charge")
    _seed_lease(bb, "agent-b", "payments.py::refund")

    code, text = _run(bb, ["free", "payments.py"])

    assert code == 1
    assert "agent-a" in text and "agent-b" in text


# --- events ------------------------------------------------------------------------


def test_events_shows_recent_events_with_seq_and_type(bb):
    _seed_lease(bb, "agent-a", "payments.py::<module>")  # emits a lease event

    code, text = _run(bb, ["events"])

    assert code == 0
    assert "#" in text  # a seq marker
    # the lease we just took should appear with the holder in its payload
    assert "agent-a" in text


def test_events_n_limits_the_window(bb):
    for i in range(5):
        _seed_lease(bb, f"agent-{i}", f"mod{i}.py::<module>")

    code, text = _run(bb, ["events", "-n", "2"])

    assert code == 0
    assert len([ln for ln in text.splitlines() if ln.strip().startswith("#")]) == 2


# --- main(): parser + unreachable server -------------------------------------------


def test_main_requires_a_subcommand(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code != 0  # argparse usage error


def test_main_reports_an_unreachable_server_without_a_traceback(capsys):
    # 127.0.0.1:9 (discard) refuses immediately -> a clean, named failure, not a stack.
    code = cli.main(["--url", "http://127.0.0.1:9", "--timeout", "1", "status"])
    assert code == 2
    err = capsys.readouterr().err
    assert "cannot reach the blackboard" in err
    assert "http://127.0.0.1:9" in err


# --- WP5.2: init-hooks + doctor ----------------------------------------------------


def _dead_client():
    """A client pointed at a refusing port -- for fs-only commands and for
    exercising doctor's server-unreachable branch."""
    return BlackboardClient("http://127.0.0.1:9", timeout=1)


@pytest.fixture()
def git_repo(tmp_path, monkeypatch):
    """A fresh git repo as cwd, with HOME pointed at a clean dir (so the global
    settings.json probe can't see the developer's real ~/.claude), and no
    activation env leaking in."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SWARMSYNC_ACTIVE", raising=False)
    monkeypatch.chdir(repo)
    return repo


def _cli(argv, client=None, cwd_out=None):
    args = cli._build_parser().parse_args(argv)
    out = io.StringIO()
    code = cli.run(args, client or _dead_client(), out)
    return code, out.getvalue()


def _check(text, label):
    """The '[ok  ]'/'[FAIL]' status doctor printed for `label`, or None."""
    for line in text.splitlines():
        if f"] {label}:" in line:
            return "ok" if line.lstrip().startswith("[ok") else "FAIL"
    return None


def test_init_hooks_writes_all_four_events_and_the_marker(git_repo):
    code, _ = _cli(["init-hooks"])
    assert code == 0

    settings = json.loads((git_repo / ".claude" / "settings.json").read_text())
    hooks = settings["hooks"]
    assert set(hooks) == {"PreToolUse", "PostToolUse", "SubagentStop", "SessionStart"}
    # exactly one swarm-sync entry per event, and the adapter subcommands are wired
    for event in hooks:
        assert sum(cli._is_swarmsync_entry(e) for e in hooks[event]) == 1
    commands = [h["command"] for e in hooks["PreToolUse"] for h in e["hooks"]]
    assert any(c.endswith("precheck") for c in commands)
    assert (git_repo / ".swarmsync-active").exists()  # coordination turned on


def test_init_hooks_is_idempotent(git_repo):
    _cli(["init-hooks"])
    _cli(["init-hooks"])  # a second run must not duplicate

    hooks = json.loads((git_repo / ".claude" / "settings.json").read_text())["hooks"]
    for event in hooks:
        assert sum(cli._is_swarmsync_entry(e) for e in hooks[event]) == 1


def test_init_hooks_preserves_foreign_hooks_and_other_settings(git_repo):
    claude = git_repo / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps(
            {
                "model": "claude-opus-4-8",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "/usr/bin/my-linter"}],
                        }
                    ]
                },
            }
        )
    )

    _cli(["init-hooks"])

    settings = json.loads((claude / "settings.json").read_text())
    assert settings["model"] == "claude-opus-4-8"  # untouched
    pre = settings["hooks"]["PreToolUse"]
    assert any("my-linter" in h["command"] for e in pre for h in e["hooks"])  # foreign kept
    assert sum(cli._is_swarmsync_entry(e) for e in pre) == 1  # ours added


def test_init_hooks_dry_run_touches_nothing(git_repo):
    code, text = _cli(["init-hooks", "--dry-run"])
    assert code == 0
    assert "dry-run" in text
    assert not (git_repo / ".claude" / "settings.json").exists()
    assert not (git_repo / ".swarmsync-active").exists()


def test_doctor_flags_unreachable_off_and_unwired(git_repo):
    code, text = _cli(["--url", "http://127.0.0.1:9", "--timeout", "1", "doctor"])
    assert code != 0
    assert _check(text, "server reachable") == "FAIL"
    assert _check(text, "coordination active") == "FAIL"  # no marker/env
    assert _check(text, "hooks wired") == "FAIL"  # nothing in settings.json


def test_doctor_hooks_and_marker_pass_after_init(git_repo):
    _cli(["init-hooks"])  # writes hooks + drops marker

    _, text = _cli(["--url", "http://127.0.0.1:9", "--timeout", "1", "doctor"])
    assert _check(text, "hooks wired") == "ok"
    assert _check(text, "coordination active") == "ok"
    assert _check(text, "in a git repo") == "ok"


def test_doctor_flags_a_non_git_cwd(tmp_path, monkeypatch):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(plain)

    _, text = _cli(["--url", "http://127.0.0.1:9", "--timeout", "1", "doctor"])
    assert _check(text, "in a git repo") == "FAIL"
