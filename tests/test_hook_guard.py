"""`scripts/swarmsync-hook-guard` -- the fast opt-in shell guard.

The guard makes NORMAL (non-coordinated) editing pay ~zero overhead: it exits 0
immediately without ever launching Python unless a coordinated session is active
(env `SWARMSYNC_ACTIVE` set, or a `.swarmsync-active` marker at the project
root). It is fail-open by construction: a missing/non-executable adapter still
lets the edit through.

These tests drive the REAL script as a subprocess across the whole matrix
(dormant / active-via-env / active-via-marker / fail-open), plus the two
HOOK_BIN resolution paths (`command -v` on PATH, and the `../.venv/bin`
sibling fallback relative to the script's own location) so "did the adapter
actually launch?" is directly observable and the tests never depend on a real
server being up.
"""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_SRC = REPO_ROOT / "scripts" / "swarmsync-hook-guard"

# A PATH with no `swarmsync-hook` anywhere on it, but with real coreutils
# (dirname, etc.) so the guard's own control flow still runs -- this forces
# `resolve_hook_bin()` past `command -v` into the sibling-fallback branch.
BARE_PATH = "/usr/bin:/bin"


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _copy_guard(dest_dir: Path) -> Path:
    """Copy the real, UNMODIFIED guard script into `dest_dir` (a `scripts/`
    dir, so a sibling `../.venv/bin/` resolves the way it does in the real repo)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    guard = dest_dir / "swarmsync-hook-guard"
    guard.write_text(GUARD_SRC.read_text())
    _make_executable(guard)
    return guard


def _write_stub_adapter(dest_dir: Path, sentinel: Path, exit_code: int = 0) -> Path:
    """A stand-in swarmsync-hook: records that it launched (+ its argv) then exits.
    Always named `swarmsync-hook` so both resolution paths (PATH lookup and the
    `../.venv/bin` sibling) can find it by its real name."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stub = dest_dir / "swarmsync-hook"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{sentinel}"\n'
        f"exit {exit_code}\n"
    )
    _make_executable(stub)
    return stub


