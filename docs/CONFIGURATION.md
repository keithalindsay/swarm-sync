# Configuration reference

Everything swarm-sync reads from its environment, plus the raw Claude Code hook block the
[README's setup section](../README.md#use-with-claude-code) installs for you. The README carries the
narrative; this file is the lookup table you come back to.

---

## Environment variables

Everything is optional — unset knobs use the defaults below, and a garbage value falls back to the
default rather than crashing (a typo must never take the blackboard down or silently disable a
lease). All reads go through [`swarmsync/config.py`](../swarmsync/config.py), the one module allowed
to touch the environment.

| Variable | Default | Meaning |
|---|---|---|
| `SWARMSYNC_ACTIVE` | unset (off) | Hook opt-in: exactly `1` activates coordination for a session regardless of marker files (the `.swarmsync-active` marker is the per-repo alternative). |
| `SWARMSYNC_URL` | `http://127.0.0.1:8787` | Blackboard base URL the hook adapter talks to. Must match where `swarmsync-serve` is listening. |
| `SWARMSYNC_TOKEN` | unset (no auth) | Bearer token required on every mutating route when set; the hook sends it automatically. |
| `SWARMSYNC_ROOTS` | launch cwd | Managed-root allow-list for `/index`/`/integrate` (403 outside it). Exactly **one** root; `--root` sets it for you. |
| `SWARMSYNC_DB` | `swarmsync.db` | Default SQLite path for the launcher's `--db` (the flag wins). `SWARM_SYNC_DB` is honored as a deprecated alias, with a stderr warning. |
| `SWARMSYNC_LEASE_TTL` | `300` (seconds) | Lease TTL the hook acquires/renews with. Zero/negative/over-ceiling values are refused loudly and the default used — a typo must not disable lease protection. |
| `SWARMSYNC_GATE_TIMEOUT` | `600` (seconds) | Wall-clock ceiling on the integrator's pytest gate; also widens the agent client's `/integrate` HTTP timeout to match. |
| `SWARMSYNC_MAX_LEASES_PER_AGENT` | `256` | Cap on active leases one agent id may hold (bounds `ensure_parcel` abuse). |
| `SWARMSYNC_MAX_BODY_BYTES` | `10485760` (10 MiB) | Request bodies declaring more than this are rejected 413 before buffering. |
| `SWARMSYNC_EVENTS_COMPACT_INTERVAL` | `60` (seconds) | How often the background reaper runs an events-compaction pass. |
| `SWARMSYNC_EVENTS_HEARTBEAT_MAX_AGE` | `3600` (1 hour) | Retention window for heartbeat events — the keepalive traffic that dominates log growth. |
| `SWARMSYNC_EVENTS_MAX_AGE` | `604800` (7 days) | Retention horizon for any event (still-open integrate audit rows survive regardless). |

[`DESIGN.md` §7a](DESIGN.md#7a-operational-surface-env--launchers) is the contract-level version of
the same surface, including *why* each bound exists.

---

## Launcher flags

There is exactly **one** launcher: `swarmsync-serve` (the `swarm-sync` console script is an alias of
the same `main`).

```bash
swarmsync-serve --root /path/to/your/repo --db /tmp/swarmsync.db --port 8787
```

| Flag | Default | Meaning |
|---|---|---|
| `--root` | launch cwd | The single managed root. `/index` and `/integrate` refuse paths outside it (403). |
| `--db` | `$SWARMSYNC_DB`, else `swarmsync.db` | SQLite blackboard path. |
| `--host` | `127.0.0.1` | Bind address. Keep it on localhost — see the README's security section. |
| `--port` | `8787` | Matches the hook adapter's default `SWARMSYNC_URL`. |
| `--fresh` | off | Rotate the existing DB aside and start on an empty schema. |

## `swarmsync` CLI flags

The operator/agent CLI takes two global flags before the subcommand:

| Flag | Default | Meaning |
|---|---|---|
| `--url` | `$SWARMSYNC_URL`, else `http://127.0.0.1:8787` | Blackboard base URL to talk to. |
| `--timeout` | `8.0` | Per-request timeout, in seconds. |

Subcommand flags:

| Command | Flag | Meaning |
|---|---|---|
| `events` | `-n N` | How many recent events to show (default 20). |
| `events` | `--follow` | Keep polling for new events until interrupted. |
| `init-hooks` | `--global` | Write `~/.claude/settings.json` instead of `<cwd>/.claude/settings.json`. |
| `init-hooks` | `--dry-run` | Print what would be written; touch nothing. |
| `init-hooks` | `--command BASE` | Override the hook command base (default: the guard shim, or `swarmsync-hook` when `scripts/` isn't shipped). |

---

## The Claude Code hook block

`swarmsync init-hooks` writes this for you (idempotently) into
`<cwd>/.claude/settings.json`, or `~/.claude/settings.json` with `--global`. This is what it writes,
for anyone who prefers to paste it by hand — point the `command` paths at your actual swarm-sync
checkout:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [ { "type": "command", "command": "/path/to/swarm-sync/scripts/swarmsync-hook-guard precheck", "timeout": 10 } ] }
    ],
    "PostToolUse": [
      { "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [ { "type": "command", "command": "/path/to/swarm-sync/scripts/swarmsync-hook-guard postupdate", "timeout": 10 } ] }
    ],
    "SubagentStop": [
      { "hooks": [ { "type": "command", "command": "/path/to/swarm-sync/scripts/swarmsync-hook-guard release", "timeout": 10 } ] }
    ],
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "/path/to/swarm-sync/scripts/swarmsync-hook-guard session-start", "timeout": 15 } ] }
    ]
  }
}
```

Wire `scripts/swarmsync-hook-guard`, **not** `swarmsync-hook` directly. The guard is a zero-overhead
shim: when swarm-sync isn't active for a repo it exits `0` immediately without even starting Python,
so normal, non-coordinated editing pays nothing. It only launches the real adapter when a session is
active.

## Turning coordination on

Enforcement is opt-in per repo. `init-hooks` drops the marker at the repo's git toplevel; by hand:

```bash
touch /path/to/your/repo/.swarmsync-active     # on
rm    /path/to/your/repo/.swarmsync-active     # off
```

Or set `SWARMSYNC_ACTIVE=1` in the environment — handy for CI. When neither is set, the hooks are a
no-op and every edit is allowed, so installing the hooks never interferes with ordinary work.
