# Phase 6 · B6 — reconcile-apply acks to PG + mutation guard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Under `kanban.backend=postgres`, make `apply_reconcile_decision` apply `keep_parked`/`keep_blocked` acks to live PG (closing B1's suppression loop) and hard-guard every other (mutation) option with a clear error instead of silently mutating the frozen sqlite.

**Architecture:** Prepend a `resolve_backend()=="postgres"` dispatch to `apply_reconcile_decision` (scoped so it can never fall through to the sqlite frozen-db body), routing to a new `_apply_reconcile_decision_pg` that guards mutation options and, for acks, validates the signature against a fresh PG reconcile pass (B1's `_collect_reconcile_actions_pg`) then writes the idempotency comment via the backend-aware store. sqlite path byte-identical; CLI-only.

**Tech Stack:** Python, psycopg 3, pytest with the docker `postgres:16-alpine` `_pg_dsn` fixture.

---

## Ground rules (apply to EVERY task)

- **Never edit** `hermes_cli/kanban_db.py`, `hermes_cli/kanban_liveness.py`, `hermes_cli/kanban_writer_daemon.py` — import only.
- **sqlite byte-identical:** only a dispatch is prepended to `apply_reconcile_decision`; the existing `with kb.connect(path): …` body is unchanged.
- **No DSN/secret in logs or returns:** `db_path` is the redacted PG DSN (`_redacted_pg_dsn` → `host:port/db`); error reasons use `type(exc).__name__` or a fixed message; never `str(exc)` on connectivity errors.
- **Lazy psycopg imports** inside `_apply_reconcile_decision_pg`, never module top.
- **Test interpreter:** `cd .worktrees/kanban-pg-phase6-b6 && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`. Export `HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban"` before any pytest. NEVER the live Supabase DB; only run pytest (fixtures monkeypatch `pg_pool.get_pool` to the local container); do NOT run the gateway/dashboard/`hermes kanban` CLI.
- **Commits** end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Reference (read before implementing)

- `hermes_cli/kanban_reconciler.py::apply_reconcile_decision` (~line 2156) — the sqlite path you mirror for acks + dispatch from. Input validation (option-set, task_id, packet_signature, confirm_dry_run, remediate pr_head) is at the top; the ack branch is `if option in {"keep_parked","keep_blocked"}: comment_id = kb.add_comment(...)`; the idempotency early-return is the `existing_comment is not None and option in {...}` block.
- `_apply_error(message, *, board, task_id, option, packet=None)` (~line 1712) — returns `{ok:False, board, task_id, option, error, packet, mutation_applied:False}`.
- `_reconcile_decision_applied_comment(*, option, packet_signature, category, mutation)` (~line 1717) — the exact comment text.
- `_reconcile_decision_comment_matches(comment, *, option, packet_signature)` + `_find_decision_packet`, `classify_wake_triage`, `actions_to_dicts`, `_collect_reconcile_actions_pg` (all in the same file, from B1).
- `kanban_board_doctor._redacted_pg_dsn()` → `postgres://host:port/db`.
- `PostgresKanbanStore.add_comment(task_id, *, author, body) -> int` and `.list_comments(task_id) -> list[Comment]` (Comment has `.id`, `.body`).
- CLI caller: `hermes_cli/kanban/cli.py::_cmd_reconcile` — on `--apply-option` calls `apply_reconcile_decision(...)`, renders `format_reconcile_apply_text(result)`, returns `0 if result.get("ok") else 2`. No dashboard apply endpoint exists.

---

## Task 1: backend dispatch + `_apply_reconcile_decision_pg` (ack port + mutation guard)

**Files:**
- Modify: `hermes_cli/kanban_reconciler.py` (prepend dispatch to `apply_reconcile_decision`; add `_apply_reconcile_decision_pg`)
- Test: `tests/hermes_cli/kanban/test_reconcile_apply_pg.py` (create)

**Review:** adversarial (live-core; writes to the live PG board).

- [ ] **Step 1: Write the failing tests** — create `tests/hermes_cli/kanban/test_reconcile_apply_pg.py`:

```python
"""apply_reconcile_decision on the Postgres backend: ports keep_parked/keep_blocked
acks to live PG; hard-guards every mutation option (no frozen-sqlite write)."""
import time, uuid
import pytest

from hermes_cli.kanban import pg_pool
from hermes_cli.kanban.store_postgres import PostgresKanbanStore
from hermes_cli import kanban_reconciler as krec


@pytest.fixture
def pg(_pg_dsn, monkeypatch):
    pool = pg_pool.make_pool(_pg_dsn); pg_pool.ensure_schema(pool)
    board = f"apply_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: board)
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s, pool, board
    finally:
        s.close(); pool.close()


def _seed_blocked_with_done_parent(s):
    """Produce a blocked_with_completed_parents decision packet."""
    parent = s.create_task(title="parent")
    child = s.create_task(title="child", parents=[parent])
    s.complete_task(parent, summary="done")
    s.block_task(child, reason="needs decision")
    return child


def _ack_option_and_sig(board, child):
    """Run a PG reconcile pass and return (ack_option, packet_signature) for child."""
    res = krec.run_reconciler(board=board, ready_age_seconds=900)
    pkts = res["wake_triage"].get("decision_packets") or []
    pkt = next((p for p in pkts if p.get("task_id") == child), None)
    assert pkt is not None, f"no decision packet for {child}; packets={pkts}"
    plans = pkt.get("operator_plans") or {}
    opt = next((o for o in ("keep_parked", "keep_blocked") if o in plans), None)
    assert opt is not None, f"no ack option in operator_plans={list(plans)}"
    return opt, pkt["packet_signature"]


def test_keep_ack_writes_pg_comment_and_suppresses(pg):
    s, pool, board = pg
    child = _seed_blocked_with_done_parent(s)
    opt, sig = _ack_option_and_sig(board, child)
    res = krec.apply_reconcile_decision(
        task_id=child, option=opt, packet_signature=sig,
        confirm_dry_run=True, board=board, author="jensen")
    assert res["ok"] is True
    assert res["mutation_applied"] is True
    assert res["db_path"].startswith("postgres://")        # live PG, not frozen sqlite
    # the ack comment landed in PG
    assert any("Jensen reconcile decision applied" in c.body and f"option={opt};" in c.body
               for c in s.list_comments(child))
    # follow-up reconcile suppresses the packet (B1 loop closed)
    res2 = krec.run_reconciler(board=board, ready_age_seconds=900)
    assert res2["wake_triage"].get("suppressed_decision_packet_count", 0) >= 1
    assert not any(p.get("task_id") == child
                   for p in (res2["wake_triage"].get("decision_packets") or []))


def test_keep_ack_is_idempotent(pg):
    s, pool, board = pg
    child = _seed_blocked_with_done_parent(s)
    opt, sig = _ack_option_and_sig(board, child)
    first = krec.apply_reconcile_decision(task_id=child, option=opt, packet_signature=sig,
                                          confirm_dry_run=True, board=board, author="jensen")
    assert first["mutation_applied"] is True
    second = krec.apply_reconcile_decision(task_id=child, option=opt, packet_signature=sig,
                                           confirm_dry_run=True, board=board, author="jensen")
    assert second["ok"] is True
    assert second.get("idempotent") is True
    assert second["mutation_applied"] is False
    # exactly one ack comment
    acks = [c for c in s.list_comments(child)
            if "Jensen reconcile decision applied" in c.body and f"option={opt};" in c.body]
    assert len(acks) == 1


@pytest.mark.parametrize("bad_option", ["unblock", "close", "reclaim_dead_running",
                                        "clear_orphan_claim_lock", "remediate_parent_closeout"])
def test_mutation_options_guarded_no_write(pg, bad_option):
    s, pool, board = pg
    child = _seed_blocked_with_done_parent(s)
    before = s.get_task(child).status
    res = krec.apply_reconcile_decision(
        task_id=child, option=bad_option, packet_signature="whatever",
        confirm_dry_run=True, board=board, author="jensen",
        pr_head_sha="a1b2c3d4")  # valid-looking sha so remediate passes input validation
    assert res["ok"] is False
    assert "not available on the postgres backend" in res["error"]
    assert s.get_task(child).status == before   # NO mutation
    # no reconcile-decision comment written
    assert not any("Jensen reconcile decision applied" in c.body for c in s.list_comments(child))


def test_signature_mismatch_parity(pg):
    s, pool, board = pg
    child = _seed_blocked_with_done_parent(s)
    opt, _ = _ack_option_and_sig(board, child)
    res = krec.apply_reconcile_decision(task_id=child, option=opt,
                                        packet_signature="wrong-sig",
                                        confirm_dry_run=True, board=board, author="jensen")
    assert res["ok"] is False
    assert "packet_signature does not match" in res["error"]


def test_no_packet_parity(pg):
    s, pool, board = pg
    live = s.create_task(title="not a decision")  # ready, no packet
    res = krec.apply_reconcile_decision(task_id=live, option="keep_parked",
                                        packet_signature="x", confirm_dry_run=True,
                                        board=board, author="jensen")
    assert res["ok"] is False
    assert "no current decision packet" in res["error"]


def test_backend_unavailable_no_leak(monkeypatch):
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: "default")
    class _BadPool:
        def connection(self, *a, **k): raise RuntimeError("conn to secret-host:5432 failed")
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: _BadPool())
    res = krec.apply_reconcile_decision(task_id="t_x", option="keep_parked",
                                        packet_signature="x", confirm_dry_run=True,
                                        author="jensen")
    assert res["ok"] is False
    assert "secret-host" not in str(res)         # no raw exception / DSN leak
    assert "postgres backend unavailable" in res["error"]
```

NOTE: `_seed_blocked_with_done_parent` assumes `create_task(parents=[parent])` + `complete_task` + `block_task` produce a `blocked_with_completed_parents_decision` action that `classify_wake_triage` groups into a decision packet with an ack option in `operator_plans`. Verify this by running the first test; if `blocked_with_completed_parents_decision` maps only to `keep_blocked` (not `keep_parked`), `_ack_option_and_sig` already picks whichever ack option the packet offers — no change needed. If the packet has NO ack option at all, STOP and report (the kind→option mapping needs inspecting in `_decision_hint_for_kinds`/`_operator_plans_for_packet`).

- [ ] **Step 2: Run tests — verify they fail**

Run: `export HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban" && venv/bin/python -m pytest tests/hermes_cli/kanban/test_reconcile_apply_pg.py -v`
Expected: FAIL — `apply_reconcile_decision` has no PG branch, so it opens the default sqlite DB; the ack tests find no packet / write nowhere visible to the PG store, and the guard tests don't return the "not available on the postgres backend" error.

- [ ] **Step 3: Prepend the dispatch in `apply_reconcile_decision`.** Find the spot AFTER the input validation (after the `if option == "remediate_parent_closeout": … _validate_reconcile_pr_head_sha(…)` block) and BEFORE `path = kb.kanban_db_path(board=board)`. Insert:

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
    # ---- existing sqlite body below, UNCHANGED ----
    path = kb.kanban_db_path(board=board)
    ...
```
The `try/except` wraps ONLY the `resolve_backend()` decision; `_apply_reconcile_decision_pg` is called OUTSIDE it (a PG-path error must never fall through to the frozen-sqlite body — the B1 lesson).

- [ ] **Step 4: Add `_apply_reconcile_decision_pg`** (place it just above `apply_reconcile_decision`):

```python
def _apply_reconcile_decision_pg(*, task_id, option, packet_signature, board,
                                 ready_age_seconds, author, now=None):
    """Postgres path for apply_reconcile_decision. Ports keep_parked/keep_blocked
    acks to live PG (write the idempotency comment via the store, closing B1's
    suppression loop) and hard-guards every mutation option. Never raises out;
    never touches the frozen sqlite board."""
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    from hermes_cli.kanban_board_doctor import _redacted_pg_dsn
    slug = board or kb.get_current_board()
    as_of = int(now if now is not None else time.time())
    db_path = _redacted_pg_dsn()
    if option not in {"keep_parked", "keep_blocked"}:
        return _apply_error(
            f"reconcile apply option '{option}' is not available on the postgres "
            "backend (only keep_parked/keep_blocked acks are supported; mutation "
            "apply pending — Phase 6 B6)",
            board=slug, task_id=task_id, option=option)
    try:
        pool = pg_pool.get_pool()
        store = PostgresKanbanStore(board=slug, pool=pool)
        with pool.connection() as conn:
            actions = _collect_reconcile_actions_pg(
                conn, slug, store, ready_age_seconds=ready_age_seconds, now=as_of)
    except Exception as exc:
        return _apply_error(
            f"postgres backend unavailable: {type(exc).__name__}",
            board=slug, task_id=task_id, option=option)
    action_dicts = actions_to_dicts(actions)
    triage = classify_wake_triage(action_dicts)
    packet = _find_decision_packet(triage.get("decision_packets") or [], task_id)
    if packet is None:
        return _apply_error("no current decision packet for task",
                            board=slug, task_id=task_id, option=option)
    if packet.get("packet_signature") != packet_signature:
        return _apply_error("packet_signature does not match current decision packet",
                            board=slug, task_id=task_id, option=option, packet=packet)
    plan = (packet.get("operator_plans") or {}).get(option)
    if not isinstance(plan, dict):
        return _apply_error("selected option is not available for current decision packet",
                            board=slug, task_id=task_id, option=option, packet=packet)
    mutation = "comment_only"
    comment = _reconcile_decision_applied_comment(
        option=option, packet_signature=packet_signature,
        category=packet.get("primary_category"), mutation=mutation)
    existing = None
    for c in store.list_comments(task_id):
        if _reconcile_decision_comment_matches(c, option=option,
                                               packet_signature=packet_signature):
            existing = c
            break
    if existing is not None:
        return {"ok": True, "board": slug, "db_path": db_path, "task_id": task_id,
                "option": option, "packet_signature": packet_signature, "packet": packet,
                "plan": plan, "comment_id": existing.id, "comment": existing.body,
                "mutation_applied": False, "mutation": mutation, "idempotent": True,
                "as_of": as_of}
    comment_id = store.add_comment(task_id, author=author or "jensen", body=comment)
    return {"ok": True, "board": slug, "db_path": db_path, "task_id": task_id,
            "option": option, "packet_signature": packet_signature, "packet": packet,
            "plan": plan, "comment_id": comment_id, "comment": comment,
            "mutation_applied": True, "mutation": mutation, "as_of": as_of}
```

- [ ] **Step 5: Run tests — verify pass + sqlite apply suite green**

Run: `export HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban" && venv/bin/python -m pytest tests/hermes_cli/kanban/test_reconcile_apply_pg.py -v` → all PASS.
Run: `venv/bin/python -m pytest tests/hermes_cli -k "reconcile or reconcil" -q` → existing sqlite reconcile/apply tests PASS (sqlite body unchanged).

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/kanban_reconciler.py tests/hermes_cli/kanban/test_reconcile_apply_pg.py
git commit -m "feat(kanban-pg): reconcile-apply ports keep_parked/keep_blocked acks to PG, guards mutations

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Verification — byte-identical sqlite + forbidden files + no-leak

**Files:** none (verification only). Add a fix only if a gap is found.

- [ ] **Step 1: Prove forbidden files untouched**

Run: `git diff --stat main -- hermes_cli/kanban_db.py hermes_cli/kanban_liveness.py hermes_cli/kanban_writer_daemon.py`
Expected: empty.

- [ ] **Step 2: Prove the sqlite `apply_reconcile_decision` body is byte-identical**

Run: `git diff main -- hermes_cli/kanban_reconciler.py`
Inspect: the ONLY changes are (a) the prepended `use_pg` dispatch block and (b) the new `_apply_reconcile_decision_pg` function. The original `apply_reconcile_decision` body from `path = kb.kanban_db_path(...)` onward is unchanged character-for-character.

- [ ] **Step 3: DSN-leak grep**

Run: `git diff main | grep -iE '^\+' | grep -iE 'dsn|password|postgres://' | grep -viE 'redact|host:port|type\(exc\)|_redacted_pg_dsn|postgresql://postgres:postgres@127|partial|#'`
Expected: no line returning/logging a raw DSN. `db_path` is always the redacted form; errors are type-name-only.

- [ ] **Step 4: Full reconcile + plugins suite, both backends**

Run: `export HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban" && venv/bin/python -m pytest tests/hermes_cli/kanban tests/hermes_cli/test_kanban_db.py tests/plugins -q`
Expected: all green on sqlite + postgres params.

- [ ] **Step 5: Commit (only if a verification-driven fix was needed)** — otherwise no-op.

---

## Self-review (plan author, before handoff)

- **Spec coverage:** ack port (Task 1, `_apply_reconcile_decision_pg` ack branch + suppression test) ✓; mutation guard for ALL non-ack options incl. early-dispatch ones (Task 1 guard + parametrized guard test) ✓; signature/no-packet parity (tests) ✓; no-leak (test + Task 2 grep) ✓; sqlite byte-identical + forbidden files (Task 2) ✓; idempotency (test) ✓; dispatch scoped to avoid frozen-sqlite fall-through (Task 1 Step 3) ✓.
- **Placeholders:** none — all code complete; the only judgment call (ack-option in operator_plans) is handled robustly by `_ack_option_and_sig` picking whichever ack option the packet offers, with a STOP-and-report fallback.
- **Type/name consistency:** `_apply_reconcile_decision_pg` signature matches the dispatch call; return shapes mirror the sqlite path's keys + `_apply_error`; `store.add_comment(task_id, author=, body=)` / `store.list_comments(task_id)` match the Protocol; reuses (`_collect_reconcile_actions_pg`, `classify_wake_triage`, `_find_decision_packet`, `_reconcile_decision_applied_comment`, `_reconcile_decision_comment_matches`, `_apply_error`) all exist on this branch (B1).
