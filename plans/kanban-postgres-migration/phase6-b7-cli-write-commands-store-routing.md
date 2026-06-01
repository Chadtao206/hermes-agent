# Phase 6 · B7 — Tier-1 CLI write commands → store routing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `hermes kanban wake-arm`, `profile-subs add`, `claim`, and `notify subscribe` from a direct writable `kb.connect()`/`connect_closing()` to the backend-aware `_make_store()`, so they work under the live config (postgres + single_writer) instead of raising `DirectWriteForbidden` / writing the frozen sqlite DB.

**Architecture:** Each handler in `hermes_cli/kanban/cli.py` converts to the established `store = _make_store(); try: <store calls> finally: store.close()` pattern (mirror `_cmd_profile_subs_remove` / `_cmd_comment`), unconditional — the store dispatches the backend. This *fixes* (not preserves) the sqlite path: under sqlite+single_writer the store routes writes through `kb.write_session()` (the daemon); upstream sqlite-without-single-writer is behaviorally unchanged.

**Tech Stack:** Python, argparse CLI handlers, psycopg 3, pytest with the docker `postgres:16-alpine` fixture.

---

## Ground rules (apply to EVERY task)

- **Never edit** `hermes_cli/kanban_db.py`, `hermes_cli/kanban_liveness.py`, `hermes_cli/kanban_writer_daemon.py` — import only.
- Each command's **output text + exit codes are unchanged**; only the connection/write routing changes.
- No DSN/secret in logs (these handlers log nothing new).
- Default backend stays sqlite in code/tests.
- **Test interpreter:** `cd .worktrees/kanban-pg-phase6-b7 && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`. Export `HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban"` before any pytest. NEVER the live Supabase DB; only run pytest (fixtures monkeypatch the pool to the local container); do NOT run the gateway/dashboard or live `hermes kanban` against the real config.
- **Commits** end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Reference

- Migration pattern (already-migrated): `_cmd_profile_subs_remove` (cli.py:3388) and `_cmd_comment` (cli.py:2506) — `store = _make_store(); try: store.<m>(...) finally: store.close()`.
- `_make_store()` (cli.py:46) returns the backend-aware store (PostgresKanbanStore under `resolve_backend()=="postgres"`, else SqliteKanbanStore).
- Store methods used (all present on the Protocol + PostgresKanbanStore): `claim_task(task_id, *, ttl_seconds=None, claimer=None)`, `get_task(task_id)`, `set_workspace_path(task_id, path)`, `add_notify_sub(**kwargs)`, `list_profile_event_subs(**kwargs)`, `add_profile_event_sub(**kwargs)`.
- Pure helpers that STAY (no conn): `kb.resolve_workspace(task)`, `kb.list_profiles_on_disk()`, `_profile_author()`.

---

## Task 1: migrate `wake-arm` + `profile-subs add` (the profile-event-sub pair)

**Files:**
- Modify: `hermes_cli/kanban/cli.py` (`_cmd_profile_subs_add` ~3310, `_cmd_kanban_wake_arm` ~3428)
- Test: `tests/hermes_cli/kanban/test_cli_write_commands_pg.py` (create)

**Review:** spec-compliance + code-quality.

- [ ] **Step 1: Write the failing tests** — create `tests/hermes_cli/kanban/test_cli_write_commands_pg.py`:

