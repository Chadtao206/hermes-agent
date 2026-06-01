# Phase 6 · B1 — specify + decompose + PG reconciler — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Under `kanban.backend=postgres`, route `POST /tasks/{id}/specify`, `POST /tasks/{id}/decompose`, and `GET /reconcile` (+ `hermes kanban reconcile`) onto live Postgres instead of the frozen `~/.hermes/kanban.db`; keep the sqlite path byte-identical.

**Architecture:** Two new atomic methods on `PostgresKanbanStore` (`specify_triage_task`, `decompose_triage_task`) mirroring their `kanban_db` twins event-for-event; `kanban_specify`/`kanban_decompose` get a `resolve_backend()=="postgres"` branch at the DB-touch points only. A PG reconciler (`_run_reconciler_pg` + `_collect_reconcile_actions_pg`) mirrors `kanban_board_doctor._run_board_doctor_pg` for the 9 core action kinds (2 niche kinds deferred behind a logged `partial` note), reusing the pure downstream classifiers. The dashboard `/reconcile` PG no-op branch is removed (run_reconciler self-dispatches).

**Tech Stack:** Python, psycopg 3 (`dict_row`, `Jsonb`, `conn.transaction()`), pytest with a docker `postgres:16-alpine` fixture, FastAPI dashboard plugin.

---

## Ground rules (apply to EVERY task)

- **Never edit** `hermes_cli/kanban_db.py`, `hermes_cli/kanban_liveness.py`, `hermes_cli/kanban_writer_daemon.py` — import only.
- **sqlite byte-identical:** every change is `if resolve_backend()=="postgres": <new> else: <existing-verbatim>`. Do not touch sqlite code paths.
- **No DSN/secret in logs:** redact to `host:port/db` (reuse the doctor's `_redacted_pg_dsn`) or log only `type(exc).__name__`.
- **Lazy imports in PG branches:** `from hermes_cli.kanban.store_postgres import PostgresKanbanStore` and `from hermes_cli.kanban import pg_pool` go *inside* the PG branch, never at module top (sqlite-only/upstream deployments may lack psycopg).
- **resolve-bslug-once:** in a PG branch resolve `slug = kb.get_current_board()` exactly once and reuse it for the store + every PG read.
- **Test interpreter:** `cd .worktrees/kanban-pg-phase6-b1 && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`. Postgres tests need `HERMES_PG_TEST_DSN` exported to the throwaway docker container (the controlling session provides it). NEVER the live Supabase DB.
- **Commits:** end every commit message with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Reference source (read before implementing)

- `hermes_cli/kanban_db.py:5605-5693` — `specify_triage_task` (sqlite); `:5696-5894` — `decompose_triage_task` (sqlite). The PG methods mirror these event-for-event.
- `hermes_cli/kanban/store_postgres.py:53-260` — `PostgresKanbanStore` idioms: `_emit` (66), `_row_to_task` (87), `create_task` (106), `get_task` (228); module `_new_task_id` (51) = `"t_"+secrets.token_hex(4)`; `_canonical_assignee` imported (25); `recompute_ready` (586), `add_comment` (712), `list_comments` (734). Transaction idiom: `with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur: with conn.transaction(): ...`.
- `hermes_cli/kanban_board_doctor.py:134-268` — `_redacted_pg_dsn` + `_run_board_doctor_pg`: the connectivity-probe / board-scoped-SQL / `string_agg` / expanded-`GROUP BY` / repeated-aggregate-`HAVING` / board-scoped-JOIN template for the reconciler port.
- `hermes_cli/kanban_reconciler.py:775-1186` — `collect_reconcile_actions` (sqlite): the 9 core sections + 2 deferred. `:491-595` pure helpers (`_pid_alive`, `_signature`, `_action`, `_sort_actions`, `_profile_spawnable`, `_host_prefix`, `_is_review_lane`); `:658-688` `_pre_spawn_validation_errors_for_reconcile`; `:1189-1281` `actions_to_dicts`/`_filter_acknowledged_decision_packets`/`run_reconciler`; `:1317-1341` `_find_existing_reconcile_decision_comment`.

**The 9 core kinds to port:** `dead_running_candidate`, `expired_claim_candidate`, `stale_heartbeat_observed` (all from the `status='running'` scan); `stale_run_metadata`; `orphan_claim_lock_observed`; `blocked_with_completed_parents_decision`; `scheduled_with_completed_parents_decision`; `pre_spawn_validation_decision`; and the `old_ready_{spawnable,nonspawnable}` catch-all. **Deferred (2):** `review_parent_pr_head_evidence_missing` and `repeated_failure_signature_decision`.

---

## Task 1: `PostgresKanbanStore.specify_triage_task`

**Files:**
- Modify: `hermes_cli/kanban/store_postgres.py` (add method on `PostgresKanbanStore`, near `create_task`)
- Test: `tests/hermes_cli/kanban/test_store_specify_decompose_pg.py` (create)

**Review:** adversarial (live-core store method).

- [ ] **Step 1: Write the failing test** — create `tests/hermes_cli/kanban/test_store_specify_decompose_pg.py`:

```python
"""Cross-backend conformance for the triage composite writes (specify/decompose).

The composite methods live on PostgresKanbanStore only; the sqlite equivalents
are kanban_db.specify_triage_task / decompose_triage_task. These tests drive each
backend the backend-appropriate way and assert identical resulting state + event
shapes, so PG parity with sqlite is pinned.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban.store_postgres import PostgresKanbanStore


def _specify(store, task_id, **kw):
    """Specify on whichever backend `store` is."""
    if isinstance(store, PostgresKanbanStore):
        return store.specify_triage_task(task_id, **kw)
    with kb.connect_closing() as conn:
        return kb.specify_triage_task(conn, task_id, **kw)


def _kinds(store, task_id):
    return [e.kind for e in store.list_events(task_id)]


def test_specify_promotes_triage_to_todo_with_changes(store):
    tid = store.create_task(title="rough idea", triage=True)
    assert store.get_task(tid).status == "triage"
    ok = _specify(store, tid, title="Tightened title", body="**Goal** ...",
                  author="alice")
    assert ok is True
    t = store.get_task(tid)
    assert t.status == "todo"
    assert t.title == "Tightened title"
    assert t.body == "**Goal** ..."
    kinds = _kinds(store, tid)
    assert "specified" in kinds
    # audit comment recorded because fields changed + author given
    bodies = [c.body for c in store.list_comments(tid)]
    assert any("Specified" in b for b in bodies)


def test_specify_status_only_no_comment_no_changed_fields(store):
    tid = store.create_task(title="keep title", triage=True)
    ok = _specify(store, tid, author="alice")  # nothing changes but status
    assert ok is True
    assert store.get_task(tid).status == "todo"
    assert store.list_comments(tid) == []  # no audit comment for status-only


def test_specify_returns_false_when_not_in_triage(store):
    tid = store.create_task(title="already live")  # default status ready
    assert _specify(store, tid, title="x") is False
    assert store.get_task(tid).status != "triage"  # unchanged


def test_specify_blank_title_raises(store):
    tid = store.create_task(title="x", triage=True)
    with pytest.raises(ValueError):
        _specify(store, tid, title="   ")
```

- [ ] **Step 2: Run test — verify the PG cases fail**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_specify_decompose_pg.py -k specify -v`
Expected: sqlite params PASS (kb already implements it); `postgres` params FAIL with `AttributeError: 'PostgresKanbanStore' object has no attribute 'specify_triage_task'`.

- [ ] **Step 3: Implement `specify_triage_task` on `PostgresKanbanStore`** (insert after `get_task`/`list_tasks`, before the status-transition section). Mirrors `kanban_db.specify_triage_task:5605-5693` exactly:

```python
    def specify_triage_task(self, task_id, *, title=None, body=None,
                            assignee=None, author=None) -> bool:
        """Flesh out a triage task and promote it to ``todo`` (PG mirror of
        kanban_db.specify_triage_task). Single transaction; emits one
        ``specified`` event; optional inline audit comment; recompute_ready()
        outside the txn. Returns False when missing / not in triage."""
        if title is not None and not title.strip():
            raise ValueError("title cannot be blank")
        assignee = _canonical_assignee(assignee)
        promoted = False
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT title, body, assignee FROM tasks "
                    "WHERE board=%s AND id=%s AND status='triage'",
                    (self.board, task_id))
                existing = cur.fetchone()
                if existing is None:
                    return False
                sets = ["status='todo'"]
                params: list[Any] = []
                changed_fields: list[str] = []
                if title is not None and title.strip() != (existing["title"] or ""):
                    sets.append("title=%s")
                    params.append(title.strip())
                    changed_fields.append("title")
                if body is not None and (body or "") != (existing["body"] or ""):
                    sets.append("body=%s")
                    params.append(body)
                    changed_fields.append("body")
                if assignee is not None and assignee != (existing["assignee"] or None):
                    sets.append("assignee=%s")
                    params.append(assignee)
                    changed_fields.append("assignee")
                params.extend([self.board, task_id])
                cur.execute(
                    f"UPDATE tasks SET {', '.join(sets)} "
                    f"WHERE board=%s AND id=%s AND status='triage'",
                    tuple(params))
                if cur.rowcount != 1:
                    return False
                if changed_fields and author and author.strip():
                    cur.execute(
                        "INSERT INTO task_comments (board, task_id, author, body, created_at) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (self.board, task_id, author.strip(),
                         "Specified — updated " + ", ".join(changed_fields)
                         + " and promoted to todo.", int(time.time())))
                self._emit(cur, task_id, "specified",
                           {"changed_fields": changed_fields} if changed_fields else None)
                promoted = True
        if promoted:
            self.recompute_ready()
        return True
