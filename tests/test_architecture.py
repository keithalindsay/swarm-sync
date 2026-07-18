"""Import-direction guard (WP4.1, finding A2: package-layering inversion).

Target layering: `blackboard <- {classifier, server, coordinator, agent, hooks}`,
strictly one-directional. Before WP4.1, `server/events.py` + `server/leases.py`
(pure SQLite domain code) were misfiled under `server/`, so `coordinator/`
imported `swarmsync.server` while `server/app.py` imported `coordinator` -- a
bidirectional server<->coordinator dependency one import away from a cycle.

Rules enforced here, by AST-parsing every module file under the `swarmsync`
package (never importing/executing the walked modules):

  1. No module under `swarmsync/blackboard/` imports from `swarmsync.server`,
     `swarmsync.coordinator`, `swarmsync.agent`, `swarmsync.hooks`, or
     `swarmsync.classifier`. (The deprecated re-export shims
     `server/events.py` / `server/leases.py` are the CONVERSE direction --
     server importing blackboard -- which is the correct direction anyway, so
     no exemption is needed for them under this rule.)

  2. No module under `swarmsync/coordinator/` imports from `swarmsync.server`.
     No whitelist: after the WP4.1 move the coordinator needs nothing from the
     server layer at all -- it operates on the blackboard directly.

Plus a shim-compatibility test proving the old `swarmsync.server.events` /
`swarmsync.server.leases` import paths still resolve to the same objects for
third-party callers.
"""
from __future__ import annotations

import ast
from pathlib import Path

import swarmsync

PACKAGE_ROOT = Path(swarmsync.__file__).resolve().parent


