# B7-tail: CLI write commands → store (swarm / archive --rm / dispatch) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the last sqlite-coupled `hermes kanban` write commands (`swarm`, `archive --rm`, `dispatch`) through the backend-aware `KanbanStore` so they work on the live Postgres board instead of failing the single-writer guard / writing the frozen sqlite file.

**Architecture:** Mirror B7. Each command resolves `store = _make_store()` (backend-aware) and calls store methods; new store-surface methods are added to the `KanbanStore` Protocol + both impls (PG mirrors the sqlite `kanban_db` semantics, sqlite wraps `kb.*`); `dispatch` reuses the existing `kanban_glue.run_dispatch_tick` with the gateway's callback wiring; `archive --rm` gains a dry-run/`--confirm` safety guard. `hermes_cli/kanban_db.py`, `kanban_liveness.py`, `kanban_writer_daemon.py` are **import-only** (forbidden upstream files).

**Tech Stack:** Python 3.11, psycopg3 (PG via `pg_pool` + `dict_row`), pytest (cross-backend conformance via the `store` fixture; PG-param needs docker — skips here), argparse CLI.

**Worktree:** `.worktrees/phase6-b7-tail` (branch `feat/phase6-b7-tail-cli-writes`, off `main` `423c17f01`). Run all commands from there.

**Test runner:** `venv/bin/python -m pytest … -q -p no:cacheprovider` from the repo root (`/Users/ctao/.hermes/hermes-agent` paths inside the worktree). Set `HERMES_KANBAN_BACKEND=sqlite` for test runs as a safety net against stray live-Supabase connections; PG-param conformance auto-skips without docker.

---

## Task 1: Tier 2 — `swarm` → store

**Files:**
- Modify: `hermes_cli/kanban_swarm.py` (functions `create_swarm`, `post_blackboard_update`, `latest_blackboard`)
- Modify: `hermes_cli/kanban/cli.py` (`_cmd_swarm`, ~line 1968)
- Test: `tests/hermes_cli/test_kanban_swarm.py` (3 existing calls)

`create_swarm` only uses store-available ops: `create_task` (with `parents=`), `complete_task`, `add_comment`/`list_comments` (via the two blackboard helpers). No `link_tasks` needed (parents are passed to `create_task`).

- [ ] **Step 1: Update the failing test calls to pass a store**

In `tests/hermes_cli/test_kanban_swarm.py`, the 3 calls currently pass a sqlite `conn`. Replace the connection setup with a `SqliteKanbanStore` and pass it. Add at the top:

```python
from hermes_cli.kanban.store_sqlite import SqliteKanbanStore
```

For each test, replace the `with kb.connect() as conn:` + `create_swarm(conn, …)` pattern with:

```python
    store = SqliteKanbanStore(board=None)
    try:
        created = create_swarm(
            store,
            goal="…",            # keep each test's existing kwargs verbatim
            workers=[…],
            verifier_assignee="…",
            synthesizer_assignee="…",
        )
    finally:
        store.close()
    # assertions that previously used `conn` to read must now read via the store
    # or a fresh kb.connect() read — e.g.:
    with kb.connect() as conn:
        root = kb.get_task(conn, created.root_id)
    assert root.status == "done"
```

Keep each test's existing assertions; only the *acquisition* of rows for assertions changes (read via a fresh `kb.connect()` or `store.get_task`). The `kanban_home`-style fixture must set `HERMES_KANBAN_BACKEND=sqlite` and `kb.single_writer_enabled → False` (mirror `tests/hermes_cli/test_kanban_closeout_guard.py:14-24`) so `store._write` takes the `_LocalWriter` path.

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_swarm.py -q -p no:cacheprovider`
Expected: FAIL — `create_swarm()` still expects a `conn` (TypeError / attribute error on the store).

- [ ] **Step 3: Refactor `create_swarm` + the two blackboard helpers to take a store**

In `hermes_cli/kanban_swarm.py`:

Change the signature and every DB call. `create_swarm(conn, …)` → `create_swarm(store, …)`. Replace:
- `kb.create_task(conn, …)` → `store.create_task(…)` (every occurrence: root, each worker, verifier, synthesizer — keep all kwargs identical, including `parents=`)
- `kb.complete_task(conn, root, summary=…, metadata=…)` → `store.complete_task(root, summary=…, metadata=…)`
- `latest_blackboard(conn, root)` → `latest_blackboard(store, root)`
- `post_blackboard_update(conn, root, …)` → `post_blackboard_update(store, root, …)`

Update `post_blackboard_update`:

```python
def post_blackboard_update(store, root_id, *, author, key, value) -> int:
    """Append one structured update to the swarm root blackboard."""
    _require_text(root_id, "root_id")
    author = _require_text(author, "author")
    key = _require_text(key, "key")
    payload = json.dumps({"key": key, "value": value}, ensure_ascii=False, sort_keys=True)
    return store.add_comment(root_id, author=author, body=BLACKBOARD_PREFIX + payload)