```

NOTE: confirm the PG `task_comments` column list against `hermes_cli/kanban/pg_schema.sql` before finalizing (sqlite omits `board`; PG includes it — every PG insert in this file already carries `board`). Adjust the column list to match the schema exactly.

- [ ] **Step 4: Run test — verify pass on both backends**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_specify_decompose_pg.py -k specify -v`
Expected: all PASS (sqlite + postgres).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/store_postgres.py tests/hermes_cli/kanban/test_store_specify_decompose_pg.py
git commit -m "feat(kanban-pg): PostgresKanbanStore.specify_triage_task (sqlite parity)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `PostgresKanbanStore.decompose_triage_task`

**Files:**
- Modify: `hermes_cli/kanban/store_postgres.py` (add method next to `specify_triage_task`)
- Test: `tests/hermes_cli/kanban/test_store_specify_decompose_pg.py` (extend)

**Review:** adversarial (live-core store method; atomicity-critical).

- [ ] **Step 1: Write the failing test** — append to the test file:

```python
def _decompose(store, task_id, **kw):
    if isinstance(store, PostgresKanbanStore):
        return store.decompose_triage_task(task_id, **kw)
    with kb.connect_closing() as conn:
        return kb.decompose_triage_task(conn, task_id, **kw)


def test_decompose_creates_child_graph_and_promotes_root(store):
    root = store.create_task(title="big idea", triage=True)
    children = [
        {"title": "child A", "body": "do A", "assignee": "engineer", "parents": []},
        {"title": "child B", "body": "do B", "assignee": "reviewer", "parents": [0]},
    ]
    child_ids = _decompose(store, root, root_assignee="orchestrator",
                           children=children, author="alice")
    assert isinstance(child_ids, list) and len(child_ids) == 2
    a, b = child_ids
    # root promoted + reassigned
    rt = store.get_task(root)
    assert rt.status == "todo"
    assert rt.assignee == "orchestrator"
    # children exist, status todo, assignees routed
    assert store.get_task(a).assignee == "engineer"
    assert store.get_task(b).assignee == "reviewer"
    # B depends on A (sibling link); root depends on both children
    assert a in store.parent_ids(b)            # A is a parent of B
    assert set(store.parent_ids(root)) >= {a, b}  # root waits on both children
    # events on root + children
    assert "decomposed" in [e.kind for e in store.list_events(root)]
    assert "created" in [e.kind for e in store.list_events(a)]
    assert "linked" in [e.kind for e in store.list_events(b)]


def test_decompose_returns_none_when_not_in_triage(store):
    live = store.create_task(title="live")  # ready, not triage
    assert _decompose(store, live, root_assignee=None,
                      children=[{"title": "c"}]) is None


def test_decompose_empty_children_returns_none(store):
    root = store.create_task(title="x", triage=True)
    assert _decompose(store, root, root_assignee=None, children=[]) is None
    assert store.get_task(root).status == "triage"  # unchanged


def test_decompose_cycle_raises(store):
    root = store.create_task(title="x", triage=True)
    children = [{"title": "a", "parents": [1]}, {"title": "b", "parents": [0]}]
    with pytest.raises(ValueError):
        _decompose(store, root, root_assignee=None, children=children)
    assert store.get_task(root).status == "triage"  # atomic abort, no children
```

