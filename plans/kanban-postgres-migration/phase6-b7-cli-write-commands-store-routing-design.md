# Phase 6 · B7 — migrate Tier-1 sqlite-coupled CLI write commands to the store (design)

**Status:** approved (design phase). Branch `feat/kanban-pg-phase6-b7` (worktree `.worktrees/kanban-pg-phase6-b7`), off `main` `65aa9ff71`.
**Origin:** a diagnosis (this session) of the operator-reported error `writable kanban.connect() called outside the single-writer daemon thread while kanban.single_writer_daemon is enabled; use kb.write_session() instead`, which traced to a class of CLI WRITE commands that bypass the backend-aware store. See [[kanban-pg-phase6]].

## Problem

Several `hermes kanban` WRITE subcommands open a **writable** connection directly — `with kb.connect() as conn:` or `with kb.connect_closing() as conn:` (the latter calls `connect()` with no `readonly=True`, so it is also writable) — and call `kb.<write>(conn, …)` instead of routing through the backend-aware store (`_make_store()`). Under the live config (`kanban.single_writer_daemon: true` + `kanban.backend: postgres`) this breaks two ways:
1. **`DirectWriteForbidden`** — `single_writer_enabled()` (kanban_db.py:1913) is backend-independent and returns `True` from the config flag, so the guard at kanban_db.py:1769 refuses any direct writable `connect()` outside the daemon thread. (Under PG the daemon isn't even running — B3 — so `write_session()` wouldn't help either; the correct path is the store.)
2. **Wrong DB** — `kb.connect()` is sqlite, so absent the guard these commands would write the **frozen** `~/.hermes/kanban.db`, not live Postgres.

Reproduced live: `hermes kanban wake-arm <task>` and `hermes kanban claim <task>` both raise the guard error. The full broken set (audit): Tier-1 trivial `wake-arm`/`profile-subs add`/`claim`/`notify subscribe`; Tier-2 `swarm`; Tier-3 `dispatch`; Tier-4 `archive --rm`. (`specify`/`decompose` are NOT affected — B1 made them backend-aware. The autonomous gateway dispatcher, workers, and dashboard all route through the store and are fine.)

## Goal (B7 = Tier 1 only)

Migrate the four Tier-1 commands — `wake-arm`, `profile-subs add`, `claim`, `notify subscribe` — to route through `_make_store()`, mirroring the already-migrated `profile-subs remove` / `notify unsubscribe` / `comment`. This unblocks operator wake-arming + claims on the live PG board. Tier 2–4 are deferred (documented below).

## Scope decision (settled)

**Tier 1 only** (user decision). The four Tier-1 commands need only store methods that already exist on the `KanbanStore` Protocol + `PostgresKanbanStore`. Tier 2 (`swarm` — refactor `kanban_swarm.create_swarm(conn)` to take a store), Tier 3 (`dispatch` — `kb.dispatch_once` vs `store.dispatch_plan` interface mismatch; the gateway auto-dispatch path is already store-routed and fine), and Tier 4 (`archive --rm` — needs a new `delete_archived_task` store method; destructive) are deferred as tracked follow-ups.

## Key distinction from B1/B2/B6 (NOT sqlite-byte-identical — it's a FIX)

B1/B2/B6 preserved the sqlite path byte-identical. B7 does **not**: the current direct `kb.connect()` is itself broken under sqlite+single_writer (same guard), so we are *fixing* the sqlite path, not preserving it. Routing through `_make_store()` corrects **both** backends:
- **postgres** → `PostgresKanbanStore` → live PG.
- **sqlite + single_writer** → `SqliteKanbanStore._write` → `kb.write_session()` (the daemon) — the *correct* single-writer path (vs the current guard-tripping direct connect).
- **sqlite without single_writer** (upstream default) → `write_session()` opens a local writable conn — behaviorally ≈ the old `kb.connect()`, so no regression for default deployments.

`kanban_db.py`, `kanban_liveness.py`, `kanban_writer_daemon.py`: import-only, zero edits. Default backend stays sqlite in code/tests.

## Per-command migration (all store methods already exist)

Pattern (mirror `_cmd_profile_subs_remove` cli.py:3388 / `_cmd_comment` cli.py:2506):
```python
    store = _make_store()
    try:
        ...  # store.<method>(...)
    finally:
        store.close()
```

### `_cmd_claim` (cli.py:2474)
Replace `with kb.connect_closing() as conn:` body:
- `kb.claim_task(conn, args.task_id, ttl_seconds=args.ttl)` → `store.claim_task(args.task_id, ttl_seconds=args.ttl)`.
- on None: `kb.get_task(conn, args.task_id)` → `store.get_task(args.task_id)` (for the status/lock reason message; uses `existing.status` / `existing.claim_lock`).
- `kb.resolve_workspace(task)` → **unchanged** (pure helper on a Task; not a store method).
- `kb.set_workspace_path(conn, task.id, str(workspace))` → `store.set_workspace_path(task.id, str(workspace))`.
Print lines unchanged. (claim + set_workspace_path were each their own write_txn on the shared conn → two store calls, two txns — same granularity, no atomicity regression.)