```python
"""Tier-1 CLI write commands route through the backend-aware store (no direct
writable kb.connect()). Verified under the Postgres backend, incl. that they
never open a direct sqlite connection (would raise DirectWriteForbidden live)."""
import argparse
import uuid
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import pg_pool
from hermes_cli.kanban.store_postgres import PostgresKanbanStore
from hermes_cli.kanban import cli as kcli


@pytest.fixture
def pg(_pg_dsn, monkeypatch):
    """Force the CLI's _make_store() onto a fresh PG board."""
    pool = pg_pool.make_pool(_pg_dsn); pg_pool.ensure_schema(pool)
    board = f"cli_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: board)
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s, board
    finally:
        s.close(); pool.close()


def _forbid_direct_connect(monkeypatch):
    """Make any direct kb.connect() blow up, so the test proves the command
    routes through the (PG) store and never opens a sqlite connection."""
    def _boom(*a, **k):
        raise AssertionError("direct kb.connect() called — should route via the store")
    monkeypatch.setattr(kb, "connect", _boom)


def test_profile_subs_add_routes_through_store(pg, monkeypatch):
    s, board = pg
    tid = s.create_task(title="needs a sub")
    _forbid_direct_connect(monkeypatch)
    ns = argparse.Namespace(task_id=tid, profile="default", name="op-sub", json=False)
    rc = kcli._cmd_profile_subs_add(ns)
    assert rc == 0
    subs = s.list_profile_event_subs(task_id=tid, profile="default", enabled_only=False)
    assert any((x.get("name") or "") == "op-sub" for x in subs)


def test_wake_arm_routes_through_store(pg, monkeypatch):
    s, board = pg
    tid = s.create_task(title="orchestrator root")
    monkeypatch.setattr(kb, "list_profiles_on_disk", lambda: ["default"])
    monkeypatch.delenv("HERMES_KANBAN_EVENT_WAKE", raising=False)
    _forbid_direct_connect(monkeypatch)
    ns = argparse.Namespace(task_id=tid, profile="default",
                            name="jensen-orchestrator", json=False)
    rc = kcli._cmd_kanban_wake_arm(ns)
    assert rc == 0
    subs = s.list_profile_event_subs(task_id=tid, profile="default", enabled_only=False)
    armed = [x for x in subs if (x.get("name") or "") == "jensen-orchestrator"]
    assert armed and bool(armed[0].get("wake_agent"))
```