- [ ] **Step 2: Run test — verify PG cases fail**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_specify_decompose_pg.py -k decompose -v`
Expected: sqlite PASS; postgres FAIL (`AttributeError: ... 'decompose_triage_task'`).

- [ ] **Step 3: Implement `decompose_triage_task` on `PostgresKanbanStore`.** Mirrors `kanban_db.decompose_triage_task:5696-5894`. Pre-validation + cycle detection are pure Python copied verbatim from the sqlite source (lines 5731-5776); the transaction body uses PG SQL + `_emit`. Use the module `_new_task_id()` and imported `_canonical_assignee`:

```python
    def decompose_triage_task(self, task_id, *, root_assignee, children,
                              author=None, auto_promote=True):
        """Fan a triage task into a child graph and promote the root to ``todo``
        (PG mirror of kanban_db.decompose_triage_task). One transaction; emits
        created/linked/decomposed events; recompute_ready() after iff auto_promote.
        Returns child ids (input order) or None (missing/not-triage/empty/cycle)."""
        if not children:
            return None
        if root_assignee is not None:
            root_assignee = _canonical_assignee(root_assignee)
        # --- pre-validate child shape (verbatim from kanban_db) ---
        for idx, child in enumerate(children):
            if not isinstance(child, dict):
                raise ValueError(f"child[{idx}] is not a dict")
            title = child.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f"child[{idx}].title is required")
            parents_idx = child.get("parents") or []
            if not isinstance(parents_idx, list):
                raise ValueError(f"child[{idx}].parents must be a list")
            for p in parents_idx:
                if not isinstance(p, int) or p < 0 or p >= len(children):
                    raise ValueError(
                        f"child[{idx}].parents[{p}] is not a valid index into children")
                if p == idx:
                    raise ValueError(f"child[{idx}] cannot list itself as a parent")
        # --- cycle detection (Kahn, verbatim from kanban_db) ---
        _in_deg = [0] * len(children)
        _adj: list[list[int]] = [[] for _ in range(len(children))]
        for _i, _c in enumerate(children):
            for _p in (_c.get("parents") or []):
                _adj[_p].append(_i)
                _in_deg[_i] += 1
        _queue = [_i for _i in range(len(children)) if _in_deg[_i] == 0]
        _seen = 0
        while _queue:
            _node = _queue.pop()
            _seen += 1
            for _nb in _adj[_node]:
                _in_deg[_nb] -= 1
                if _in_deg[_nb] == 0:
                    _queue.append(_nb)
        if _seen != len(children):
            raise ValueError("cyclic dependency detected in decomposed children list")

        now = int(time.time())
        child_ids: list[str] = []
        committed = False
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT id, status, tenant FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                root_row = cur.fetchone()
                if root_row is None or root_row["status"] != "triage":
                    return None
                tenant = root_row["tenant"]
                # create children (status='todo', workspace_kind='scratch')
                for child in children:
                    new_id = _new_task_id()
                    body = child.get("body")
                    cur.execute(
                        "INSERT INTO tasks (board, id, title, body, assignee, status, "
                        "workspace_kind, tenant, created_at, created_by) "
                        "VALUES (%s,%s,%s,%s,%s,'todo','scratch',%s,%s,%s)",
                        (self.board, new_id, child["title"].strip(),
                         body if isinstance(body, str) else None,
                         _canonical_assignee(child.get("assignee")),
                         tenant, now, (author or "decomposer")))
                    self._emit(cur, new_id, "created",
                               {"by": author or "decomposer",
                                "from_decompose_of": task_id})
                    child_ids.append(new_id)
                # sibling parent links
                for idx, child in enumerate(children):
                    for p_idx in child.get("parents") or []:
                        parent_id = child_ids[p_idx]
                        child_id = child_ids[idx]
                        cur.execute(
                            "INSERT INTO task_links (board, parent_id, child_id) "
                            "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            (self.board, parent_id, child_id))
                        self._emit(cur, child_id, "linked",
                                   {"parent": parent_id, "child": child_id})
                # root as child of every child (root waits for the whole graph)
                for cid in child_ids:
                    cur.execute(
                        "INSERT INTO task_links (board, parent_id, child_id) "
                        "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (self.board, cid, task_id))
                # flip root triage -> todo (+ assignee)
                sets = ["status='todo'"]
                params: list[Any] = []
                if root_assignee is not None:
                    sets.append("assignee=%s")
                    params.append(root_assignee)
                params.extend([self.board, task_id])
                cur.execute(
                    f"UPDATE tasks SET {', '.join(sets)} WHERE board=%s AND id=%s",
                    tuple(params))
                if author and author.strip():
                    cur.execute(
                        "INSERT INTO task_comments (board, task_id, author, body, created_at) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (self.board, task_id, author.strip(),
                         "Decomposed into " + ", ".join(child_ids)
                         + ". Root will wake when all children complete.", now))
                self._emit(cur, task_id, "decomposed",
                           {"child_ids": child_ids, "root_assignee": root_assignee})
                committed = True
        if committed and auto_promote:
            self.recompute_ready()
        return child_ids
```

NOTE: verify the `task_links` PG column list (the schema may carry `relation_type` with a default; sqlite's `INSERT OR IGNORE INTO task_links (parent_id, child_id)` omits it). `create_task` (line 217) inserts `task_links (board, parent_id, child_id, relation_type) ... 'dependency'`. To match sqlite's dependency default, insert `relation_type='dependency'` explicitly (or rely on the column default if the schema sets one — check `pg_schema.sql`). Use the same form `create_task` uses.

- [ ] **Step 4: Run test — verify pass on both backends**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_specify_decompose_pg.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/store_postgres.py tests/hermes_cli/kanban/test_store_specify_decompose_pg.py
git commit -m "feat(kanban-pg): PostgresKanbanStore.decompose_triage_task (sqlite parity)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: route `kanban_specify` through PG

**Files:**
- Modify: `hermes_cli/kanban_specify.py` (`specify_task` read+write branch; `list_triage_ids` branch)
- Test: `tests/hermes_cli/test_kanban_specify_pg.py` (create)

**Review:** adversarial (live dashboard/CLI write path).

- [ ] **Step 1: Write the failing test** — `tests/hermes_cli/test_kanban_specify_pg.py`. Drives `specify_task` with the backend forced to postgres via a monkeypatched `resolve_backend` + a PG store on a test board, stubbing the aux LLM:

```python
"""kanban_specify routes its DB read+write through Postgres under backend=postgres."""
import uuid
import pytest

from hermes_cli import kanban_specify
from hermes_cli.kanban import pg_pool
from hermes_cli.kanban.store_postgres import PostgresKanbanStore


@pytest.fixture
def pg_store(_pg_dsn, monkeypatch):
    pool = pg_pool.make_pool(_pg_dsn)
    pg_pool.ensure_schema(pool)
    board = f"spec_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: board)
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s
    finally:
        s.close(); pool.close()


def _stub_llm(monkeypatch, title, body):
    class _Msg:  # minimal OpenAI-shaped response
        def __init__(self): self.content = f'{{"title": "{title}", "body": "{body}"}}'
    class _Choice:
        def __init__(self): self.message = _Msg()
    class _Resp:
        def __init__(self): self.choices = [_Choice()]
    class _Completions:
        def create(self, **kw): return _Resp()
    class _Chat:
        def __init__(self): self.completions = _Completions()
    class _Client:
        def __init__(self): self.chat = _Chat()
    import agent.auxiliary_client as ac
    monkeypatch.setattr(ac, "get_text_auxiliary_client", lambda *a, **k: (_Client(), "m"))
    monkeypatch.setattr(ac, "get_auxiliary_extra_body", lambda *a, **k: None)


def test_specify_task_writes_postgres(pg_store, monkeypatch):
    tid = pg_store.create_task(title="rough", triage=True)
    _stub_llm(monkeypatch, "Tightened", "Goal body")
    outcome = kanban_specify.specify_task(tid, author="alice")
    assert outcome.ok is True
    t = pg_store.get_task(tid)
    assert t.status == "todo"            # mutated LIVE pg, not frozen sqlite
    assert t.title == "Tightened"


def test_list_triage_ids_reads_postgres(pg_store, monkeypatch):
    a = pg_store.create_task(title="t1", triage=True)
    pg_store.create_task(title="live")  # ready, excluded
    ids = kanban_specify.list_triage_ids()
    assert a in ids
```

(Conftest note: this test needs the `_pg_dsn` fixture. Add a thin `tests/hermes_cli/conftest.py` that imports/reuses `_pg_dsn` from the kanban conftest, or move the test under `tests/hermes_cli/kanban/`. Prefer placing the file at `tests/hermes_cli/kanban/test_kanban_specify_pg.py` so the existing `_pg_dsn` fixture is in scope.)

- [ ] **Step 2: Run test — verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_specify_pg.py -v`
Expected: FAIL — `specify_task` still calls `kb.connect_closing()` (writes the default sqlite DB), so `pg_store.get_task(tid).status` stays `triage`.

- [ ] **Step 3: Implement the PG branch in `kanban_specify.py`.** Replace the two `kb.connect_closing()` DB-touch points in `specify_task` with a backend branch, and add a branch to `list_triage_ids`. The LLM/parse middle is unchanged.

Read (top of `specify_task`, currently lines 153-160):
```python
    backend = _resolve_backend()
    _pg = None
    if backend == "postgres":
        _pg = _pg_store()                 # constructs PostgresKanbanStore(board=slug), slug resolved once
        task = _pg.get_task(task_id)
    else:
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, task_id)
```

Write (bottom of `specify_task`, currently lines 242-249):
```python
    if backend == "postgres":
        ok = _pg.specify_triage_task(
            task_id, title=new_title, body=new_body,
            author=author or _profile_author())
    else:
        with kb.connect_closing() as conn:
            ok = kb.specify_triage_task(
                conn, task_id, title=new_title, body=new_body,
                author=author or _profile_author())
```

Add module-level helpers (lazy imports inside, so sqlite-only deployments never import psycopg):
```python
def _resolve_backend() -> str:
    try:
        from hermes_cli.kanban.store import resolve_backend
        return resolve_backend()
    except Exception:
        return "sqlite"


def _pg_store():
    """Construct a PG store bound to the current board (resolve slug once)."""
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    return PostgresKanbanStore(board=kb.get_current_board())
```

`list_triage_ids` PG branch:
```python
def list_triage_ids(*, tenant=None):
    if _resolve_backend() == "postgres":
        tasks = _pg_store().list_tasks(status="triage", tenant=tenant,
                                       include_archived=False)
        return [t.id for t in tasks]
    with kb.connect_closing() as conn:                       # EXISTING verbatim
        tasks = kb.list_tasks(conn, status="triage", tenant=tenant,
                              include_archived=False)
    return [t.id for t in tasks]
```

- [ ] **Step 4: Run test — verify pass; confirm sqlite suite green**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_specify_pg.py -v` → PASS.
Run: `venv/bin/python -m pytest tests/hermes_cli -k specify -v` → existing sqlite specify tests PASS (sqlite branch unchanged).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_specify.py tests/hermes_cli/kanban/test_kanban_specify_pg.py
git commit -m "feat(kanban-pg): route kanban_specify through the store under backend=postgres

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: route `kanban_decompose` through PG

**Files:**
- Modify: `hermes_cli/kanban_decompose.py` (`decompose_task` read + both write branches; `list_triage_ids`)
- Test: `tests/hermes_cli/kanban/test_kanban_decompose_pg.py` (create)

**Review:** adversarial (live dashboard/CLI write path).

- [ ] **Step 1: Write the failing test** — mirror Task 3's fixture (copy the `pg_store` fixture + `_stub_llm`, but stub the **decomposer** model name and return a fanout graph). Place at `tests/hermes_cli/kanban/test_kanban_decompose_pg.py`:

```python
"""kanban_decompose routes through Postgres under backend=postgres."""
import json, uuid
import pytest

from hermes_cli import kanban_decompose
from hermes_cli.kanban import pg_pool
from hermes_cli.kanban.store_postgres import PostgresKanbanStore


@pytest.fixture
def pg_store(_pg_dsn, monkeypatch):
    pool = pg_pool.make_pool(_pg_dsn); pg_pool.ensure_schema(pool)
    board = f"dec_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: board)
    # make the decomposer's chosen assignees valid + the default route resolvable
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda n: True)
    monkeypatch.setattr("hermes_cli.profiles.list_profiles", lambda: [])
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "orchestrator")
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s
    finally:
        s.close(); pool.close()