```

Update `latest_blackboard`:

```python
def latest_blackboard(store, root_id: str) -> dict[str, Any]:
    """Merge structured blackboard comments on a root card (later wins)."""
    merged: dict[str, Any] = {}
    authors: dict[str, str] = {}
    for comment in store.list_comments(root_id):
        body = comment.body or ""
        if not body.startswith(BLACKBOARD_PREFIX):
            continue
        try:
            payload = json.loads(body[len(BLACKBOARD_PREFIX):])
        except json.JSONDecodeError:
            continue
        key = payload.get("key")
        if not isinstance(key, str) or not key:
            continue
        merged[key] = payload.get("value")
        authors[key] = comment.author
    if authors:
        merged["_authors"] = authors
    return merged
```

Drop the now-unused `import sqlite3` only if nothing else in the module uses it (grep first; leave it if other annotations reference it). Update type hints from `sqlite3.Connection` to `"KanbanStore"` (string annotation; no import needed under `from __future__ import annotations`, which the module already has).

- [ ] **Step 4: Point the CLI at a store**

In `hermes_cli/kanban/cli.py` `_cmd_swarm` (~1977), replace:

```python
    with kb.connect_closing() as conn:
        created = ks.create_swarm(
            conn,
            goal=args.goal,
            …
        )
```

with:

```python
    store = _make_store()
    try:
        created = ks.create_swarm(
            store,
            goal=args.goal,
            workers=workers,
            verifier_assignee=args.verifier,
            synthesizer_assignee=args.synthesizer,
            tenant=args.tenant,
            created_by=args.created_by or _profile_author(),
            priority=args.priority,
            idempotency_key=getattr(args, "idempotency_key", None),
        )
    finally:
        store.close()
```

- [ ] **Step 5: Run tests to verify pass**

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_swarm.py -q -p no:cacheprovider`
Expected: PASS (all 3).

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/kanban_swarm.py hermes_cli/kanban/cli.py tests/hermes_cli/test_kanban_swarm.py
git commit -m "feat(kanban-pg): route kanban swarm through the store (B7-tail Tier 2)"
```

---

## Task 2: Tier 4 — `delete_archived_task` store method (Protocol + sqlite + PG)

**Files:**
- Modify: `hermes_cli/kanban/store.py` (Protocol)
- Modify: `hermes_cli/kanban/store_sqlite.py`
- Modify: `hermes_cli/kanban/store_postgres.py`
- Test: `tests/hermes_cli/kanban/test_store_conformance.py`

- [ ] **Step 1: Write the failing conformance test**

Append to `tests/hermes_cli/kanban/test_store_conformance.py` (uses the existing `store` fixture, which parametrizes both backends):

```python
def test_delete_archived_task_only_when_archived(store):
    tid = store.create_task(title="purge me", assignee="engineer")
    # not archived → refused, no mutation
    assert store.delete_archived_task(tid) is False
    assert store.get_task(tid) is not None
    # archive, then add related rows, then purge
    store.add_comment(tid, author="engineer", body="note")
    assert store.archive_task(tid) is True
    assert store.delete_archived_task(tid) is True
    assert store.get_task(tid) is None
    assert store.list_comments(tid) == []
    assert store.list_events(tid) == []
    # deleting again → False (already gone)
    assert store.delete_archived_task(tid) is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py -k delete_archived -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: … has no attribute 'delete_archived_task'`.

- [ ] **Step 3: Add the Protocol method**

In `hermes_cli/kanban/store.py`, in the `KanbanStore` Protocol near `delete_task` (~line 159):

```python
    def delete_archived_task(self, task_id: str) -> bool: ...
