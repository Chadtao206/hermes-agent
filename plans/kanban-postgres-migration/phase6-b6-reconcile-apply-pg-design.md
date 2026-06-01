# Phase 6 · B6 — reconcile-apply: port acks to PG, guard mutations (design)

**Status:** approved (design phase). Branch `feat/kanban-pg-phase6-b6` (worktree `.worktrees/kanban-pg-phase6-b6`), off `main` `409f60b47`.
**Predecessor:** [[kanban-pg-phase6]] B1 (`phase6-b1-specify-decompose-reconcile{-design,}.md`) — B1's final holistic review surfaced this footgun; B6 closes it. Reuses B1's `_collect_reconcile_actions_pg`, `_reconcile_decision_comment_matches`, `_run_reconciler_pg`/`run_reconciler` PG path, and the `_filter_acknowledged_decision_packets_pg` suppression read.

## Problem

`hermes_cli/kanban_reconciler.py::apply_reconcile_decision` (the `hermes kanban reconcile --apply-option <opt> …` WRITE path, CLI-only — `cli.py::_cmd_reconcile`) has **no backend dispatch**. Under `kanban.backend=postgres` it opens `kb.connect(kanban_db_path())` and mutates the **frozen** `~/.hermes/kanban.db`, then reports `ok=True`. Two harms:
1. **Silent footgun:** an operator running `--apply-option unblock/close/reclaim_*/…` silently mutates the frozen sqlite snapshot while the live PG board is untouched, and gets a success message.
2. **Broken ack loop:** the `keep_parked`/`keep_blocked` acks write their idempotency comment to frozen sqlite, but B1's live PG reconciler reads acks from **PG** comments (`_filter_acknowledged_decision_packets_pg` → `store.list_comments` + `_reconcile_decision_comment_matches`). So acks never suppress anything on PG — the operator can't quiet a parked decision; it re-wakes forever.

B1 made the reconcile **read** live (so the *fresh-reconcile-pass* signature validation inside apply now runs against live PG), which makes the apply/write mismatch more reachable and more confusing (validates against live PG, writes frozen sqlite).

## Goal