def _stub_decomposer(monkeypatch, payload: dict):
    raw = json.dumps(payload)
    class _Msg:
        def __init__(self): self.content = raw
    class _Choice:
        def __init__(self): self.message = _Msg()
    class _Resp:
        def __init__(self): self.choices = [_Choice()]
    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw): return _Resp()
    import agent.auxiliary_client as ac
    monkeypatch.setattr(ac, "get_text_auxiliary_client", lambda *a, **k: (_Client(), "m"))
    monkeypatch.setattr(ac, "get_auxiliary_extra_body", lambda *a, **k: None)


def test_decompose_fanout_writes_postgres(pg_store, monkeypatch):
    root = pg_store.create_task(title="big", triage=True)
    _stub_decomposer(monkeypatch, {
        "fanout": True, "rationale": "split",
        "tasks": [
            {"title": "A", "body": "a", "assignee": "engineer", "parents": []},
            {"title": "B", "body": "b", "assignee": "reviewer", "parents": [0]},
        ]})
    outcome = kanban_decompose.decompose_task(root, author="alice")
    assert outcome.ok is True and outcome.fanout is True
    assert len(outcome.child_ids) == 2
    assert pg_store.get_task(root).status == "todo"        # LIVE pg
    assert "decomposed" in [e.kind for e in pg_store.list_events(root)]


