# WS3 — Delete the Dead `review` Status Lane

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` or
> `superpowers:executing-plans`. Independent of other workstreams. Smallest; do early to reduce
> merge-tax.

**Goal:** Remove the unreachable `review`-status infrastructure (no code path ever sets a task
to `review`, and it isn't a valid initial status), keeping review/revise on the documented
`kanban_block(reason="review-required: …")` + `assignee=reviewer` + **PR-head-SHA closeout**
convention. Stop implying a capability that doesn't exist; cut recurring merge cost.

**Critical boundary — DO NOT remove the reviewer convention:** the PR-head-SHA closeout gating
(`kanban_db.py:3413-3479`, `_review_runtime`, the "completion blocked: reviewer closeout must
reference current PR head" path) is the **kept** mechanism. WS3 only removes the dormant
*status lane*: `claim_review_task`, the dispatcher's `status='review'` scan, the
`status='review'` membership in `VALID_STATUSES`, and the reconciler checks that exist *solely*
to service review-status tasks (`old_review_spawnable`, `review_skill_provenance_missing`).

**Key anchors (verified):**
- `kanban_db.py:365` `VALID_STATUSES` includes `'review'`; `:3862` `claim_review_task`;
  `:3893`/`:7163`/`:7295` `status='review'` reads; dispatcher review-spawn (`:7562-7646`,
  `:7609`/`:7626` force-load `sdlc-review`).
- `kanban_reconciler.py:720` review skill check; `:1237` `COUNT(*) … status='review'`.
- `review` is NOT in `VALID_INITIAL_STATUSES` (`:366`) — confirms unreachability via creation.

---

### Task 1: Migrate any stray `review` rows (safety before removal)

**Files:**
- Modify: `hermes_cli/kanban_db.py` (add `migrate_review_status_rows(conn)`)
- Test: `tests/hermes_cli/test_kanban_review_migration.py`

- [ ] **Step 1: Failing test**

```python
# tests/hermes_cli/test_kanban_review_migration.py
from hermes_cli import kanban_db as kb

def test_migrate_moves_review_rows_to_ready(tmp_path):
    db = tmp_path / "kanban.db"
    conn = kb.connect(db_path=db, readonly=False, _bootstrap=True)
    conn.execute("INSERT INTO tasks (id,title,status) VALUES ('r','x','review')")
    conn.commit()
    moved = kb.migrate_review_status_rows(conn)
    assert moved == 1
    assert conn.execute("SELECT status FROM tasks WHERE id='r'").fetchone()["status"] == "ready"
```

- [ ] **Step 2: Run red** — `python -m pytest tests/hermes_cli/test_kanban_review_migration.py -v` → FAIL (no function).

- [ ] **Step 3: Implement** — add to `kanban_db.py` near the other migration helpers:

```python
def migrate_review_status_rows(conn) -> int:
    """One-shot: convert any legacy 'review' tasks to 'ready'. Idempotent."""
    cur = conn.execute("UPDATE tasks SET status='ready' WHERE status='review'")
    conn.commit()
    return cur.rowcount or 0
```

Call it once from the schema/migration path (`_migrate_add_optional_columns` neighborhood,
inside the init lock) so existing live boards are cleaned on next open.

- [ ] **Step 4: Run green** — PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_review_migration.py
git commit -m "feat(kanban): migrate legacy 'review' tasks to ready before lane removal"
```

---

### Task 2: Remove `claim_review_task` + dispatcher review-scan

**Files:**
- Modify: `hermes_cli/kanban_db.py` (delete `claim_review_task:3862`; remove review-status
  branch of `dispatch_once` `:7562-7646` and the `status='review'` query `:7295`)
- Modify: `gateway/run.py` if it has a review-specific dispatch branch (search `claim_review`)
- Test: `tests/hermes_cli/test_kanban_no_review_dispatch.py`

- [ ] **Step 1: Failing test**

```python
# tests/hermes_cli/test_kanban_no_review_dispatch.py
import inspect
from hermes_cli import kanban_db as kb

def test_claim_review_task_removed():
    assert not hasattr(kb, "claim_review_task")

def test_dispatch_once_has_no_review_query():
    assert "status = 'review'" not in inspect.getsource(kb.dispatch_once)
```

- [ ] **Step 2: Run red** — FAIL (function still present).

- [ ] **Step 3: Implement** — delete `claim_review_task` and every call to it; remove the
  review-status spawn block from `dispatch_once` and the `status='review'` query. Read each
  region first; keep the `ready`-status dispatch path untouched.

- [ ] **Step 4: Run green** — `python -m pytest tests/hermes_cli/test_kanban_no_review_dispatch.py tests/hermes_cli/ -k kanban -v` → PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_db.py gateway/run.py tests/hermes_cli/test_kanban_no_review_dispatch.py
git commit -m "refactor(kanban): remove dormant review-status dispatch/claim path"
```

---

### Task 3: Remove review-status-only reconciler checks + drop from `VALID_STATUSES`

**Files:**
- Modify: `hermes_cli/kanban_reconciler.py` (remove `old_review_spawnable`,
  `review_skill_provenance_missing`, the `status='review'` count `:1237`, review skill check
  `:720`)
- Modify: `hermes_cli/kanban_db.py:365` (drop `'review'` from `VALID_STATUSES`)
- Modify: `website/docs/user-guide/features/kanban.md` (remove `review` from the state list)
- Test: `tests/hermes_cli/test_kanban_reviewer_convention_intact.py`

- [ ] **Step 1: Failing test** — the guard that proves we did NOT break the kept convention:

```python
# tests/hermes_cli/test_kanban_reviewer_convention_intact.py
from hermes_cli import kanban_db as kb

def test_review_not_a_valid_status():
    assert "review" not in kb.VALID_STATUSES

def test_reviewer_pr_head_closeout_gate_still_present():
    # The KEPT convention: reviewer completion must reference the current PR head.
    import inspect
    src = inspect.getsource(kb.complete_task)
    assert "pr head" in src.lower() or "pr_head" in src.lower()
```

- [ ] **Step 2: Run red** — FAIL (`review` still in VALID_STATUSES).

- [ ] **Step 3: Implement** — drop `'review'` from `VALID_STATUSES`; delete the two
  review-status-only reconciler action kinds and their decision hints/tests fixtures; update the
  docs state list. **Leave `complete_task`'s PR-head-SHA reviewer gating and any
  `review_parent_pr_head_evidence_missing` reconciler check untouched** — those are the kept
  convention, not the dead lane. If unsure whether a reconciler check is lane-only, grep for
  whether it references `status='review'` (lane → remove) vs a parent's closeout evidence
  (convention → keep).

- [ ] **Step 4: Run green** — `python -m pytest tests/hermes_cli/ tests/plugins/ -k "kanban or reconcil" -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_reconciler.py hermes_cli/kanban_db.py \
        website/docs/user-guide/features/kanban.md \
        tests/hermes_cli/test_kanban_reviewer_convention_intact.py
git commit -m "refactor(kanban): drop review status + review-lane reconciler checks; keep reviewer convention"
```

---

## WS3 acceptance criteria

- `grep -rn "status = 'review'\|claim_review_task\|'review'" hermes_cli/ gateway/` returns only
  the migration helper (and no dispatch/claim/validation references).
- Reviewer PR-head-SHA closeout gating still works (guard test passes).
- All existing kanban + reconciler tests pass; docs no longer list a `review` column.