```

- [ ] **Step 4: Add the sqlite impl**

In `hermes_cli/kanban/store_sqlite.py`, next to `delete_task` (~line 172):

```python
    def delete_archived_task(self, task_id: str) -> bool:
        return self._write("delete_archived_task", task_id=task_id)
```

(`_LocalWriter.__getattr__` resolves `delete_archived_task` → `kb.delete_archived_task(conn, task_id=…)`, confirmed at `kanban_db.py:2031`.)

- [ ] **Step 5: Add the PG impl (mirror sqlite cascade, board-scoped)**

In `hermes_cli/kanban/store_postgres.py`, add a method on `PostgresKanbanStore` (place near `delete_task` / the other write methods):

```python
    def delete_archived_task(self, task_id: str) -> bool:
        """Permanently remove an already-archived task + its related rows.

        Mirrors hermes_cli.kanban_db.delete_archived_task: only an ``archived``
        task may be deleted (so accidental loss needs a deliberate archive
        first). Board-scoped; single transaction. Returns True iff a row was
        deleted. (Parity note: the PG-only kanban_profile_event_* tables are
        NOT purged here, matching the sqlite cascade.)
        """
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT status FROM tasks WHERE board=%s AND id=%s FOR UPDATE",
                    (self.board, task_id),
                )
                row = cur.fetchone()
                if row is None or row["status"] != "archived":
                    return False
                cur.execute(
                    "DELETE FROM task_links WHERE board=%s AND (parent_id=%s OR child_id=%s)",
                    (self.board, task_id, task_id),
                )
                cur.execute("DELETE FROM task_comments WHERE board=%s AND task_id=%s",
                            (self.board, task_id))
                cur.execute("DELETE FROM task_events WHERE board=%s AND task_id=%s",
                            (self.board, task_id))
                cur.execute("DELETE FROM task_runs WHERE board=%s AND task_id=%s",
                            (self.board, task_id))
                cur.execute("DELETE FROM kanban_notify_subs WHERE board=%s AND task_id=%s",
                            (self.board, task_id))
                cur.execute("DELETE FROM tasks WHERE board=%s AND id=%s",
                            (self.board, task_id))
                return cur.rowcount == 1
```

- [ ] **Step 6: Run conformance to verify pass (sqlite param; PG skips without docker)**

Run: `HERMES_KANBAN_BACKEND=sqlite venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py -k delete_archived -q -p no:cacheprovider`
Expected: PASS for sqlite param; PG param `s` (skipped — no docker). Also run a structural import check:
`venv/bin/python -c "import hermes_cli.kanban.store_postgres as s; assert hasattr(s.PostgresKanbanStore,'delete_archived_task')"`
Expected: no error.

- [ ] **Step 7: Commit**

```bash
git add hermes_cli/kanban/store.py hermes_cli/kanban/store_sqlite.py hermes_cli/kanban/store_postgres.py tests/hermes_cli/kanban/test_store_conformance.py
git commit -m "feat(kanban-pg): add delete_archived_task to the store (PG + sqlite, board-scoped cascade) (B7-tail Tier 4)"
```

---

## Task 3: Tier 4 — `archive --rm` CLI: dry-run default + `--confirm`

**Files:**
- Modify: `hermes_cli/kanban/cli.py` (`_cmd_archive` ~2734; the `--rm` arg parser ~784-790)
- Test: `tests/hermes_cli/test_kanban_archive_rm.py` (new)

- [ ] **Step 1: Add the `--confirm` flag to the archive parser**

In the archive subparser (~line 784, where `--rm` is defined), add:

```python
    p_archive.add_argument(
        "--confirm", action="store_true",
        help="Actually delete with --rm (default is a dry-run preview)",
    )