def test_decompose_no_fanout_specifies_postgres(pg_store, monkeypatch):
    root = pg_store.create_task(title="single", triage=True)
    _stub_decomposer(monkeypatch, {
        "fanout": False, "rationale": "one unit",
        "title": "Tightened", "body": "spec", "assignee": "engineer"})
    outcome = kanban_decompose.decompose_task(root, author="alice")
    assert outcome.ok is True and outcome.fanout is False
    assert pg_store.get_task(root).status == "todo"
    assert "specified" in [e.kind for e in pg_store.list_events(root)]
```

- [ ] **Step 2: Run test — verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_decompose_pg.py -v`
Expected: FAIL — root stays `triage` on the PG board (writes hit sqlite).

- [ ] **Step 3: Implement the PG branch in `kanban_decompose.py`.** Same helper pattern as Task 3 (`_resolve_backend`, `_pg_store`). Branch the root read (lines 284-286), the fanout=false write (lines 373-381), and the fanout=true write (lines 442-450); add the `list_triage_ids` branch.

Read root:
```python
    backend = _resolve_backend()
    _pg = _pg_store() if backend == "postgres" else None
    if backend == "postgres":
        task = _pg.get_task(task_id)
    else:
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, task_id)
```

fanout=false write:
```python
        if backend == "postgres":
            ok = _pg.specify_triage_task(
                task_id, title=title_val, body=body_val,
                assignee=assignee_val, author=audit_author)
        else:
            with kb.connect_closing() as conn:
                ok = kb.specify_triage_task(
                    conn, task_id, title=title_val, body=body_val,
                    assignee=assignee_val, author=audit_author)
```

fanout=true write (keep the existing `try/except ValueError/Exception` wrapper; only the inner call branches):
```python
    try:
        if backend == "postgres":
            child_ids = _pg.decompose_triage_task(
                task_id, root_assignee=orchestrator, children=children,
                author=audit_author, auto_promote=auto_promote)
        else:
            with kb.connect_closing() as conn:
                child_ids = kb.decompose_triage_task(
                    conn, task_id, root_assignee=orchestrator, children=children,
                    author=audit_author, auto_promote=auto_promote)
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")
```

Add the same `_resolve_backend` / `_pg_store` helpers as Task 3, and the `list_triage_ids` PG branch (status="triage", limit=1000 to match the existing sqlite call).

- [ ] **Step 4: Run test — verify pass; sqlite decompose suite green**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_decompose_pg.py -v` → PASS.
Run: `venv/bin/python -m pytest tests/hermes_cli -k decompose -v` → existing sqlite decompose tests PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_decompose.py tests/hermes_cli/kanban/test_kanban_decompose_pg.py
git commit -m "feat(kanban-pg): route kanban_decompose through the store under backend=postgres

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: PG reconciler collector (`_collect_reconcile_actions_pg`, 9 core kinds)

**Files:**
- Modify: `hermes_cli/kanban_reconciler.py` (add `_run_reconciler_pg`-supporting collector + a pure comment-matcher; refactor `_find_existing_reconcile_decision_comment` to use it)
- Test: `tests/hermes_cli/kanban/test_kanban_reconciler_pg.py` (create)

**Review:** adversarial (live diagnostic path; read-only).

- [ ] **Step 1: Write the failing test.** Seed each of the 9 core kinds on a PG board and assert the corresponding action kind appears. Use the `_pg_dsn` fixture + a `PostgresKanbanStore`; manipulate rows via the store where possible and raw PG SQL for crash-lane fields. Place at `tests/hermes_cli/kanban/test_kanban_reconciler_pg.py`:

```python
"""PG reconciler: collector emits the 9 core action kinds; 2 niche deferred."""
import time, uuid
import pytest

from hermes_cli.kanban import pg_pool
from hermes_cli.kanban.store_postgres import PostgresKanbanStore
from hermes_cli import kanban_reconciler as krec


@pytest.fixture
def pg(_pg_dsn):
    pool = pg_pool.make_pool(_pg_dsn); pg_pool.ensure_schema(pool)
    board = f"rec_{uuid.uuid4().hex[:8]}"
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s, pool, board
    finally:
        s.close(); pool.close()


def _kinds(pool, board, **kw):
    with pool.connection() as conn:
        actions = krec._collect_reconcile_actions_pg(
            conn, board, ready_age_seconds=kw.get("ready_age_seconds", 900),
            now=kw.get("now", int(time.time())))
    return {a.kind for a in actions}


def test_orphan_claim_lock_observed(pg):
    s, pool, board = pg
    t = s.create_task(title="x")                  # ready
    with pool.connection() as conn:               # set a claim_lock while not running
        conn.execute("UPDATE tasks SET claim_lock=%s WHERE board=%s AND id=%s",
                     ("host:abc", board, t)); conn.commit()
    assert "orphan_claim_lock_observed" in _kinds(pool, board)


def test_old_ready_nonspawnable(pg):
    s, pool, board = pg
    t = s.create_task(title="x", assignee=None)   # unassigned -> nonspawnable
    old = int(time.time()) - 10_000
    with pool.connection() as conn:
        conn.execute("UPDATE tasks SET created_at=%s WHERE board=%s AND id=%s",
                     (old, board, t)); conn.commit()
    assert "old_ready_nonspawnable" in _kinds(pool, board, ready_age_seconds=900)


def test_blocked_with_completed_parents_decision(pg):
    s, pool, board = pg
    parent = s.create_task(title="p")
    child = s.create_task(title="c", parents=[parent])
    s.complete_task(parent, summary="done")       # parent -> done
    s.block_task(child, reason="manual")
    assert "blocked_with_completed_parents_decision" in _kinds(pool, board)


