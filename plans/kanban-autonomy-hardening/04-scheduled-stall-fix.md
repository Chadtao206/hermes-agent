# WS4 — Scheduled-Park Silent-Stall Fix

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` or
> `superpowers:executing-plans`. Independent of other workstreams.

**Goal:** A task the respawn guard parks to `scheduled` (active-PR detected) must return to
`ready` automatically once its guard condition clears (PR merged/closed) — instead of sitting
silently until the reconciler's age-threshold sweep happens to notice, or a human unblocks it.

**Architecture:** Add a deterministic promoter pass to the dispatcher tick: for tasks in
`scheduled` parked with the `active_pr` reason, re-evaluate the *same* respawn-guard condition
that parked them; if it no longer holds, transition `scheduled → ready` (so normal dispatch
picks them up) and emit a `ready` event. Behind `kanban.promote_scheduled_on_guard_clear`
(default `false`). `scheduled` deliberately stays out of `WAKE_ARM_EVENT_KINDS` (parking is not
a terminal event); the general "scheduled with completed parents" case continues to route
through the reconciler and WS6 alerting.

**Key anchors (verified):**
- Respawn guard parks to `scheduled` for `active_pr` at `kanban_db.py:7498-7511`, appending a
  `scheduled` event with that reason.
- `recompute_ready` (`:3699`) promotes `todo → ready` but intentionally does **not** touch
  `scheduled`. `unblock_task` (`:5119`) handles `scheduled → ready` on explicit action.
- `dispatch_once` (`:7178`) is the per-tick entry the gateway calls.

---

### Task 1: `active_pr` guard re-evaluation helper

**Files:**
- Modify: `hermes_cli/kanban_db.py` (extract/expose `active_pr_guard_holds(task) -> bool` from
  the existing respawn-guard logic so park and un-park share one predicate)
- Test: `tests/hermes_cli/test_kanban_active_pr_guard.py`

- [ ] **Step 1: Failing test**

```python
# tests/hermes_cli/test_kanban_active_pr_guard.py
from hermes_cli import kanban_db as kb

def test_guard_holds_when_pr_open(monkeypatch):
    monkeypatch.setattr(kb, "_detect_active_pr", lambda task: True, raising=False)
    assert kb.active_pr_guard_holds(task=_fake_task()) is True

def test_guard_clears_when_pr_merged(monkeypatch):
    monkeypatch.setattr(kb, "_detect_active_pr", lambda task: False, raising=False)
    assert kb.active_pr_guard_holds(task=_fake_task()) is False

def _fake_task():
    class T:  # minimal shape the predicate reads
        id = "t1"; branch_name = "feat/x"; assignee = "engineer"
    return T()
```

- [ ] **Step 2: Run red** — FAIL (no `active_pr_guard_holds`).

- [ ] **Step 3: Implement** — locate the inline active-PR detection used by the respawn guard at
  `:7493-7510`, extract it into `active_pr_guard_holds(task)` (and a `_detect_active_pr(task)`
  seam for testing), and call that helper from the *park* site so both sides use one predicate.

- [ ] **Step 4: Run green** — PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_active_pr_guard.py
git commit -m "refactor(kanban): extract shared active_pr_guard_holds predicate"
```

---

### Task 2: Promote `scheduled → ready` when the guard clears

**Files:**
- Modify: `hermes_cli/kanban_db.py` (add `promote_cleared_scheduled(conn) -> int`; call it from
  `dispatch_once` under the flag, before the ready-spawn scan)
- Test: `tests/hermes_cli/test_kanban_promote_scheduled.py`

- [ ] **Step 1: Failing test**

```python
# tests/hermes_cli/test_kanban_promote_scheduled.py
from hermes_cli import kanban_db as kb

def test_scheduled_active_pr_task_promotes_when_pr_clears(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    conn = kb.connect(db_path=db, readonly=False, _bootstrap=True)
    conn.execute(
        "INSERT INTO tasks (id,title,status,assignee,scheduled_reason) "
        "VALUES ('t1','x','scheduled','engineer','active_pr')")  # adapt cols to schema
    conn.commit()
    monkeypatch.setattr(kb, "active_pr_guard_holds", lambda task: False)  # PR merged
    promoted = kb.promote_cleared_scheduled(conn)
    assert promoted == 1
    assert conn.execute("SELECT status FROM tasks WHERE id='t1'").fetchone()["status"] == "ready"

def test_scheduled_still_parked_when_guard_holds(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    conn = kb.connect(db_path=db, readonly=False, _bootstrap=True)
    conn.execute(
        "INSERT INTO tasks (id,title,status,assignee,scheduled_reason) "
        "VALUES ('t2','x','scheduled','engineer','active_pr')")
    conn.commit()
    monkeypatch.setattr(kb, "active_pr_guard_holds", lambda task: True)  # PR still open
    assert kb.promote_cleared_scheduled(conn) == 0
    assert conn.execute("SELECT status FROM tasks WHERE id='t2'").fetchone()["status"] == "scheduled"
```

