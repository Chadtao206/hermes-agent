# Kanban Postgres read-path completion — design

- **Date:** 2026-05-31
- **Status:** Approved design; implementation plan to follow.
- **Context:** Post-Phase-5 cutover hardening. The board is live on Supabase
  Postgres (`kanban.backend=postgres`), and the gateway dispatcher/notifier and
  the board doctor/liveness now read Postgres. But two **read paths** were never
  routed through the `resolve_backend()`/`KanbanStore` seam and still read the
  **frozen** `<HERMES_HOME>/kanban.db` (writes to it stopped ~09:50 on 2026-05-31;
  Postgres authoritative since):
  1. The `hermes kanban` **board-management CLI** (`list`/`ls`/`show`/`liveness`/
     `unblock`/`reassign`/`archive`/`comment`/…) — fails under Postgres.
  2. The **worker in-agent kanban tools** (`tools/kanban_tools.py`) — **split-brain**:
     writes (`kanban_complete`/`kanban_block`) go through the store (Postgres) but
     reads (`kanban_show`, worker-context build, list) read frozen sqlite.
- **Smoking gun:** a task created after the 09:50 freeze (e.g. `t_be80f0ba`) has
  zero rows in the frozen sqlite but exists in Postgres; a worker spawned for it
  calls `kanban_show` → "not found" → exits without `kanban_complete`/`block` →
  auto-blocked as a `protocol_violation` (rc=0). This manufactures the post-cutover
  backlog of protocol-violation blocks.

## Goal

Under `kanban.backend=postgres`, the worker in-agent kanban tools and the
`hermes kanban` board-management CLI **read and resolve the live Postgres board**.
Under `sqlite` they behave **exactly** as today.

## Root cause (why workers don't even resolve Postgres)

`resolve_backend()` reads `load_config()`. A dispatcher-spawned worker runs with
`HERMES_HOME=<profile dir>` (set by the upstream spawn at `kanban_db.py:8383`),
so it loads the **profile** `config.yaml`. The profile `kanban:` blocks carry
dispatch settings only — **no `backend: postgres`, no `postgres.dsn`** (only the
root config has them). So inside a worker, `resolve_backend()` → `sqlite` and
`resolve_dsn()` → raises. Today both worker reads and writes therefore default to
sqlite regardless of code routing. The fix needs a **propagation** layer in
addition to read routing.

## Hard boundaries

- `hermes_cli/kanban_db.py` is **not edited** (upstream merge hot-spot). No new
  SQL is added there; its existing helpers are reused.
- The **sqlite path is byte-identical**. Every change is
  `if resolve_backend()=="postgres": <new> else: <existing>`.
- **No secret leakage** — the DSN lives only in the root config + the gateway
  process env; it is never logged. Existing redactions preserved.
- `gateway/run.py` is touched **minimally** (one idempotent env-export block at
  startup) — it is live core; sequence it carefully.

## Architecture — three layers

### Layer 1 — Propagation (workers resolve Postgres + get the DSN)

- **`hermes_cli/kanban/store.py::resolve_backend()`** gains an env override:
  if `HERMES_KANBAN_BACKEND` is set to a valid backend, it wins; otherwise fall
  through to the existing config read (→ sqlite default). Defensive: an invalid
  value is ignored (falls through). ~3 lines, fork-owned.
- **`gateway/run.py`** startup: when the **root** config resolves
  `backend=postgres`, export into `os.environ` (once, before the dispatcher
  spawns workers):
  - `HERMES_KANBAN_BACKEND=postgres`
  - `HERMES_KANBAN_PG_DSN=<resolved dsn>` (only if not already set in env)

  Spawned workers inherit both through the existing `env = dict(os.environ)` copy
  at `kanban_db.py:8383` (unedited). `resolve_dsn()` already prefers
  `HERMES_KANBAN_PG_DSN`. The DSN value is never written to a log line.

### Layer 2 — Worker read routing (`tools/kanban_tools.py`)

The write tools already use `_store()` (→ `kanban_store(board)`); this layer makes
the **read** tools consistent under Postgres while leaving the sqlite path
byte-identical:

- The read tools (`kanban_show`, the board-listing path that builds
  `_task_summary_dict`, and worker-context assembly) branch on
  `resolve_backend()`:
  - **sqlite:** unchanged — `_connect()` → `kb.snapshot_connect`/`kb.connect`,
    `kb.get_task(conn, …)`, `kb.build_worker_context(conn, …)`, etc.
  - **postgres:** `store = _store()` and call the store read methods
    (`get_task`, `list_comments`, `list_events`, `list_runs`, `parent_ids`,
    `child_ids` incl. rollup `relation_type`, `latest_summary`/`latest_summaries`),
    and `store.build_worker_context(task_id)` (new method, below).
- `_connect()` itself is **not** removed — it remains the sqlite reader.

### Layer 3 — CLI (`hermes_cli/kanban/cli.py`)

