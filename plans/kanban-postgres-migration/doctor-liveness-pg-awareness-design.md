# Doctor / liveness Postgres-awareness — design

- **Date:** 2026-05-31
- **Status:** Approved design; implementation plan to follow.
- **Context:** Post-Phase-5 cutover operational hardening. The kanban board is now
  live on Supabase Postgres (`kanban.backend=postgres`), but the board doctor and
  the gateway board-liveness probe are **sqlite-only** — they read the now-frozen
  `<HERMES_HOME>/kanban.db`, so their health signals are stale and misleading
  (the gateway logged a `liveness breach` off frozen sqlite immediately after the
  cutover). This makes them read the **live backend** instead.
- **Not** the runbook's "Phase 6" (retire SQLite life-support + web dashboard) —
  this is a focused diagnostics fix.

## Goal

`run_board_doctor`, `kanban_liveness.compute_board_liveness`, and the gateway
`_run_liveness_check_once` loop must reflect the **live** kanban backend. Under
`kanban.backend=postgres` they read Postgres; under `sqlite` they behave exactly
as today.

**Hard boundaries**
- The **sqlite path is byte-identical** (default + legacy deployments unaffected).
- `hermes_cli/kanban_db.py` is **not** edited (upstream merge-hot-spot).
- `kanban_board_doctor.py` and `kanban_liveness.py` are **fork-owned** (verified via
  git history) — editing them is in-bounds.
- The gateway change is **PG-path-only**; the sqlite liveness loop stays byte-identical.
- **No secret leakage** — the PG `db_path` field is a redacted identifier (never the
  password).

## Background (current structure)

- **`run_board_doctor(board, ready_age_seconds)`** (`kanban_board_doctor.py`):
  1. `_quick_check(path)` — **sqlite file integrity** (header bytes,
     `kanban_health.run_readonly_health_bundle` / `PRAGMA quick_check`). A `critical`
     here returns early. *Meaningless for PG.*
  2. `kb.snapshot_connect(board)` → sqlite conn → **six logical-invariant checks**
     via SQL, each emitting an issue dict `{severity, kind, message, …, action}`:
     `orphan_task_link`, `orphan_profile_event_subscription`, `stale_running_task`
     (uses `_alive(pid)` OS check + expired-claim + stale-heartbeat),
     `stale_running_run`, `blocked_with_completed_parents`, `old_ready_task`.
  3. A `reconcile_summary` embed (runs a sqlite-side reconciler; suppresses some
     `blocked_with_completed_parents`).
  Returns `{ok, board, db_path, issues, reconcile_summary?, as_of}`.
- **`kanban_liveness.compute_board_liveness(conn, *, now) -> Liveness`**: three
  metric queries — `oldest_ready_age_seconds`, `oldest_blocked_done_parents_age_seconds`,
  `oldest_stale_running_age_seconds` — sqlite SQL (`?` placeholders, no `board`
  column). `evaluate(snap, *, thresholds) -> list[Breach]` is **already
  backend-agnostic** (operates on the `Liveness` dataclass).
- **gateway `_run_liveness_check_once(state)`**: per board, opens a sqlite conn and
  calls `compute_board_liveness` → `evaluate` → dedup-alert (`logger.error`).

The PG schema mirrors these tables with a `board TEXT` column and `%s` placeholders;
Phase-4's migrator already encodes the orphan/dangling JOIN logic in
`migrate_sqlite_to_pg._INTEGRITY_CHECKS`.

## Architecture — backend-branch in the fork-owned diagnostics

Selected approach: branch on `resolve_backend()` inside the doctor/liveness modules
(+ the gateway loop). The logical/liveness invariants are re-expressed as
board-scoped PG SQL; the sqlite code paths are left untouched.

### PG doctor path (`run_board_doctor`)

```
backend = resolve_backend()
if backend != "postgres":   # sqlite (and any non-pg) -> existing code, unchanged
    <current implementation>
else:
    # 1. connectivity in place of file-integrity
    try: pool connect + SELECT 1
    except: issues.append(critical "pg_unreachable"); return early (mirrors db_unreadable shape)
    # 2. the six logical checks as board-scoped PG SQL (row-returning), emitting the
    #    SAME issue kinds/severities/fields as the sqlite path
    # 3. db_path = redacted "postgres://<host>:<port>/<db>"
    # (reconcile_summary embed omitted on PG — see below)
```