### `_cmd_notify_subscribe` (cli.py:3138)
- `kb.get_task(conn, args.task_id)` existence check → `store.get_task(args.task_id)`.
- `kb.add_notify_sub(conn, task_id=…, platform=…, chat_id=…, thread_id=…, user_id=…, notifier_profile=…)` → `store.add_notify_sub(task_id=…, platform=…, chat_id=…, thread_id=…, user_id=…, notifier_profile=…)`.
Print line unchanged.

### `_cmd_profile_subs_add` (cli.py:3334)
- `kb.get_task` → `store.get_task`.
- the `existed` check `kb.list_profile_event_subs(conn, task_id=…, profile=…, enabled_only=False)` → `store.list_profile_event_subs(task_id=…, profile=…, enabled_only=False)`.
- `kb.add_profile_event_sub(conn, **add_kwargs)` → `store.add_profile_event_sub(**add_kwargs)`.
- the `stored` readback `kb.list_profile_event_subs(...)` → `store.list_profile_event_subs(...)`.
`add_kwargs` construction + the JSON/text output unchanged.

### `_cmd_kanban_wake_arm` (cli.py:3428)
- The `HERMES_KANBAN_EVENT_WAKE == "1"` refusal guard + `kb.list_profiles_on_disk()` profile-existence check → **unchanged** (pure / no conn).
- the `with kb.connect() as conn:` block → store: `store.get_task`, `store.list_profile_event_subs` (existed + stored readback), `store.add_profile_event_sub(task_id=…, profile=…, name=…, event_kinds=…, include_children=True, wake_agent=True, wake_prompt=…, enabled=True)`.
Output unchanged.

**Store methods used** (all present): `claim_task(task_id, *, ttl_seconds=, claimer=)`, `get_task(task_id)`, `set_workspace_path(task_id, path)`, `add_notify_sub(**kwargs)`, `list_profile_event_subs(**kwargs)`, `add_profile_event_sub(**kwargs)`. Confirm the dict shape `store.list_profile_event_subs` returns matches the `.get("name")` access (the sqlite path returns dicts; the migrated `notify list` already relies on the store returning dicts).

## Testing

Test interpreter: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`; docker `postgres:16-alpine` via `HERMES_PG_TEST_DSN`; never the live Supabase DB.

- **PG functional + no-direct-connect regression** (`tests/hermes_cli/kanban/test_cli_write_commands_pg.py`, new): for each of the four commands, with the backend forced to postgres (pg fixture: monkeypatch `pg_pool.get_pool` to the fixture pool, `hermes_cli.kanban.store.resolve_backend`→`"postgres"`, `kanban_db.get_current_board`→test board), build an `argparse.Namespace` and call the `_cmd_*` handler. Assert: (a) it returns 0 and the sub/claim is created **in the PG store** (e.g. `store.list_profile_event_subs(...)` shows it; `store.get_task(tid).status == "running"` for claim); (b) it raises **no** `DirectWriteForbidden`. For the headline regression, monkeypatch `kanban_db.connect` to raise `AssertionError("direct connect")` for the duration of the PG-backend call and assert the command STILL succeeds (proves it never opens a direct sqlite connection under PG).
- **sqlite functional** (cross-backend via the existing `store` fixture where practical, or a sqlite-backed CLI test): the command still works on the sqlite backend (sub/claim created) — no regression for the default path.
- Existing CLI tests for these commands stay green (adjust any that asserted the old `kb.connect()` usage).

## Review

Spec-compliance + code-quality review per command. Extra care (adversarial-lite) on `_cmd_claim` (the None-reason branch + workspace resolution + the two-write sequence). These are CLI handlers (not the gateway/plugin_api/store_postgres adversarial-list live-core), so standard two-stage review suffices; escalate `claim` to adversarial if its migration looks non-trivial.

## Constraints / guarantees
- `kanban_db.py`/`kanban_liveness.py`/`kanban_writer_daemon.py`: import-only.
- No DSN/secret in logs (these commands log nothing new; errors are existing messages).
- Default backend stays sqlite in code/tests.
- Output text + exit codes of each command unchanged.

## File inventory
- Edit: `hermes_cli/kanban/cli.py` (`_cmd_claim`, `_cmd_notify_subscribe`, `_cmd_profile_subs_add`, `_cmd_kanban_wake_arm`).
- Test: `tests/hermes_cli/kanban/test_cli_write_commands_pg.py` (new).

## Out of scope (deferred follow-ups)
- **Tier 2** — `swarm`: refactor `kanban_swarm.create_swarm(conn, …)` to take a store and route create_task/complete_task/add_comment through it.
- **Tier 3** — `dispatch` (manual CLI tick): reconcile `kb.dispatch_once` vs `store.dispatch_plan` (the CLI command is read-after-reclaim/promote and doesn't spawn; likely wraps `dispatch_plan().result`). The gateway's auto-dispatch is already store-routed.
- **Tier 4** — `archive --rm`: add a `delete_archived_task` method to the Protocol + PostgresKanbanStore (cascade delete across task_links/comments/events/runs/notify_subs), then migrate. Destructive → human sign-off.
- B4 (Supabase Auth/RLS/Realtime + live dashboard), B5 (frozen kanban.db fate) — separate Phase-6 items.
