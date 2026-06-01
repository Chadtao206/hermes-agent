# Phase 6 · B1 — specify + decompose + PG reconciler under Postgres (design)

**Status:** approved (design phase). Branch `feat/kanban-pg-phase6-b1` (worktree `.worktrees/kanban-pg-phase6-b1`), off `main` `9f9f568f2`.
**Predecessors:** Part A dashboard read-path (`dashboard-readpath{-design,}.md`), the doctor/liveness PG awareness (`doctor-liveness-pg-awareness{-design,}.md`), Phase 4.5 tail close-out (`phase-3-tail-closeout-design.md`). Mirrors their patterns.

## Problem

The kanban board is LIVE on Supabase Postgres (`kanban.backend=postgres`). Three control-plane operations are still sqlite-coupled and therefore touch the **frozen** `~/.hermes/kanban.db` instead of live PG:

1. `POST /tasks/{id}/specify` → `hermes_cli/kanban_specify.py::specify_task` uses `kb.connect_closing()` + `kb.specify_triage_task()` (sqlite) internally. The dashboard "Specify" button silently mutates the frozen board.
2. `POST /tasks/{id}/decompose` → `hermes_cli/kanban_decompose.py::decompose_task` uses `kb.connect_closing()` + `kb.specify_triage_task()` / `kb.decompose_triage_task()` (sqlite). The dashboard "Decompose" button silently mutates the frozen board.
3. `GET /reconcile` → `plugin_api.py` currently serves a **graceful no-op** under PG because `hermes_cli/kanban_reconciler.py::run_reconciler` is sqlite-only (`_snapshot_connect` of the on-disk DB). `hermes kanban reconcile` (cli.py:1492) is likewise stale under PG.

## Goal

Close all three split-brains in a single branch. specify/decompose mutate live PG with **byte-identical event/state parity** to sqlite. `/reconcile` (dashboard + CLI) returns **real** PG actions for the 9 core action kinds, with the 2 niche kinds explicitly deferred behind a documented + logged note. The sqlite path stays byte-identical; the upstream-hot-spot files are import-only.

## Scope decisions (settled up front)

