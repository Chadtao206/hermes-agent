# Kanban Postgres Cutover Runbook — Phase 5

- **Date:** 2026-05-30
- **Phase:** 5 — Human-driven live cutover
- **Depends on:** Phase 4 complete (migrator `hermes_cli/kanban/migrate_sqlite_to_pg.py` merged)
- **Branch at design lock:** `main` @ `157d4dab3` (Phase 3 glue); Phase 4 tooling on `feat/kanban-pg-phase4-migrate`

---

## 1. Scope and safety

**Scope:** one board, slug `default`, stored in `<HERMES_HOME>/kanban.db` (~6 MB,
~7 300 rows). No other boards exist; the migrator enforces this with a single-board
guard and will refuse loudly if more than one board is found on disk.

**Source is read-only:** the migrator opens the SQLite file with
`sqlite3.connect("file:<path>?mode=ro", uri=True)`. It never writes to or
mutates the source database.

**Cutover is reversible — but only in a narrow window.** The rollback path (§8)
flips `kanban.backend` back to `sqlite` and restarts the gateway. The untouched
`kanban.db` resumes as if nothing happened. **This window closes the moment the
Postgres backend takes any write that diverges from the SQLite snapshot** (e.g.
the first task status change after the flip). After that point the two stores are
out of sync and rollback is not safe without re-migrating or discarding Postgres
writes. The one-way door is called out explicitly at §8.

---

## 2. Preconditions gate (BLOCKING — must all be true before proceeding)

Work through this checklist in order. **Do not proceed past any unchecked item.**

### 2a. Every phase-3-tail item is CLOSED

These are tracked by marker comments `# phase-3-tail:` in
`hermes_cli/kanban/store_postgres.py` and `hermes_cli/kanban_glue.py`, and
recorded in the `kanban-pg-phase3-glue` memory note. They are runtime
correctness gaps in the Postgres backend that are orthogonal to moving data but
must be resolved before the backend is trusted in production.

| # | Item | Status required |
|---|------|----------------|
| 1 | PG `detect_crashed_workers`: reap-registry clean-exit (rc=0) must be classified as a protocol violation, not a normal exit | CLOSED |
| 2 | PG pre-spawn validation: auto-block emit when validation fails (matching SQLite behavior) | CLOSED |
| 3 | PG host-local `SIGTERM → grace → SIGKILL` kill-ladder: currently delegated to an injected `signal_fn`; reconcile `store_postgres.signal_fn(pid, sig)` vs `kanban_glue.enforce_runtime_kill` ladder so the full escalation runs on PG | CLOSED |
| 4 | Systemic-spawn-failure sibling pre-emptive block in the glue (currently missing for PG path) | CLOSED |
| 5 | (Cleanup) Dead gateway helpers `_kanban_advance` / `_rewind` / `_unsub` / `_profile_*` are now unreachable from the notifier; remove to prevent confusion | CLOSED |
| 6 | Confirm whether non-single-writer SQLite is a supported production config; this determines which of items 1–4 are truly blocking vs. nice-to-have | RESOLVED |

**GO / NO-GO:** if any item above is still open, stop here. Raise a ticket, close
it, and re-run this checklist from the top.

### 2b. Supabase Postgres provisioned and reachable

- A Supabase project exists for this deployment.
- The **transaction-pooler** connection string is in hand. It looks like:
  `postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres`
  (port 6543, not 5432). No `LISTEN/NOTIFY` is used, so transaction-mode pooling
  is safe.
- Test connectivity from the gateway host:

```bash
cd /Users/ctao/.hermes/hermes-agent
venv/bin/python -c "
import psycopg, sys
dsn = '<supabase-dsn>'
with psycopg.connect(dsn) as c:
    print('connected, server:', c.info.server_version)
"
```

Expected output: `connected, server: <PG version integer>`. Any error is a
**NO-GO** — fix connectivity before proceeding.

### 2c. `postgres` extra installed

The migrator imports `psycopg` at module top; the store imports `psycopg_pool`.
Both are in the `postgres` optional extra. Verify:

```bash
cd /Users/ctao/.hermes/hermes-agent
venv/bin/pip install -e '.[postgres]'
venv/bin/python -c "import psycopg; import psycopg_pool; print('ok')"
```

Expected: `ok`. If the import fails, install is incomplete — **NO-GO**.

### 2d. Maintenance window scheduled

- Notify all users/agents that will be affected (the gateway will be down for the
  duration of §4 Quiesce through §7 Restart + verify).
- Expected downtime: under 5 minutes in normal conditions.

### 2e. Fresh backup of `kanban.db` taken