- The six PG checks reuse the migrator's `_INTEGRITY_CHECKS` JOIN logic (written as
  row-returning, board-scoped `SELECT`s) for the two orphan checks, and new
  board-scoped PG SQL for `stale_running_*`, `blocked_with_completed_parents`
  (`GROUP_CONCAT` → `string_agg`), and `old_ready_task`. `stale_running_task`'s
  pid/claim/heartbeat logic is unchanged Python over the rows (host-local `_alive`
  is valid for the single-host deployment).
- **`reconcile_summary` is omitted on the PG path** (it runs a sqlite-side
  reconciler). Consequently the PG doctor surfaces `blocked_with_completed_parents`
  **without** the reconciler's suppression. A reconcile-on-PG embed is a possible
  later follow-up. (Approved deviation.)
- **`db_path`** for PG is a redacted `postgres://<host>:<port>/<db>` string derived
  from the parsed DSN — **never** the password. (Approved.)

### PG liveness path

- Add `compute_board_liveness_pg(cur, board, *, now) -> Liveness` to
  `kanban_liveness.py`: the three metric queries as board-scoped PG SQL. The sqlite
  `compute_board_liveness(conn, *, now)` and `evaluate(...)` are unchanged.
- gateway `_run_liveness_check_once`: branch per board on `resolve_backend()` —
  sqlite → current (open sqlite conn → `compute_board_liveness`); **postgres → obtain
  a pooled cursor → `compute_board_liveness_pg(cur, board, now)`** → same `evaluate`
  + same dedup-alert path. The sqlite-specific subsystem flags
  (`notifier_disabled`/`writer_disabled`) are passed through unchanged for sqlite
  and supplied as empty/N-A for the PG branch.

## Components touched

| File | Change |
|---|---|
| `hermes_cli/kanban_board_doctor.py` | add the `resolve_backend()=="postgres"` branch + PG-SQL logical checks + connectivity check + redacted `db_path`; sqlite path unchanged |
| `hermes_cli/kanban_liveness.py` | add `compute_board_liveness_pg(cur, board, *, now)`; sqlite `compute_board_liveness` + `evaluate` unchanged |
| `gateway/run.py` | `_run_liveness_check_once`: PG-path-only branch (no sqlite conn under PG); sqlite branch byte-identical |
| `hermes_cli/kanban/cli.py` (doctor cmd) | none — calls `run_board_doctor`, now backend-aware |

## Testing

- **Cross-backend parity (conformance docker-PG fixture):** seed a board with known
  defects — an orphan `task_link`, a `blocked` task whose dependency parents are all
  done, an old `ready` task — in **both** backends; assert `run_board_doctor` returns
  the **same set of issue `kind`s**, and `compute_board_liveness` (sqlite) vs
  `compute_board_liveness_pg` (PG) return matching metric values.
- **PG-targeted:** connectivity-check path (a bad DSN → `pg_unreachable` critical);
  `db_path` is the redacted `postgres://…` form with no password substring.
- **Gateway liveness PG branch:** a focused test that the loop computes via the PG
  path (no sqlite conn opened) under `backend=postgres`.
- Existing sqlite doctor/liveness tests stay green (byte-identical path).

## Risks & mitigations

- **Issue-shape drift between backends.** Mitigate: the parity test asserts identical
  issue kinds; the PG checks emit the same dicts.
- **`gateway/run.py` touch (live core).** Mitigate: PG-path-only; sqlite liveness loop
  byte-identical; covered by a focused test; sequence the gateway change last.
- **Secret leakage via `db_path`.** Mitigate: redact to host:port/db, never password;
  asserted by a test.
- **`reconcile_summary` parity gap.** Accepted: PG omits the reconciler embed (surfaces
  `blocked_with_completed_parents` unsuppressed) — documented; reconcile-on-PG is a
  later follow-up.

## Success criteria

- Under `kanban.backend=postgres`, `hermes kanban doctor` reports against the **live
  PG board** (correct `db_path`, real issues) and the gateway liveness probe computes
  PG metrics (no more false breaches off frozen sqlite).
- sqlite path byte-identical; `kanban_db.py` unedited; no password in any output.
- Cross-backend parity test green; existing tests green.