```

- [ ] **Step 2: Write the failing CLI test**

Create `tests/hermes_cli/test_kanban_archive_rm.py`:

```python
from __future__ import annotations
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "sqlite")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: False)
    kb.init_db()
    return home


def _archived_task() -> str:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="purge me", assignee="engineer")
        kb.archive_task(conn, tid)
    return tid


def test_archive_rm_dry_run_does_not_delete(kanban_home, capsys):
    from hermes_cli.kanban.cli import _cmd_archive
    import argparse
    tid = _archived_task()
    rc = _cmd_archive(argparse.Namespace(task_ids=None, purge_ids=[tid], confirm=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out.lower()
    with kb.connect() as conn:
        assert kb.get_task(conn, tid) is not None  # NOT deleted


def test_archive_rm_confirm_deletes(kanban_home):
    from hermes_cli.kanban.cli import _cmd_archive
    import argparse
    tid = _archived_task()
    rc = _cmd_archive(argparse.Namespace(task_ids=None, purge_ids=[tid], confirm=True))
    assert rc == 0
    with kb.connect() as conn:
        assert kb.get_task(conn, tid) is None  # deleted
```

(If `_cmd_archive` reads other `args` attributes, include them in the Namespace with their defaults — check the handler before running.)

- [ ] **Step 3: Run to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_archive_rm.py -q -p no:cacheprovider`
Expected: FAIL — dry-run test fails (current code deletes immediately, no "dry-run" output).

- [ ] **Step 4: Rewrite the `--rm` purge path in `_cmd_archive`**

Replace the `if purge_ids:` block (~2744-2753) with a store-routed, dry-run-default version:

```python
    if purge_ids:
        store = _make_store()
        try:
            confirm = bool(getattr(args, "confirm", False))
            if not confirm:
                any_deletable = False
                for tid in purge_ids:
                    task = store.get_task(tid)
                    if task is None or task.status != "archived":
                        print(f"cannot delete {tid} (must already be archived)", file=sys.stderr)
                        continue
                    any_deletable = True
                    n_events = len(store.list_events(tid))
                    n_comments = len(store.list_comments(tid))
                    n_runs = len(store.list_runs(tid))
                    n_links = len(store.parent_ids(tid)) + len(store.child_ids(tid))
                    print(
                        f"would delete {tid}: events={n_events} comments={n_comments} "
                        f"runs={n_runs} links={n_links}"
                    )
                print("(dry-run — pass --confirm to permanently delete)")
                return 0 if any_deletable else 1
            failed: list[str] = []
            for tid in purge_ids:
                if not store.delete_archived_task(tid):
                    failed.append(tid)
                    print(f"cannot delete {tid} (must already be archived)", file=sys.stderr)
                else:
                    print(f"Deleted {tid}")
            return 0 if not failed else 1
        finally:
            store.close()
```

(`list_runs` is on the store Protocol; `parent_ids`/`child_ids` give the link count without a new method. Remove the old `with kb.connect_closing()` purge block + its `delete_archived_task is not in the store protocol` comment.)

- [ ] **Step 5: Run tests to verify pass**

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_archive_rm.py -q -p no:cacheprovider`
Expected: PASS (both).

- [ ] **Step 6: Update docs/help for the behavior change**

In `website/docs/reference/cli-commands.md` (and the zh-Hans mirror) where `archive --rm` is documented, note that `--rm` is now a dry-run preview by default and requires `--confirm` to delete. Keep it to the two sentences needed.

- [ ] **Step 7: Commit**

```bash
git add hermes_cli/kanban/cli.py tests/hermes_cli/test_kanban_archive_rm.py website/docs/reference/cli-commands.md website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/reference/cli-commands.md
git commit -m "feat(kanban-pg): route archive --rm through the store + dry-run/--confirm guard (B7-tail Tier 4)"
```

---

## Task 4: Tier 3 — `dispatch` live tick → `run_dispatch_tick`

**Files:**
- Modify: `hermes_cli/kanban/cli.py` (`_cmd_dispatch` ~2785; imports)
- Test: `tests/hermes_cli/test_kanban_dispatch_cli.py` (new)

The glue returns a **summary dict** (counts) plus `spawned_ids` / `auto_blocked_ids` lists. Output is rebuilt from that dict (some legacy id-lists like crashed/timed_out/stale become counts — acceptable; the gateway uses the same dict).

- [ ] **Step 1: Write the failing test (live tick routes through the glue, mock spawn)**

Create `tests/hermes_cli/test_kanban_dispatch_cli.py`:

```python
from __future__ import annotations
from pathlib import Path
import argparse
import pytest
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "sqlite")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: False)
    kb.init_db()
    return home


