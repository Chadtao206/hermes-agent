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

## Step C — the merged gateway pass (6c + WS2 Task 3)

### C1 — DONE (`064239eee`, reviewed ✅, flag-off byte-for-byte unchanged, 0 new regressions)
- Module helper `_spawn_writer_daemons(board_db_paths, *, auto_recovery, keep, interval)` in
  `gateway/run.py` — dedups by resolved path, `acquire_singleton`, starts thread, `register_daemon`;
  now exception-safe per board (one bad board logs + cleans up + continues).
- `GatewayRunner.__init__`: `self._kanban_writer_daemons = []`. New `_start_kanban_writer_daemon()`
  (reads `kanban.writer_auto_recovery`/`writer_backup_keep`/`writer_backup_interval_seconds` via
  `load_config()`; enumerates `list_boards(include_archived=False)` → resolves each via
  `kanban_db_path(board=slug)`). Called immediately before the notifier watcher launches.
- `stop()/_stop_impl`: unregister-then-shutdown each daemon (best-effort), clears the list.
- Dispatcher routing: per-board dispatch closure routes `daemon.execute("dispatch_once", <same
  kwargs>)` when flag on + daemon registered; `except RuntimeError` backs off (no legacy
  corruption-streak disable); flag-off path unchanged. Tests in
  `tests/gateway/test_kanban_writer_lifecycle.py` (+ a daemon `execute("dispatch_once", dry_run=True)`
  test guarding the routed, non-allowlisted op).

### C2 — DONE & GREEN (one squashed commit; not yet reviewed)
All four pieces below are done, tested (TDD red→green), and verified regression-free:
the `tests/hermes_cli/ -k kanban` failing-set is **byte-identical pre- and post-C2** (22
pre-existing contamination failures, pass individually — confirmed by stashing the C2 source
changes and re-running). Committed as one squashed commit (per user request; see `git log` —
the C2 commit subject is "route notifier board writes through single-writer daemon + watchdog").
Still pending: full two-stage review.

- **Pure helpers (WS2 Task 3):** `_writer_auto_recovery_enabled()` (reads
  `kanban.writer_auto_recovery`) + `_classify_board_write_error(msg, *, disabled_set, db_path)`
  → `"backoff_retry"` / `"disable"` / `"other"` (never mutates the set; caller owns side effects).
  Tests: `tests/gateway/test_kanban_writer_watchdog.py`.
- **`GatewayRunner._board_write(board, op, **kwargs)`** — daemon-vs-direct router mirroring C1.
  The 7 notifier write helpers (`_kanban_advance`/`_unsub`/`_rewind`, `_kanban_profile_advance`/
  `_rewind`/`_record_success`/`_record_failure`) now route through it; the two `record_*` keep
  their legacy-fallback by picking the op name then routing. Flag off → direct write (unchanged).
- **`_collect` (in `_kanban_notifier_watcher`):** per board, `board_daemon = lookup_daemon(...)`
  when flag on; conn opened `readonly=True` when a daemon owns the board (reads only), and both
  `claim_unseen_events_for_sub` + `claim_unseen_events_for_profile_sub` read-modify-writes route
  through `board_daemon.execute(...)`. Corruption-confirm funnel
  (`_record_notifier_corruption_confirmation`) backs off (no hard-disable, no streak mutation)
  when `single_writer_enabled()` + classifier says `backoff_retry`. Flag off = byte-for-byte.
- **Watchdog (WS2 Task 3):** `WriterDaemon` gains `is_alive()`, `wait_until_serving()`
  (+ `_writer_ready` Event, set in `_writer_loop`), and `restart_writer_thread()` (in-place
  revival — no socket/singleton churn). `_spawn_writer_daemons` now waits for readiness before
  registering (closes the startup race / duplicate-writer window). `GatewayRunner` gains
  `_writer_watchdog_tick()` (revive dead thread; alert-once via `_emit_writer_daemon_alert` when
  `health()["disabled"]`; re-arms alert when the board recovers) + the async
  `_kanban_writer_watchdog()` loop, started right after `_start_kanban_writer_daemon()`.
  Config read factored into `_kanban_writer_recovery_cfg()`. Tests:
  `tests/gateway/test_kanban_writer_watchdog_restart.py`,
  `tests/gateway/test_kanban_notifier_single_writer.py`.

Grep check done: no writable `_kb.connect(board=` remains on any notifier write path under the
flag — every cursor/claim/wake write routes through the daemon; only `readonly=True` reads stay
on a direct conn.

