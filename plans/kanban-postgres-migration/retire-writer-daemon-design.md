# Retire the sqlite single-writer daemon under Postgres — design (Phase 6 / B3)

- **Date:** 2026-05-31
- **Status:** Approved design; implementation plan to follow.
- **Context:** Phase 6 sub-project B3. The kanban board is live on Postgres
  (`kanban.backend=postgres`). The gateway still starts the sqlite **single-writer
  daemon** at startup (`gateway/run.py:_start_kanban_writer_daemon`, called ~5129),
  gated only on `single_writer_enabled()` — which checks the
  `kanban.single_writer_daemon` **config flag, not the backend**. So under Postgres
  the daemon still starts, **opens the frozen `<HERMES_HOME>/kanban.db` read-write,
  activates WAL** (a file handle + `-wal`/`-shm` sidecars on the frozen file), and
  holds a thread + AF_UNIX socket + watchdog loop — yet **never receives a write**
  (the dispatcher's PG branch and the notifier both route through the store/glue;
  the dispatcher's daemon branch is sqlite-only). Pure dead weight that also touches
  the frozen archive file.

## Goal

Under `kanban.backend=postgres`, the gateway does **not** start the sqlite
single-writer daemon (nor its watchdog). Under `sqlite` the behavior is
**byte-identical** to today. `kanban_db.py` and `kanban_writer_daemon.py`
(upstream) are **not edited**.

## Audit — what routes to the daemon under PG (so stopping it is safe)

- **Dispatcher:** `gateway/run.py` ~6960 — under `resolve_backend()=="postgres"`
  routes through `PostgresKanbanStore` + `kanban_glue.run_dispatch_tick`; the
  daemon branch (~7032) only runs under sqlite. ✓
- **Notifier:** `_kanban_store(board)` + `kanban_glue.run_notifier_tick(store, …)`
  (store owns conn/daemon routing — PG under PG); heartbeats via the
  backend-agnostic notifier **sidecar** (`record_notifier_heartbeat`, separate DB). ✓
- **`_board_write()`** (`gateway/run.py:6347`): **dead code — zero callers
  repo-wide** (only the def + the unrelated `_classify_board_write_error`/
  `_board_write_error_is_corruption` helpers share the substring). It would route to
  the daemon if called, but nothing calls it. Left as-is (removal is out of scope).
- No code calls `write_session()` / `connect(readonly=False)` under PG at runtime
  (the sqlite store isn't instantiated under PG; the PG store uses `pg_pool`). So
  stopping the daemon while `single_writer_enabled()` stays `True` is safe — nothing
  hits the `DirectWriteForbidden` guard.

## Architecture — one fork-owned gate, two call sites

Both start methods currently early-return on `not _kb.single_writer_enabled()`.
Add the backend to that condition via a single helper so the rule lives in one place:

```python
def _writer_daemon_should_run(self) -> bool:
    """The sqlite single-writer daemon serves no purpose under Postgres
    (writes route through the store), and would needlessly open the frozen
    kanban.db + WAL. Run it only for the sqlite backend with the flag on."""
    import hermes_cli.kanban_db as _kb
    try:
        from hermes_cli.kanban.store import resolve_backend
        if resolve_backend() == "postgres":
            return False
    except Exception:
        pass  # resolution failure → fall back to flag-only (sqlite default)
    return _kb.single_writer_enabled()
```

- **`_start_kanban_writer_daemon()`** (~5582): replace
  `if not _kb.single_writer_enabled(): return` with
  `if not self._writer_daemon_should_run(): return` (+ a one-line info log when
  skipping under postgres). → under PG no daemon spawns; `self._kanban_writer_daemons`
  stays empty; the frozen DB is never opened.
- **`_kanban_writer_watchdog()`** (~5668): replace its
  `if not _kb.single_writer_enabled(): return` with the same
  `if not self._writer_daemon_should_run(): return`. → the 10 s health-poll loop
  doesn't spin under PG.

Everything else (the daemon module, `single_writer_enabled()`, the guard, the
dispatcher's sqlite branch, `write_session`, the corruption/recovery machinery)
is untouched — dormant under PG, intact for sqlite deployments.

## Components touched

| File | Change |
|---|---|
| `gateway/run.py` | add `_writer_daemon_should_run()` helper; use it in `_start_kanban_writer_daemon` + `_kanban_writer_watchdog` (replacing the flag-only guard) |
| `hermes_cli/kanban_db.py` | **none** (upstream) |
| `hermes_cli/kanban_writer_daemon.py` | **none** (upstream) |

## Testing

- **New** `tests/gateway/test_kanban_writer_daemon_postgres.py` (no docker needed —
  monkeypatch `hermes_cli.kanban.store.resolve_backend` → `"postgres"`):
  - `_start_kanban_writer_daemon()` under PG is a no-op: no daemons registered
    (`self._kanban_writer_daemons` empty/unset), `_spawn_writer_daemons` not called
    (assert via monkeypatch/spy), and the frozen `kanban.db` is not opened
    read-write (no `.kanban-writer.sock` created in a tmp board dir).
  - `_writer_daemon_should_run()` returns `False` under postgres even with
    `single_writer_enabled()` True; returns `True` under sqlite with the flag on;
    `False` under sqlite with the flag off.
  - The watchdog loop body no-ops under PG (returns immediately).
- **Regression (byte-identical sqlite):** the existing
  `tests/gateway/test_kanban_writer_lifecycle.py`,
  `test_kanban_writer_watchdog.py`, `test_kanban_writer_watchdog_restart.py`,
  `test_kanban_notifier_single_writer.py` stay green (sqlite path unchanged — these
  run with the default sqlite backend, so `_writer_daemon_should_run()` ==
  `single_writer_enabled()`).
- **Boundaries:** `git diff main -- hermes_cli/kanban_db.py hermes_cli/kanban_writer_daemon.py`
  empty; default backend sqlite.
- Interpreter: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`.

## Risks & mitigations

- **The existing tests assume the flag drives startup** → they run under the default
  sqlite backend, where `_writer_daemon_should_run()` reduces to
  `single_writer_enabled()`, so they're unaffected. Confirm by running them.
- **`resolve_backend()` raising inside the gateway** → the helper catches and falls
  back to the flag-only behavior (sqlite default), so a config hiccup can never
  wedge the gateway by mis-gating.
- **Live gateway is the orchestrator** → the change activates only on a gateway
  restart; until then the idle daemon keeps running harmlessly. The restart is the
  one operationally-significant step, done with explicit human sign-off + post-restart
  verification.

## Operational rollout (human-in-the-loop)

After merge to main + push to chad: **restart the live launchd gateway**
(`ai.hermes.gateway`) and verify on the new process:
- no writer-daemon thread / no `<HERMES_HOME>/.kanban-writer.sock`;
- no fresh `-wal`/`-shm` sidecars created on the frozen `kanban.db`;
- the gateway is healthy — dispatcher spawns + notifier deliver via Postgres (a
  startup log without writer-daemon lines; `hermes kanban` still works; no errors).
- Rollback: revert is a config/code flip; the daemon is recreated on the next
  restart if needed (no data migration involved).

## Success criteria

- Under PG, a restarted gateway runs **no** sqlite writer daemon/watchdog and does
  not touch the frozen `kanban.db`; dispatch + notify continue via Postgres.
- sqlite deployments unchanged (byte-identical; existing writer tests green).
- `kanban_db.py` / `kanban_writer_daemon.py` unedited.