def _module_name(path: Path) -> str:
    """Dotted module name of `path` relative to the `swarmsync` package root."""
    rel = path.relative_to(PACKAGE_ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imported_modules(path: Path) -> list[tuple[int, str]]:
    """(lineno, dotted-module) for every import statement in `path`, AST-parsed.

    `from X import Y` contributes both `X` and `X.Y` (Y may be a submodule --
    `from swarmsync import server` must register as an import of
    `swarmsync.server`). Relative imports are resolved against the file's own
    package before the same expansion.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module = _module_name(path)
    package_parts = module.split(".")
    if path.name != "__init__.py":
        package_parts = package_parts[:-1]

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                anchor = package_parts[: len(package_parts) - (node.level - 1)]
                base = ".".join(anchor + ([node.module] if node.module else []))
            if base:
                out.append((node.lineno, base))
            for alias in node.names:
                if alias.name != "*":
                    out.append((node.lineno, f"{base}.{alias.name}" if base else alias.name))
    return out


def _violations(subpackage: str, forbidden_prefixes: tuple[str, ...]) -> list[str]:
    """Human-readable rule violations for every module under `subpackage`."""
    found: list[str] = []
    for path in sorted((PACKAGE_ROOT / subpackage).rglob("*.py")):
        for lineno, imported in _imported_modules(path):
            for prefix in forbidden_prefixes:
                if imported == prefix or imported.startswith(prefix + "."):
                    found.append(f"{path.relative_to(PACKAGE_ROOT.parent)}:{lineno} imports {imported}")
    return found


def test_walk_actually_sees_modules() -> None:
    """Guard the guard: an empty/mislocated walk must not vacuously pass."""
    blackboard = list((PACKAGE_ROOT / "blackboard").rglob("*.py"))
    names = {p.name for p in blackboard}
    assert {"events.py", "leases.py", "db.py"} <= names, names
    # And the parser really extracts imports (leases.py imports blackboard.events).
    imports = {mod for _, mod in _imported_modules(PACKAGE_ROOT / "blackboard" / "leases.py")}
    assert "swarmsync.blackboard.events" in imports, imports


def test_blackboard_imports_no_other_swarmsync_layer() -> None:
    """Rule 1: blackboard is the bottom layer -- it imports no sibling package."""
    forbidden = (
        "swarmsync.server",
        "swarmsync.coordinator",
        "swarmsync.agent",
        "swarmsync.hooks",
        "swarmsync.classifier",
    )
    violations = _violations("blackboard", forbidden)
    assert not violations, "blackboard must not import upper layers:\n" + "\n".join(violations)


def test_module_symbol_is_assigned_exactly_once() -> None:
    """WP4.4 (A5): the parcel-id scheme has ONE home.

    The literal `"<module>"` may be ASSIGNED to a name in exactly one place --
    `swarmsync/blackboard/parcel_id.py` (the `MODULE_SYMBOL` definition). Every
    other module must import the constant, so an id-scheme change stays a
    one-file edit. Only assignment statements are checked (via AST), so
    docstring/comment mentions and uses of the imported constant are free.
    """
    assignments: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if isinstance(value, ast.Constant) and value.value == "<module>":
                    rel = path.relative_to(PACKAGE_ROOT.parent).as_posix()
                    assignments.append(f"{rel}:{node.lineno}")
    assert len(assignments) == 1 and assignments[0].startswith(
        "swarmsync/blackboard/parcel_id.py:"
    ), (
        "the literal '<module>' must be assigned exactly once, in "
        "swarmsync/blackboard/parcel_id.py (import MODULE_SYMBOL from there); "
        f"found: {assignments}"
    )


def test_coordinator_does_not_import_server() -> None:
    """Rule 2: no coordinator->server imports (verified zero; no whitelist needed)."""
    violations = _violations("coordinator", ("swarmsync.server",))
    assert not violations, "coordinator must not import server:\n" + "\n".join(violations)


def test_legacy_server_import_paths_still_work() -> None:
    """The deprecated shims at the old paths re-export the same objects."""
    from swarmsync.blackboard import events, leases
    from swarmsync.server import events as legacy_events
    from swarmsync.server import leases as legacy_leases

    assert legacy_events.emit is events.emit
    assert legacy_events.tail is events.tail
    assert legacy_events.compact_events is events.compact_events
    assert legacy_events.drop_pheromone is events.drop_pheromone
    assert legacy_events.decay_pheromone is events.decay_pheromone
    assert legacy_events.EVENTS_COMPACTED is events.EVENTS_COMPACTED

    assert legacy_leases.acquire is leases.acquire
    assert legacy_leases.heartbeat is leases.heartbeat
    assert legacy_leases.release is leases.release
    assert legacy_leases.DEFAULT_TTL_SECONDS == leases.DEFAULT_TTL_SECONDS


# --- WP4.2 (A4/U9): config.py is the ONE module that reads the environment ---------


def _environ_reads(path: Path) -> list[str]:
    """Every `os.environ` attribute/subscript access and `os.getenv`-style read
    in `path`, AST-parsed (never imported/executed).

    Flags, with line numbers:
      * any `os.environ` / `os.getenv` / `os.putenv` / `os.unsetenv` attribute
        access -- this covers `.get(...)`, subscripting, `{**os.environ}`, and
        bare writes alike, since the subscript/call node always contains the
        Attribute node;
      * `from os import environ` / `getenv` / `putenv` / `unsetenv`, which
        would otherwise smuggle the same reads in under a local name.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(PACKAGE_ROOT.parent).as_posix()
    banned = {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in banned
        ):
            found.append(f"{rel}:{node.lineno} accesses os.{node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in banned:
                    found.append(f"{rel}:{node.lineno} imports os.{alias.name}")
    return found


def test_only_config_reads_the_environment() -> None:
    """WP4.2: every env knob is read through `swarmsync.config`'s typed accessors.

    Before this WP, ~12 `SWARMSYNC_*` variables were each parsed at point of use
    with local fallback copies that drifted (the gate-timeout pair) and one
    misnamed outlier (`SWARM_SYNC_DB`). The accessors (and the one sanctioned
    env WRITE, `config.set_roots`, plus the one sanctioned passthrough,
    `config.subprocess_env`) now live in `swarmsync/config.py` -- and NOTHING
    else under `swarmsync/` may touch the environment. No other whitelist: a
    site that can't go through config is a design finding, not an exemption.
    """
    config_py = PACKAGE_ROOT / "config.py"
    assert config_py.exists(), "swarmsync/config.py has moved -- update this guard"
    # Guard the guard: the detector must actually see config.py's own reads.
    assert _environ_reads(config_py), "the env-read detector detects nothing"

    violations: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if path == config_py:
            continue
        violations.append("\n".join(_environ_reads(path)))
    violations = [v for v in violations if v]
    assert not violations, (
        "only swarmsync/config.py may read (or write) the process environment; "
        "route these through a config accessor:\n" + "\n".join(violations)
    )