(If `list_profile_event_subs` returns objects rather than dicts on PG, adjust the `.get(...)` access accordingly — check `PostgresKanbanStore.list_profile_event_subs`'s return shape and match what the CLI handler already expects via `s.get("name")`.)

- [ ] **Step 2: Run — verify FAIL**

Run: `export HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban" && venv/bin/python -m pytest tests/hermes_cli/kanban/test_cli_write_commands_pg.py -k "profile_subs_add or wake_arm" -v`
Expected: FAIL — the handlers call `kb.connect()`, which the test monkeypatches to raise `AssertionError`.

- [ ] **Step 3: Migrate `_cmd_profile_subs_add`.** Replace the `with kb.connect() as conn:` block (the read-check + `existed` + `add_profile_event_sub` + `stored` readback) with store calls. Keep the `add_kwargs` construction (lines ~3344-3363) and the output block (3372+) unchanged:

```python
    store = _make_store()
    try:
        if store.get_task(task_id) is None:
            print(f"no such task: {task_id}", file=sys.stderr)
            return 1
        existed = any(
            (s.get("name") or "") == name
            for s in store.list_profile_event_subs(
                task_id=task_id, profile=profile, enabled_only=False,
            )
        )
        add_kwargs: dict[str, Any] = {
            "task_id": task_id,
            "profile": profile,
            "name": name,
        }
        if default_events:
            add_kwargs["event_kinds"] = None
        else:
            kinds = _parse_event_kinds_flag(events_raw)
            if kinds is not None:
                add_kwargs["event_kinds"] = kinds
        if include_children is not None:
            add_kwargs["include_children"] = include_children
        if wake_agent is not None:
            add_kwargs["wake_agent"] = wake_agent
        if wake_prompt is not _PROFILE_SUB_CLI_UNSET:
            add_kwargs["wake_prompt"] = wake_prompt
        if enabled is not None:
            add_kwargs["enabled"] = enabled
        store.add_profile_event_sub(**add_kwargs)
        stored = [
            s for s in store.list_profile_event_subs(
                task_id=task_id, profile=profile, enabled_only=False,
            )
            if (s.get("name") or "") == name
        ]
    finally:
        store.close()
```
(The `sub_id = _format_profile_sub_id(...)` + JSON/text output that follows is unchanged — `existed` and `stored` remain in scope after the try/finally.)

- [ ] **Step 4: Migrate `_cmd_kanban_wake_arm`.** Keep the `HERMES_KANBAN_EVENT_WAKE` refusal guard + the `kb.list_profiles_on_disk()` profile check (lines ~3434-3454) unchanged. Replace the `with kb.connect() as conn:` block (3456-3482) with:

```python
    store = _make_store()
    try:
        if store.get_task(task_id) is None:
            print(f"kanban wake-arm: no such task: {task_id}", file=sys.stderr)
            return 1
        existed = any(
            (s.get("name") or "") == name
            for s in store.list_profile_event_subs(
                task_id=task_id, profile=profile, enabled_only=False,
            )
        )
        store.add_profile_event_sub(
            task_id=task_id,
            profile=profile,
            name=name,
            event_kinds=list(_WAKE_ARM_EVENT_KINDS),
            include_children=True,
            wake_agent=True,
            wake_prompt=_WAKE_ARM_PROMPT,
            enabled=True,
        )
        stored = [
            s for s in store.list_profile_event_subs(
                task_id=task_id, profile=profile, enabled_only=False,
            )
            if (s.get("name") or "") == name
        ]
    finally:
        store.close()
```
(The `sub_id`/output block after is unchanged.)

- [ ] **Step 5: Run — verify pass + sqlite CLI suite green**

Run: `... -m pytest tests/hermes_cli/kanban/test_cli_write_commands_pg.py -k "profile_subs_add or wake_arm" -v` → PASS.
Run: `... -m pytest tests/hermes_cli -k "profile_sub or wake_arm" -q` → existing CLI tests green (update any that asserted the old `kb.connect()` usage to the store path).

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/kanban/cli.py tests/hermes_cli/kanban/test_cli_write_commands_pg.py
git commit -m "fix(kanban-pg): route wake-arm + profile-subs add through the store (no direct connect)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: migrate `claim` + `notify subscribe`

**Files:**
- Modify: `hermes_cli/kanban/cli.py` (`_cmd_claim` ~2474, `_cmd_notify_subscribe` ~3138's handler)
- Test: `tests/hermes_cli/kanban/test_cli_write_commands_pg.py` (extend)

**Review:** spec-compliance + code-quality; **adversarial-lite on `claim`** (None-reason branch + workspace + two-write sequence).

- [ ] **Step 1: Write the failing tests** — append:

```python
def test_claim_routes_through_store(pg, monkeypatch):
    s, board = pg
    tid = s.create_task(title="claim me")
    monkeypatch.setattr(kb, "resolve_workspace", lambda task: "/tmp/ws-" + task.id)
    _forbid_direct_connect(monkeypatch)
    ns = argparse.Namespace(task_id=tid, ttl=900)
    rc = kcli._cmd_claim(ns)
    assert rc == 0
    t = s.get_task(tid)
    assert t.status == "running"
    assert t.workspace_path == "/tmp/ws-" + tid


def test_claim_unclaimable_reports_reason(pg, monkeypatch):
    s, board = pg
    tid = s.create_task(title="x")
    s.claim_task(tid)                      # already running
    monkeypatch.setattr(kb, "resolve_workspace", lambda task: "/tmp/ws")
    _forbid_direct_connect(monkeypatch)
    ns = argparse.Namespace(task_id=tid, ttl=900)
    rc = kcli._cmd_claim(ns)               # cannot claim -> rc 1, no crash
    assert rc == 1


def test_notify_subscribe_routes_through_store(pg, monkeypatch):
    s, board = pg
    tid = s.create_task(title="notify me")
    _forbid_direct_connect(monkeypatch)
    ns = argparse.Namespace(task_id=tid, platform="slack", chat_id="C123",
                            thread_id=None, user_id=None, notifier_profile="ops")
    rc = kcli._cmd_notify_subscribe(ns)
    assert rc == 0
    subs = s.list_notify_subs(tid)
    assert any(x["platform"] == "slack" and x["chat_id"] == "C123" for x in subs)
```

(Confirm `Task` exposes `workspace_path` — used by `test_claim_routes_through_store`; if the attribute name differs, read it from `kanban_db.Task` and adjust. `list_notify_subs` returns dicts per the migrated `notify list`.)

- [ ] **Step 2: Run — verify FAIL**

Run: `export HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban" && venv/bin/python -m pytest tests/hermes_cli/kanban/test_cli_write_commands_pg.py -k "claim or notify" -v`
Expected: FAIL — the handlers call `kb.connect_closing()` → `kb.connect()` → the monkeypatched `AssertionError`.

- [ ] **Step 3: Migrate `_cmd_claim`** (replace the whole body):

```python
def _cmd_claim(args: argparse.Namespace) -> int:
    store = _make_store()
    try:
        task = store.claim_task(args.task_id, ttl_seconds=args.ttl)
        if task is None:
            existing = store.get_task(args.task_id)
            if existing is None:
                print(f"no such task: {args.task_id}", file=sys.stderr)
                return 1
            print(
                f"cannot claim {args.task_id}: status={existing.status} "
                f"lock={existing.claim_lock or '(none)'}",
                file=sys.stderr,
            )
            return 1
        workspace = kb.resolve_workspace(task)
        store.set_workspace_path(task.id, str(workspace))
    finally:
        store.close()
    print(f"Claimed {task.id}")
    print(f"Workspace: {workspace}")
    return 0
```
(`task`/`workspace` are function-scoped, so the trailing prints — which were already outside the old `with` block — still work. The two writes claim+set_workspace_path were each their own write_txn before; two store calls preserve that granularity.)

- [ ] **Step 4: Migrate `_cmd_notify_subscribe`** (replace the `with kb.connect_closing() as conn:` block; keep the print):

```python
    store = _make_store()
    try:
        if store.get_task(args.task_id) is None:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            return 1
        store.add_notify_sub(
            task_id=args.task_id,
            platform=args.platform, chat_id=args.chat_id,
            thread_id=args.thread_id, user_id=args.user_id,
            notifier_profile=args.notifier_profile or _profile_author(),
        )
    finally:
        store.close()
    print(f"Subscribed {args.platform}:{args.chat_id}"
          + (f":{args.thread_id}" if args.thread_id else "")
          + f" to {args.task_id}")
    return 0
```

- [ ] **Step 5: Run — verify pass + sqlite suite green**

Run: `... -m pytest tests/hermes_cli/kanban/test_cli_write_commands_pg.py -v` → all PASS.
Run: `... -m pytest tests/hermes_cli -k "claim or notify" -q` → existing CLI tests green (update any asserting the old direct-connect usage).

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/kanban/cli.py tests/hermes_cli/kanban/test_cli_write_commands_pg.py
git commit -m "fix(kanban-pg): route claim + notify subscribe through the store (no direct connect)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Verification — no remaining Tier-1 direct connects + forbidden files + suites

**Files:** none (verification only). Fix only if a gap is found.

- [ ] **Step 1: Forbidden files untouched**

Run: `git diff --stat main -- hermes_cli/kanban_db.py hermes_cli/kanban_liveness.py hermes_cli/kanban_writer_daemon.py`
Expected: empty.

- [ ] **Step 2: The 4 Tier-1 handlers no longer open a direct writable connection**

Run: `git diff main -- hermes_cli/kanban/cli.py | grep -nE "kb\.connect\(\)|connect_closing\(\)"`
Inspect: the removed lines (`-`) are the 4 migrated handlers' connects; confirm no `+` line re-introduces `kb.connect()`/`connect_closing()` in `_cmd_claim`/`_cmd_notify_subscribe`/`_cmd_profile_subs_add`/`_cmd_kanban_wake_arm`. (Deferred Tier 2-4 commands — swarm/dispatch/archive --rm/repair-db — MAY still have them; that's expected.)

- [ ] **Step 3: Full CLI + kanban suite, both backends**

Run: `export HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban" && venv/bin/python -m pytest tests/hermes_cli/kanban tests/hermes_cli/test_kanban_db.py -q`
Expected: all green on sqlite + postgres params.

- [ ] **Step 4: Commit (only if a verification-driven fix was needed)** — otherwise no-op.

---

## Self-review (plan author, before handoff)

- **Spec coverage:** wake-arm (Task 1) ✓; profile-subs add (Task 1) ✓; claim (Task 2, incl. unclaimable-reason branch) ✓; notify subscribe (Task 2) ✓; store-routing pattern mirrors profile-subs remove ✓; no-direct-connect regression (the `_forbid_direct_connect` monkeypatch under PG) ✓; PG functional (artifact created in PG) ✓; forbidden files + no-reintroduced-connect (Task 3) ✓; output/exit-codes unchanged (handlers keep their print/return lines) ✓.
- **Placeholders:** none — full new handler bodies + complete test code; the only verify-and-adjust notes (list_profile_event_subs/Task `workspace_path` shapes) cite the exact thing to check.
- **Type/name consistency:** all four handlers use `store = _make_store(); try: … finally: store.close()`; store methods (`claim_task`/`get_task`/`set_workspace_path`/`add_notify_sub`/`list_profile_event_subs`/`add_profile_event_sub`) match the Protocol; pure helpers (`kb.resolve_workspace`, `kb.list_profiles_on_disk`, `_profile_author`) unchanged; `_forbid_direct_connect`/`pg` fixtures consistent across tasks.