Under `backend=postgres`: (1) apply `keep_parked`/`keep_blocked` acks to **live PG** (write the idempotency comment via the backend-aware store, closing B1's suppression loop), and (2) **hard-guard** every other (mutation) option with a clear, loud error instead of a silent frozen-sqlite mutation. sqlite path byte-identical; CLI-only (no dashboard apply endpoint exists). Mutation apply-on-PG is explicitly deferred.

## Scope decision (settled)

**Port acks, guard mutations** (user decision). Ported options: `keep_parked`, `keep_blocked` (pure idempotency-comment acks; `mutation="comment_only"`). Guarded options (return a clear `_apply_error`, no mutation): `unblock`, `close`, `manual_review_with_stale_pr_risk`, `remediate_parent_closeout`, `clear_orphan_claim_lock`, `reclaim_dead_running`, `reclaim_expired_claim`, `close_stale_run_metadata`. A real PG port of the mutation options is deferred to a later sub-project.

## Architecture

`apply_reconcile_decision` keeps its signature and all existing input validation (option-set membership, `task_id`/`packet_signature`/`confirm_dry_run` required, `remediate_parent_closeout` pr_head_sha validation). Immediately **after** that validation and **before** `path = kb.kanban_db_path(board=board)`, prepend a backend dispatch:

```python
    use_pg = False
    try:
        from hermes_cli.kanban.store import resolve_backend
        use_pg = resolve_backend() == "postgres"
    except Exception:
        use_pg = False  # backend undecidable -> default/upstream deployments use sqlite
    if use_pg:
        return _apply_reconcile_decision_pg(
            task_id=task_id, option=option, packet_signature=packet_signature,
            board=board, ready_age_seconds=ready_age_seconds, author=author, now=now)
    # ---- existing sqlite body, verbatim (kb.connect(path) ... ) ----
```

Mirror the B1 lesson (the reconcile fall-through bug fixed in B1): the `try/except` wraps ONLY the `resolve_backend()` decision (setting `use_pg`); `_apply_reconcile_decision_pg` is called **outside** the catch, so a PG-path error can never silently fall through to the sqlite frozen-db body. `_apply_reconcile_decision_pg` owns its own errors (the step-3 `try/except` returns an `_apply_error` dict; it never raises out).

### New `_apply_reconcile_decision_pg(*, task_id, option, packet_signature, board, ready_age_seconds, author, now=None) -> dict`

Lazy imports inside (`pg_pool`, `PostgresKanbanStore`, `_redacted_pg_dsn` from `kanban_board_doctor`).

1. `slug = board or kb.get_current_board()` (resolve once); `as_of = int(now or time.time())`; `db_path = _redacted_pg_dsn()`.
2. **Guard non-acks:** `if option not in {"keep_parked","keep_blocked"}: return _apply_error("reconcile apply option '<option>' is not available on the postgres backend (only keep_parked/keep_blocked acks are supported; mutation apply pending — Phase 6 B6)", board=slug, task_id=task_id, option=option)`.
3. **Fresh PG reconcile pass** (validate the signature against live PG, same as sqlite does with `collect_reconcile_actions`):
   ```python
   try:
       pool = pg_pool.get_pool()
       store = PostgresKanbanStore(board=slug, pool=pool)
       with pool.connection() as conn:
           actions = _collect_reconcile_actions_pg(conn, slug, store,
                                                    ready_age_seconds=ready_age_seconds, now=as_of)
   except Exception as exc:
       return _apply_error(f"postgres backend unavailable: {type(exc).__name__}",
                           board=slug, task_id=task_id, option=option)
   action_dicts = actions_to_dicts(actions)
   ```
4. **Packet/signature validation** (identical checks + error messages to the sqlite path):
   - `triage = classify_wake_triage(action_dicts)`; `packet = _find_decision_packet(triage.get("decision_packets") or [], task_id)`.
   - `packet is None` → `_apply_error("no current decision packet for task", …)`.
   - `packet.get("packet_signature") != packet_signature` → `_apply_error("packet_signature does not match current decision packet", …, packet=packet)`.
   - `plan = (packet.get("operator_plans") or {}).get(option)`; `not isinstance(plan, dict)` → `_apply_error("selected option is not available for current decision packet", …, packet=packet)`.
5. **Build comment + idempotency:**
   - `mutation = "comment_only"`; `comment = _reconcile_decision_applied_comment(option=option, packet_signature=packet_signature, category=packet.get("primary_category"), mutation=mutation)`.
   - existing-ack check: iterate `store.list_comments(task_id)`, match with `_reconcile_decision_comment_matches(c, option=option, packet_signature=packet_signature)`; if found, return the **idempotent** success shape (`mutation_applied=False, idempotent=True, comment_id=existing.id, comment=existing.body, …`).
6. **Write the ack:** `comment_id = store.add_comment(task_id, author=author or "jensen", body=comment)`.
7. **Return** the success shape mirroring the sqlite path's keys exactly: `{ok:True, board:slug, db_path, task_id, option, packet_signature, packet, plan, comment_id, comment, mutation_applied:True, mutation, as_of}`.

All return shapes mirror the sqlite `apply_reconcile_decision` return + `_apply_error`, so `format_reconcile_apply_text` (the CLI renderer) handles them with no change. `db_path` is the redacted PG DSN (never a frozen-sqlite path, never a secret).

## What this reuses (no new helpers beyond `_apply_reconcile_decision_pg`)

`_collect_reconcile_actions_pg`, `actions_to_dicts`, `classify_wake_triage`, `_find_decision_packet`, `_reconcile_decision_applied_comment`, `_reconcile_decision_comment_matches`, `_apply_error` (all in `kanban_reconciler.py`); `PostgresKanbanStore.add_comment` / `.list_comments`; `kanban_board_doctor._redacted_pg_dsn`; `kanban_db.get_current_board`.

## Guarantees / constraints

- `kanban_db.py`, `kanban_liveness.py`, `kanban_writer_daemon.py`: import-only, zero edits.
- sqlite apply path byte-identical (only a dispatch prepended; the entire `with kb.connect(path)` body unchanged).
- No DSN/secret in logs or returns: `db_path` redacted to `host:port/db`; error reasons use `type(exc).__name__` or a fixed message; never `str(exc)` on connectivity errors.
- Default backend stays sqlite in code and tests. CLI-only (no dashboard apply endpoint — verified absent in `plugin_api.py`).
- `_apply_reconcile_decision_pg` is **read + comment-write only** for acks; it performs NO task-state mutation and refuses every mutation option.

## Testing (`tests/hermes_cli/kanban/test_reconcile_apply_pg.py`, docker-PG)

- **End-to-end suppression loop:** seed a `blocked_with_completed_parents_decision` packet on a PG board (parent done + child blocked, so `classify_wake_triage` yields a packet whose `operator_plans` include `keep_parked`); read the packet's `packet_signature` from `run_reconciler` (PG); call `apply_reconcile_decision(option="keep_parked", …)`; assert `ok=True, mutation_applied=True`, the ack comment is present in `store.list_comments`, and a follow-up `run_reconciler` (PG) **suppresses** that packet (`wake_triage.suppressed_decision_packet_count >= 1` / the action no longer surfaces).
- **Idempotency:** a second `keep_parked` apply with the same signature → `idempotent=True`, `mutation_applied=False`, no duplicate comment.
- **Mutation guard:** `apply_reconcile_decision(option="unblock", …)` (and one reclaim/`clear_orphan_claim_lock`) under PG → `ok=False`, `error` mentions "not available on the postgres backend", and the task's status/state is **unchanged** (verified via `store.get_task`).
- **Validation parity:** no-packet and signature-mismatch under PG return the same error messages/shape as sqlite.
- **No-leak:** the backend-unavailable path (monkeypatch `pg_pool.get_pool` to raise with a host-bearing message) → `ok=False`, error is type-name-only, no host/DSN in the result.
- sqlite apply tests stay green (byte-identical body); cross-backend conftest `_pg_dsn` fixture in scope.

Test interpreter: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`; PG via the docker `postgres:16-alpine` fixture (`HERMES_PG_TEST_DSN`), never the live Supabase DB.

## Review

Adversarial code review (live-core: `kanban_reconciler.py` apply path writes to the live PG board). Focus: no silent fall-through to frozen sqlite on a PG-path error (the B1 lesson); the guard covers ALL mutation options incl. the early-dispatch ones; ack idempotency + the end-to-end suppression loop actually closes; no DSN leak; sqlite byte-identical.

## File inventory

- Edit: `hermes_cli/kanban_reconciler.py` (backend dispatch in `apply_reconcile_decision` + new `_apply_reconcile_decision_pg`).
- Test: `tests/hermes_cli/kanban/test_reconcile_apply_pg.py` (new).

## Out of scope (deferred)

- Real PG port of the mutation options (`unblock`/`close`/`reclaim_*`/`remediate_parent_closeout`/`clear_orphan_claim_lock`/`close_stale_run_metadata`) — guarded for now; a later sub-project can route them through the store / new PG SQL.
- B2 (crash-lane parity), B4 (Auth/RLS/Realtime + live dashboard), B5 (frozen kanban.db fate) — separate.