# ... seed tests for: dead_running_candidate (running + dead worker_pid),
# expired_claim_candidate (running + claim_expires in the past),
# stale_heartbeat_observed (running + old last_heartbeat_at),
# stale_run_metadata (running task_run not the current run),
# scheduled_with_completed_parents_decision (scheduled + terminal parents + age),
# pre_spawn_validation_decision (ready + missing profile/skill). One test each.


def test_deferred_kinds_absent_from_collector(pg):
    s, pool, board = pg
    # the collector never emits the 2 deferred kinds
    kinds = _kinds(pool, board)
    assert "review_parent_pr_head_evidence_missing" not in kinds
    assert "repeated_failure_signature_decision" not in kinds
```

- [ ] **Step 2: Run test — verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_reconciler_pg.py -v`
Expected: FAIL — `AttributeError: module 'hermes_cli.kanban_reconciler' has no attribute '_collect_reconcile_actions_pg'`.

- [ ] **Step 3: Implement `_collect_reconcile_actions_pg`.** Add to `kanban_reconciler.py`. It takes an open psycopg connection, the board `slug`, `ready_age_seconds`, `now`; returns `list[ReconcileAction]`. Translate each of the 9 sqlite queries (`collect_reconcile_actions:789-1184`) to board-scoped PG SQL using the board_doctor template (`string_agg`, expanded `GROUP BY`, repeated-aggregate `HAVING`, `JOIN ... AND p.board=l.board`, `id = ANY(%s)`), reusing the pure helpers verbatim. Use `conn.cursor(row_factory=dict_row)`. **Do NOT** emit the 2 deferred kinds. For `pre_spawn_validation_decision`, build a `kb.Task` per ready row via `PostgresKanbanStore(board=slug, pool=...).get_task(id)` — or, since you already hold `conn`, fetch `SELECT *` and map with the store's `_row_to_task`. Skeleton (each block mirrors the sqlite section's details dict EXACTLY — copy the `_action(...)` calls and `details={...}` verbatim from the sqlite source; only the row source changes):

```python
def _collect_reconcile_actions_pg(conn, slug, *, ready_age_seconds=15*60, now=None):
    """PG mirror of collect_reconcile_actions for the 9 core kinds. Read-only.
    Deferred (not emitted): review_parent_pr_head_evidence_missing,
    repeated_failure_signature_decision."""
    from psycopg.rows import dict_row
    as_of = int(now if now is not None else time.time())
    ready_age_seconds = max(1, int(ready_age_seconds or 900))
    actions: list[ReconcileAction] = []
    host_prefix = _host_prefix()
    store = PostgresKanbanStore_for(slug, conn)  # see note below for Task-mapping

    with conn.cursor(row_factory=dict_row) as cur:
        # 1) running tasks -> dead/expired/stale_heartbeat (mirror lines 789-851)
        cur.execute(
            "SELECT id, title, assignee, worker_pid, claim_lock, claim_expires, "
            "last_heartbeat_at, current_run_id, started_at FROM tasks "
            "WHERE board=%s AND status='running' ORDER BY started_at, created_at, id",
            (slug,))
        for row in cur.fetchall():
            # ... copy the pid_alive/expired/stale_hb logic + the three
            #     _action(...) appends + details dicts VERBATIM from 798-851 ...
            ...
        # 2) stale_run_metadata (mirror 855-881) -- board-scoped JOIN
        # 3) orphan_claim_lock_observed (mirror 890-923)
        # 4) blocked_with_completed_parents_decision (mirror 927-953)
        #    SQL via the doctor template: string_agg, GROUP BY c.id,c.title,c.assignee,c.created_at,
        #    HAVING COUNT(l.parent_id)>0 AND COUNT(...)=SUM(...)
        # 5) scheduled_with_completed_parents_decision (mirror 960-999) + age gate
        # 8) pre_spawn_validation_decision (mirror 1049-1079): for each ready+unclaimed
        #    row, task = store.get_task(row["id"]); errors = _pre_spawn_validation_errors_for_reconcile(task)
        # 9) old_ready_{spawnable,nonspawnable} (mirror 1159-1184)
        ...
    return _sort_actions(actions)
```

NOTE (Task-mapping): the cleanest way to get a `kb.Task` for `pre_spawn_validation_decision` is to construct a `PostgresKanbanStore(board=slug)` bound to the same pool and call `.get_task(id)`. Pass the store in from `_run_reconciler_pg` (Task 6) rather than re-resolving the pool here — `_collect_reconcile_actions_pg(conn, slug, store=..., ...)`. Define the signature as `_collect_reconcile_actions_pg(conn, slug, store, *, ready_age_seconds, now)`.