- **All three deliverables in one cycle / one branch** (user decision).
- **Reconciler fidelity = core 9 kinds; defer 2 niche kinds** (user decision):
  - **Ported (9):** `dead_running_candidate`, `expired_claim_candidate`, `stale_heartbeat_observed`, `stale_run_metadata`, `orphan_claim_lock_observed`, `blocked_with_completed_parents_decision`, `scheduled_with_completed_parents_decision`, `pre_spawn_validation_decision`, plus the residual catch-all `info` section (the loop-var `kind` at reconciler ~line 1159–1188).
  - **Deferred (2):** `review_parent_pr_head_evidence_missing` (PR-workflow-coupled: parent run-metadata PR-head SHA evidence) and `repeated_failure_signature_decision` (systemic same-fingerprint aggregation — overlaps B2's M1 work). These are NOT emitted on PG; instead `_run_reconciler_pg` attaches a top-level `partial` block enumerating them and logs once at INFO. No silent caps.

## Architecture & the load-bearing seam decision

specify/decompose are **atomic composite writes** that emit *single, specific* event shapes in one transaction:

- `kb.specify_triage_task` (kanban_db.py:5605–5693): one `write_txn` — updates `title`/`body`/`assignee` only when provided & different, flips `status: triage→todo`, emits **one** `"specified"` event with payload `{"changed_fields":[...]}` (or `None` when only status changed), optionally inserts an inline audit comment (only when `changed_fields` non-empty **and** `author` provided; the `commented` event is intentionally skipped), then runs `recompute_ready(conn)` **outside** the txn. Returns `bool` (`False` if not found / not in triage / race).
- `kb.decompose_triage_task` (kanban_db.py:5696–5894): pre-validates child dicts + sibling-parent **cycle detection** (Kahn) *outside* the txn (→ `None` on empty/cycle, `ValueError` on malformed). Then one `write_txn`: fetch root (must be `triage`, else `None`); create each child (`INSERT tasks` `status='todo'`, `workspace_kind='scratch'`, `created_by=author or 'decomposer'`, emit `"created"` `{"by":author, "from_decompose_of":task_id}`); link sibling parents (`INSERT OR IGNORE task_links`, emit `"linked"` `{"parent","child"}`); link **root as child of every child** (`task_links(child_id, root_id)`); promote root `triage→todo` (+`assignee=root_assignee` if provided); optional audit comment; emit **one** `"decomposed"` `{"child_ids":[...], "root_assignee":...}`. Then `recompute_ready(conn)` outside the txn iff `auto_promote`. Returns `list[str]` child ids (input order) or `None`.

The sqlite **store** (`SqliteKanbanStore`) cannot host these: its `_write(op, **kwargs)` routes through `kb.write_session(...)`, whose writer op-set is defined in the forbidden `kanban_db.py` / `kanban_writer_daemon.py`. Adding `specify_triage_task`/`decompose_triage_task` to the shared `KanbanStore` Protocol would force a sqlite implementation that can't be built without editing those files.

**Decision:** add the two composite methods to **`PostgresKanbanStore` only** (not the shared Protocol). Call them from a `resolve_backend()=="postgres"` branch in the two `hermes_cli` modules. The sqlite path stays verbatim (calls `kb.*` directly). This keeps the Protocol honest, the forbidden files untouched, and the sqlite path byte-identical.

### Backend-branch shape (specify/decompose modules)

Both `specify_task` and `decompose_task` keep their existing signatures (callers — dashboard, CLI — do not pass a board; the dashboard env-pins `HERMES_KANBAN_BOARD` around the call, the CLI uses `--board` via the same env). The backend branch wraps **only the DB-touch points**; the LLM/config/roster middle is backend-agnostic and shared:

```
backend = resolve_backend()
# --- read root task ---
if backend == "postgres":
    slug = kb.get_current_board()            # honors the pinned env; resolve ONCE
    store = PostgresKanbanStore(board=slug)
    task = store.get_task(task_id)
else:
    with kb.connect_closing() as conn:       # EXISTING, verbatim
        task = kb.get_task(conn, task_id)
# ... shared validation + LLM call + parse ...
# --- write ---
if backend == "postgres":
    ok = store.specify_triage_task(task_id, title=..., body=..., assignee=..., author=...)
else:
    with kb.connect_closing() as conn:       # EXISTING, verbatim
        ok = kb.specify_triage_task(conn, task_id, ...)
```

- The same `store` (constructed once on the read) is reused for the write — `slug` resolved exactly once per call (resolve-bslug-once rule; here there are no `pg_reads`, only the store).
- `decompose_task`'s `fanout=false` branch reuses `store.specify_triage_task(...)` (with `assignee`); the `fanout=true` branch calls `store.decompose_triage_task(...)`.
- `list_triage_ids()` in both modules gains a PG branch using `store.list_tasks(status="triage", tenant=..., include_archived=False)`.

### New `PostgresKanbanStore.specify_triage_task`

```
def specify_triage_task(self, task_id, *, title=None, body=None,
                        assignee=None, author=None) -> bool
```
One `with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur: with conn.transaction():` block, board-scoped (`WHERE board=%s` on `self.board`):
1. `SELECT` current `title,body,assignee,status` for `task_id` — return `False` if missing or `status != 'triage'`.
2. Compute `changed_fields` (only fields provided **and** different; `assignee` via the same canonicalization sqlite uses — import the pure helper).
3. `UPDATE tasks SET ...changed..., status='todo'`.
4. If `changed_fields` and `author`: inline `INSERT task_comments` with the same body text sqlite uses (no `commented` event).
5. `self._emit(cur, task_id, "specified", {"changed_fields": changed_fields} if changed_fields else None)`.
6. After the txn: `self.recompute_ready()`.
Return `True`.

### New `PostgresKanbanStore.decompose_triage_task`

```
def decompose_triage_task(self, task_id, *, root_assignee, children,
                          author=None, auto_promote=True) -> Optional[list[str]]
```
- Pre-validate `children` shape + cycle-detect siblings **outside** the txn, mirroring sqlite (extract/reuse the pure cycle-check logic; → `None` on empty/cycle, raise `ValueError` on malformed dict).
- One transaction, board-scoped:
  1. `SELECT` root — must exist & `status='triage'`, else `None`.
  2. For each child (input order): mint id via `kb._new_task_id()` (imported), `INSERT tasks` (`status='todo'`, `workspace_kind='scratch'`, `created_by=author or 'decomposer'`, assignee canonicalized), `self._emit(cur, child_id, "created", {"by":author or "decomposer", "from_decompose_of":task_id})`.
  3. For each child's `parents`: `INSERT INTO task_links ... ON CONFLICT DO NOTHING`, `self._emit(cur, child_id, "linked", {"parent":parent_id, "child":child_id})`.
  4. For each child: `INSERT task_links(parent_id=child_id, child_id=root_id) ON CONFLICT DO NOTHING` (root waits on all children).
  5. `UPDATE tasks SET status='todo'[, assignee=root_assignee]` for root.
  6. If `author`: inline audit comment on root.
  7. `self._emit(cur, task_id, "decomposed", {"child_ids":[...], "root_assignee":root_assignee})`.
- After the txn: `self.recompute_ready()` iff `auto_promote`.
- Return `child_ids`.

Both methods use the existing private `_emit(cur, task_id, kind, payload)` (board-scoped event insert with `Jsonb`) and `recompute_ready()`. Event names, payload keys, and null-handling match the sqlite twins exactly.

## Part 3 — PG reconciler

Mirror `kanban_board_doctor._run_board_doctor_pg`:

- `run_reconciler(*, board=None, ready_age_seconds=900, now=None)` gains a top dispatch: `if resolve_backend()=="postgres": return _run_reconciler_pg(...)`. The sqlite body is unchanged (verbatim).
- `_run_reconciler_pg(*, board, ready_age_seconds, now=None, pool=None)`:
  - `slug = board or kb.get_current_board()` (resolve once).
  - `db_path = _redacted_pg_dsn()` (reuse the doctor's redaction helper or a local mirror → `postgres://host:port/db`; never the password).
  - Bounded connectivity probe (`pool.connection(timeout=5)` → `SELECT 1`); on failure return the same dict shape with `ok=False` + a critical note (mirror doctor early-return), no exception leak.
  - `actions = _collect_reconcile_actions_pg(conn, slug, ready_age_seconds, now)` (ReconcileAction objects).
  - `action_dicts = actions_to_dicts(actions)` (reused).
  - `filtered, suppressed = _filter_acknowledged_decision_packets(...)` — its only DB read is task comments; under PG fetch comments via `store.list_comments(task_id)` (or board-scoped PG SQL). Resolve `slug`/store once.
  - `wake_triage = classify_wake_triage(filtered)` (reused, pure).
  - Return the sqlite-shaped dict `{ok, board, db_path, actions, wake_triage, as_of, mutation_applied:False}` **plus** `partial = {"deferred_kinds": [...2...], "note": "..."}`; `logger.info` once that the 2 kinds are skipped on PG.
- `_collect_reconcile_actions_pg(conn, slug, ready_age_seconds, now)`: board-scoped `WHERE board=%s` PG SQL producing ReconcileAction objects for the 9 core kinds, reusing the pure helpers `_action`, `_signature`, `_pid_alive`, `_pre_spawn_validation_errors_for_reconcile`, `kb._error_fingerprint`, `kb._lane_type_for_assignee` (all operate on `Task`/strings; map PG rows → `kb.Task` where a helper needs a Task). No mutations.
- `format_reconcile_text(result, max_examples=...)` is reused unchanged (dict-driven).

**Read-only:** `run_reconciler` performs no writes on either backend (mutations live in `apply_reconcile_decision`, which neither the dashboard nor the relevant CLI path invokes). Lower risk than specify/decompose.

## Dashboard wiring

`plugin_api.py` `GET /reconcile` (lines ~749–786): delete the PG graceful-no-op branch; both backends call `kanban_reconciler.run_reconciler(...)` + `format_reconcile_text(...)` (run_reconciler self-dispatches). specify/decompose endpoints need **no change** — they already env-pin the board and call the now-backend-aware module functions.

## Byte-identical / forbidden-file guarantees

- `hermes_cli/kanban_db.py`, `kanban_liveness.py`, `kanban_writer_daemon.py`: **import-only**, zero edits. decompose imports `kb._new_task_id` + the pure assignee/cycle/fingerprint helpers; nothing is modified.
- Every sqlite code path: untouched `else:`/sqlite-body verbatim (empty diff on those lines).
- Default backend stays `sqlite` in code and tests.
- No DSN/secret in logs: redact to `host:port/db`, or log only `type(exc).__name__`. Exclude `dsn:` when grepping config.

## Testing

- **Cross-backend store conformance** (docker-PG `store` fixture vs sqlite `kb`): `specify_triage_task` and `decompose_triage_task` — parity on resulting task fields, `triage→todo` transition, child graph (sibling links + root-as-child), `assignee` routing, and the emitted event sequence (names + payload keys). Include race/no-op cases (not-in-triage → `False`/`None`) and cycle/empty rejection for decompose.
- **Reconciler PG**: seed each of the 9 core kinds on a docker-PG board; assert `run_reconciler` (PG) emits matching action dicts + signatures; assert the 2 deferred kinds yield the `partial` note (and are absent from `actions`); spot-check shape-parity vs the sqlite reconciler for overlapping kinds; connectivity-failure path returns the dict shape (no leak, no raise).
- **Dashboard** (`tests/plugins/test_kanban_dashboard_plugin_pg.py`, extend): `/reconcile` returns real PG actions (not the old no-op); `POST /specify` + `POST /decompose` mutate the **PG** board (verified through the store), not the frozen sqlite file. A DSN-leak regression assertion on the `/reconcile` failure path.
- **sqlite regression**: existing specify/decompose/reconciler + dashboard suites green; confirm the sqlite branches' diffs are empty.

Test interpreter: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`. PG tests use the docker `postgres:16-alpine` fixture; the controlling session provides `HERMES_PG_TEST_DSN` (throwaway container on `127.0.0.1:55432`) to subagents — never the live Supabase DB.

## Review plan (subagent-driven, per task)

Fresh implementer → spec-compliance review → code-quality review for each task. **Adversarial** code review for the live-core touches:
- `hermes_cli/kanban/store_postgres.py` (the two new atomic methods — atomicity, event-shape parity, board scoping),
- `plugins/kanban/dashboard/plugin_api.py` (the `/reconcile` branch change),
- `hermes_cli/kanban_reconciler.py`, `hermes_cli/kanban_specify.py`, `hermes_cli/kanban_decompose.py` (live dashboard/CLI paths; DSN-leak + byte-identical-sqlite focus).

## File inventory

- **Edit:** `hermes_cli/kanban/store_postgres.py` (+2 methods), `hermes_cli/kanban_specify.py` (PG branch + list_triage_ids), `hermes_cli/kanban_decompose.py` (PG branch + list_triage_ids), `hermes_cli/kanban_reconciler.py` (PG dispatch + `_run_reconciler_pg` + `_collect_reconcile_actions_pg`), `plugins/kanban/dashboard/plugin_api.py` (`/reconcile` branch).
- **Tests:** extend `tests/plugins/test_kanban_dashboard_plugin_pg.py`; new store specify/decompose conformance + reconciler-PG conformance tests (under `tests/hermes_cli/kanban/` following the existing PG conformance layout).

## Risks

- **decompose atomicity** — children + sibling links + root-as-child links + root promotion + events must all live in one PG transaction with rollback-on-error; the highest-fidelity piece. Cycle pre-validation reused from sqlite logic.
- **`_filter_acknowledged_decision_packets` under PG** — its comment read must use the resolved `slug`/store consistently (resolve-once).
- **Task-object reuse in `_collect_reconcile_actions_pg`** — helpers like `_pre_spawn_validation_errors_for_reconcile(task)` expect a `kb.Task`; PG rows must be mapped to `Task` faithfully (reuse the store's row→Task mapper).

## Out of scope (deferred / tracked)

- The 2 niche reconciler kinds (`review_parent_pr_head_evidence_missing`, `repeated_failure_signature_decision`) — tracked in the `partial` block; close in a later pass (the systemic one overlaps B2).
- `apply_reconcile_decision` PG port (operator-applied mutations) — not invoked by the dashboard/CLI read path; out of scope.
- Non-default board on PG (single-board `default` is the live config).