#### Original scope notes (for reference)
**Key finding (de-risks C2):** the notifier HEARTBEAT — the RCA's corrupted table — is ALREADY
isolated into a sidecar (`kanban_notifier_heartbeats.db` via `hermes_cli/kanban_notifier_sidecar.py`,
which uses its OWN `sqlite3.connect`, NOT `kb.connect`). `record_notifier_heartbeat(conn, ...)`
IGNORES `conn` ("must not mutate the main board DB") and swallows errors. So the heartbeat write is
guard-exempt and needs NO change. The board single-writer daemon + this existing sidecar split
together cover the RCA. (So the deferred "churn isolation" is effectively already done for heartbeats.)

C2 = route the notifier's remaining BOARD-DB writes through the daemon + backoff/watchdog. Under
`single_writer_enabled()`:
1. **Main watcher `_kanban_notifier_watcher` (~`:5407`):** change `conn = _kb.connect(board=slug)`
   (`:5624`) to `readonly=True` for reads (`list_notify_subs`, `get_task`). Route the read-modify-write
   cursor claims through the daemon: `claim_unseen_events_for_sub` (`:5716`) and
   `claim_unseen_events_for_profile_sub` (`:5766`) → `daemon.execute("<op>", task_id=..., ...)`
   (returns `(old_cursor, cursor, events)`). Reads after the claim use the readonly conn (a tiny
   snapshot-lag is fine; next tick catches up).
2. **Notifier helper methods (~`:6237`–`:6390`)** each open `conn = _kb.connect(board=board)` (writable)
   and do a board write — these ALL trip the guard under the flag and must route through the daemon:
   - `advance_notify_cursor` (`:6239`), `advance_profile_event_cursor` (`:6299`, `:6351`),
     `record_profile_wake_success` (`:6349`), `record_profile_wake_failure` (`:6390`), plus the
     `connect(board=board)` opens at `:6252`, `:6273`, `:6318`, `:6388` (check each: read → readonly;
     write → `daemon.execute`/`write_session`).
   - Pattern (repeat ~8×): flag on + `lookup_daemon(kanban_db_path(board=board))` → route the write
     via `daemon.execute("<write_fn>", **kwargs)`; reads → `connect(readonly=True)`; flag off →
     unchanged. Consider a small private helper on GatewayRunner, e.g.
     `_board_write(board, op, **kwargs)` that picks daemon-vs-direct, to avoid repeating the branch.
3. **Backoff instead of hard-disable + watchdog (WS2 Task 3):** when `writer_auto_recovery` on, the
   notifier board-conn corruption path (`notifier_disabled_db_paths` ~`:5545`; confirm-streak
   ~`:5639`) should back off + retry (the daemon owns recovery on its writable conn; the notifier's
   board conn is now READONLY so it only reads). Add a watchdog (alongside the daemon threads) that
   restarts a dead writer-daemon thread and emits a high-severity alert ONLY when
   `daemon.health()["disabled"]` (recovery exhausted). Gate on flags; flag-off = today exactly.
4. Note: `execute()` allows any callable `kb` fn (not just OP_ALLOWLIST) — the in-process notifier
   writes (claims, cursor advances, wake records) are trusted, so they route fine.

Full two-stage review. Watch: don't change flag-off behavior; the readonly board conn must still
detect corruption for the (backoff) path; verify no notifier write path was missed (grep
`_kb.connect(board=` in the notifier region after editing — every writable one must be gone under flag).

## WS1 Task 7 — DONE & GREEN (one squashed commit; not yet reviewed)
Migrated every `tools/kanban_tools.py` write handler to `kb.write_session` (create, complete,
block, unblock, link, comment, heartbeat + the `_auto_heartbeat` bridge); reads — including
read-after-write (`latest_run`/`get_task`) — stay on a direct conn, and `_connect` now opens
`readonly=True` under the flag so client read handlers don't trip `DirectWriteForbidden`. Added
`add_comment` + `heartbeat_claim` to `OP_ALLOWLIST` (they cross the wire from workers). Verified
regression-free: `tests/hermes_cli/ -k kanban` failing-set unchanged (22 pre-existing
contamination failures, identical to the C2 baseline).