def test_dispatch_live_tick_spawns_via_glue(kanban_home, monkeypatch):
    # one ready, assigned task
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="do it", assignee="engineer")
    # capture spawns instead of launching a real worker
    spawned = []
    monkeypatch.setattr(kb, "_default_spawn", lambda task, ws, **kw: spawned.append(task.id) or 4321)
    monkeypatch.setattr(kb, "resolve_workspace", lambda *a, **k: str(kanban_home))
    from hermes_cli.kanban.cli import _cmd_dispatch
    rc = _cmd_dispatch(argparse.Namespace(dry_run=False, json=True, max=None,
                                          failure_limit=kb.DEFAULT_SPAWN_FAILURE_LIMIT))
    assert rc == 0
    assert spawned == [tid]
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_dispatch_cli.py::test_dispatch_live_tick_spawns_via_glue -q -p no:cacheprovider`
Expected: FAIL (current code path uses `kb.dispatch_once`; the monkeypatched `_default_spawn` is still called by dispatch_once, so this may pass accidentally — if so, additionally assert the glue path by patching `kb.connect` to raise and asserting the store path still spawns; see Step 3 note).

- [ ] **Step 3: Replace the dispatch DB call with the glue**

In `_cmd_dispatch`, replace the `with kb.connect_closing() as conn: res = kb.dispatch_once(conn, …)` block (the live, non-dry-run path) with a glue call mirroring `gateway/run.py:6987`. Add `import os` if not present at module top. Build the result dict:

```python
    from hermes_cli import kanban_glue as _glue
    try:
        from hermes_cli.profiles import profile_exists as _profile_exists
    except Exception:
        _profile_exists = None
    store = _make_store()
    try:
        summary = _glue.run_dispatch_tick(
            store,
            board=kb.get_current_board(),
            spawn_fn=kb._default_spawn,
            resolve_workspace=kb.resolve_workspace,
            profile_exists=_profile_exists,
            terminate_fn=lambda pid, lock: kb._terminate_reclaimed_worker(pid, lock, signal_fn=os.kill),
            pid_alive_fn=kb._pid_alive,
            classify_exit_fn=kb._classify_worker_exit,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=getattr(args, "failure_limit", kb.DEFAULT_SPAWN_FAILURE_LIMIT),
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
        )
    finally:
        store.close()
    if getattr(args, "json", False):
        print(json.dumps(summary, indent=2))
        return 0
    # Human output rebuilt from the glue summary dict (counts; ids in *_ids).
    print(f"Reclaimed:    {summary.get('reclaimed', 0)}")
    print(f"Crashed:      {summary.get('crashed', 0)}")
    print(f"Timed out:    {summary.get('timed_out', 0)}")
    print(f"Stale:        {summary.get('stale', 0)}")
    auto_ids = summary.get('auto_blocked_ids') or []
    print(f"Auto-blocked: {summary.get('auto_blocked', len(auto_ids))}")
    if auto_ids:
        print(f"  {', '.join(auto_ids)}")
    print(f"Promoted:     {summary.get('promoted', 0)}")
    spawned_ids = summary.get('spawned_ids') or []
    print(f"Spawned:      {summary.get('spawned', len(spawned_ids))}")
    for tid in spawned_ids:
        print(f"  - {tid}")
    if summary.get('skipped_unassigned'):
        print(f"Skipped (unassigned): {summary['skipped_unassigned']}")
    return 0