- [ ] **Step 2: Run red** — FAIL (no `promote_cleared_scheduled`).

- [ ] **Step 3: Implement**

```python
def promote_cleared_scheduled(conn) -> int:
    """Promote scheduled/active_pr tasks back to ready once the PR guard clears.

    Only touches tasks parked specifically for the active_pr reason; other
    scheduled parks (time-based, operator) are left alone.
    """
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status='scheduled' "
        "AND scheduled_reason LIKE 'active_pr%'"   # adapt to how the park reason is stored
    ).fetchall()
    promoted = 0
    for row in rows:
        if active_pr_guard_holds(task=_row_to_task(row)):
            continue
        conn.execute("UPDATE tasks SET status='ready' WHERE id=? AND status='scheduled'",
                     (row["id"],))
        _append_event(conn, row["id"], "ready", {"reason": "active_pr guard cleared"})
        promoted += 1
    conn.commit()
    return promoted
```

Then call it from `dispatch_once`, guarded by the flag, just before `recompute_ready`/the ready
scan:

```python
if _kanban_cfg().get("promote_scheduled_on_guard_clear", False):
    promote_cleared_scheduled(conn)
```

> Adapt `scheduled_reason` to however the park reason is actually persisted (it may be in the
> task row, a column, or the latest `scheduled` event payload — check the park site at `:7498`).
> If it lives in the event payload, read it via the existing event accessor rather than a column.

- [ ] **Step 4: Run green** — `python -m pytest tests/hermes_cli/test_kanban_promote_scheduled.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_promote_scheduled.py
git commit -m "feat(kanban): auto-promote scheduled active_pr tasks when guard clears"
```

---

### Task 3: Flag + dispatcher wiring check

**Files:**
- Modify: `config.yaml`, `cli-config.yaml.example`

- [ ] **Step 1: Document flag**

```yaml
kanban:
  promote_scheduled_on_guard_clear: false  # WS4 — auto-unpark active_pr scheduled tasks
```

- [ ] **Step 2: Integration test**

```python
# tests/hermes_cli/test_kanban_dispatch_promotes_scheduled.py
from hermes_cli import kanban_db as kb

def test_dispatch_once_promotes_cleared_scheduled(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    conn = kb.connect(db_path=db, readonly=False, _bootstrap=True)
    conn.execute("INSERT INTO tasks (id,title,status,assignee,scheduled_reason) "
                 "VALUES ('t1','x','scheduled','engineer','active_pr')")
    conn.commit()
    monkeypatch.setattr(kb, "_kanban_cfg",
                        lambda: {"promote_scheduled_on_guard_clear": True}, raising=False)
    monkeypatch.setattr(kb, "active_pr_guard_holds", lambda task: False)
    monkeypatch.setattr(kb, "_default_spawn", lambda *a, **k: None)  # no real spawn
    kb.dispatch_once(conn)  # adapt to dispatch_once's real signature
    assert conn.execute("SELECT status FROM tasks WHERE id='t1'").fetchone()["status"] in ("ready", "running")
```

- [ ] **Step 3: Run green** — PASS.

- [ ] **Step 4: Commit**

```bash
git add config.yaml cli-config.yaml.example tests/hermes_cli/test_kanban_dispatch_promotes_scheduled.py
git commit -m "feat(kanban): wire scheduled auto-promote into dispatch tick behind flag"
```

---

## WS4 acceptance criteria

- A `scheduled`/`active_pr` task whose PR has merged is promoted to `ready` on the next
  dispatcher tick and dispatched normally — no human, no reconciler dependency.
- Time-based / operator `scheduled` parks are untouched.
- Flag off → no behavior change.
