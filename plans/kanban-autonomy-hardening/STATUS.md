# Execution Status — kanban-autonomy-hardening

Branch: `feat/kanban-autonomy-hardening` (worktree
`/Users/ctao/.hermes/hermes-agent-wt-kanban-autonomy`). Base: `main` @ `ec2bd4ff4`.

Test cmd: `cd <worktree> && PYTHONPATH="$PWD" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest <path> -q`

## Decisions made during execution
- **#1 DB direction:** single-writer daemon, keep SQLite (chosen).
- **#3 review lane:** delete the dead lane, keep reviewer convention (chosen, done).
- **Enforcement model upgraded** from the plan's process-`owner` env to a **writer-thread-local**
  guard + an **in-process daemon registry**, so the gateway's own writers (dispatcher/notifier)
  also route through the single writer thread → a TRUE single writer (not just worker-writes).
- **6c + WS2 merged** into one gateway/notifier pass (chosen) to avoid editing the merge-hotspot
  notifier method twice.

## DONE & verified (all committed, reviewed, green)
- **WS3** — dead `review` status lane removed; reviewer PR-head convention intact;
  regression-free vs clean main. (`3e28b256d`, `0e41bb635`)
- **WS1 Tasks 1–5** — wire protocol; single-writer daemon (writer-thread + queue);
  RemoteWriter client + `write_session` façade; `connect()` guard; singleton flock +
  `hermes kanban db-daemon` CLI. (`f5dd98f29`, `8daa8ee4c`, `046bfda79`, `7a1d74c30`, `76603c748`)
- **WS1 Task 6ab** — true-single-writer core: `in_writer_thread()` thread-local guard;
  in-process daemon registry (`register_daemon`/`lookup_daemon`/`unregister_daemon`);
  `WriterDaemon.execute()` (in-process, raw result, validates op + writer liveness);
  registry-aware `write_session` (in-process → `_DaemonWriter`; client → `RemoteWriter`;
  flag-off/re-entrant → direct `_LocalWriter`). (`3dfcd2c9f`)
- **WS2 Task 1** — `kanban_recovery.py`: `is_corruption_signal`, bounded `recover_board`
  ladder (checkpoint → `.recover` → restore-from-backup → exhausted), `make_online_backup`. (`3327d062d`)
- **WS2 Task 2** — daemon self-heals on corruption signal (close → recover → reopen → retry once),
  disables + `health()` on exhaustion, periodic online backup; recovery-OFF path unchanged. (`41c3074f5`)

Flags (default false; behavior unchanged when off): `kanban.single_writer_daemon` is read by
`kb.single_writer_enabled()`. (Auto-recovery currently enabled via `daemon.enable_auto_recovery()`;
config flag `kanban.writer_auto_recovery` + backup interval/keep to be wired in Step C/WS2 Task 4.)

## NEXT: Step C — the merged gateway pass (6c + WS2 Task 3) — NOT STARTED
This is the large, risky surgery on `gateway/run.py` (merge hotspot). It must:
1. **Daemon lifecycle in gateway** (behind `single_writer_enabled()`): for each board
   (`kb.list_boards(include_archived=False)`, dedup by resolved DB path) create a `WriterDaemon`,
   `enable_auto_recovery(backup_dir=<board dir>, keep, interval)`, `acquire_singleton()`,
   start `serve_forever` on a daemon thread, `register_daemon(db_path, daemon)`. Track in
   `self._kanban_writer_daemons`. On `stop()` (gateway/run.py:7476): `unregister_daemon(...)` in a
   `finally` (avoid stale-registry hang) + `daemon.shutdown()`. Start BEFORE the dispatcher/notifier
   watchers (launched at `gateway/run.py:4906`/`4912`).
2. **Dispatcher routing:** `_kanban_dispatcher_watcher` (`:6667`) currently runs
   `kb.dispatch_once(conn, spawn_fn=..., ...)` inside `asyncio.to_thread`. When flag on, route via
   `await asyncio.to_thread(daemon.execute, "dispatch_once", spawn_fn=..., <kwargs>)`
   (`dispatch_once(conn, *, spawn_fn, ttl_seconds, dry_run, max_spawn, max_in_progress,
   failure_limit, stale_timeout_seconds, board, default_assignee, max_in_progress_per_profile)`).
   Reads elsewhere stay on a read-only conn. `dispatch_once` doesn't call `write_session` internally,
   so no re-entrancy. spawn_fn is an in-process callable — fine over the queue.
3. **Notifier restructure (the hard part):** `_kanban_notifier_watcher` (`:5321`) currently opens a
   WRITABLE `_kb.connect(board=slug)` (`~:5538`) and does reads + writes on it. Under the flag that
   writable connect now RAISES `DirectWriteForbidden`. Restructure: open `connect(board=slug,
   readonly=True)` for reads; route every write through the daemon — `record_notifier_heartbeat`,
   the read-modify-write cursor claims (`claim_unseen_events_for_sub` /
   `claim_unseen_events_for_profile_sub`), and `record_profile_wake_*` — via
   `daemon.execute("<op>", ...)` (or `kb.write_session(board=slug)`). Add these op names to
   in-process use (they're trusted; `execute` allows any callable kb fn, not just OP_ALLOWLIST).
4. **Backoff instead of hard-disable + watchdog (WS2 Task 3):** the notifier/dispatcher
   corruption-confirmation hard-disable (`notifier_disabled_db_paths` ~`:5452`; dispatcher
   ~`:6989`) should, when `kanban.writer_auto_recovery` is on, back off and retry (trust the daemon
   to heal) instead of permanently disabling. Add a watchdog that restarts a dead writer-daemon
   thread and emits a high-severity alert only when `daemon.health()["disabled"]` (recovery
   exhausted). Gate all of this on the flags; flag-off = today's behavior exactly.

Suggested split: **C1** = lifecycle + dispatcher routing (more localized); **C2** = notifier
restructure + backoff/watchdog (larger/riskier). Full two-stage review on both.

## REMAINING after Step C
- **WS1 Task 7** — migrate worker-side tool write handlers in `tools/kanban_tools.py` to
  `kb.write_session` (so workers RPC the daemon). Plan: `plans/.../01-single-writer-daemon.md` Task 7.
- **WS1 Task 8** — worker spawn env: set `HERMES_KANBAN_WRITER_SOCK`; ensure workers are never
  owner/writer-thread. Plan Task 8. (Note: enforcement is now thread-local, not env-owner — workers
  are clients because they have no registered daemon + aren't the writer thread → `write_session`
  picks RemoteWriter. Confirm the socket path env is still useful or drop it.)
- **WS1 Task 9** — integration proof (killed client can't corrupt) + `config.yaml` flag docs.
- **WS2 Task 4** — `config.yaml` flags: `writer_auto_recovery`, `writer_backup_interval_seconds`,
  `writer_backup_keep`; gate the gateway recovery wiring on `writer_auto_recovery`.
- **WS4** — scheduled-park stall fix. **WS5** — `kanban_reconcile` agent tool. **WS6** — board-liveness SLO.
  (Plans: `04-`, `05-`, `06-`.)

## Known non-issues
- The broad kanban/gateway suite shows ~34 pre-existing cross-test contamination failures that are
  IDENTICAL on clean `main` (verified) — pass individually. Not regressions. Compare failing
  node-ids vs `main` before attributing any failure to this branch.
