# BUILD STATE — swarm-sync

The autonomous build loop updates this file after every unit. One line per unit; move it
between sections and append notes/blockers. Do NOT edit DESIGN.md or BUILD_PLAN.md as part of
normal builds — they are the spec. If the spec is wrong, note it under BLOCKERS and stop.

Protocol per iteration:
1. Pick the top TODO unit (they are strictly ordered — do not skip).
2. Implement it per BUILD_PLAN.md + the referenced DESIGN.md section.
3. Run its done-when test (`pytest tests/<file>`), plus the full suite to catch regressions.
4. On green: move the unit to DONE with a one-line note. On red after a real attempt: leave in
   DOING and record the failure under BLOCKERS.

## DONE
- U1  — Blackboard DB + schema — `blackboard/db.py` (connect/init_db/reset, WAL +
  foreign_keys pragmas, sqlite3.Row factory) + `blackboard/models.py` (pydantic v2
  models: Parcel, Lease, Contract, Pheromone, Intent, Event + endpoint request/
  response bodies). `tests/test_blackboard.py` green (10 tests): 6 tables created,
  `PRAGMA journal_mode` = wal, second `init_db` on same file is a no-op (no data
  loss, no dup rows), FK constraint on leases.parcel_id enforced, autoincrement
  seq/id, models validate from `dict(sqlite3.Row)`.

- U2  — Classifier: parcel extraction — `classifier/indexer.py`: `parse_file(path,
  rel_path=None)` and `index_repo(root)`, stdlib `ast` only. Emits one Parcel per
  top-level `def`/`async def` (kind="function") and per method inside a class
  (kind="method") with a **concrete, decorator-inclusive, byte-exact
  `(byte_start, byte_end)` span** and `content_hash = sha256(source[start:end])`.
  A `class` parcel (kind="class") and the per-file `<path>::<module>`
  interstitial (kind="module") are both "glue" buckets for code not inside a
  concrete symbol (decorators/header/docstring/class-level statements for a
  class; imports/module docstring/top-level statements for a module) — their
  glue is not generally contiguous, so **`byte_start`/`byte_end` are left `None`**
  for these two kinds and their `content_hash` covers the concatenation of the
  leftover byte ranges in file order (still deterministic/stable). This makes
  "non-overlapping byte spans" an unambiguous property of only the concrete
  function/method parcels (verified `ast.col_offset` is a UTF-8 *byte* offset,
  not a char offset, empirically — see indexer.py module docstring — so raw
  byte slicing against line-start byte offsets is correct even with multi-byte
  unicode source). `index_repo` walks `*.py` under root, skipping
  `__pycache__/.git/.venv/venv/.pytest_cache/node_modules` and any dot-dir.
  `tests/test_indexer.py` green (12 tests): exact parcel set for the
  2-functions+1-class-with-method fixture, correct kinds, non-overlapping
  concrete spans, hash-matches-slice, hash stability across reparse, hash
  changes only for the mutated symbol, decorator-inclusive span, unicode byte
  offsets, `index_repo` walk + junk-dir skip + determinism. Full suite green
  (22 tests: 10 U1 + 12 U2). Dogfooded `index_repo('.')` over the real project
  tree with no crashes (75 parcels).
- U2 handoff for U3 (`classifier/graph.py`): only function/method parcels have
  real `(byte_start, byte_end)` — `co_schedulable`'s symbol-mode disjointness
  check should only ever compare those (class/module glue parcels have `None`
  spans by design, so file mode is the only sane mode for them). Parcel `id`
  format is `"{rel_path}::{qualified_symbol}"` (dot-joined for `Class.method`)
  or `"{rel_path}::<module>"`; `path` on every parcel is the POSIX-relative
  path from repo root. `blast_radius` is seeded to `0` on every parcel from
  U2 — U3 owns computing/overwriting it via the reverse-dep BFS.