```

Keep the existing config-reading block (default_assignee / max_in_progress / max_spawn / max_in_progress_per_profile) above this. The `--dry-run` path is handled in Task 5 (it must branch *before* this live tick). For now, guard: if `args.dry_run`, fall through to Task 5's preview (added next); until then, leave the legacy `dispatch_once(dry_run=True)` call in place for the dry-run branch only.

- [ ] **Step 4: Run to verify pass**

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_dispatch_cli.py::test_dispatch_live_tick_spawns_via_glue -q -p no:cacheprovider`
Expected: PASS (task claimed → running, spawn captured).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/cli.py tests/hermes_cli/test_kanban_dispatch_cli.py
git commit -m "feat(kanban-pg): route kanban dispatch live tick through run_dispatch_tick (B7-tail Tier 3)"
```

---

## Task 5: Tier 3 — `dispatch --dry-run` read-only preview + double-dispatch warning

**Files:**
- Modify: `hermes_cli/kanban/cli.py` (`_cmd_dispatch` dry-run branch + a `_dispatch_preview` helper)
- Test: `tests/hermes_cli/test_kanban_dispatch_cli.py` (extend)

Read-only, **approximate** preview: lists current `ready` tasks that have an assignee (the tasks the next tick would most likely claim), without mutating. Promotable-`todo` is intentionally excluded (documented approximation; the live tick is authoritative).

- [ ] **Step 1: Write the failing dry-run + warning tests**

Append to `tests/hermes_cli/test_kanban_dispatch_cli.py`:

```python
def test_dispatch_dry_run_is_read_only(kanban_home, monkeypatch, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ready one", assignee="engineer")
    # spawn must NEVER be called in dry-run
    monkeypatch.setattr(kb, "_default_spawn",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned in dry-run")))
    from hermes_cli.kanban.cli import _cmd_dispatch
    rc = _cmd_dispatch(argparse.Namespace(dry_run=True, json=False, max=None,
                                          failure_limit=kb.DEFAULT_SPAWN_FAILURE_LIMIT))
    assert rc == 0
    out = capsys.readouterr().out
    assert tid in out
    assert "preview" in out.lower()
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"  # NOT mutated
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_dispatch_cli.py::test_dispatch_dry_run_is_read_only -q -p no:cacheprovider`
Expected: FAIL — legacy dry-run path doesn't print "preview" / may use the old format.

- [ ] **Step 3: Add the read-only preview branch + helper**

In `hermes_cli/kanban/cli.py`, add a helper:

```python
def _dispatch_preview(store, *, default_assignee: Optional[str]) -> list:
    """Read-only, APPROXIMATE preview of what the next dispatch tick would
    consider: current 'ready' tasks with an assignee (or the configured
    default_assignee). Mutates nothing. The live tick is authoritative."""
    tasks = store.list_tasks(status="ready")
    out = []
    for t in tasks:
        who = getattr(t, "assignee", None) or default_assignee
        if who:
            out.append((t.id, who))
    return out
```

At the top of `_cmd_dispatch`, after computing `default_assignee` etc., branch on dry-run BEFORE the live tick and remove the legacy `dispatch_once(dry_run=True)` path entirely:

```python
    if getattr(args, "dry_run", False):
        store = _make_store()
        try:
            candidates = _dispatch_preview(store, default_assignee=default_assignee)
        finally:
            store.close()
        if getattr(args, "json", False):
            print(json.dumps({"preview": True, "candidates": [
                {"task_id": tid, "assignee": who} for tid, who in candidates]}, indent=2))
            return 0
        print("Dispatch preview (read-only, approximate — the live tick is authoritative):")
        if not candidates:
            print("  (no ready, assigned tasks)")
        for tid, who in candidates:
            print(f"  - {tid}  ->  {who}")
        return 0
```

- [ ] **Step 4: Add the double-dispatch warning to the live tick**

Just before the live `run_dispatch_tick` call (Task 4 block), add:

```python
    try:
        from hermes_cli.gateway import find_gateway_pids
        if find_gateway_pids():
            print(
                "warning: the gateway is running and may already be dispatching; "
                "a manual tick can double-spawn. Use the gateway's embedded "
                "dispatcher, or stop it first.",
                file=sys.stderr,
            )
    except Exception:
        pass
```

- [ ] **Step 5: Add a warning test**

Append:

```python
def test_dispatch_warns_when_gateway_running(kanban_home, monkeypatch, capsys):
    with kb.connect() as conn:
        kb.create_task(conn, title="x", assignee="engineer")
    monkeypatch.setattr(kb, "_default_spawn", lambda *a, **k: None)
    monkeypatch.setattr(kb, "resolve_workspace", lambda *a, **k: str(kanban_home))
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [9999])
    from hermes_cli.kanban.cli import _cmd_dispatch
    _cmd_dispatch(argparse.Namespace(dry_run=False, json=True, max=None,
                                     failure_limit=kb.DEFAULT_SPAWN_FAILURE_LIMIT))
    assert "double-spawn" in capsys.readouterr().err
```

- [ ] **Step 6: Run the dispatch tests to verify pass**

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_dispatch_cli.py -q -p no:cacheprovider`
Expected: PASS (all 3).

- [ ] **Step 7: Commit**

```bash
git add hermes_cli/kanban/cli.py tests/hermes_cli/test_kanban_dispatch_cli.py
git commit -m "feat(kanban-pg): dispatch --dry-run read-only preview + double-dispatch warning (B7-tail Tier 3)"
```

---

## Task 6: Full verification + isolated live-PG smoke

**Files:** none (verification only)

- [ ] **Step 1: Full kanban + affected suites green (sqlite param)**

Run:
```bash
HERMES_KANBAN_BACKEND=sqlite venv/bin/python -m pytest \
  tests/hermes_cli/kanban/test_store_conformance.py \
  tests/hermes_cli/test_kanban_swarm.py \
  tests/hermes_cli/test_kanban_archive_rm.py \
  tests/hermes_cli/test_kanban_dispatch_cli.py \
  tests/hermes_cli/test_kanban_closeout_guard.py \
  -q -p no:cacheprovider
```
Expected: all PASS; PG-param conformance `s` (skipped — no docker).

- [ ] **Step 2: Structural/import check of the PG store**

Run:
```bash
venv/bin/python -c "import hermes_cli.kanban.store_postgres as s; assert hasattr(s.PostgresKanbanStore,'delete_archived_task')"
venv/bin/python -m py_compile hermes_cli/kanban/store_postgres.py hermes_cli/kanban/store_sqlite.py hermes_cli/kanban/store.py hermes_cli/kanban_swarm.py hermes_cli/kanban/cli.py
```
Expected: no errors.

- [ ] **Step 3: Isolated live-PG smoke (throwaway board — NO `default` board mutation)**

For the PG-specific paths the docker-less suite can't cover (`delete_archived_task` PG, swarm-via-store on PG), run a self-cleaning smoke on a junk board slug `_b7tail_smoke` against live PG: create → archive → `delete_archived_task` → assert gone; then a `create_swarm` on the same junk board → assert topology → clean up `DELETE … WHERE board='_b7tail_smoke'` across all kanban tables. **This touches live PG only on a throwaway board; it never touches `default` and runs no real `--rm` against `default`.** Author this as a one-off script at smoke time; confirm with the user before running if any doubt.

- [ ] **Step 4: NO live `default`-board `--rm` delete** — do not run `hermes kanban archive --rm --confirm` against a real `default` task without explicit user go-ahead. Dry-run preview against `default` is read-only and OK to demo.

---

## Self-review notes (addressed)

- **Spec coverage:** Tier 2 (Task 1), Tier 4 store method (Task 2) + CLI guard (Task 3), Tier 3 live tick (Task 4) + dry-run/warning (Task 5). Verification (Task 6).
- **No-placeholder:** all steps carry real code/commands. The dry-run preview is intentionally approximate (documented), and the dispatch human-output loses some legacy id-lists (crashed/timed_out/stale → counts) because the glue returns a summary dict — documented in Task 4.
- **Type consistency:** `delete_archived_task(self, task_id: str) -> bool` identical across Protocol/sqlite/PG; `_dispatch_preview(store, *, default_assignee)` and the glue kwargs match `run_dispatch_tick` / `gateway/run.py:6987`.

## Forbidden files (do not edit)
`hermes_cli/kanban_db.py`, `hermes_cli/kanban_liveness.py`, `hermes_cli/kanban_writer_daemon.py`.