```bash
cp /Users/ctao/.hermes/kanban.db /Users/ctao/.hermes/kanban.db.pre-pg-cutover-$(date +%Y%m%dT%H%M%S)
ls -lh /Users/ctao/.hermes/kanban.db.pre-pg-cutover-*
```

Confirm the backup file exists and has a size matching the live file (~6 MB).
**NO-GO if the copy fails.**

---

## 3. Rehearsal: dry-run against the real target

Run the migrator in dry-run mode against the real Supabase target. This creates a
throwaway schema named `kanban_dryrun_default_<mtime>` (derived from the board
slug + source file mtime, deterministic and re-run stable), applies the full PG
DDL, loads all rows, runs the complete verification stack, then drops the schema
with `DROP SCHEMA … CASCADE`. Zero residue is left in the database.

```bash
cd /Users/ctao/.hermes/hermes-agent
venv/bin/python -m hermes_cli.kanban.migrate_sqlite_to_pg \
    --dry-run \
    --dsn "<supabase-dsn>" \
    --json
```

**What the dry-run does (in order):**

1. Calls `kanban_db.list_boards()` — refuses if more than one board is found.
2. Opens `kanban.db` read-only; reads all 9 migrated tables into memory.
3. Pre-flight validation: every TEXT column decoded as strict UTF-8; every
   `task_events.payload` and `task_runs.metadata` value parsed as JSON. Any
   offender aborts with a report before any write.
4. Creates schema `kanban_dryrun_default_<mtime>`, applies `pg_schema.sql`.
5. Bulk-loads all rows (parent-first order; `board='default'` stamped on every
   row; JSON columns cast to `::jsonb`), then resequences the four IDENTITY
   tables (`task_comments`, `task_events`, `task_runs`,
   `kanban_profile_wake_events`).
6. Runs the full verification stack:
   - **Row counts:** exact match across all 9 tables, source vs. target.
   - **Referential integrity (SQL):** no orphan `task_links`, `task_comments`,
     `task_events`, `task_runs`, `kanban_notify_subs`, `kanban_profile_event_subs`,
     `kanban_profile_wake_events`, `kanban_profile_event_claims`; no dangling
     `task_events.run_id`, `tasks.current_run_id`,
     `kanban_profile_event_claims.event_id`.
   - **Id / sequence:** per-table `max(id)` matches source; IDENTITY sequence
     `last_value` sits at global `max(id)` and `is_called=True`.
   - **Store-parity:** reads tasks via both `SqliteKanbanStore` (source) and
     `PostgresKanbanStore` (dry-run schema); asserts `get_task()` field equality
     and `list_tasks()` length match.
   - **Source health:** runs `run_board_doctor` on the source SQLite file;
     surfaces any critical issues.
7. Drops the throwaway schema (`DROP SCHEMA … CASCADE`).

**Required outcome:** the JSON line printed to stdout must contain `"ok": true`
and the process must exit 0.

```
{"ok": true, "board": "default", "counts": {...}, "count_mismatches": [], ...}
```

**NO-GO conditions:**
- Exit code 1 (verify failed) — inspect `count_mismatches`, `integrity_failures`,
  `idseq_failures`, `parity_mismatches`, `source_doctor_criticals` in the JSON.
- Exit code 2 (aborted) — inspect stderr for the error message (multi-board,
  bad data, connectivity failure, or `--force`-only guard).
- Any non-empty failure list in the JSON, even with `"ok": false`.

If the dry-run reports bad data (non-UTF-8 bytes or invalid JSON), scrub the
source using the existing utility (the `kanban.db.pre_utf8_scrub_*` backups in
`HERMES_HOME` document the prior scrub procedure) and re-run the dry-run before
proceeding.

**Do not proceed to §4 until the dry-run exits 0 with `"ok": true`.**

---

## 4. Quiesce

Stop all writers against `kanban.db`.

### 4a. Stop the gateway

```bash
hermes gateway stop
```

Wait for the process to exit. Confirm:

```bash
pgrep -fl "hermes.*gateway" || echo "no gateway process"
```

Expected: `no gateway process` (or the pgrep returns nothing).

### 4b. Confirm no kanban writers remain

```bash
lsof /Users/ctao/.hermes/kanban.db 2>/dev/null || echo "no open handles"
```

Expected: `no open handles`. If any process still holds the file open, identify
and stop it before continuing.

### 4c. Confirm WAL is checkpointed

After the gateway stops cleanly, SQLite checkpoints the WAL automatically. Verify:

```bash
ls -lh /Users/ctao/.hermes/kanban.db-wal 2>/dev/null && \
    python3 -c "
import os
sz = os.path.getsize('/Users/ctao/.hermes/kanban.db-wal')
print(f'WAL size: {sz} bytes')
if sz > 0:
    print('WARNING: WAL is not empty — checkpoint may not have completed')
else:
    print('WAL is checkpointed (0 bytes)')
" || echo "kanban.db-wal absent (fully checkpointed)"
```

Expected: either the `-wal` file is absent, or it exists and is 0 bytes.
A non-zero WAL means a writer exited uncleanly. In that case, force a checkpoint:

```bash
cd /Users/ctao/.hermes/hermes-agent
venv/bin/python -c "
import sqlite3
con = sqlite3.connect('/Users/ctao/.hermes/kanban.db')
con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
con.close()
print('checkpoint done')
"
```

Re-run the WAL size check above and confirm it is 0 or absent before proceeding.

**NO-GO if the WAL cannot be checkpointed.**

---

## 5. Final load

With the gateway quiesced and the WAL empty, run the migrator in execute mode
against the **fresh** target. The target schema (`public`) must not contain any
rows for the `default` board — the migrator's guard will abort if it does (exit
code 2). Do **not** pass `--force` on the first real load; `--force` is for
re-running into an already-populated target during rehearsal only.

```bash
cd /Users/ctao/.hermes/hermes-agent
venv/bin/python -m hermes_cli.kanban.migrate_sqlite_to_pg \
    --execute \
    --dsn "<supabase-dsn>" \
    --json
```

**What `--execute` does (in order):**

1. Reads the source read-only (same pre-flight validation as dry-run).
2. Checks whether the `default` board already has rows in the `public` schema;
   aborts (exit 2) if any exist and `--force` was not passed.
3. Opens a single transaction (autocommit off): loads all rows parent-first,
   stamps `board='default'`, casts JSON to `::jsonb`, then resequences the four
   IDENTITY tables. The transaction **COMMITs** — the data load is all-or-nothing.
4. Runs the full verification stack **post-commit**: row counts, referential
   integrity, id/sequence consistency, store-parity via `PostgresKanbanStore`,
   and source-doctor. Results are reported in the JSON output. A `"ok": false`
   here means data **was** loaded but verification found a discrepancy — investigate
   before flipping config.

**Required outcome:** `"ok": true`, exit 0.

```
{"ok": true, "board": "default", "counts": {...}, "count_mismatches": [], ...}
```

**NO-GO:** any exit code other than 0, or any non-empty failure list in the JSON.
If the load aborts (exit 2), the transaction was never committed — the target is
still empty and the source is untouched. Diagnose the error from stderr, fix it,
and re-run §5 (still without `--force`, since no rows landed).

If the load commits but verify fails (exit 1), stop. Do not flip the backend.
Investigate the mismatch, determine whether it is a data integrity issue or a
verify-logic bug, resolve it, and — if the target rows need to be cleared —
re-run §5 with `--force` (which deletes the board's rows in FK order before
re-loading).

**Do not proceed to §6 until exit 0 with `"ok": true`.**

---

## 6. Flip config to Postgres backend

The backend is selected by `kanban.backend` in `~/.hermes/config.yaml` (loaded
by `hermes_cli.config.load_config()`). The DSN is resolved from the environment
variable `HERMES_KANBAN_PG_DSN` first, then from `kanban.postgres.dsn` in
`config.yaml`.

### 6a. Set the DSN

**Option A — environment variable (preferred for secrets):**

Add to `~/.hermes/.env`:

```
HERMES_KANBAN_PG_DSN=<supabase-dsn>
```

Or export it in the shell that starts the gateway:

```bash
export HERMES_KANBAN_PG_DSN="<supabase-dsn>"
```

**Option B — config file:**

Edit `~/.hermes/config.yaml`, adding or updating the `kanban` section:

```yaml
kanban:
  postgres:
    dsn: "<supabase-dsn>"
```

### 6b. Flip the backend

Edit `~/.hermes/config.yaml`, setting (or adding):

```yaml
kanban:
  backend: postgres
  postgres:
    dsn: "<supabase-dsn>"   # omit if using HERMES_KANBAN_PG_DSN env var
```

Confirm the edit is valid YAML:

```bash
venv/bin/python -c "
import yaml
with open('/Users/ctao/.hermes/config.yaml') as f:
    cfg = yaml.safe_load(f)
print('backend:', cfg.get('kanban', {}).get('backend'))
print('dsn set:', bool((cfg.get('kanban', {}).get('postgres') or {}).get('dsn')))
"
```

Expected:

```
backend: postgres
dsn set: True
```

(If using `HERMES_KANBAN_PG_DSN`, `dsn set: False` is acceptable.)