Also add the pure comment-matcher (used by Task 6's PG filter) and refactor the sqlite finder to reuse it (sqlite behavior unchanged — same three-marker match):

```python
def _reconcile_decision_comment_matches(comment, *, option, packet_signature) -> bool:
    return ("Jensen reconcile decision applied:" in comment.body
            and f"option={option};" in comment.body
            and f"packet_signature={packet_signature};" in comment.body)


def _find_existing_reconcile_decision_comment(conn, task_id, *, option, packet_signature):
    for comment in kb.list_comments(conn, task_id):
        if _reconcile_decision_comment_matches(
                comment, option=option, packet_signature=packet_signature):
            return comment
    return None
```

- [ ] **Step 4: Run test — verify the 9 kinds pass**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_reconciler_pg.py -v`
Expected: PASS (each seeded kind detected; deferred kinds absent).
Also run the sqlite reconciler suite to confirm the finder refactor didn't regress:
Run: `venv/bin/python -m pytest tests/hermes_cli -k reconcil -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_reconciler.py tests/hermes_cli/kanban/test_kanban_reconciler_pg.py
git commit -m "feat(kanban-pg): PG reconcile collector for the 9 core action kinds

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `run_reconciler` PG dispatch + `_run_reconciler_pg` + PG packet filter + partial note

**Files:**
- Modify: `hermes_cli/kanban_reconciler.py` (`run_reconciler` dispatch; `_run_reconciler_pg`; `_filter_acknowledged_decision_packets_pg`; `_redacted_pg_dsn` reuse)
- Test: `tests/hermes_cli/kanban/test_kanban_reconciler_pg.py` (extend)

**Review:** adversarial (live diagnostic path; DSN-leak focus).

- [ ] **Step 1: Write the failing test** — extend the reconciler PG test file:

```python
def test_run_reconciler_pg_returns_real_actions_and_partial(pg, monkeypatch):
    s, pool, board = pg
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: board)
    t = s.create_task(title="x")
    old = int(time.time()) - 10_000
    with pool.connection() as conn:
        conn.execute("UPDATE tasks SET created_at=%s, assignee=NULL WHERE board=%s AND id=%s",
                     (old, board, t)); conn.commit()
    res = krec.run_reconciler(ready_age_seconds=900)
    assert res["mutation_applied"] is False
    assert res["board"] == board
    assert any(a["kind"].startswith("old_ready_") for a in res["actions"])
    # deferred kinds documented, not silently dropped
    assert "partial" in res
    assert set(res["partial"]["deferred_kinds"]) == {
        "review_parent_pr_head_evidence_missing",
        "repeated_failure_signature_decision"}
    # DSN never leaked: db_path is a redacted host:port/db form, no password
    assert "://" in res["db_path"]
    assert "postgres:postgres@" not in res["db_path"]


def test_run_reconciler_pg_unreachable_returns_shape(monkeypatch):
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: "default")
    class _BadPool:
        def connection(self, *a, **k): raise RuntimeError("nope")
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: _BadPool())
    res = krec.run_reconciler()
    assert res["ok"] is False
    assert res["actions"] == []
    assert "nope" not in str(res)  # no raw exception text / no DSN
```

- [ ] **Step 2: Run test — verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_reconciler_pg.py -k run_reconciler -v`
Expected: FAIL — `run_reconciler` still snapshot-connects sqlite; no `partial` key; `board` mismatch.

- [ ] **Step 3: Implement the dispatch + PG path.** At the top of `run_reconciler` add the dispatch; implement `_run_reconciler_pg` and the PG filter. Reuse `_redacted_pg_dsn` from `kanban_board_doctor` (import it) to avoid duplicating redaction.

```python
_DEFERRED_PG_RECONCILE_KINDS = [
    "review_parent_pr_head_evidence_missing",
    "repeated_failure_signature_decision",
]
_pg_partial_logged = False  # log the deferral once per process


def run_reconciler(*, board=None, ready_age_seconds=15*60, now=None):
    try:
        from hermes_cli.kanban.store import resolve_backend
        if resolve_backend() == "postgres":
            return _run_reconciler_pg(board=board,
                                      ready_age_seconds=ready_age_seconds, now=now)
    except Exception:
        pass  # fall through to sqlite (defensive; default deployments unaffected)
    path = kb.kanban_db_path(board=board)          # EXISTING sqlite body, verbatim
    as_of = int(now if now is not None else time.time())
    with _snapshot_connect(path) as conn:
        ...                                         # unchanged


def _run_reconciler_pg(*, board, ready_age_seconds, now=None, pool=None):
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    from hermes_cli.kanban_board_doctor import _redacted_pg_dsn
    global _pg_partial_logged
    slug = board or kb.get_current_board()          # resolve once
    as_of = int(now if now is not None else time.time())
    db_path = _redacted_pg_dsn()
    partial = {"deferred_kinds": list(_DEFERRED_PG_RECONCILE_KINDS),
               "note": "PG reconcile omits PR-head-evidence + systemic-failure-"
                       "signature checks (see Phase 6 B1); run sqlite reconcile "
                       "for full coverage."}
    if not _pg_partial_logged:
        logger.info("kanban reconcile (postgres): %d action kinds deferred: %s",
                    len(_DEFERRED_PG_RECONCILE_KINDS),
                    ", ".join(_DEFERRED_PG_RECONCILE_KINDS))
        _pg_partial_logged = True
    try:
        pool = pool or pg_pool.get_pool()
        with pool.connection(timeout=5) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        logger.warning("kanban reconcile (postgres): backend unreachable: %s",
                       type(exc).__name__)
        return {"ok": False, "board": slug, "db_path": db_path, "actions": [],
                "wake_triage": {"mode": "auto_silent", "wake_agent": False,
                                "suppressed_decision_packet_count": 0},
                "as_of": as_of, "mutation_applied": False, "partial": partial}
    store = PostgresKanbanStore(board=slug, pool=pool)
    with pool.connection() as conn:
        actions = _collect_reconcile_actions_pg(
            conn, slug, store, ready_age_seconds=ready_age_seconds, now=as_of)
    action_dicts = actions_to_dicts(actions)
    action_dicts, suppressed_packets = _filter_acknowledged_decision_packets_pg(
        store, action_dicts)
    wake_triage = classify_wake_triage(action_dicts)
    wake_triage["suppressed_decision_packet_count"] = len(suppressed_packets)
    if suppressed_packets:
        wake_triage["suppressed_decision_packets"] = suppressed_packets
    return {"ok": not action_dicts, "board": slug, "db_path": db_path,
            "actions": action_dicts, "wake_triage": wake_triage, "as_of": as_of,
            "mutation_applied": False, "partial": partial}


def _filter_acknowledged_decision_packets_pg(store, action_dicts):
    """PG mirror of _filter_acknowledged_decision_packets: reads acknowledgement
    comments via the store (not a sqlite conn)."""
    triage = classify_wake_triage(action_dicts)
    suppressed_action_signatures: set[str] = set()
    suppressed_packets: list[dict] = []
    for packet in triage.get("decision_packets") or []:
        task_id = str(packet.get("task_id") or "").strip()
        packet_signature = str(packet.get("packet_signature") or "").strip()
        if not task_id or not packet_signature:
            continue
        comments = store.list_comments(task_id)
        applied_option = None
        for option in ("keep_parked", "keep_blocked"):
            if any(_reconcile_decision_comment_matches(
                    c, option=option, packet_signature=packet_signature)
                   for c in comments):
                applied_option = option
                break
        if not applied_option:
            continue
        sigs = [str(a.get("signature") or "")
                for a in packet.get("actions") or [] if a.get("signature")]
        suppressed_action_signatures.update(sigs)
        suppressed_packets.append({
            "task_id": task_id, "packet_signature": packet_signature,
            "option": applied_option,
            "action_count": int(packet.get("action_count") or len(sigs)),
            "kinds": list(packet.get("kinds") or [])})
    if not suppressed_action_signatures:
        return action_dicts, []
    filtered = [a for a in action_dicts
                if str(a.get("signature") or "") not in suppressed_action_signatures]
    return filtered, suppressed_packets
```

(Update `_collect_reconcile_actions_pg`'s signature from Task 5 to accept `store` as the third positional arg, and use `store.get_task(...)` for the pre-spawn Task lookup.)

- [ ] **Step 4: Run test — verify pass**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_reconciler_pg.py -v` → PASS.
Run: `venv/bin/python -m pytest tests/hermes_cli -k reconcil -v` → sqlite reconciler PASS (dispatch falls through unchanged when backend=sqlite).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_reconciler.py tests/hermes_cli/kanban/test_kanban_reconciler_pg.py
git commit -m "feat(kanban-pg): backend-aware run_reconciler with PG path + partial note

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: dashboard `/reconcile` — remove the PG no-op branch

**Files:**
- Modify: `plugins/kanban/dashboard/plugin_api.py:749-786` (`get_reconcile_health`)
- Test: `tests/plugins/test_kanban_dashboard_plugin_pg.py` (extend)

**Review:** adversarial (live dashboard endpoint).

- [ ] **Step 1: Write the failing test** — append to `tests/plugins/test_kanban_dashboard_plugin_pg.py`:

```python
def test_reconcile_returns_real_actions_under_postgres(pg_client):
    import time
    s = pg_client.pg_store
    t = s.create_task(title="stuck ready", assignee=None)
    # age it past the threshold via the pooled store's connection
    from hermes_cli.kanban import pg_pool
    with pg_pool.get_pool().connection() as conn:
        conn.execute("UPDATE tasks SET created_at=%s WHERE board=%s AND id=%s",
                     (int(time.time()) - 10_000, s.board, t)); conn.commit()
    r = pg_client.get("/api/plugins/kanban/reconcile?ready_age_seconds=900")
    assert r.status_code == 200
    body = r.json()
    assert body["mutation_applied"] is False
    assert any(a["kind"].startswith("old_ready_") for a in body["actions"])  # real, not no-op
    assert "partial" in body                       # deferred-kinds note present
    assert "text_preview" in body                  # format_reconcile_text ran
    assert "not yet available" not in body.get("note", "")  # old no-op note gone
```

- [ ] **Step 2: Run test — verify it fails**

Run: `venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_plugin_pg.py -k reconcile -v`
Expected: FAIL — the endpoint returns the graceful no-op (`actions == []`, `note == "reconcile is not yet available..."`, no `text_preview`/`partial`).

- [ ] **Step 3: Implement — delete the PG no-op branch.** In `get_reconcile_health`, remove the entire `if _backend() == "postgres": ... return {...}` block (lines ~749-777) so both backends fall through to the existing `run_reconciler(...) + format_reconcile_text(...)`:

```python
    board = _resolve_board(board)
    result = kanban_reconciler.run_reconciler(
        board=board,
        ready_age_seconds=max(1, int(ready_age_seconds or 900)),
    )
    result["text_preview"] = kanban_reconciler.format_reconcile_text(
        result,
        max_examples=max(0, int(max_examples or 0)),
    )
    return result
```

(`run_reconciler` now self-dispatches to PG. `board = _resolve_board(board)` is filesystem-based but single-board `default` resolves correctly; the PG path re-resolves `slug = kb.get_current_board()` internally.)

- [ ] **Step 4: Run test — verify pass; dashboard suites green**

Run: `venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_plugin_pg.py -v` → PASS.
Run: `venv/bin/python -m pytest tests/plugins -k reconcile -v` → sqlite dashboard reconcile PASS (unchanged path).

- [ ] **Step 5: Commit**

```bash
git add plugins/kanban/dashboard/plugin_api.py tests/plugins/test_kanban_dashboard_plugin_pg.py
git commit -m "feat(kanban-pg): dashboard /reconcile uses the backend-aware run_reconciler

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Holistic verification + byte-identical sqlite proof

**Files:** none (verification only). No new code unless a gap is found.

- [ ] **Step 1: Full kanban + plugins suites, both backends**

Run:
```bash
HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" venv/bin/python -m pytest \
  tests/hermes_cli/kanban tests/hermes_cli/test_kanban_db.py tests/plugins -q
```
Expected: all green on sqlite + postgres params. Investigate any failure; a known pre-existing cross-package isolation flake is `tests/.../test_store_factory.py::test_kanban_backend_reads_config` — confirm it also fails on `main` before dismissing.

- [ ] **Step 2: Prove the forbidden files are untouched**

Run: `git diff --stat main -- hermes_cli/kanban_db.py hermes_cli/kanban_liveness.py hermes_cli/kanban_writer_daemon.py`
Expected: empty output (zero changes).

- [ ] **Step 3: Prove the sqlite code paths are byte-identical**

Run: `git diff main -- hermes_cli/kanban_specify.py hermes_cli/kanban_decompose.py hermes_cli/kanban_reconciler.py plugins/kanban/dashboard/plugin_api.py`
Inspect: every change is additive (new helpers/branches) or an `if postgres: ... else: <verbatim>` wrap; the sqlite/else bodies are unchanged character-for-character. The reconciler `_find_existing_reconcile_decision_comment` refactor must produce identical behavior (same three-marker match).

- [ ] **Step 4: DSN-leak grep**

Run: `git diff main | grep -iE "dsn|password|postgres://" | grep -v "redact\|host:port\|type(exc)"`
Expected: no line that logs/returns a raw DSN or password. `db_path` is always the redacted `host:port/db` form.

- [ ] **Step 5: Commit (if any verification-driven fixes were needed)** — otherwise no-op.

---

## Self-review (run by the plan author before handoff)

- **Spec coverage:** specify PG (Tasks 1,3) ✓; decompose PG (Tasks 2,4) ✓; PG reconciler 9 kinds (Task 5) ✓; run_reconciler dispatch + partial + filter (Task 6) ✓; dashboard wiring (Task 7) ✓; byte-identical + forbidden-file + DSN guarantees (Task 8) ✓; cross-backend conformance + dashboard + DSN-leak tests ✓.
- **Type/name consistency:** `_collect_reconcile_actions_pg(conn, slug, store, *, ready_age_seconds, now)` (Task 5 signature reconciled with Task 6 caller) ✓; `_reconcile_decision_comment_matches` used by both the sqlite finder and the PG filter ✓; `_pg_store()`/`_resolve_backend()` helpers identical in kanban_specify + kanban_decompose ✓; PG store methods `specify_triage_task`/`decompose_triage_task` names match call sites ✓.
- **Open verification items for the implementer:** confirm `task_comments` and `task_links` PG column lists against `hermes_cli/kanban/pg_schema.sql` (Tasks 1, 2); confirm `_pg_dsn` fixture is in scope for the new `tests/hermes_cli/kanban/test_kanban_*_pg.py` files (they sit in the kanban test package, so it is).