- **`main()` preamble (~line 1372-1379):** when `resolve_backend()=="postgres"`,
  **skip** the sqlite `kb.init_db()` (the Postgres schema is ensured by the store /
  `pg_pool.ensure_schema`). This one change unblocks every handler that already
  uses `_make_store()` (`unblock`/`reassign`/`archive`/`comment`/`edit`/`block`/
  `link`/`list`/`create`/…). Implementation: extend the existing
  `observational_actions` early-skip with a backend check, or guard the
  `kb.init_db()` call directly.
- **Three still-sqlite-coupled read handlers** route through the seam:
  - `_cmd_show` (`kb.connect_closing()` + `kb.get_task`/…): branch to store reads
    under Postgres (same JSON/no-JSON output shape).
  - `_cmd_liveness` (`kb.connect` + sqlite `compute_board_liveness`): branch to the
    already-built `kanban_liveness.compute_board_liveness_pg` (added in the
    doctor/liveness phase), mirroring the gateway loop.
  - `_cmd_context` (`kb.build_worker_context(conn, …)`): branch to
    `store.build_worker_context(task_id)` under Postgres.

### The store-primitive `build_worker_context`

- Add `build_worker_context(self, task_id: str) -> str` to the `KanbanStore`
  protocol (`store.py`).
- **sqlite impl** (`store_sqlite.py`): `self._read(lambda c: kb.build_worker_context(c, task_id))`
  → **byte-identical** to today's behavior.
- **postgres impl** (`store_postgres.py`): reassemble the **identical** context
  string from store primitives — the task, its dependency parents (each with the
  latest run summary / `ended_at`), children, and the closeout-requirement block —
  using `get_task`/`parent_ids`/`child_ids`/`latest_summaries`/`list_runs`. No new
  SQL in `kanban_db.py`.
- Guarded by a **cross-backend parity test** (see Testing): the PG output must
  equal the sqlite output for identical seed data. This is the defense against
  format drift.

## Components touched

| File | Change |
|---|---|
| `hermes_cli/kanban/store.py` | `resolve_backend()` env override; add `build_worker_context` to the protocol |
| `hermes_cli/kanban/store_sqlite.py` | `build_worker_context` → delegate to `kb.build_worker_context` (byte-identical) |
| `hermes_cli/kanban/store_postgres.py` | `build_worker_context` → reassemble from store primitives |
| `gateway/run.py` | one idempotent startup env-export block under the postgres check |
| `tools/kanban_tools.py` | read tools branch on `resolve_backend()` → store reads under PG; sqlite path unchanged |
| `hermes_cli/kanban/cli.py` | `main()` init_db preamble backend-branch; `_cmd_show`/`_cmd_liveness`/`_cmd_context` PG routing |
| `hermes_cli/kanban_db.py` | **none** (untouched) |

## Testing

- **`build_worker_context` parity** (docker-PG conformance fixture): seed identical
  task graphs (with dependency parents that have run summaries, children, comments)
  in both backends; assert `store.build_worker_context(tid)` returns the **same
  string**.
- **`kanban_show` / `_cmd_show` parity:** identical task → matching output shape
  across backends.
- **Propagation:** `resolve_backend()` returns `postgres` when
  `HERMES_KANBAN_BACKEND=postgres` and config says sqlite (env wins); invalid env
  value falls through to config. A worker-env simulation (profile-style config
  with no kanban backend + inherited `HERMES_KANBAN_BACKEND`/`HERMES_KANBAN_PG_DSN`)
  resolves `postgres` + DSN.
- **CLI under Postgres:** `list`/`show`/`unblock`/`reassign`/`archive` succeed
  against PG with no `could not initialize database` sqlite error.
- **Worker write smoke (PG):** a worker-context store resolves PG and
  `complete_task` lands in Postgres (not frozen sqlite).
- **Regression:** existing sqlite worker-tool + CLI + doctor/liveness tests stay
  green (byte-identical sqlite path).

## Risks & mitigations

- **`build_worker_context` format drift between backends** → the parity test fails
  loudly if the strings diverge; PG impl is reviewed against the upstream sqlite
  function during implementation.
- **Worker writes were also sqlite-bound** → Layer 1 fixes reads *and* writes
  (same `resolve_backend`); the worker write smoke asserts a completion lands in PG.
- **Live `gateway/run.py` touch** → a single idempotent env-export block behind the
  postgres check, sequenced last; activated on the next gateway restart.
- **Secret leakage via env export** → the DSN is set into `os.environ` only and
  never logged; the export logs presence, not value.

## Success criteria

- Under `kanban.backend=postgres`: spawned workers resolve Postgres + DSN, their
  `kanban_show`/context reads hit the live board, and `kanban_complete`/`block`
  land in Postgres — no more "task not found" / rc=0 protocol-violation auto-blocks
  caused by frozen-sqlite reads.
- `hermes kanban` board-management commands (`list`/`show`/`unblock`/`reassign`/
  `archive`/`comment`/…) work against the live Postgres board.
- sqlite path byte-identical; `kanban_db.py` unedited; no DSN in any output/log.
- `build_worker_context` cross-backend parity test green; existing tests green.