**Sub-feature (needed for fidelity, not in the original plan):** typed write-gate exceptions now
survive the single-writer boundary. `complete_task`'s `PRHeadGateError`/`HallucinatedCardsError`
(and `ExternalHandoffGateError`) would otherwise flatten to `RuntimeError`/`RemoteWriteError`,
breaking `_handle_complete`'s `except kb.<Error>` guidance. Added `kb.serialize_kanban_error()` /
`reconstruct_kanban_error()` + a registry; `Response.error_payload` on the wire; the daemon stores
the original exc + payload, `execute()` re-raises the typed object in-process (RuntimeError for
non-gate errors, preserving the dispatcher's backoff contract), and `RemoteWriter` reconstructs the
typed exception client-side. So `except kb.PRHeadGateError` works on every transport.
Tests: `test_kanban_error_serialization.py`, `test_kanban_writer_typed_errors.py`,
`test_kanban_tools_write_session.py`.

## WS1 Task 8 — DONE & GREEN (one squashed commit; not yet reviewed)
`writer_socket_path()` now honors a `HERMES_KANBAN_WRITER_SOCK` override (highest precedence,
mirroring `HERMES_KANBAN_DB`); new `_writer_client_env(board)` returns that pin under the flag
(empty when off); `_default_spawn` injects it (`env.update(_writer_client_env(board=board))`).
So a worker's `write_session` → `RemoteWriter` connects to exactly the daemon the dispatcher knows
serves this board, immune to path-resolution drift. **Confirmed + resolved the STATUS open
question:** the old `HERMES_KANBAN_WRITER_OWNER` env is obsolete — nothing reads it; "workers are
never owners" is enforced structurally by the writer-thread-local guard (a worker has no registered
in-process daemon and isn't the writer thread → `write_session` picks RemoteWriter; a stray
writable `connect()` is refused by the Task-4 guard). No owner env is set. Flag off = unchanged.
Verified regression-free (`-k kanban` failing-set unchanged at 22). Tests: `test_kanban_spawn_env.py`.

## WS1 Task 9 — DONE & GREEN (one squashed commit; not yet reviewed) → **WS1 COMPLETE**
WS1 acceptance gate proven: `tests/hermes_cli/test_kanban_writer_integration.py` spins up the
daemon, hammers it with 6 concurrent `RemoteWriter` clients (+ a 7th client process SIGKILL'd
mid-write), and asserts the board still passes `PRAGMA integrity_check` with all committed rows
intact — the structural cure (no client holds a writable handle; only the daemon's one writer
thread writes) holds. Flag docs added to `cli-config.yaml.example` (new `kanban:` section). The
repo has no tracked `config.yaml` (that's the user's live file — left untouched). Verified
regression-free (`-k kanban` failing-set unchanged at 22).

**WS1 (single-writer daemon) is now end-to-end complete:** protocol, daemon, client, guard,
lifecycle, gateway start/stop + dispatcher routing (C1), notifier routing + watchdog (C2),
tool-handler migration + typed-error preservation (Task 7), worker spawn env (Task 8), and the
kill-during-write acceptance proof (Task 9).

## WS2 Task 4 — effectively DONE (folded into Task 9's config edit + earlier wiring)
The `kanban:` section in `cli-config.yaml.example` now documents `writer_auto_recovery`,
`writer_backup_interval_seconds`, and `writer_backup_keep`. The gateway recovery wiring is already
gated on `writer_auto_recovery` (`_kanban_writer_recovery_cfg()` → `_spawn_writer_daemons(...,
auto_recovery=...)` enables recovery only when true; `_writer_auto_recovery_enabled()` gates the
notifier backoff). No separate work remains for WS2 Task 4.

## WS4 — DONE & GREEN (one squashed commit; not yet reviewed)
Scheduled-park silent-stall fixed. `active_pr_guard_holds(conn, *, task_id, assignee)` extracted
from `check_respawn_guard` so park + un-park share one predicate; the dispatcher park site now
stamps a structured `respawn_guard='active_pr'` marker on the `scheduled` event payload.
`promote_cleared_scheduled(conn)` un-parks scheduled/active_pr tasks back to `ready` once the PR
comment ages out of the guard window (re-evaluating the same predicate), targeting ONLY tasks with
that marker — time-based/operator `schedule_task` parks are untouched. Wired into `dispatch_once`
(before `recompute_ready`, skipped on `dry_run`) behind `kanban.promote_scheduled_on_guard_clear`
(default false → unchanged); flag documented in `cli-config.yaml.example`. Verified regression-free
(`-k kanban` failing-set unchanged at 22). Tests: `test_kanban_active_pr_guard.py`,
`test_kanban_promote_scheduled.py`, `test_kanban_dispatch_promotes_scheduled.py`.

## REMAINING
- **WS5** — `kanban_reconcile` agent tool. **WS6** — board-liveness SLO. (Plans: `05-`, `06-`.)
- Cross-cutting: a full two-stage review of the WS1 + C2 + WS4 work (none of these commits is reviewed yet).

## Known non-issues
- The broad kanban/gateway suite shows ~34 pre-existing cross-test contamination failures that are
  IDENTICAL on clean `main` (verified) — pass individually. Not regressions. Compare failing
  node-ids vs `main` before attributing any failure to this branch.
