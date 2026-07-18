"""The ONE home for swarm-sync's environment-variable surface (WP4.2, A4/U9).

Before this module, ~12 `SWARMSYNC_*` knobs were each read at point-of-use with
local parse-and-fallback copies (two of them -- the gate-timeout pair in
`coordinator/gate.py` and `agent/client.py` -- had drifted into separate
implementations), plus one misnamed outlier (`SWARM_SYNC_DB`). Now:

  * Every env NAME is a module constant here, and every read goes through a
    typed accessor below. `tests/test_architecture.py` enforces that no other
    module under `swarmsync/` touches `os.environ`/`os.getenv` at all.
  * Every accessor reads the environment AT CALL TIME -- never cached at import
    -- so tests that monkeypatch the environment keep working, and a knob can be
    changed between requests without a restart (several servers-side accessors
    are deliberately read per-request).
  * Parse/fallback semantics are byte-for-byte the ones the local copies had:
    unset/garbage values fall back to the documented default rather than raise
    (an operator typo must never crash the server or silently disable a lease).

LAYERING: this module is imported by `blackboard/` (the base layer), so it must
import NOTHING from `swarmsync` -- stdlib only.

The one deliberately-raw accessor is `lease_ttl()`: the hook adapter's
`_hook_lease_ttl` owns the parse/clamp/warn for that knob (its warnings need an
error stream and the lease-TTL floor/ceiling constants from
`blackboard.models`, which this module cannot import), so config hands it the
raw string and stays out of the way.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

# --- env-var names: the single source of truth ------------------------------------

TOKEN_ENV = "SWARMSYNC_TOKEN"
ROOTS_ENV = "SWARMSYNC_ROOTS"
GATE_TIMEOUT_ENV = "SWARMSYNC_GATE_TIMEOUT"
LEASE_TTL_ENV = "SWARMSYNC_LEASE_TTL"
ACTIVE_ENV = "SWARMSYNC_ACTIVE"
URL_ENV = "SWARMSYNC_URL"
DB_ENV = "SWARMSYNC_DB"
# The pre-WP4.2 misnamed outlier (every other var is SWARMSYNC_*). Still honored
# by `db_path()` as a deprecated alias, with a one-line stderr warning.
DB_ENV_DEPRECATED = "SWARM_SYNC_DB"
MAX_LEASES_PER_AGENT_ENV = "SWARMSYNC_MAX_LEASES_PER_AGENT"
MAX_BODY_BYTES_ENV = "SWARMSYNC_MAX_BODY_BYTES"
EVENTS_COMPACT_INTERVAL_ENV = "SWARMSYNC_EVENTS_COMPACT_INTERVAL"
EVENTS_HEARTBEAT_MAX_AGE_ENV = "SWARMSYNC_EVENTS_HEARTBEAT_MAX_AGE"
EVENTS_MAX_AGE_ENV = "SWARMSYNC_EVENTS_MAX_AGE"

# --- defaults ---------------------------------------------------------------------

# Blackboard base URL the hook adapter talks to. 8787 is `swarmsync-serve`'s (now
# the ONLY launcher's) default port, so out of the box the two agree.
DEFAULT_URL = "http://127.0.0.1:8787"

# The unified launcher's default SQLite path (was `swarmsync-serve`'s default;
# the retired second launcher's `blackboard.db` default is gone with it -- WP4.2's
# one deliberate behavior change).
DEFAULT_DB_PATH = "swarmsync.db"

# Wall-clock ceiling for the integrator's pytest gate, seconds (`coordinator/gate.py`).
DEFAULT_GATE_TIMEOUT_SECONDS = 600.0

# Per-agent active-lease cap (`blackboard/leases.py`, WP3.3 S1/P2).
DEFAULT_MAX_LEASES_PER_AGENT = 256

# Request-body cap, bytes (`server/app.py`, WP3.3 S5).
DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024

# Events-compaction throttle, seconds (`coordinator/reaper.py`, WP3.1 S2).
DEFAULT_EVENTS_COMPACT_INTERVAL = 60.0

# Retention window for heartbeat-class events, seconds (`blackboard/events.py`).
DEFAULT_EVENTS_HEARTBEAT_MAX_AGE = 3600.0

# Retention horizon for ANY event, seconds (`blackboard/events.py`).
DEFAULT_EVENTS_MAX_AGE = 7 * 86400.0


# --- typed accessors (each reads the environment at call time) --------------------


def token() -> Optional[str]:
    """`SWARMSYNC_TOKEN`: bearer token gating the blackboard's mutating routes.

    Default: unset (None) -- NO auth; dev/test/demo run open. When set, the
    server (`app.require_token`) 401s any mutating request without a matching
    `Authorization: Bearer` header, and the hook adapter sends it on every
    request. Returned raw: consumers treat empty-as-unset via truthiness,
    exactly as the point-of-use reads always did.
    """
    return os.environ.get(TOKEN_ENV)


def roots() -> list[str]:
    """`SWARMSYNC_ROOTS`: the managed-root allow-list for `/index` + `/integrate`.

    Default: the process's current working directory. Parsed exactly as
    `app._managed_roots` always did: split on `os.pathsep`, drop empty entries,
    realpath each (so symlink escapes are compared against the canonical root).
    More than one root is a config error the server refuses at startup
    (`app.check_single_root`) -- this accessor just reports what is set.
    """
    raw = os.environ.get(ROOTS_ENV)
    if raw:
        candidates = [r for r in raw.split(os.pathsep) if r]
    else:
        candidates = [os.getcwd()]
    return [os.path.realpath(r) for r in candidates]


def set_roots(root: str) -> None:
    """Set `SWARMSYNC_ROOTS` for this process (the launcher's `--root` flag).

    The ONE sanctioned write to the environment in the codebase (WP4.2):
    `serve.main` used to poke `os.environ` directly to hand `--root` to
    `app.py`'s per-request readers. The env var remains the transport -- request
    handlers re-read `roots()` per call, deliberately, so tests can repoint it
    -- but the write now has exactly one, documented home.
    """
    os.environ[ROOTS_ENV] = root


def gate_timeout() -> float:
    """`SWARMSYNC_GATE_TIMEOUT`: pytest-gate ceiling, in seconds. Default 600.

    The gate runs just-merged, agent-authored code while `POST /integrate`
    holds the ONE global integrate lock, so this ceiling is what stops a
    non-terminating test wedging integration permanently. The agent client
    derives its own `/integrate` HTTP timeout from this same value (+margin).

    Unset, empty, unparseable, or non-positive (incl. NaN) values fall back to
    the default. WP4.2 unification note: the two pre-existing copies
    (`gate._gate_timeout`, `client._integrate_timeout`) were verified
    behaviorally IDENTICAL on every input class, so this accessor preserves
    both; `gate.py`'s explicit early-return structure was kept as the safer,
    more legible spelling.
    """
    raw = os.environ.get(GATE_TIMEOUT_ENV)
    if not raw:
        return DEFAULT_GATE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_GATE_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_GATE_TIMEOUT_SECONDS


def lease_ttl() -> Optional[str]:
    """`SWARMSYNC_LEASE_TTL`: the RAW lease-TTL string the hook acquires/renews with.

    Default: unset (None) -- the adapter falls back to its 300s
    `DEFAULT_HOOK_LEASE_TTL_SECONDS`. Deliberately returned unparsed: the
    parse/clamp/warn semantics (C9: refuse zero/negative/over-ceiling values
    loudly rather than silently disabling lease protection; C13: warn on
    below-floor values) live in `hooks.adapter._hook_lease_ttl`, which needs an
    error stream and the TTL floor/ceiling constants from `blackboard.models`
    that this bottom-layer module cannot import.
    """
    return os.environ.get(LEASE_TTL_ENV)


def active() -> bool:
    """`SWARMSYNC_ACTIVE`: the hook adapter's env half of the opt-in gate.

    Default: off. Exactly `"1"` activates coordination regardless of marker
    files (e.g. CI/demo runs); any other value counts as unset -- the
    `.swarmsync-active` marker file is the other, filesystem half
    (`adapter._is_active`).
    """
    return os.environ.get(ACTIVE_ENV) == "1"


def url() -> str:
    """`SWARMSYNC_URL`: blackboard base URL the hook adapter talks to.

    Default `http://127.0.0.1:8787` -- matching the (now single) launcher's
    default port, so a stock `swarmsync-serve` and a stock hook find each other
    with no configuration.
    """
    return os.environ.get(URL_ENV, DEFAULT_URL)


def db_path() -> str:
    """`SWARMSYNC_DB`: default blackboard SQLite path for the launcher's `--db`.

    Default `swarmsync.db` (in the launch cwd). Precedence: an explicit `--db`
    flag beats `SWARMSYNC_DB`, which beats the deprecated `SWARM_SYNC_DB` alias
    (honored with a one-line stderr warning -- it was the one knob that broke
    the `SWARMSYNC_*` naming convention), which beats the default.
    """
    raw = os.environ.get(DB_ENV)
    if raw is not None:
        return raw
    legacy = os.environ.get(DB_ENV_DEPRECATED)
    if legacy is not None:
        sys.stderr.write(
            f"swarm-sync: {DB_ENV_DEPRECATED} is deprecated; use {DB_ENV} instead\n"
        )
        return legacy
    return DEFAULT_DB_PATH


def max_leases_per_agent() -> int:
    """`SWARMSYNC_MAX_LEASES_PER_AGENT`: per-agent active-lease cap. Default 256.

    Bounds how many parcels one agent id can hold at once, so a client that
    clears the token gate cannot mint unlimited `ensure_parcel` rows/leases
    (WP3.3 S1/P2). Generous by default -- a legitimate broker wave leases tens
    of parcels. Unset/empty/garbage values fall back to the default (any
    parseable int is accepted as-is, matching the original read).
    """
    raw = os.environ.get(MAX_LEASES_PER_AGENT_ENV)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_LEASES_PER_AGENT


def max_body_bytes() -> int:
    """`SWARMSYNC_MAX_BODY_BYTES`: request-body size cap. Default 10 MiB.

    Requests declaring a larger Content-Length are 413ed before the framework
    buffers a byte (WP3.3 S5); every legitimate body here is a small JSON
    document. Read per request, so it is tunable without a restart.
    Unset/empty/garbage values fall back to the default (any parseable int is
    accepted as-is, matching the original read).
    """
    raw = os.environ.get(MAX_BODY_BYTES_ENV)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_BODY_BYTES


def events_compact_interval() -> float:
    """`SWARMSYNC_EVENTS_COMPACT_INTERVAL`: events-compaction throttle, seconds.

    Default 60. The reaper loop may tick every second, but a full-table DELETE
    scan every tick would be waste -- one compaction pass per interval keeps the
    heartbeat backlog bounded (WP3.1 S2). Unset/garbage/non-positive values
    fall back to the default.
    """
    raw = os.environ.get(EVENTS_COMPACT_INTERVAL_ENV)
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_EVENTS_COMPACT_INTERVAL
        if value > 0:
            return value
    return DEFAULT_EVENTS_COMPACT_INTERVAL


def events_heartbeat_max_age() -> float:
    """`SWARMSYNC_EVENTS_HEARTBEAT_MAX_AGE`: heartbeat-event retention, seconds.

    Default 3600 (1 hour). Heartbeat rows are the per-renewal keepalive traffic
    that dominates the event log's growth, so they get a short window; every
    other event type keeps the long `events_max_age()` horizon (WP3.1 S2).
    Unset/garbage/non-positive values fall back to the default.
    """
    raw = os.environ.get(EVENTS_HEARTBEAT_MAX_AGE_ENV)
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_EVENTS_HEARTBEAT_MAX_AGE
        if value > 0:
            return value
    return DEFAULT_EVENTS_HEARTBEAT_MAX_AGE


def events_max_age() -> float:
    """`SWARMSYNC_EVENTS_MAX_AGE`: retention horizon for ANY event, seconds.

    Default 604800 (7 days). Events older than this are pruned by compaction --
    except any `integrate_started` row still referenced by `open_integrations`,
    which survives unconditionally (crash recovery reads the projection, but
    the audit row behind a still-open integrate is kept intact).
    Unset/garbage/non-positive values fall back to the default.
    """
    raw = os.environ.get(EVENTS_MAX_AGE_ENV)
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_EVENTS_MAX_AGE
        if value > 0:
            return value
    return DEFAULT_EVENTS_MAX_AGE


def subprocess_env(**overrides: str) -> dict[str, str]:
    """A full copy of this process's environment, with `overrides` applied.

    NOT a knob: this exists for spawning subprocesses that must inherit the
    whole environment (the pytest gate, `coordinator/gate.py`). Centralized
    here so the architecture guard ("only config.py touches os.environ") stays
    meaningful without a whitelist -- an environment PASSTHROUGH is the one
    read that genuinely cannot be expressed as a named accessor.
    """
    return {**os.environ, **overrides}