- U3  — Dependency graph, blast radius, frozen contracts — `classifier/graph.py`:
  `DepGraph` dataclass (`parcels_by_id`, `edges` dependent->dependency,
  `reverse_edges` dependency->dependents, `cross_module_files`, `signatures`).
  `build_graph(parcels, root)` re-reads each file's source (via `root` +
  each parcel's `path`) and does one more `ast` pass per file: resolves
  `import X` / `from X import name` against the repo's own dotted-module
  namespace (derived from parcel paths — anything not in the repo, e.g.
  stdlib/third-party, is silently skipped, conservative per DESIGN §6),
  preferring a *symbol-precise* edge (`from X import name` where `name`
  matches a top-level function/class in `X`) over a module-granularity
  fallback edge to `X`'s `<module>` parcel. Call edges are attributed to
  the enclosing top-level `def`/method body via `ast.walk` + a per-file
  import-alias table (handles `helper()`, `h()` after `as h`, and
  `mod_a.helper()` attribute access — all 3 exercised in
  `tests/test_graph.py`'s fixture). `blast_radius(graph)` = BFS over
  `reverse_edges` per node (transitive dependents, self excluded even
  under a cycle). `extract_contracts(parcels, graph, blast, threshold=3)`
  — **note the signature takes `graph` as a required second positional
  arg**, a deliberate deviation from the one-line sketch left in the U3
  stub, because "imported across module boundaries" needs graph info, not
  just the blast dict. Only top-level `function`/`class` parcels are
  contract-eligible; `Contract.symbol` is stored as the full **parcel id**
  (e.g. `"mod_a.py::helper"`), not the bare name, since `symbol` is a
  schema PRIMARY KEY and bare names collide across files. Signatures are
  computed once per top-level def/class during the same `build_graph`
  pass (function: `name(params...)` with defaults via `ast.unparse`;
  class: `class Name(public_method_sig, ...)`, non-underscore methods
  only) and cached on `graph.signatures[parcel_id] = (signature,
  type_hash)`; `type_hash = sha256(signature)`. `co_schedulable(a, b,
  mode="file"|"symbol", graph=None, frozen_ids=None)` — structural
  disjointness first (file: path differs; symbol: byte spans disjoint,
  conservatively `False` if either parcel has no concrete span); the
  frozen-contract clause only engages if the caller passes both `graph`
  and `frozen_ids` (so the plain 3-arg call the BUILD_PLAN done-when
  describes still works with no graph at all). Raises `ValueError` on an
  unrecognized `mode`. `tests/test_graph.py` green (15 tests) on a
  5-module fixture (`mod_a.helper` imported 3 ways by `mod_b`/`mod_c`/
  `mod_d`, `mod_e` fully unrelated): blast_radius(helper) >= 3, helper
  returned as a frozen contract with `signature="helper(x, y=1)"` +
  64-char hex `type_hash`, low-blast/non-cross-module symbols (incl. a
  same-file-only helper and an underscore-private one) excluded from
  contracts, `co_schedulable` file/symbol-mode + frozen-contract-clause
  cases, determinism, and an unknown-mode `ValueError`. Full suite green
  (37 tests: 22 prior + 15 U3). Dogfooded `build_graph`/`blast_radius`/
  `extract_contracts` over the real project tree (107 parcels, no
  crashes): correctly surfaced `models.py::Parcel`/`Contract` and
  `graph.py`'s/`indexer.py`'s own public functions as contracts, while
  correctly *excluding* private same-file-only helpers (e.g.
  `indexer.py::_abs_offset`, blast_radius 34 but never imported
  cross-module) — confirms the cross-module filter is doing real work,
  not just threshold-gating.
- U3 handoff for U4 (`classifier/store.py` / index API population): call
  `build_graph(parcels, root)` then `blast_radius(graph)` then
  `extract_contracts(parcels, graph, blast)` in that order; write the
  resulting `blast` values back onto each `Parcel.blast_radius` before
  upserting into `parcels` (U3 does not mutate the `Parcel` objects it's
  given — it returns a separate `dict[parcel_id, int]` — so U4 owns
  assigning `parcel.blast_radius = blast[parcel.id]`, defaulting to `0`
  for any parcel `blast_radius` doesn't mention, e.g. a parcel with no
  reverse-dependents. Also set `Parcel.contract_hash` from
  `graph.signatures[parcel.id][1]` (the `type_hash`) for any parcel whose
  id appears in the `extract_contracts` result, so a leased-file's
  `contract_hash` and the `contracts` table's `type_hash` agree.
  Re-indexing (incremental, per DESIGN §3) just means: re-run
  `build_graph` over the (still small) parcel set every time for the
  prototype — there's no incremental graph-diffing here, only the
  classifier's *file walk* is incremental per DESIGN's intent; U3 doesn't
  implement partial-graph updates, full rebuild each time is what's
  wired.

- U4  — Index API population — `classifier/store.py`: `run_index(conn, root,
  threshold=FREEZE_THRESHOLD) -> IndexResult(parcels, contracts, graph)` runs the
  full U2→U3 pipeline (`index_repo` → `build_graph` → `blast_radius` →
  `extract_contracts`) then writes the result into the blackboard, exactly per
  U3's handoff note: each `Parcel.blast_radius` is assigned from the `blast` dict
  (defaulting 0) since U3 doesn't mutate the parcels it's given, and
  `Parcel.contract_hash` is stamped from `graph.signatures[id][1]` (the
  `type_hash`) for every parcel that made the contract cut, so a parcel row's
  `contract_hash` always agrees with the matching `contracts.type_hash` row.
  `upsert_parcels`/`upsert_contracts` are single-transaction (`BEGIN`/`executemany`/
  `COMMIT`, rollback on any exception) `INSERT ... ON CONFLICT DO UPDATE`s keyed on
  `parcels.id` / `contracts.symbol` — re-running `run_index` over an unchanged repo
  leaves row counts unchanged (verified against both a synthetic 5-module fixture
  and the real project tree: 129 parcels / 12 contracts, stable across two runs).
  Two deliberate design calls, both documented in store.py's module docstring:
  (1) **`state_summary` is never touched by the upsert** — it's omitted from both
  the INSERT column list and the ON CONFLICT SET clause, because DESIGN §5.4 makes
  the *integrator* (U10) the sole authority that regenerates it on merge; a plain
  re-index must not clobber an agent's/integrator's existing note. (2) **contract
  `version` bumps only when `type_hash` changes** on re-run (a plain SQL `CASE` in
  the upsert: `WHEN contracts.type_hash != excluded.type_hash THEN
  contracts.version + 1 ELSE contracts.version`), otherwise preserved — this is
  independent of and simpler than the exclusive-lease-gated `contract_change`
  event flow in DESIGN §5.3 (that's U5/U12's job when an agent deliberately
  changes a frozen signature under a lease); this unit only keeps `version` honest
  under repeated `POST /index` calls. **No stale-row pruning**: a parcel/contract
  whose source symbol disappears between two `run_index` calls is left in the DB
  rather than deleted (deleting is unsafe in general once `leases`/`pheromone`
  reference `parcels.id` under `foreign_keys=ON`) — left as a known gap, natural
  fit for the integrator's "re-index the touched files on land" step (DESIGN
  §5.4) in a later unit, not required by this unit's done-when.
  `tests/test_index_api.py` green (12 tests): one row per parcel + one row per
  frozen contract on a fixture matching test_graph.py's shape, parcel row field
  correctness (path/kind/blast_radius/content_hash/byte span, contract_hash ==
  contracts.type_hash for `mod_a.py::helper`, contract_hash is None for a
  non-contract parcel), re-run produces zero duplicate rows, re-run updates
  content_hash/updated_at in place after a source edit while preserving a
  manually-set state_summary, contract version bumps to 2 when helper's
  signature changes and stays at 1 when it doesn't, upsert no-ops on an empty
  list, and `run_index` tolerates the still-empty `sample_repo/` (U13 hasn't
  built it out yet) without crashing. Full suite green (49 tests: 37 prior + 12
  U4). Dogfooded `run_index('.')` over the real project tree twice in a row:
  129 parcels / 12 contracts both times, no duplication.
- U4 handoff for U5 (`server/leases.py`): `run_index` is the function
  `POST /index` (U7) should call — it takes a live `conn` (from
  `blackboard.db.connect`/`init_db`) and a repo root, and does the writes itself
  (no separate "then call upsert" step needed from the endpoint handler). The
  lease manager doesn't need anything new from `parcels` beyond what U1's schema
  already has (`id` as the FK target), but note `parcels.contract_hash` is now
  reliably populated for every frozen-contract symbol post-`run_index`, which
  U5/U12 can use to detect "this write target is a frozen contract" without a
  separate `contracts` table join if convenient.

- U5  — Lease manager (atomic CAS) — `server/leases.py`: `acquire(conn, parcel_id,
  agent_id, mode="read"|"write"|"exclusive", ttl=DEFAULT_TTL_SECONDS=30.0,
  intent=None) -> LeaseResult` is a single parameterized `INSERT ... SELECT ...
  WHERE NOT EXISTS (...)` statement, exactly per DESIGN §5.2's SQL — one statement
  is its own implicit SQLite transaction even though `db.connect` uses
  `isolation_level=None` (autocommit), so the CAS is atomic without needing an
  explicit `BEGIN`/`COMMIT` wrapper. Conflict rule: existing active,
  un-expired (`ttl_expires_at > now`) lease blocks the new acquire iff either side
  is `write`/`exclusive` (read+read is the only mutually-compatible pair).
  `cur.rowcount == 1` → granted (returns `LeaseResult(granted=True,
  lease_id=cur.lastrowid)`); `== 0` → denied (`LeaseResult(granted=False,
  reason=...)`, no row inserted — verified explicitly in tests, not just implied
  by the result). **Lazy expiry**: an expired holder (`ttl_expires_at <= now`) is
  already excluded by the WHERE clause, so acquiring against an only-expired lease
  succeeds immediately — correctness never depends on the reaper (U11) having run
  first; the reaper's job is purely to *mark* `reaped` + emit the event for
  observability/reassignment. `heartbeat(conn, lease_id, agent_id, ttl)` and
  `release(conn, lease_id, agent_id)` are both ownership-scoped
  (`id=? AND agent_id=? AND status='active'` in the UPDATE's WHERE) and return
  `bool` — a stale/foreign/nonexistent/already-released lease_id is a **silent
  no-op** (`False`), never an exception, so a late heartbeat/release race from an
  agent that already lost/finished its lease can't corrupt another agent's state.
  `acquire` raises `ValueError` on an unrecognized mode string and lets SQLite's
  own `sqlite3.IntegrityError` propagate for a `parcel_id` that doesn't exist in
  `parcels` (FK enforcement from U1, deliberately not swallowed — a lease on a
  nonexistent parcel is a caller bug, not a race, and callers already need init_db
  order right).
  **Event emission without U6**: `acquire`/`heartbeat`/`release` all write
  `lease_granted`/`lease_denied`/`heartbeat`/`released` rows straight into the
  `events` table via a tiny private `_emit()` (agent_id, type, JSON payload, ts) —
  this does NOT import `server/events.py` (still U6's unbuilt stub), keeping U5
  independent of U6 per the BUILD_PLAN independence note. When U6 lands its
  richer `emit()`/`tail()`, swapping `_emit` to call it is a same-shape no-op
  (identical columns) — flagged here so U6 doesn't have to rediscover this.
  `tests/test_leases.py` green (17 tests): the 3 literal done-when assertions
  (write/write → exactly one granted + one denied, with the losing side
  confirmed to have inserted zero rows; read/read → both granted, two distinct
  lease ids; release → parcel immediately re-acquirable) plus broader coverage —
  every mode-pair conflict matrix (read/write, write/read, exclusive vs all
  three), disjoint parcels never conflict, lazy-expiry acquire succeeds past a
  negative-ttl lease, heartbeat bumps both `heartbeat_at`/`ttl_expires_at` for the
  true owner and is a no-op for a non-owner/unknown/already-released lease,
  release is idempotent (second call on the same lease returns `False`) and
  rejects a non-owner, `ValueError` on a bogus mode string, and
  `sqlite3.IntegrityError` on a nonexistent `parcel_id`. Full suite green (66
  tests: 49 prior + 17 U5).
- U5 handoff for U6 (`server/events.py`): once `emit`/`tail` exist, refactor
  `leases.py`'s private `_emit` to call the real `emit()` instead (same table,
  same columns — `agent_id, type, payload(JSON), ts` — so this is purely a
  dedup/cleanup, not a behavior change; keep `leases.py`'s tests green after the
  swap as the acceptance check). Also worth reusing for U6: `leases.py` already
  demonstrates the "ownership-scoped UPDATE returns bool, never raises on a
  stale/foreign id" pattern that the reaper (U11) will want when it flips
  `status='reaped'` on expired leases it doesn't own either.
- U5 handoff for U7 (`server/app.py`): `POST /lease` should call
  `leases.acquire(conn, body.parcel_id, body.agent_id, mode=body.mode,
  ttl=body.ttl or DEFAULT_TTL_SECONDS, intent=body.intent)` and return the
  `LeaseResult` directly (the `LeaseRequest`/`LeaseResult` models already exist in
  `blackboard/models.py` from U1 and match this module's signature 1:1 — no
  translation layer needed). `POST /heartbeat` and `POST /release` map straight
  onto `heartbeat()`/`release()` too; note both return plain `bool`, so the
  endpoint handler owns turning `False` into whatever HTTP-shape (e.g. a 200 with
  `{"ok": false}` vs a 404/409) DESIGN doesn't prescribe — U7's call to make.

- U6  — Event log + pheromone — `server/events.py`: `emit(conn, type_, agent_id=None,
  payload=None, ts=None) -> seq` is now the single write path into the append-only
  `events` table (INSERT, JSON-serialize payload, `cur.lastrowid`); rejects a
  `type_` outside `blackboard.models.EventType`'s Literal with `ValueError` (same
  fail-fast pattern as `leases.acquire`'s bad-mode check) so a typo can't silently
  poison the replay log. `tail(conn, since_seq=0, limit=1000) -> list[Event]` is a
  plain `WHERE seq > ? ORDER BY seq ASC LIMIT ?`, returned as `Event` pydantic
  models built via `Event.model_validate(dict(row))`; `payload` comes back as the
  raw JSON **string** (matching the `Event` model / schema column exactly) —
  callers `json.loads()` it themselves if they want the dict. `drop_pheromone(conn,
  parcel_id, agent_id, kind, strength, ts=None) -> Pheromone` is an
  `INSERT ... ON CONFLICT (parcel_id, agent_id, kind) DO UPDATE` upsert — the
  schema's PRIMARY KEY is exactly that triple, so re-dropping the same
  (parcel, agent, kind) replaces strength/updated_at in place, never duplicates a
  row. `decay_pheromone(conn, half_life, ts=None) -> int` does **multiplicative
  exponential decay**: for every pheromone row, `strength *= 0.5 **
  (elapsed_since_updated_at / half_life)`, then bumps that row's `updated_at` to
  `now` — so a periodic decay loop (U11) compounds correctly off real elapsed
  wall-clock time between calls instead of double-decaying against a stale
  timestamp (verified: two successive half-life-spaced calls take strength
  1.0 → 0.5 → 0.25, not 1.0 → 0.5 → 0.5). Clamped to a floor of `max(0.0, ...)`
  per the BUILD_PLAN done-when wording ("never below 0") even though exponential
  decay of a non-negative strength can't mathematically go negative; also guards
  against negative `elapsed` (a `ts` earlier than a row's `updated_at`, i.e. clock
  skew or an out-of-order call) by clamping `elapsed` itself to `0.0` first, so
  strength can never *grow* from calling decay "in the past." Raises `ValueError`
  on `half_life <= 0` (would divide-by-zero or invert decay into growth).
  Returns `0` and never raises on an empty pheromone table.
  **Refactor per U5's own handoff note**: `server/leases.py`'s inline private
  `_emit` helper is gone — it now imports `events.emit as _emit` directly. The
  call sites needed zero changes: `_emit(conn, type_, agent_id, payload, ts=now)`'s
  positional shape already matched `emit`'s signature 1:1, so this was a pure
  dedup with no behavior change (confirmed: full `test_leases.py` still green
  after the swap, plus a new regression test in `test_events.py` asserting a
  `lease_granted` from `leases.acquire` is visible via `events.tail`).
  `tests/test_events.py` green (20 tests): the 3 literal done-when assertions
  (monotonically increasing seq incl. exact `(1,2,3)` on a fresh db; `tail(since=k)`
  returns only seq>k in ascending order; decay reduces strength and never goes
  negative, incl. an extreme-elapsed case that saturates to ~0) plus broader
  coverage — emit persists agent_id/type/payload/ts exactly, agent_id/payload are
  optional (system events), unrecognized event type raises `ValueError` and writes
  zero rows, default `ts` is real wall-clock `time.time()`, `tail`'s default
  `since_seq=0` returns everything, `tail`'s `limit` is respected, tailing past the
  high-water-mark returns `[]`, pheromone upsert dedups on the
  (parcel_id, agent_id, kind) key but distinct kind/agent is a separate row,
  decay-by-exactly-one-half-life gives 0.5 (`pytest.approx`), decay compounds
  correctly across two calls, decay on an empty table is a no-op returning `0`,
  decay rejects `half_life<=0`, and the clock-skew guard (`ts` earlier than a
  row's `updated_at`) doesn't grow strength. Full suite green (86 tests: 66 prior
  + 20 U6).
- U6 handoff for U7 (`server/app.py`): `GET /events?since={seq}` should call
  `events.tail(conn, since_seq=seq)` directly and return the list of `Event`
  models (FastAPI/pydantic will serialize them; note `payload` serializes as the
  raw JSON *string*, not a nested object — if the wire contract wants a nested
  object, U7 owns `json.loads`-ing it in the response model, `events.py` itself
  does not parse payload back out). `POST /intent` (DESIGN §4.3 step 2, "declares
  ... emits a `planned` pheromone + event") should call **both**
  `events.drop_pheromone(conn, parcel_id, agent_id, "planned", strength)` for each
  target parcel **and** `events.emit(conn, "planned", agent_id, payload={...})` —
  `drop_pheromone` deliberately does not emit an event itself (single
  responsibility: it only touches the `pheromone` table), so the endpoint handler
  is where those two calls get paired, not `events.py`.
- U6 handoff for U11 (`coordinator/reaper.py`): `decay_pheromone` is ready to be
  called from a periodic loop as-is — pass a fixed `half_life` (no default is set
  in `events.py`; U11 owns picking/config'ing that constant) each tick and ignore
  the returned touched-count unless useful for logging. The reaper's own
  `reaped`-event emission should go through `events.emit(conn, "reaped",
  agent_id=<the dead agent>, payload={"lease_id": ..., "parcel_id": ...})` — same
  pattern `leases.py` now uses, so there's no new pattern to invent.

- U7  — FastAPI server — `server/app.py`: `create_app(db_path) -> FastAPI` factory
  (mirrors `db.init_db`'s "pass a path, get a handle" shape) opens/inits the
  blackboard at `db_path` immediately — no need to enter the app as an ASGI
  lifespan context just to hit endpoints — and registers a `lifespan` that
  closes that connection on shutdown for callers that do use
  `with TestClient(app) as c:` or run it under uvicorn. All 9 DESIGN §4.2
  endpoints are wired as thin shells over already-tested U4/U5/U6 functions
  (`classifier.store.run_index`, `server.leases.acquire/heartbeat/release`,
  `server.events.emit/tail/drop_pheromone`) — U7 added no new coordination
  logic, only request/response shapes:
  - `POST /index` — body `{root, threshold?}` → calls `run_index`, returns
    `{root, parcels: <count>, contracts: <count>}`.
  - `GET /parcels` — one dict per parcel row **plus an `active_leases`** list
    (`[{lease_id, agent_id, mode}, ...]`) computed by joining
    `leases WHERE status='active' AND ttl_expires_at > now` in Python — this
    is the "+ lease status" DESIGN §4.2 asks for; note it filters *unexpired*
    leases the same way `leases.acquire`'s CAS does (lazy-expiry — an
    expired-but-still-`status='active'` row is invisible here too, not just
    to a new acquire), so `GET /parcels`/`GET /leases` never lie about a
    lease the reaper (U11) hasn't gotten around to marking `reaped` yet.
  - `GET /leases` — same active+unexpired filter, flat lease rows.
  - `POST /intent` — upserts into `intents` (PK is `(agent_id, task)`,
    `ON CONFLICT DO UPDATE`), drops a `planned` pheromone **per target
    parcel**, emits one `planned` event with the whole target list in the
    payload.
  - `POST /lease` → straight `leases.acquire(...)`, `response_model=LeaseResult`.
  - `POST /heartbeat` / `POST /release` → `{"ok": bool}` from
    `leases.heartbeat`/`leases.release`'s own bool returns (added
    `ReleaseBody{agent_id, lease_id}` locally in app.py — release's wire shape
    isn't in `blackboard/models.py` yet, it's the same two fields as
    `HeartbeatBody` but a distinct model for clarity).
  - `GET /contract/{symbol:path}` — the `:path` converter matters: a
    contract's `symbol` is the full **parcel id** (`"mod_a.py::helper"`, per
    U3's storage choice), which contains no literal `/` in the flat-file
    fixtures used so far but *would* for a subpackage path — plain `{symbol}`
    would truncate at the first `/`. 404s on an unknown symbol.
  - `POST /parcel/update` — updates `content_hash`/`state_summary`
    (`COALESCE`'d so an agent posting `state_summary=None` doesn't blank an
    existing note)/`updated_at`; 404s on an unknown `parcel_id`; drops a
    `done` pheromone (schema's `pheromone.kind` comment lists
    `planned|touched|done` — this endpoint is literally DESIGN §4.3 step 6's
    "done" signal, no other unit owns dropping it) and emits a `done` event.
  - `GET /events?since=&limit=` → `events.tail(...)`, `response_model=list[Event]`.
  - `POST /integrate` — **wired but returns HTTP 501.** `coordinator/integrator.py`
    (U10) is still a commented-out stub with no callable `integrate()` — there
    is nothing to submit a branch to yet. The handler validates
    `IntegrateBody` and 501s with a message pointing at U10 rather than faking
    merge/test-gate behavior inside U7's scope. **U10 must replace this
    handler's body** with a call into `coordinator.integrator.integrate(...)`
    — the route, request model, and test scaffold (`test_integrate_is_wired_but_not_yet_implemented`
    in `tests/test_server.py`) are ready for that swap.
  - **No reaper/decay background task wired at startup either**, same reason:
    `coordinator/reaper.py` (U11) has no callable functions yet (also a
    comments-only stub). DESIGN §4.2's "Background (startup): reaper +
    pheromone decay run as asyncio tasks" is explicitly deferred to whichever
    of U11/U12 first has something real to schedule — `create_app`'s
    `lifespan` is the natural place to add
    `asyncio.create_task(reaper.run(conn))` once it exists.
  - **`db.connect` gained `check_same_thread=False`** (one-line change to
    `blackboard/db.py`, U1's file) — this was a **hard blocker without it**:
    Starlette's `TestClient` runs sync route handlers off a threadpool/anyio
    portal thread different from the thread that calls `create_app`, and
    stock `sqlite3.connect` refuses cross-thread use by default
    (`sqlite3.ProgrammingError: SQLite objects created in a thread can only
    be used in that same thread`). Safe for a single shared connection served
    to one request at a time (the prototype's single-writer model is "one
    connection/DB file", not "one OS thread"); did not touch `isolation_level`
    or the WAL/foreign_keys pragmas. Confirmed `tests/test_blackboard.py`
    (single-threaded) is unaffected.
  `tests/test_server.py` green (15 tests, using `create_app(tmp_path/"blackboard.db")`
  + `with TestClient(app) as client:` per BUILD_PLAN's literal done-when list):
  index→parcels round-trip incl. `active_leases`/blast_radius/content_hash on
  the fixture's `mod_a.py::helper`, re-index doesn't duplicate rows, write/write
  lease contention → granted+denied (and both `GET /parcels`'s `active_leases`
  and `GET /leases` reflect the held lease), read/read both grant, intent
  round-trip + `planned` event visible via `/events`, heartbeat ok=true for
  owner / false for a stranger, release ok=true + frees the parcel + idempotent
  second release ok=false, parcel/update round-trip + `done` event + 404 on an
  unknown parcel id, contract 200 (signature/version/64-hex type_hash) + 404 on
  an unknown symbol, `/events?since=` filtering+ordering incl. default
  `since=0`, `/integrate` 501, and two `create_app` instances on different
  `db_path`s are fully isolated from each other. Full suite green (101 tests:
  86 prior + 15 U7).
- U7 handoff for U8 (`worktree/git_ops.py`): no blackboard/server dependency —
  independent as BUILD_PLAN's independence note says. Nothing from U7 needed.
- U7 handoff for U9 (`agent/client.py`): the wire shapes to hit are now live and
  tested — `BlackboardClient` can literally mirror `test_server.py`'s request/
  response JSON shapes 1:1 (e.g. `POST /release` wants
  `{"agent_id", "lease_id"}`, returns `{"ok": bool}`; `POST /lease` body maps
  straight onto `blackboard.models.LeaseRequest` and its response deserializes
  straight into `LeaseResult`). `GET /events` payload comes back as a JSON
  *string* per-event (`Event.payload`), not a nested object — the client (or
  runner) needs its own `json.loads(ev["payload"])` if it wants the structured
  dict, `events.tail`/the endpoint deliberately don't parse it back out (U6's
  original design choice, unchanged by U7).
- U7 handoff for U10 (`coordinator/integrator.py`): once `integrate(conn, repo,
  branch, base_commit, changed_parcels)` exists, swap `POST /integrate`'s
  handler body in `server/app.py` from the current `raise HTTPException(501,
  ...)` to `return integrator.integrate(conn, repo, body.branch,
  body.base_commit, changed_parcels)` (repo path + changed_parcels resolution
  is U10's/U12's call — `IntegrateBody` only carries `agent_id`/`branch`/
  `base_commit` today; add fields there if the real handler needs more). Update
  `test_integrate_is_wired_but_not_yet_implemented` into a real integration test
  at that point rather than leaving it asserting 501.
- U7 handoff for U11 (`coordinator/reaper.py`): once `reaper.run(conn,
  interval)` exists as a real async callable, wire it into `create_app`'s
  `lifespan` in `server/app.py` (`asyncio.create_task(...)` right after `yield`'s
  setup point, cancel it in the shutdown half) — the DESIGN §4.2 "Background
  (startup)" note was deliberately left undone in U7 for exactly this reason.

- U8  — git worktree ops — `swarmsync/worktree/git_ops.py`: thin argv-list (never
  `shell=True`) subprocess wrappers around `git`, per the file's own pre-existing docstring
  contract. `init_repo(path, initial_branch="integration")` commits whatever's already under
  `path` (or an `--allow-empty` commit if nothing's there) and returns the initial commit sha;
  it sets a **repo-local** (never global) commit identity + `commit.gpgsign=false` so commits
  work unattended in a sandbox with no git identity configured, and writes a `.gitignore`
  containing `.worktrees/` as part of that same initial commit — a deliberate choice so
  `.worktrees/` (see next) never shows up as untracked clutter in `git status` on the main
  checkout. Defaulting `initial_branch` to `"integration"` is the key design call: it means
  the repo's own main checkout (this `path`, never a worktree) **is** the shared integration
  branch's working tree by construction, so `merge_branch`'s default `into="integration"`
  lands with zero extra setup — no separate "create the integration branch" step anywhere.
  `add_worktree(repo, name, base_commit=None)` → `git worktree add <repo>/.worktrees/<name>
  -b <name> <base_commit>`, `base_commit` defaults to `repo`'s current HEAD if omitted;
  returns the worktree `Path`. `commit_all(worktree, message, allow_empty=False)` is `git add
  -A && git commit`, returns the new sha. `current_commit(repo, ref="HEAD")` is a stripped
  `git rev-parse`. `changed_files(repo, branch, base)` is `git diff --name-only
  base..branch`. `merge_branch(repo, branch, into="integration")` runs **in the main repo
  checkout, never inside a worktree** (since `into`'s working tree lives there) —
  `git checkout into` then `git merge --no-ff branch`; on success returns `(True, [])`. On a
  textual conflict it captures conflicted paths via `git diff --name-only --diff-filter=U`,
  then **runs `git merge --abort` itself** before returning `(False, <sorted paths>)` — so
  `into`'s tree is always left exactly as it was pre-call (DESIGN §5.4's "leave trunk
  untouched" on reject); callers never see or have to clean up a half-finished merge. Not
  internally locked — the integrator (U10) must call it serially, one branch at a time, since
  each call mutates `into`'s shared working tree in place. A merge failure that produces **no**
  conflicted paths (bad branch name, dirty tree, etc.) is NOT swallowed as `(False, [])` — it
  raises `GitOpsError` instead, so a real plumbing error can't be misread as a
  touch-set-misprediction conflict by a caller pattern-matching on the tuple.
  `remove_worktree(repo, name, delete_branch=True)` does `git worktree remove --force` +
  best-effort (`check=False`) `git branch -D` — used to discard an orphaned/reaped agent's
  worktree per DESIGN §6.
  `tests/test_git_ops.py` green (8 tests): `add_worktree` creates an isolated dir on its own
  branch whose file edits never leak back into the main repo checkout (mutated a file inside
  the worktree, asserted the main checkout's copy was untouched); `base_commit` omitted
  defaults to HEAD; two worktrees editing **disjoint** files (`fileA.txt`/`fileB.txt`) both
  `merge_branch` into `integration` with `(True, [])` and integration ends up with both edits;
  `changed_files` reports exactly the touched path; an **overlapping** edit — two branches cut
  from the same base, both rewriting the identical line of `fileA.txt` to different content —
  merges the first cleanly then returns `(False, ["fileA.txt"])` for the second, with the
  post-abort trunk content and `git status`/`MERGE_HEAD` state proving the abort left no
  residue; `remove_worktree` deletes both the dir and the branch; merging a nonexistent branch
  raises `GitOpsError` rather than a bogus `(False, [])`. Full suite green (109 tests: 101
  prior + 8 U8). No blackboard/server import anywhere in `git_ops.py` — confirmed independent
  per BUILD_PLAN's U1–U8 independence note; it's pure subprocess + `pathlib`.
- U8 handoff for U9 (`agent/runner.py`): the per-agent lifecycle is now `git_ops.add_worktree(
  repo, agent_id, base_commit)` → mutator edits files under the returned `Path` → `git_ops.
  commit_all(worktree, message)` → (later, integrator/U10) `git_ops.merge_branch(repo,
  agent_id, into="integration")`. `add_worktree`'s branch name **is** the agent_id/task id by
  convention in the tests here — reuse that 1:1 so `merge_branch(repo, agent_id)` needs no
  extra bookkeeping to find the right branch. Demo/test setup should call `git_ops.init_repo`
  once up front (e.g. pointed at a copy of `sample_repo/`) to get both the integration branch
  and a `base_commit` to hand every agent.
- U8 handoff for U10 (`coordinator/integrator.py`): call `git_ops.merge_branch` serially (one
  branch at a time — it is not internally locked) and branch on the returned tuple: `(True,
  [])` → emit `merged`, proceed to the impact-test gate (DESIGN §5.4 step 2); `(False,
  conflicts)` → that IS the "textual conflict = touch-set misprediction" signal DESIGN §5.4
  calls out — reject + emit `merge_rejected` with `conflicts` in the payload, do **not** retry
  the merge or attempt any auto-resolution. Note `merge_branch` already aborts the failed
  merge itself, so U10 does not need its own `git merge --abort` cleanup step. Use `git_ops.
  changed_files(repo, branch, base_commit)` post-merge to get the touched-file list for impact
  test selection + re-indexing (DESIGN §5.4's "re-index the touched files" step). If the
  pytest gate goes red *after* a clean merge, U10 needs its own rollback (e.g. `git reset
  --hard` the integration branch back to its pre-merge sha) — `git_ops.py` has no "undo a
  landed merge" primitive, only "abort an in-progress conflicted one," since those are
  different failure points in DESIGN §5.4's two-step flow (merge, then test).

- U9  — Agent client + runner + mutators — `agent/client.py`: `BlackboardClient(http)`
  is duck-typed over its transport — pass a plain base-url `str` and it opens its own
  `httpx.Client` (real deployment), or pass any object exposing `.get`/`.post` with an
  httpx-shaped response and it uses that directly. In particular you can hand it a
  `fastapi.testclient.TestClient` instance straight up — this is the literal
  "against a running TestClient server" shape BUILD_PLAN's U9 done-when asks for, no
  real socket/uvicorn needed for tests. One method per DESIGN §4.2 endpoint
  (`parcels/leases/events/contract/intent/lease/heartbeat/release/parcel_update/
  integrate`), all thin JSON-body wrappers matching `test_server.py`'s already-tested
  wire shapes exactly. `contract(symbol)` returns `None` on a 404 (not a frozen
  contract) instead of raising. `integrate(...)` deliberately does **not**
  `raise_for_status()` — it stamps the response's `_status_code` into the returned
  dict and lets the caller decide; today that's always `501` since U10 doesn't exist,
  and `runner.run_agent` treats that as "submitted, pending" rather than a failure of
  the agent-level protocol.
  `agent/runner.py`: `run_agent(agent_id, client, repo, task, target_parcels, mutator,
  mutator_kwargs=None, base_commit=None, lease_mode="write", lease_ttl=None,
  heartbeat_interval=5.0, read_contracts=None) -> AgentResult` implements DESIGN §4.3's
  full 6-step per-agent loop verbatim: (1) advisory `GET /parcels` + `GET /events` read,
  (2) `POST /intent`, (3) `POST /lease` per target parcel — **on ANY denial, releases
  whatever partial lease set was already granted and returns early**
  (`status="lease_denied"`) rather than proceeding with an incomplete lock set or
  leaking held leases, (4) `GET /contract` for each `read_contracts` symbol (returned
  as `contract_snapshot` on the result — drift-detection-vs-plan-time-snapshot is left
  for the broker/U12, this unit just fetches current state), (5) `git_ops.add_worktree`
  + `mutator(worktree, **mutator_kwargs)` + `commit_all`, with a background
  `_Heartbeater` daemon thread bumping every held lease's TTL every `heartbeat_interval`
  seconds for the duration of step 5 (stopped in a `finally` before commit) — this is
  the thread DESIGN §4.3 says "so a crash (killed process) simply stops heartbeating ->
  reaper reclaims the lease" (money-shot #4, U11's territory to actually exercise), (6)
  for each target parcel, **re-parses the freshly committed worktree file
  (`classifier.indexer.parse_file`) and posts the real re-derived `content_hash`** —
  never trusts anything the mutator claims — via `POST /parcel/update` with a
  deterministic `state_summary` (`kind + symbol + byte-span + task`, per DESIGN §2's
  heuristic), then `POST /integrate` (branch name == `agent_id`, matching U8's
  handoff convention so a future integrator needs no extra bookkeeping to find the
  branch), then `POST /release` on every lease held.
  `agent/mutators.py`: `edit_function_body(worktree, path, symbol, new_body)`,
  `change_signature(worktree, path, symbol, new_sig)`, `fix_call_site(worktree, path,
  symbol, old, new)`, `break_a_test(worktree, path, symbol)` (built on
  `edit_function_body`, raises `RuntimeError` unconditionally — money-shot #5's
  test-gate-rejection mutator), `slow_edit(worktree, path, symbol, new_body,
  hang=True, delay=5.0)` (applies the edit to disk FIRST, then either hangs forever
  or sleeps `delay`s — money-shot #4's mutator: the edit is real, uncommitted work
  sitting in the worktree when an external harness SIGKILLs the hung process). All
  five locate their target via a private `_find_def` (stdlib `ast`, handles both a
  bare top-level function name and a `"Class.method"` dotted symbol) and mutate only
  that node's own line range (`node.lineno`/`node.end_lineno`/`node.body[0].lineno`),
  so two mutators targeting two different symbols in the same file produce
  non-overlapping textual hunks by construction (money-shot #1's precondition).
  **`change_signature` assumes a single-line `def ...:` header** (documented in its
  docstring) — true for every symbol in the test fixtures/`sample_repo` this
  prototype's mutators are exercised against; a multi-line signature isn't handled.
  `tests/test_agent.py` green (11 tests): the literal done-when (one `run_agent`
  declares intent, acquires a write-lease, edits via a mutator in its worktree,
  commits, posts parcel_update, releases — verified by exact event-type-ordering
  `planned < lease_granted < done < released` for the agent AND a committed diff via
  `git_ops.changed_files` showing only the touched file changed) plus: lease-denied
  backoff releases zero leases and never creates a worktree; two agents editing
  different functions in the *same* file both complete `run_agent` independently and
  `merge_branch` cleanly with zero conflicts (money-shot #1's building block, not yet
  concurrent/threaded — that's U12/U14's job); `read_contracts` round-trips (`None`
  for a non-contract symbol in this tiny 1-file fixture); every mutator incl. a method
  (`Class.method` dotted symbol), `fix_call_site`'s not-found `ValueError`, and
  `slow_edit(hang=False)`'s apply-then-return path. Full suite green (120 tests: 109
  prior + 11 U9).
- U9 handoff for U10 (`coordinator/integrator.py`): `run_agent` already calls
  `client.integrate(agent_id, branch=agent_id, base_commit=base_commit)` at the end of
  every successful run and stores the raw response dict (incl. `_status_code`) on
  `AgentResult.integrate_result` — once U10 makes `POST /integrate` do something real,
  `test_agent.py`'s `test_run_agent_full_lifecycle` assertion
  `integrate_result["_status_code"] == 501` will need updating to whatever the real
  success shape is (likely 200 + a `merged`/`merge_rejected` status field) — that's
  expected churn, not a regression. Branch name is always `agent_id` (matches U8's own
  handoff convention), so `git_ops.merge_branch(repo, agent_id, into="integration")`
  is exactly what U10 should call. `git_ops.changed_files(repo, branch, base_commit)`
  gives the touched-file list for impact test selection / re-indexing.
- U9 handoff for U11 (`coordinator/reaper.py`): `_Heartbeater` in `runner.py` is a
  plain daemon thread — nothing about it prevents building a real crash test: spawn
  `run_agent` (or just the lease+worktree+`mutators.slow_edit(hang=True)` prefix of
  it) in a subprocess and SIGKILL it after it's committed the edit but while
  `slow_edit` is hung; its lease's `heartbeat_at`/`ttl_expires_at` will simply stop
  advancing, and `leases.acquire`'s existing lazy-expiry (U5) already lets a fresh
  agent re-acquire the same parcel once `ttl_expires_at` passes — the reaper's own
  job (per U5/U6's handoff notes) is only to *mark* the row `reaped` + emit the event
  for observability, not to be a correctness dependency.
- U9 handoff for U12 (`coordinator/broker.py`): `run_agent`'s `status="lease_denied"`
  early-return (with `denied_parcels`) is the exact signal the broker needs to decide
  "serialize this task, retry later" vs. "dispatch concurrently" — no new lease-result
  shape needed. `read_contracts` + `contract_snapshot` on `AgentResult` is the seam for
  the broker to diff a task's plan-time contract version against what the agent
  observed mid-run and decide whether to force a re-plan (DESIGN §5.3/§5.5) — U9
  itself does no diffing, just fetch-and-report.

- U10  — Serial test-gated integrator — `coordinator/integrator.py`: **found fully
  implemented (and `tests/test_integrator.py` green, 9 tests) with `POST /integrate`
  already wired live in `server/app.py` when this session started building U11 —
  MEMORY.md's DONE list just hadn't been updated for it. Backfilling that record
  here so the build log matches what's actually on disk.** `integrate(conn, repo,
  branch, base_commit=None, into="integration", agent_id=None,
  expected_read_deps=None, test_dir="tests", threshold=None) -> IntegrateResult`
  (status: `merged|merge_rejected|needs_rebase`): (1) optional optimistic
  re-check (DESIGN §5.5) — if the caller passes `expected_read_deps={id:
  expected_hash}`, a mismatch vs current `parcels.content_hash`/
  `contracts.type_hash` short-circuits to `needs_rebase` with **no merge
  attempted**; (2) `git_ops.merge_branch(repo, branch, into)` — a textual
  conflict is treated as touch-set misprediction, rejected + `merge_rejected`
  event, `merge_branch` already self-aborts so trunk is untouched; (3)
  `run_impact_tests` — pytest restricted to test files whose source mentions a
  changed file's module stem, full-`test_dir` fallback when nothing matches
  (or `test_dir` doesn't exist -> whole-repo fallback); red -> `git_ops.
  reset_hard(repo, pre_merge_sha, branch=into)` undoes the just-landed merge
  commit, `merge_rejected` with the captured log; (4) on land only: re-index
  via `classifier.store.run_index`, then **authoritatively regenerate
  `state_summary`** (`regenerate_summary` — deterministic `kind + symbol +
  byte-span + signature(if any) + blast_radius`, never trusts the agent's
  self-reported note per DESIGN §5.4/§6) for every parcel in the branch's
  changed files, `reindexed` event. Not internally locked — callers (the
  broker, U12) must submit one branch at a time; `git_ops.reset_hard` is a
  U10-added primitive on top of U8's `git_ops.py` (a plain `git reset --hard`
  to a given sha on `into`'s branch, needed for the step-3 rollback path U8's
  handoff note flagged as missing). `server/app.py`'s `POST /integrate` calls
  straight into this (swapped off U7's original 501 stub).
- U11  — Reaper + pheromone decay loop — `coordinator/reaper.py`:
  `reap_once(conn, now=None) -> list[int]` finds every `active` lease with
  `ttl_expires_at <= now` (same boundary `leases.acquire`'s CAS already treats
  as not-blocking, i.e. lazy expiry means the parcel was already
  re-acquirable before this runs — the reaper is bookkeeping/observability,
  not a correctness dependency, exactly per U5/U6's handoff notes), flips
  each to `status='reaped'` (ownership-scoped `WHERE ... AND status='active'`
  so a second call/race never double-reaps or double-emits), and emits one
  `reaped` event per row (`payload={lease_id, parcel_id, agent_id}`) via
  `server.events.emit`. Returns the reaped lease ids in `id` order; `[]` on
  nothing due. `decay_once(conn, half_life=DEFAULT_HALF_LIFE, ts=None) -> int`
  is a thin pass-through to `server.events.decay_pheromone` (kept as its own
  function per this module's pre-existing docstring contract so a test/caller
  can run one decay pass without the async loop). `run(conn,
  interval=DEFAULT_INTERVAL, half_life=DEFAULT_HALF_LIFE, iterations=None)`
  is the `async` background loop DESIGN §4.2 calls for: each pass runs
  `reap_once` then `decay_once` immediately, then `await asyncio.sleep
  (interval)`; `iterations=None` (real deployment) loops until the task is
  cancelled — `asyncio.CancelledError` propagates naturally out of the
  pending sleep, no swallowing; `iterations=N` bounds it to exactly N passes
  so tests can `await reaper.run(...)` directly without racing wall-clock
  time or needing a second task to cancel it. Picked constants (not
  prescribed by DESIGN, U6 explicitly left them for U11):
  `DEFAULT_HALF_LIFE=60.0`s, `DEFAULT_INTERVAL=1.0`s (matches DESIGN §8's
  "poll `events` every 1s" choice).
  **Also wired the DESIGN §4.2 "Background (startup): reaper + pheromone
  decay run as asyncio tasks" note into `server/app.py`** (U7's handoff had
  explicitly deferred this to whichever unit first had a real callable):
  `create_app(db_path, reaper_interval=reaper.DEFAULT_INTERVAL,
  pheromone_half_life=reaper.DEFAULT_HALF_LIFE)` now starts
  `asyncio.create_task(reaper.run(conn, interval=reaper_interval,
  half_life=pheromone_half_life))` right after lifespan startup and does
  `task.cancel()` + awaits it (catching the `CancelledError`) before closing
  `conn` on shutdown. `reaper_interval=None` disables the background loop
  entirely (no task created) for a caller/test that wants to drive
  `reap_once`/`decay_once` by hand instead — used by one of the new tests to
  prove the loop really is opt-outable, not just fast.
  `tests/test_reaper.py` green (18 tests): the 3 literal done-when
  assertions (expired lease -> `reaped` status + `reaped` event with the
  right payload + parcel immediately re-acquirable by a new agent) plus:
  unexpired lease left `active`, an already-`released` lease is never
  touched/re-emitted, idempotent across repeated `reap_once` calls (no
  double-emit), empty-table no-op, multiple expired leases all reaped in id
  order, default `now=None` uses real wall-clock time, `decay_once` reduces
  strength correctly (`pytest.approx`) and floors at 0 on an extreme elapsed
  gap, runs without error on an empty pheromone table, propagates
  `ValueError` on `half_life<=0`, the bounded-`iterations` async loop reaps +
  decays and doesn't double-emit across its two passes, `iterations=0` is a
  true no-op, a real `asyncio.create_task` + `task.cancel()` unwinds cleanly
  via `CancelledError`, and two new end-to-end tests through the actual
  FastAPI app (`create_app` + `TestClient`) proving the background loop
  really does reap an expired lease on its own at a fast interval, and that
  `reaper_interval=None` truly disables it (lease stays `active` after a
  0.2s sleep with no interval running). Full suite green (145 tests: 127
  prior [incl. U10's 9, previously untracked in this file] + 18 U11).
- U11 handoff for U12 (`coordinator/broker.py`): a `reaped` event (from either
  `reap_once` directly or the background `run` loop) is the signal the
  broker needs to reassign a task — `payload["parcel_id"]`/`["agent_id"]`
  tell it which parcel freed up and which agent died; per U9's own handoff,
  the orphan worktree/branch is never merged (integrator only sees `POST
  /integrate` submissions from agents that finished), so nothing extra needs
  cleaning up on trunk — the broker just needs to spin up a fresh agent
  against the same parcel/task. Reap correctness does **not** depend on the
  background loop having ticked yet (lazy expiry in `leases.acquire`), so the
  broker can also call `reaper.reap_once(conn)` synchronously itself right
  before a scheduling decision if it wants an up-to-date `reaped`-status view
  without waiting on the 1s background cadence.

- U12  — Broker (task→parcel scheduling) — `coordinator/broker.py`:
  `Task(task_id, targets, mutator, mutator_kwargs=None, read_deps=(),
  base_commit=None, max_attempts=5)` — `targets` is a list of `(file,
  symbol_or_None)` hints (the BUILD_PLAN's own "target file/symbol hints"
  wording). `resolve_task(conn, task, mode="file"|"symbol") -> list[parcel_id]`
  maps those hints to concrete, blackboard-known parcel ids: `mode="file"`
  (DESIGN §2's de-risking default *enforced lease granularity*) collapses
  EVERY hint to its file's whole-file `"{file}::<module>"` interstitial
  parcel regardless of which symbol was named — this is what makes file-mode
  scheduling and file-mode LEASING agree (two tasks in the same file share
  one lock and can never race at symbol granularity, even though the
  mutator itself only ever touches its own named symbol's byte range).
  `mode="symbol"` leases the specific named symbol, falling back to the
  whole-file id when no symbol was given or that exact symbol parcel doesn't
  exist (classifier's own conservative DESIGN §6 fallback). Raises
  `ValueError` on an unknown mode or a hint whose file was never indexed.
  `schedulable(conn, task_a, task_b, mode=, graph=None, frozen_ids=None) ->
  bool` — every parcel `task_a` resolves to must be `classifier.graph.
  co_schedulable` with every parcel `task_b` resolves to (DESIGN §3's "whole
  target-parcel sets pairwise co-schedulable"). `group_schedulable(conn,
  tasks, ...) -> list[list[Task]]` greedily partitions `tasks` (input order
  preserved) into dispatch "waves": each wave a maximal run of mutually
  co-schedulable tasks, a conflicting task starts a new wave.
  `load_scheduling_graph(conn, repo) -> (DepGraph, frozen_ids)` re-derives a
  fresh graph from the blackboard's CURRENT parcel rows (for the
  frozen-contract clause) without re-running the whole `run_index` pipeline.
  `run(conn, repo, tasks, client, n_agents=4, mode="file",
  contract_aware=True, retry_backoff=0.2) -> dict[task_id, AgentResult]` —
  partitions into waves, dispatches each wave's tasks CONCURRENTLY (one
  `ThreadPoolExecutor` per wave, bounded by `n_agents`) through the existing
  `agent.runner.run_agent` (U9) — the broker owns no edit logic of its own,
  purely scheduling + spawning — and fully drains (`future.result()`) one
  wave, including all its tasks' retries, before starting the next. A task
  that comes back `status="lease_denied"` is retried under a fresh
  `f"{task_id}-attempt-{n}"` agent id (note: hyphen, not `::` — git branch
  names reject colons, and `run_agent`'s branch name IS the agent id) with a
  `retry_backoff` sleep between attempts, up to `task.max_attempts`; this
  ONE retry loop is both money-shot #2 (contended parcel serializes, waits,
  then lands) AND this unit's own "a task whose agent is reaped is
  reassigned and completes" done-when — no special-casing needed, because
  `leases.acquire`'s lazy expiry (U5) makes a reassigned attempt succeed the
  moment the dead holder's TTL lapses, whether or not `reaper.reap_once` (also
  called once per attempt here, purely for the event-log bookkeeping trail)
  has literally flipped that row to `reaped` yet.
  **Two real concurrency bugs found and fixed while building this unit** —
  U12 is the FIRST unit in the whole build to genuinely dispatch multiple
  threads doing real work (lease/heartbeat/worktree/commit/integrate) against
  the one shared blackboard connection + shared git repo at the same time;
  every earlier unit's tests were single-threaded, so these were latent:
  (1) **`server/app.py`'s own docstring already flags `/integrate` as "not
  internally locked, callers must submit one branch at a time"** — but
  `run_agent` calls `client.integrate(...)` as the automatic last step of
  every successful task, so two concurrently-dispatched agents in the same
  wave would otherwise race `git checkout <into>` / `git merge` against the
  SAME shared main-repo checkout. Fixed IN THIS UNIT (not by touching
  `run_agent`/`integrator.py`, which stay exactly as U9/U10 left them): `run`
  wraps whatever `client` it's given in a small `_SerializingIntegrateClient`
  (this module) that funnels only `.integrate()` through one
  `threading.Lock()`, passing every other method straight through via
  `__getattr__` — worktree edits (lease/heartbeat/mutate/commit, each
  agent's own isolated directory per DESIGN §5.1) stay genuinely parallel;
  only the shared-trunk merge step serializes, exactly the invariant DESIGN
  §5.4 needs. (2) **`server/leases.py`'s `acquire()` used `cur.lastrowid`**
  to report the new lease's id — `sqlite3_last_insert_rowid()` is a
  per-CONNECTION (not per-statement) value, and with genuinely concurrent
  threads sharing ONE connection there is a real race window between "this
  thread's INSERT executes" and "this thread reads lastrowid" during which
  ANOTHER thread's INSERT on the same connection can clobber it — reproduced
  directly (two distinct grants both reported the same `lease_id`; the
  loser's later `release()` then silently no-op'd against the WRONG row
  since it's ownership-scoped, leaving its REAL lease stuck `active` for the
  full 30s TTL and starving a same-file follow-up task that should have
  landed immediately after). **Fixed in `server/leases.py`'s `acquire()`**
  (U5's file — a correctness bug, not a U12-local workaround) by switching
  the INSERT to `... RETURNING id` and reading the id off `cur.fetchone()`
  instead of `cur.lastrowid` — immune to the race since the id comes back on
  the same statement's own result set. SQLite 3.35+ required (this host:
  3.37.2, confirmed via `sqlite3.sqlite_version`); `tests/test_leases.py`
  stays green unchanged (single-threaded, so `lastrowid` and `RETURNING`
  agreed there all along — this bug was invisible until real concurrency
  existed). Also added **`PRAGMA busy_timeout=5000`** to
  `blackboard/db.py`'s `_configure` (same file U7 already touched for
  `check_same_thread=False`) — `sqlite3.threadsafety==3` (serialized) on this
  host makes sharing one connection across threads safe in principle, but a
  losing writer's default behavior on a lock conflict is to raise
  immediately rather than wait; harmless to every earlier (single-threaded)
  unit's tests, only engages under real contention.
  **Read-dependencies are fetched, not leased**: DESIGN's `resolve_task`
  prose says "read-deps -> read-leases," but `run_agent` (U9, already
  built+tested) only ever takes `read_contracts` as a plan-time snapshot
  fetch (no lease) — `task.read_deps` threads straight through to that
  unchanged; a literal read-lease is left for whichever later unit
  (money-shot #3, U15) actually needs to gate on one.
  `tests/test_broker.py` green (10 tests, stress-tested 20+ repeated runs
  with zero flakes after the two fixes above — was flaky ~1-in-8 runs
  BEFORE the `lastrowid` fix, confirmed via a standalone repro script):
  the literal done-when in two tests — (a) 3 tasks (2 disjoint across
  `mod_a.py`/`mod_b.py`, 1 overlapping `mod_a.py` again) dispatch as
  exactly 2 waves; a custom timeline-recording test mutator proves the 2
  disjoint tasks' wall-clock windows genuinely OVERLAPPED (real concurrent
  dispatch, not just "both eventually ran") while the 3rd only started once
  BOTH wave-1 tasks had fully finished (true serialization); all 3 land
  merged, final file contents show all 3 edits; (b) a pre-seeded active
  (not-yet-expired) lease under a fake `"agent-dead"` agent forces the
  broker's first dispatch attempt to genuinely lose the CAS race
  (`lease_denied`), then age out mid-retry-loop — asserts the event order
  `lease_denied` < `reaped` (agent_id=="agent-dead") < `done` (agent_id !=
  "agent-dead"), and the task still lands merged — plus: `resolve_task`
  symbol-mode (named symbol / bare-file fallback / nonexistent-symbol
  fallback), file-mode vs. symbol-mode `schedulable` for two symbols in one
  file (False vs. True), `resolve_task` `ValueError` on an unindexed file
  and an unknown mode, `group_schedulable` on an all-disjoint task list ->
  one wave, `read_deps` round-trips into `AgentResult.contract_snapshot`.
  Full suite green (155 tests: 145 prior + 10 U12), re-run 20+ times with
  zero flakes.
- U12 handoff for U13 (`sample_repo/`): the broker is mode-agnostic re:
  repo shape — it just needs `run_index`/`POST /index` to have already
  populated `parcels` for whatever files a `Task`'s `targets` name.
  `sample_repo` needs >=1 file with two independent functions (for the
  disjoint-vs-overlapping money-shot #1 story) and a high-fan-in symbol
  (frozen-contract candidate, for money-shot #3/U15) — `broker.run`'s
  `contract_aware=True` default will pick that up automatically via
  `load_scheduling_graph` with zero extra wiring once `sample_repo` exists.
- U12 handoff for U14 (`demo/run_demo.py`): `broker.run(conn, repo, tasks,
  client, n_agents=...)` is the one call the demo harness needs to drive
  money-shots #1/#2 end-to-end (#1: give it 2 same-file, different-symbol
  tasks with `mode="symbol"` so they actually get distinct locks and land
  concurrently rather than file-mode's default single-lock serialization;
  #2: a 3rd task targeting an already-held parcel demonstrates
  `lease_denied` -> wait -> `lease_granted` -> `merged` via the same retry
  loop). Money-shot #4 (crash recovery) needs a REAL process kill (a
  subprocess running the lease+worktree+`mutators.slow_edit(hang=True)`
  prefix, SIGKILLed) per U9/U11's own handoff notes — this unit's "reaped ->
  reassigned" test proves the broker's retry loop handles that correctly
  once the lease genuinely ages out/gets reaped, but does not itself launch
  or kill a subprocess (out of scope for a scheduling unit); U14 owns
  wiring the actual kill.
- U12 handoff for U15 (money-shot #3): `schedulable`/`group_schedulable`
  already take `graph`/`frozen_ids` and, via `run`'s `contract_aware=True`
  default, automatically veto co-scheduling a task against a frozen contract
  its edges show a dependency on (`classifier.graph.co_schedulable`'s own
  clause, unchanged) — U15 mainly needs a `contract_change` event emission
  path (an agent taking an EXCLUSIVE lease + changing a signature) and a
  dependent's re-plan step; the broker's scheduling machinery doesn't need
  new surface for that, just a task sequencing than exercises it.

- U13 — Sample repo + its test suite — `sample_repo/{calc,formats,api}.py` (flat
  top-level modules, no `__init__.py` — deliberate) + `sample_repo/tests/{conftest,
  test_calc,test_formats,test_api}.py` + `tests/test_sample_repo.py` (5 new tests)
  + fixed a stale U4-era assertion in `tests/test_index_api.py`
  (`test_run_index_on_real_sample_repo_dir_does_not_crash` asserted `sample_repo/`
  indexes to `parcels == [] and contracts == []` — true only because `sample_repo/`
  didn't have any `.py` files yet pre-U13; renamed to
  `test_run_index_on_real_sample_repo_dir` and now asserts the real, populated
  result instead, since this unit is exactly the one that populates it).
  **Shape:** `calc.py` has 4 independent top-level functions (`add`/`sub`/`mul`/
  `div`, none call each other) — money-shot #1's fixture: any two of them have
  disjoint byte spans in the SAME file, so symbol-mode `co_schedulable` is `True`
  between them (asserted directly in `test_sample_repo.py`). `formats.py` (named
  imports `from calc import add, div, mul`) and `api.py` (module import
  `import calc` + attribute calls, plus named `import formats`) both call
  `calc.add`, and so does `sample_repo/tests/test_calc.py`'s own `test_add`/
  `test_add_negative` — three-plus distinct calling parcels is enough to clear
  `graph.FREEZE_THRESHOLD` (3) on its own (before even counting BFS transitive
  closure through e.g. `api.py::summarize`'s own callers in `test_api.py`), and
  since every caller lives in a different file than `calc.py`, `calc.py::add`
  comes out cross-module → `extract_contracts` reliably makes it sample_repo's
  frozen-contract candidate every run (verified directly: `test_sample_repo.py`
  asserts `"calc.py::add"` by name, not just "some contract exists"). This is the
  literal fixture money-shot #3 (U15) needs: an exclusive-lease-worthy, real,
  cross-module signature to change.
  **`sample_repo/tests/conftest.py`** (new, tiny) inserts `sample_repo/` itself
  onto `sys.path` keyed off `__file__`, not cwd — needed because sample_repo is
  deliberately NOT a package (`calc`/`formats`/`api` import each other as bare
  top-level modules, matching how `classifier/graph.py`'s dotted-module
  resolution derives names from each file's path relative to whatever root
  `index_repo`/`build_graph` were given, i.e. `sample_repo/` itself once U14's
  demo indexes it directly). Without this, `from calc import add` in
  `sample_repo/tests/*.py` only resolves when the cwd happens to already be on
  `sys.path`; anchoring on `__file__` instead makes `pytest sample_repo/tests`
  (this unit's own done-when, cwd=repo-root), the integrator's own
  `pytest tests` (`swarmsync/coordinator/integrator.py::run_impact_tests`,
  cwd=the git-worktree checkout of this same tree), and a bare `pytest` run from
  inside `sample_repo/` all agree.
  `tests/test_sample_repo.py` green (5 tests): >=3 top-level modules
  (`calc.py`/`formats.py`/`api.py`), a real cross-file import/call edge exists in
  the built graph, `calc.py`'s two-independent-functions/symbol-mode-disjoint
  property, `calc.py::add` is a frozen contract with `blast_radius >=
  FREEZE_THRESHOLD` and `graph.is_cross_module`, and a subprocess
  `pytest -q tests` run with `cwd=sample_repo/` exits 0 (14 tests, sample_repo's
  own suite). Full project suite green (160 tests: 155 prior + 5 new; the
  renamed `test_index_api.py` test is a modification, not an addition), re-run
  twice with zero flakes.
  Handoff for U14 (`demo/run_demo.py`): index `sample_repo/` directly as the
  repo root (not the swarm-sync project root) so parcel/module dotted-names
  resolve the same way `test_sample_repo.py` already verified — money-shot #1
  can target `calc.py`'s `add`/`sub` (or `mul`/`div`) with `mode="symbol"` tasks;
  money-shot #2 can contend a write-lease on either of those same two parcels
  from a 3rd task; money-shot #5's test-breaking edit can target any function in
  `sample_repo/tests/test_calc.py` (breaking an assertion) paired with an edit to
  the corresponding `calc.py` function, so the integrator's pytest gate rejects
  it for a real (not synthetic) reason.
  Handoff for U15 (money-shot #3): `calc.py::add`'s frozen-contract status is
  real and reproducible every run (not a fixture coincidence) — U15's agent can
  `change_signature` on `"add"` in `calc.py` under an exclusive lease, emit
  `contract_change`, and `formats.py::total_with_tax` / `api.py::summarize` /
  `api.py::report` (all real call sites, all covered by
  `sample_repo/tests/{test_formats,test_api}.py`) are the dependents whose call
  sites a re-planning agent fixes and re-lands green.

- U14 — End-to-end demo: money shots #1, #2, #4, #5 — `demo/run_demo.py` (+ new
  `demo/_crash_agent.py` helper) + `tests/test_demo.py`. `run_demo(workdir=None,
  keep=False) -> dict(workdir, results, all_ok)` is the reusable entry point;
  `main() -> int` (the `python demo/run_demo.py` console entry) prints a
  `[PASS]`/`[FAIL]` line per assertion plus a final `PASS: money-shot #N ...`
  summary block and returns/exits `0` iff every check passed. **Deliberate
  design call: the WHOLE demo runs against a REAL live `uvicorn` server**
  (`_ServerThread`, a background thread running `uvicorn.Server.run()` against
  a free localhost port), not FastAPI's in-process `TestClient`/ASGI
  transport — money-shot #4 needs an agent that lives in a genuinely separate
  OS process so a real `SIGKILL` only kills the agent, never the coordinator;
  once that's required for #4, every other shot talks the same real HTTP
  wire protocol too (via `agent.client.BlackboardClient(base_url)`), rather
  than mixing an in-process shortcut for some shots and a real socket for
  others. `POST /index` is called directly via a raw `httpx.post(...)` (same
  pattern `tests/test_broker.py`'s own fixture already uses) since
  `BlackboardClient` has no `.index()` wrapper — that endpoint is a one-shot
  setup step, not part of the agent lifecycle `BlackboardClient` wraps.
  **Money shots #1+#2 are driven through ONE `broker.run()` call**, exactly
  per U12's own handoff note: 3 `mode="symbol"` tasks against `calc.py`'s
  `sub`/`mul`/`div` — `sub`/`mul` prove #1 (disjoint symbol-mode leases, both
  land merged with zero conflicts; a custom `_timed_edit` wrapper records
  wall-clock start/end per edit so the demo PROVES genuine overlap, not just
  "both eventually ran" — same technique `tests/test_broker.py` validated);
  `div` proves #2 via an EXTERNALLY pre-seeded `manual-editor` write-lease on
  `calc.py::div` (long TTL=8s so it cannot merely time out — that would be
  #4's story, not #2's) released by a timer thread ~1.2s in, so `div`'s task
  genuinely loses its first CAS race (`lease_denied`, observed directly in
  the event log) and lands only after the holder's explicit `release`, not
  an expiry. **Money-shot #4 is the one genuinely new piece of infra this
  unit adds**: `demo/_crash_agent.py` is a standalone script (not a package,
  no `__init__.py` in `demo/` — same shape as `run_demo.py` itself) that
  `subprocess.Popen`'d, does the normal `run_agent` lifecycle against the
  live server's real URL with `mutators.slow_edit(hang=True)` as its
  mutator (writes its edit to disk, then spins forever) — `run_demo.py`
  polls `/events` for a real `lease_granted` from `crash-agent`, sleeps
  briefly to let the write actually land on disk, confirms the process is
  still alive (genuinely hung, not exited), then `proc.kill()` (real
  `SIGKILL`) + `proc.wait()`, then polls for a `reaped` event (the live
  server's own background reaper loop, `reaper_interval=0.5s` on this app,
  does the reclaiming — no manual `reap_once` call needed), asserts trunk's
  `formats.py` is byte-identical to pre-shot (the dead process's edit never
  reached `integration` — worktree isolation, not any special cleanup, is
  what guarantees this), then reassigns via a plain fresh `run_agent(...)`
  call (not `broker.run` — a single task doesn't need the broker's
  wave/retry machinery) targeting the SAME parcel, which lands merged.
  Money-shot #5 is the simplest: one `run_agent` with `mutators.
  break_a_test` on `api.py::summarize` (a real call site covered by
  `sample_repo/tests/test_api.py::test_summarize`) -> `merge_rejected` with
  `reason="tests_failed"`, trunk's `api.py` byte-identical to before, and
  the full `sample_repo` suite re-verified green afterward.
  `tests/test_demo.py` green (5 tests, ~30s total): (1) the LITERAL
  done-when — `python demo/run_demo.py` as a real subprocess exits 0 and its
  stdout contains `"PASS: money-shot #1/#2/#4/#5"` + `"PASS: overall"` and
  no `"FAIL: money-shot"`; (2) `run_demo.run_demo()` called in-process
  reports `all_ok=True` and every one of `shot1/shot2/shot4/shot5/overall`
  `True`; (3) the integration branch's `git log --merges` shows >=3 distinct
  landed agent branches (DESIGN §7's own "≥3 concurrent agents" framing,
  checked directly against git history, not just the printed summary);
  (4) the blackboard's `events` table has zero `merge_rejected` rows with
  `reason="merge_conflict"` (shot #5's `reason="tests_failed"` rejection IS
  expected and present) — the literal "zero same-file textual collisions
  reached integration" acceptance criterion; (5) the full `sample_repo` test
  suite is independently re-run and green at the very end. Full project
  suite green (165 tests: 160 prior + 5 new). **One flake observed** on a
  full-suite run immediately after several rapid manual `python
  demo/run_demo.py` invocations (`test_run_demo_lands_at_least_three_
  distinct_agents_worth_of_commits` failed the ">=3 distinct merge branches"
  assertion once; passed in isolation immediately after) — root-caused to
  real ambient CPU contention on this dev box (a long-running, unrelated
  `price_action.engine` process pinning a core, plus other background
  daemons), not a logic bug: this unit's timing margins (holder-release
  delay, retry backoff × attempts, crash-agent TTL/reap-wait) are real
  wall-clock sleeps racing real thread scheduling, so they are exactly the
  kind of thing ambient load can occasionally blow through. **Widened all
  margins** in response (holder release 1.2s->1.5s, holder lease
  ttl 8s->20s so it can never itself expire mid-retry and get conflated with
  #4's expiry story, `retry_backoff` 0.3s->0.4s with `max_attempts`
  10->20 for the contended `div` task -- up to ~7.6s of retry headroom now vs.
  a 1.5s release delay -- crash-agent `lease_granted` wait 10s->15s, its
  pre-kill disk-write buffer 0.4s->0.6s, and the post-kill `reaped`-event wait
  `ttl + 8s` -> `ttl + 15s`). Re-ran the full suite 3 more consecutive times
  after widening (165/165 green every time) plus 3 more bare `python
  demo/run_demo.py` runs (all exit 0, zero `[FAIL]` lines, ~7s each) -- no
  further flakes. If a future run ever flakes again on this same test, look
  at ambient system load first before assuming a scheduling/logic regression
  in `broker.py`/`leases.py`.
  Handoff for U15 (money-shot #3, extends this same `demo/run_demo.py`):
  `calc.py::add` is untouched by this unit on purpose (reserved for U15's
  `change_signature` under an exclusive lease, per U13's own handoff) — add
  a `_run_shot3(...)` following this file's existing `_run_shot4`/`_run_shot5`
  shape, append `"shot3"` to `SHOT_ORDER`/`SHOT_LABELS`, and update the
  final `"ALL MONEY SHOTS..."` message (currently explicitly says "#3 lands
  in unit U15" — that sentence is the one line this unit expects U15 to
  change). The live server / `client` / `conn` / `repo` this file's
  `run_demo()` already sets up are exactly what a `_run_shot3` needs — no
  new infra, just another scripted scenario using `mutators.change_signature`
  + `mutators.fix_call_site` (both already built in U9) against
  `calc.py::add` and its real dependents (`formats.py::total_with_tax`,
  `api.py::summarize`, `api.py::report`).

- U15 — Money shot #3: frozen-contract change + dependent re-plan — extended
  `coordinator/integrator.py` (beyond the file list, see note below),
  `agent/runner.py`, `coordinator/broker.py`, `demo/run_demo.py`,
  `tests/test_demo.py` (+ `tests/test_integrator.py`, `tests/test_agent.py`,
  `tests/test_broker.py`). Three real, independently-testable pieces:
  (1) **`integrator.integrate()` now detects a landed frozen-contract change**:
  right before/after its existing re-index step it snapshots `contracts.
  type_hash` for every symbol whose FILE this branch touched, diffs old vs.
  new after `run_index`, and for any symbol that genuinely changed emits a
  real `contract_change` event (old/new signature + version) and reports the
  symbol on a new `IntegrateResult.contract_changes` list. This lives in the
  integrator (not the agent) on purpose: only a real landed before/after
  `type_hash` diff is trustworthy (DESIGN §5.4/§6 "lying blackboard" — an
  agent's own self-report is exactly what that rule forbids trusting).
  (2) **`agent/runner.run_agent` gained `lease_modes: Optional[dict[parcel_id,
  mode]]`** — a per-parcel override on top of the existing uniform
  `lease_mode`, plus `AgentResult.lease_modes_used` reporting what was
  actually granted per parcel.
  (3) **`coordinator/broker.py`'s `_run_task_once`/`_run_task_with_retries`
  thread the SAME `frozen_ids` set `run()` already computes for
  `group_schedulable` down into `run_agent` as `lease_modes={parcel_id:
  "exclusive" for parcel_id in target_parcels if parcel_id in frozen_ids}`** —
  DESIGN §5.3's "changing a frozen contract requires an exclusive lease" is
  now an ENFORCED runtime invariant, not a convention a caller has to
  remember. Verified this doesn't touch any prior unit's fixtures (test_agent.
  py/test_broker.py's tiny 1-2-file repos have zero cross-module references,
  so no frozen contracts ever register there → `lease_modes` stays empty →
  behavior byte-identical to pre-U15 for every existing test). **Deliberately
  went outside the BUILD_PLAN's literal 3-file list** to touch
  `coordinator/integrator.py`: emitting an event requires DB write access,
  which only server-side code has (`agent/client.py`'s HTTP client has no
  generic "emit an event" call, and adding one would have meant touching
  MORE files — `client.py` + `server/app.py` — for a less architecturally
  sound result). `coordinator/integrator.py` is itself under `coordinator/`
  (DESIGN's own "the coordinator marks dependent parcels..." wording), so
  this is the natural, DRY home for auto-detecting ANY landed contract
  change, not just ones a demo script declares by hand.
  `demo/run_demo.py`'s new `_run_shot3`: `calc.py::add` (sample_repo's
  existing frozen contract, U13) gets a NEW optional param
  (`rounding=None`, unused by `add`'s own body) via `mutators.
  change_signature` — deliberately backward-compatible so `test_calc.py`'s
  direct `add(2, 3)` calls keep passing the signature-change task's OWN
  impact-selected test gate (which only re-checks tests reachable from
  `calc.py`, i.e. before either dependent has fixed anything) — then TWO
  real dependents (`formats.py::total_with_tax`, `api.py::summarize`) each
  declare `calc.py::add` as a `read_deps` contract and land a `mutators.
  fix_call_site` fix (`rounding=2`). All three run through ONE `broker.run`
  call, symbol mode: `co_schedulable`'s pre-existing frozen-contract clause
  (DESIGN §3, built in U3/U12 — NO changes needed there) already forces the
  signature-change task into its own earlier wave ahead of both dependents,
  since `calc.py::add` sits in `frozen_ids` and both dependents are its real
  reverse-dependencies. **Fixed a genuine pre-existing shot4 fragility this
  exposed**: shot4's "trunk untouched by the crashed agent" check used to
  diff trunk's `formats.py` against the STATIC on-disk `sample_repo/
  formats.py` — invalid now that shot3 legitimately edits
  `formats.py::total_with_tax` earlier in the same run. Changed it to
  snapshot trunk's `formats.py` at the START of `_run_shot4` instead
  (order-independent, correct regardless of what ran before it).
  `tests/test_demo.py` green (6 tests, ~55s total): the existing 5 U14 tests
  (updated for 5 shots + the new final-message string) plus one new
  `test_run_demo_shot3_emits_contract_change_and_lands_dependent_fixes`
  checking the blackboard/git state directly (not just printed PASS lines):
  a `contract_change` event exists with the right old/new signature, an
  `exclusive` `lease_granted` event exists for `calc.py::add`, the
  `contract_change` event's seq precedes both dependents' `merged` events,
  both call sites actually show `rounding=2` on trunk, and the full
  `sample_repo` suite is green. `python demo/run_demo.py` as a real
  subprocess: exit 0, all five `PASS: money-shot #N` lines +
  `PASS: overall`, zero `FAIL: money-shot` lines, `ALL FIVE MONEY SHOTS
  PASS` in stdout — verified 3 consecutive bare runs, all green, ~7-8s each.
  Full project suite: **170 passed** (165 prior + 5 new: 1 in
  test_agent.py for `lease_modes`, 1 in test_broker.py for the
  auto-exclusive-lease behavior, 2 in test_integrator.py for
  `contract_change` detection [positive + negative case], 1 new case in
  test_demo.py) — re-ran the full suite 3 consecutive times, 170/170 green
  every time, no flakes observed.

  **This was the LAST unit in BUILD_PLAN.md.** Per its own "Final
  acceptance (whole prototype)" line: `python demo/run_demo.py` exits 0 with
  all five money shots PASS, zero same-file textual collisions reach
  `integration`, and every landed commit leaves `sample_repo` tests green —
  all independently verified above. The prototype is feature-complete
  against DESIGN.md + BUILD_PLAN.md as written.

## DOING
_(none — BUILD_PLAN.md is fully implemented)_

## TODO
_(none — U1 through U15 are all DONE; see U15's own note above)_

## INTEGRATION / END-TO-END PROOF (post-build verification pass, 2026-07-16)

Ran the full suite + demo fresh against `.venv` (Python 3.11.0rc1) with no code changes
required — everything U1-U15 left behind still holds:

- **Full test suite**: `.venv/bin/python -m pytest -q` → **170 passed**, 0 failed, 1
  pre-existing harmless warning (starlette/httpx TestClient deprecation notice), ~54s.
  No breakages found, nothing to fix.
- **Demo** (`python demo/run_demo.py`): ran **3 consecutive times**, exit 0 every time,
  zero `[FAIL]` lines, all 5 money-shot PASS blocks + `PASS: overall` + the final
  `ALL FIVE MONEY SHOTS PASS` banner every run. No flakes observed on this pass (U14's
  MEMORY note had recorded one prior flake under ambient CPU load on the dev box — not
  reproduced here).
- **Money shots, all 5 honestly PASS** (not stubbed, not faked — each is a real assertion
  against live blackboard events / git history / re-run pytest, per DESIGN §7):
  1. Concurrent disjoint symbol-mode edits (`calc.py::sub` + `calc.py::mul`) land clean,
     zero textual conflicts, wall-clock overlap independently verified.
  2. Contended parcel (`calc.py::div`): real `lease_denied` against an externally-held
     lease, waits, acquires after release, lands merged.
  3. Frozen-contract change (`calc.py::add` gains `rounding=None`) emits a real
     `contract_change` event with old/new signature+version; two real dependents
     (`formats.py::total_with_tax`, `api.py::summarize`) re-read the contract and land
     fixed call sites, in the correct event order.
  4. Crash mid-edit: `crash-agent` subprocess genuinely SIGKILLed while holding a lease
     and mid-`slow_edit`; reaper reaps it (real `reaped` event); integration branch never
     contains the dead agent's edit; task reassigned to a fresh agent and lands.
  5. Test-breaking edit (`api.py::summarize` via `break_a_test`) is rejected by the
     integrator's pytest gate (`merge_rejected`, `reason=tests_failed`); trunk stays
     byte-identical; `sample_repo` suite re-verified green afterward.
- **Overall DESIGN §7 acceptance criterion held**: zero same-file textual collisions
  reached `integration` across the whole run, and the `sample_repo` test suite was green
  at the end.
- **Nothing stubbed or faked in the demo path.** The one known stub in the whole codebase
  is the `state_summary` LLM-generation hook (DESIGN §2 always said this would be a
  deterministic heuristic for the prototype, regenerated authoritatively by the
  integrator on merge — this is spec-compliant, not a shortcut) and the tree-sitter
  multi-language classifier backend (explicitly out of scope per DESIGN §8). Both
  are pre-existing, intentional, and already documented in DESIGN.md itself.
- **Conclusion: the swarm-sync / Pheromesh prototype is proven end-to-end.** No fixes
  were needed during this integration pass — the U1-U15 build log's own claims
  (170/170 tests, all 5 money shots) held up under independent re-verification.

## NOTES
- Stack: Python 3.11, FastAPI + Uvicorn, SQLite (WAL), stdlib `ast` classifier, git worktrees
  via subprocess, pytest gate. Deps pinned in pyproject.toml / requirements.txt.
- venv: `.venv` at repo root built with `python3.11 -m venv .venv` (host default `python3` is
  3.10, which is below the `requires-python >=3.11` floor — always invoke python3.11
  explicitly when (re)creating the venv). Installed via `pip install -e ".[dev]"`. Reuse this
  venv for all future units; do not recreate unless deps change.
- U1 handoff for U2 (classifier/indexer.py): `swarmsync.blackboard.db.connect`/`init_db` are
  the ONLY functions that should open the DB file (single-writer invariant, per db.py's own
  docstring) — the classifier should build parcel dicts/`Parcel` model instances and hand them
  to a store/insert helper rather than opening its own connection. `Parcel.model_validate` takes
  a plain dict (use `dict(row)` if starting from a `sqlite3.Row`) — from_attributes is on if you
  ever have an object instead. `contract_hash`/`content_hash`/`byte_start`/`byte_end`/
  `state_summary`/`territory` are all Optional — only `id`, `path`, `updated_at` are required.
- Agents are deterministic scripted mutators for the prototype (reproducible, no API keys).
  The `agent/client.py` seam lets a real Claude Agent SDK worker drop in later.
- Enforced lease granularity defaults to FILE; symbol-mode is per-parcel opt-in for money-shot #1.
- Independence: U1-U8 have no cross-dependencies beyond their stated predecessors and can each be
  built + tested in isolation. U9+ integrate them.

## BLOCKERS
_(none)_