def _run_guard(guard: Path, *, active_env: bool, project_dir: Path, path: str, args=()):
    env = {"PATH": path}
    if active_env:
        env["SWARMSYNC_ACTIVE"] = "1"
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    return subprocess.run(
        [str(guard), *args],
        env=env,
        cwd=str(project_dir),
        # The guard reads its payload with an unconditional `INPUT=$(cat)`. Without
        # an explicit stdin it inherits the terminal under `pytest -s` (capture off)
        # and blocks forever. DEVNULL keeps `-s` usable for watching live output.
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_dormant_session_exits_zero_without_launching_the_adapter(tmp_path):
    """No env activation, no marker -> allow the edit (exit 0) and NEVER launch
    the adapter (the zero-overhead normal-editing path)."""
    project = tmp_path / "proj"
    project.mkdir()
    sentinel = tmp_path / "launched"
    bin_dir = tmp_path / "bin"
    _write_stub_adapter(bin_dir, sentinel)
    guard = _copy_guard(tmp_path / "scripts")

    result = _run_guard(
        guard, active_env=False, project_dir=project, path=f"{bin_dir}:{BARE_PATH}"
    )

    assert result.returncode == 0
    assert not sentinel.exists(), "guard launched the adapter while dormant"


def test_active_via_env_resolves_hook_bin_on_path_and_forwards_argv(tmp_path):
    """`command -v swarmsync-hook` (PATH resolution) is tried first."""
    project = tmp_path / "proj"
    project.mkdir()
    sentinel = tmp_path / "launched"
    bin_dir = tmp_path / "bin"
    _write_stub_adapter(bin_dir, sentinel)
    guard = _copy_guard(tmp_path / "scripts")

    result = _run_guard(
        guard,
        active_env=True,
        project_dir=project,
        path=f"{bin_dir}:{BARE_PATH}",
        args=["precheck"],
    )

    assert result.returncode == 0
    assert sentinel.exists(), "guard did not launch the adapter when active"
    # `exec "$HOOK_BIN" "$@"` -> the guard forwards its subcommand argv through.
    assert sentinel.read_text().strip() == "precheck"


def test_active_via_marker_file_launches_the_adapter(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".swarmsync-active").write_text("")  # the opt-in marker
    sentinel = tmp_path / "launched"
    bin_dir = tmp_path / "bin"
    _write_stub_adapter(bin_dir, sentinel)
    guard = _copy_guard(tmp_path / "scripts")

    # env activation OFF: only the marker makes it active.
    result = _run_guard(
        guard,
        active_env=False,
        project_dir=project,
        path=f"{bin_dir}:{BARE_PATH}",
        args=["precheck"],
    )

    assert result.returncode == 0
    assert sentinel.exists(), "marker file did not activate the guard"


def test_active_resolves_hook_bin_via_venv_sibling_fallback_when_not_on_path(tmp_path):
    """REGRESSION (the fix this stage makes): the guard must find `swarmsync-hook`
    relative to ITS OWN location (`<checkout>/scripts/../.venv/bin/swarmsync-hook`)
    even when nothing named `swarmsync-hook` is on PATH -- e.g. a repo checked out
    somewhere other than the single absolute path the old script hardcoded.

    Fails on the OLD guard (hardcoded
    `HOOK_BIN="/home/keith/projects/swarm-sync/.venv/bin/swarmsync-hook"`): that
    exact path does not exist under this test's isolated tmp_path checkout, so
    the old script would silently fall through to the fail-open branch and never
    launch the adapter (`sentinel` would never be written). The NEW guard
    resolves `dirname "$0"/../.venv/bin/swarmsync-hook` relative to wherever the
    script itself lives, so it finds the stub here and launches it.
    """
    project = tmp_path / "proj"
    project.mkdir()
    sentinel = tmp_path / "launched"
    # Lay out a repo-shaped tree: <checkout>/scripts/swarmsync-hook-guard next to
    # <checkout>/.venv/bin/swarmsync-hook -- nowhere near the real swarm-sync
    # checkout's absolute path.
    checkout = tmp_path / "some-other-checkout"
    guard = _copy_guard(checkout / "scripts")
    _write_stub_adapter(checkout / ".venv" / "bin", sentinel)

    result = _run_guard(
        guard,
        active_env=True,
        project_dir=project,
        path=BARE_PATH,  # deliberately no swarmsync-hook anywhere on PATH
        args=["precheck"],
    )

    assert result.returncode == 0
    assert sentinel.exists(), "guard did not fall back to the ../.venv/bin sibling"
    assert sentinel.read_text().strip() == "precheck"


def test_active_but_adapter_unavailable_is_fail_open(tmp_path):
    """Active session but the adapter is missing/non-executable via EITHER
    resolution path -> the guard must STILL exit 0 so editing keeps working
    (fail-open by construction)."""
    project = tmp_path / "proj"
    project.mkdir()
    # No stub anywhere: not on PATH, and no ../.venv/bin sibling either.
    guard = _copy_guard(tmp_path / "some-other-checkout" / "scripts")

    result = _run_guard(
        guard, active_env=True, project_dir=project, path=BARE_PATH, args=["precheck"]
    )

    assert result.returncode == 0, "guard was not fail-open with an unavailable adapter"


def test_active_adapter_exit_code_propagates(tmp_path):
    """When the adapter DOES run (active + executable), the guard `exec`s it, so
    the adapter's own exit code is what the tool call sees (e.g. a structured
    deny path). Confirms the guard is a transparent passthrough, not a swallow."""
    project = tmp_path / "proj"
    project.mkdir()
    sentinel = tmp_path / "launched"
    bin_dir = tmp_path / "bin"
    _write_stub_adapter(bin_dir, sentinel, exit_code=2)
    guard = _copy_guard(tmp_path / "scripts")

    result = _run_guard(
        guard,
        active_env=True,
        project_dir=project,
        path=f"{bin_dir}:{BARE_PATH}",
        args=["precheck"],
    )

    assert sentinel.exists()
    assert result.returncode == 2