---

## 7. Restart and verify

### 7a. Restart the gateway

```bash
hermes gateway start
```

Watch the startup logs for errors:

```bash
hermes logs --tail 50
```

Look for the absence of:
- `disk I/O error`
- `database is locked`
- `database disk image is malformed`
- `kanban backend=postgres but no DSN configured`

A line like `kanban backend: postgres` (or similar) in the startup output
confirms the Postgres backend was selected.

### 7b. Smoke check: list tasks

```bash
hermes kanban list
```

Confirm the output shows the migrated tasks (count should match the pre-cutover
SQLite board; the dry-run JSON printed per-table counts for reference).

### 7c. Smoke check: create → claim → complete a throwaway task

```bash
hermes kanban create --title "pg-cutover-smoke-test" --description "Delete me after cutover verify"
# Note the task ID printed (e.g. t_abc123)
hermes kanban show t_abc123
hermes kanban archive t_abc123
```

Confirm:
- `create` returns a new task ID.
- `show` returns the task with correct fields.
- `archive` succeeds and the task no longer appears in `hermes kanban list`.

### 7d. Smoke check: dashboard loads

Open the Hermes web dashboard in a browser. Confirm:
- The kanban board page loads without error.
- Tasks are visible and counts match expectations.
- No JS console errors related to kanban API calls.

### 7e. Watch logs for errors

```bash
hermes logs --tail 200 | grep -iE "disk I/O|corrupt|malformed|ioerr|pg_error|kanban"
```

Expected: zero matches for `disk I/O`, `corrupt`, `malformed`, `ioerr`. Any
kanban log lines should reflect normal operation (task state changes, dispatcher
ticks, notifier heartbeats).

**If all smoke checks pass, the cutover is complete.**

---

## 8. Rollback

**Valid only before Postgres takes divergent writes — this is a one-way door.**

The moment any write (task creation, status change, comment, run record) lands in
Postgres after the flip, the two stores diverge. Rolling back after that point
means discarding those Postgres writes. If writes have landed, do not rollback
silently — decide explicitly whether to discard the Postgres-side writes or
re-migrate from the current SQLite snapshot (which would overwrite the Postgres
data and lose those writes).

**If the gateway has not yet started (i.e. §7a has not completed) or no writes
have landed, rollback is clean:**

1. Edit `~/.hermes/config.yaml` and set:

```yaml
kanban:
  backend: sqlite
```

2. Restart the gateway:

```bash
hermes gateway start
```

3. Confirm the SQLite backend is active:

```bash
hermes kanban list
```

The untouched `kanban.db` resumes exactly where it left off. The Postgres data
remains in Supabase and is not deleted; it can be cleaned up later or used for
a re-attempt.

**One-way door:** once the Postgres backend has accepted divergent writes, this
rollback procedure is unsafe without first deciding what to do with those writes.
Plan the rollback window accordingly — ideally, the smoke checks in §7 are run
immediately after the flip so the rollback window is under a few minutes.

---

## 9. Decommission (Phase 6 — out of scope for now)

After the Postgres backend is proven stable in production (days to weeks), the
following SQLite life-support machinery can be retired:

- Single-writer daemon (`kanban_db.py` write-session / RemoteWriter / `OP_ALLOWLIST`
  / `snapshot_connect`)
- Corruption-recovery, IOERR / `malformed` retry paths
- Dashboard write-routing shims
- The `backend=sqlite` branch of `kanban_store()` factory (or retain for
  non-production deployments)

Also in Phase 6: begin the web-dashboard work enabled by Postgres:
- Supabase Auth / Row-Level Security for multi-user access
- Supabase Realtime (replacing the polling notifier)
- A web-accessible read/write kanban UI

**These are explicitly out of scope for Phase 5 and this runbook.**

---

## Quick reference: CLI flags and exit codes

```
venv/bin/python -m hermes_cli.kanban.migrate_sqlite_to_pg \
    (--dry-run | --execute)   # REQUIRED, mutually exclusive
    [--force]                 # --execute only: delete board rows before re-loading
    [--dsn DSN]               # Postgres DSN (else HERMES_KANBAN_PG_DSN → kanban.postgres.dsn)
    [--board SLUG]            # Board slug (else single-board guard via list_boards())
    [--json]                  # Emit JSON report to stdout
    [--report PATH]           # Also write JSON report to PATH
```

| Exit code | Meaning |
|-----------|---------|
| 0 | Success — verification passed |
| 1 | Verification failed — data loaded but verify found mismatches |
| 2 | Aborted before any write — bad args, multi-board, connectivity, bad source data, or target-guard refused |
