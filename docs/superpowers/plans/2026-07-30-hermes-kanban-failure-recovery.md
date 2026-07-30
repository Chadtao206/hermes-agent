# Hermes Kanban Failure Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the five confirmed Kanban failure mechanisms without weakening goal validation, disk safety, task isolation, or retry ownership.

**Architecture:** Keep each repair at its existing ownership boundary: the local review-delegator owns review handoff and cron exit semantics; Kanban tools own request-scoped session attribution; the DB kernel owns scratch-GC and worktree materialization; the PR-review gatherer owns remediation-card runtime policy. Existing live cards are recovered only after the corresponding tests pass and deployed code is active.

**Tech Stack:** Python 3.11, pytest, SQLite, Hermes CLI/Kanban kernel, local no-agent cron scripts.

## Global Constraints

- Preserve the goal-completion judge; do not bypass or disable it.
- Preserve the 20 GiB disk watermark.
- Never share one linked worktree checkout between sibling tasks.
- Never auto-retry a timed-out PR remediation whose live side effects have not been reconciled.
- Keep `kanban-blocked-watch` and both diagnostic `w` jobs paused until verification is complete.
- Use `scripts/run_tests.sh` for Hermes repository tests.

---

### Task 1: Make review delegation goal-aware and failure-visible

**Files:**
- Modify: `/Users/ctao/.hermes/scripts/review_delegator/runner.py`
- Modify: `/Users/ctao/.hermes/scripts/review_delegator/tests/test_router.py`

**Interfaces:**
- Consumes: the engineer card's latest `review-required` block reason and child `show` payloads.
- Produces: `_complete_engineer(engineer_id, lane_id, evidence)` and a nonzero `main()` result when `run()` reports errors.

- [ ] **Step 1: Add failing regression tests**

```python
def test_complete_engineer_passes_handoff_evidence(monkeypatch):
    calls = []
    monkeypatch.setattr(
        R,
        "_hermes",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "",
    )
    R._complete_engineer("t_eng", "t_rev", "review-required: commit abc; tests 27/27")
    args, kwargs = calls[0]
    assert "commit abc; tests 27/27" in args[args.index("--result") + 1]
    assert kwargs["timeout"] > 60

def test_live_does_not_complete_suppressed_reviewer_child(monkeypatch):
    shows = _show_with_reviewer_child(
        "t_eng", "review-required: commit abc", "t_rev"
    )
    shows["t_rev"]["comments"] = [
        {"body": "  SUPPRESS duplicate reviewer lane; canonical lane t_other"}
    ]
    _wire(monkeypatch, shows)
    created, completed = [], []
    monkeypatch.setattr(
        R,
        "_create_reviewer_lane",
        lambda plan: created.append(plan.engineer_id) or "t_duplicate",
    )
    monkeypatch.setattr(
        R,
        "_complete_engineer",
        lambda eid, lane, evidence: completed.append((eid, lane, evidence)),
    )
    summary = R.run("LIVE")
    assert created == []
    assert completed == []
    assert summary["completed_stuck"] == 0
    assert summary["skipped"] == 1

def test_main_returns_nonzero_when_run_reports_errors(monkeypatch):
    monkeypatch.setattr(R, "_acquire_lock", lambda: 123)
    monkeypatch.setattr(R, "run", lambda mode: {"errors": 1})
    monkeypatch.setattr(R.os, "close", lambda fd: None)
    monkeypatch.setattr(R.LOCK_PATH, "unlink", lambda **kwargs: None)
    assert R.main() == 1
```

- [ ] **Step 2: Run the tests and verify the expected failures**

Run: `PYTHONPATH=/Users/ctao/.hermes/scripts /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest /Users/ctao/.hermes/scripts/review_delegator/tests/test_router.py -q`

Expected: failures show that `_complete_engineer` accepts only two arguments, suppressed children remain actionable, and `main()` returns zero for a summary containing errors.

- [ ] **Step 3: Implement the minimal behavior**

```python
def _complete_engineer(engineer_id: str, lane_id: str, evidence: str) -> None:
    result = f"{evidence.strip()}\n\nReviewer handoff delegated to lane {lane_id}."
    _hermes(
        "complete",
        engineer_id,
        "--result",
        result,
        "--metadata",
        json.dumps({
            "reviewer_lane": lane_id,
            "delegated_by": "review-delegator",
        }),
        timeout=HERMES_TIMEOUT,
    )

def _reviewer_child_suppressed(show: dict) -> bool:
    return any((comment.get("body") or "").lstrip().upper().startswith("SUPPRESS")
               for comment in show.get("comments", []))
```

Pass the block reason to both completion paths, include a `suppressed` flag in each loaded child summary, exclude suppressed reviewer children from the awaiting-status list and lane lookup while retaining their reviewer assignee for duplicate prevention, set the default outer CLI timeout to 90 seconds (above the nested 60-second goal-judge budget), and return 1 from `main()` when `summary["errors"] > 0`.

- [ ] **Step 4: Run all review-delegator tests**

Run: `PYTHONPATH=/Users/ctao/.hermes/scripts /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest /Users/ctao/.hermes/scripts/review_delegator/tests -q`

Expected: all tests pass.

### Task 2: Make Kanban task session attribution request-scoped

**Files:**
- Modify: `tools/kanban_tools.py:1150-1163`
- Modify: `tests/tools/test_kanban_tools.py:1367-1431`

**Interfaces:**
- Consumes: `gateway.session_context.get_session_env("HERMES_SESSION_ID")`.
- Produces: a task `session_id` belonging to the active ContextVar even when another thread overwrites `os.environ`.

- [ ] **Step 1: Add the failing concurrency-boundary test**

```python
def test_create_prefers_request_scoped_session_over_foreign_process_env(monkeypatch, worker_env):
    monkeypatch.setenv("HERMES_SESSION_ID", "foreign-cron")
    tokens = set_session_vars(session_id="origin-cron")
    try:
        result = json.loads(kt._handle_create({"title": "child", "assignee": "peer"}))
    finally:
        reset_session_vars()
    assert kb.get_task(conn, result["task_id"]).session_id == "origin-cron"
```

- [ ] **Step 2: Verify RED**

Run: `./scripts/run_tests.sh tests/tools/test_kanban_tools.py -q`

Expected: the new test receives `foreign-cron` from the process environment.

- [ ] **Step 3: Use `get_session_env` for the fallback**

```python
from gateway.session_context import get_session_env

session_id = (
    args.get("session_id")
    or _current_origin_session_id()
    or get_session_env("HERMES_SESSION_ID", "")
    or None
)
```

- [ ] **Step 4: Verify GREEN**

Run: `./scripts/run_tests.sh tests/tools/test_kanban_tools.py tests/gateway/test_session_env.py -q`

Expected: all tests pass.

### Task 3: Let GC ignore unrelated ancestors outside managed scratch storage

**Files:**
- Modify: `hermes_cli/kanban_db.py:5428-5513`
- Modify: `tests/hermes_cli/test_kanban_db.py:2445-2522`

**Interfaces:**
- Consumes: a managed scratch candidate, task workspace reference, and the managed workspaces root.
- Produces: protection only for equal paths, references nested inside a candidate, or containing references that themselves live within managed scratch storage.

- [ ] **Step 1: Extend the GC behavior test with an active broad ancestor**

Create a `dir` task whose workspace is the test `HERMES_HOME`, then keep the existing literal expectation that terminal scratch roots and the orphan remain eligible. This test catches the production regression where `/Users/ctao/.hermes` protects every nested managed scratch workspace.

- [ ] **Step 2: Verify RED**

Run: `./scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py -q`

Expected: the eligible set is empty or missing terminal candidates because the broad ancestor overlaps them.

- [ ] **Step 3: Add a directional protection predicate**

```python
def _workspace_reference_protects_candidate(candidate: Path, referenced: Path, root: Path) -> bool:
    if referenced == candidate or referenced.is_relative_to(candidate):
        return True
    return referenced.is_relative_to(root) and candidate.is_relative_to(referenced)
```

Use this predicate in `gc_scratch_workspaces`; retain `_is_managed_scratch_path`, symlink rejection, owner exclusion, and dry-run behavior unchanged.

- [ ] **Step 4: Verify GREEN and safety cases**

Run: `./scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py -q`

Expected: terminal candidates are eligible despite the external ancestor, while active scratch, exact references, and nested references remain protected.

### Task 4: Preserve a decomposed root anchor without sharing its checkout

**Files:**
- Modify: `hermes_cli/kanban_db.py:6280-6305`
- Modify: `tests/hermes_cli/test_kanban_worktree_isolation.py`

**Interfaces:**
- Consumes: the decomposed root's linked-worktree path.
- Produces: child rows carrying that path as an anchor; `_resolve_worktree_workspace` recognizes the foreign branch and materializes `<repo>/.worktrees/<child-id>`.

- [ ] **Step 1: Replace the storage-shape assertion with a failing end-to-end isolation test**

Create a real repository and root linked worktree, decompose two children on a board without `default_workdir`, resolve both child workspaces, and assert:

```python
assert child_a_workspace == repo / ".worktrees" / child_a
assert child_b_workspace == repo / ".worktrees" / child_b
assert child_a_workspace != child_b_workspace != root_workspace
```

- [ ] **Step 2: Verify RED**

Run: `./scripts/run_tests.sh tests/hermes_cli/test_kanban_worktree_isolation.py -q`

Expected: resolution raises the current “no default_workdir” error because child paths were cleared.

- [ ] **Step 3: Preserve the root path as an anchor**

For inherited `worktree` children, set `child_ws_path = root_ws_path`. Do not alter the resolver's existing foreign-branch fallback, which is what creates unique sibling worktrees.

- [ ] **Step 4: Verify GREEN and the decompose suite**

Run: `./scripts/run_tests.sh tests/hermes_cli/test_kanban_worktree_isolation.py tests/hermes_cli/test_kanban_decompose_db.py tests/hermes_cli/test_kanban_db.py -q`

Expected: all tests pass and sibling paths remain unique.

### Task 5: Give PR-remediation workers a realistic hard runtime

**Files:**
- Modify: `/Users/ctao/.hermes/scripts/pr/pr_review_gather.py:156-170`
- Modify: `/Users/ctao/.hermes/scripts/pr/tests/test_pr_review_gather.py`

**Interfaces:**
- Consumes: a PR requiring an engineer fix lane.
- Produces: a dispatch command with a 60-minute hard runtime and the existing single-attempt side-effect safety fence.

- [ ] **Step 1: Add a failing command-contract test**

```python
def test_fix_dispatch_allows_sixty_minutes_without_automatic_side_effect_retry():
    command = g.render_dispatch_cmd(PR, "fix", True, summary="blocker")
    assert "--max-runtime 60m" in command
    assert "--max-retries 1" in command
```

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=/Users/ctao/.hermes/scripts/pr /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest /Users/ctao/.hermes/scripts/pr/tests/test_pr_review_gather.py -q`

Expected: command contains `--max-runtime 30m`.

- [ ] **Step 3: Change the generated runtime to 60 minutes**

Keep `--max-retries 1`; a timed-out task that may already have pushed must be reconciled before a human-triggered retry.

- [ ] **Step 4: Verify GREEN**

Run the same test command and expect all tests to pass.

### Task 6: Integration, live recovery, and guarded resume

**Files:**
- Verify: worktree git diff and local-script diffs.
- Runtime state: `/Users/ctao/.hermes/cron/jobs.json`, `/Users/ctao/.hermes/kanban.db`.

**Interfaces:**
- Consumes: passing regression suites and deployed code.
- Produces: a resumed `kanban-blocked-watch`, repaired existing decompose children, and evidence that no repeated judge rejection or session misattribution occurs.

- [ ] **Step 1: Run combined regression suites**

Run repository tests:

`./scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_worktree_isolation.py tests/hermes_cli/test_kanban_decompose_db.py tests/tools/test_kanban_tools.py tests/gateway/test_session_env.py -q`

Run local automation tests:

`PYTHONPATH=/Users/ctao/.hermes/scripts /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest /Users/ctao/.hermes/scripts/review_delegator/tests -q`

`PYTHONPATH=/Users/ctao/.hermes/scripts/pr /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest /Users/ctao/.hermes/scripts/pr/tests/test_pr_review_gather.py -q`

- [ ] **Step 2: Inspect diffs and commit the repository-owned fixes**

Commit only `tools/kanban_tools.py`, `hermes_cli/kanban_db.py`, their tests, and this plan on `fix/hermes-kanban-failure-recovery`. Local automation files remain outside the repository and are reported separately.

- [ ] **Step 3: Present the verified branch for integration**

Follow `superpowers:finishing-a-development-branch`; do not merge into `main` without the user's integration choice.

- [ ] **Step 4: After integration, repair the four existing decomposed child rows with a guarded SQLite transaction**

The four children were created from root `t_61bf1456`. Its linked-worktree path is `/Users/ctao/code/work/ylopo/apps/mf-business-api/.worktrees/t_61bf1456`. Before mutating, take a SQLite online backup and re-run the read-only preflight. Then execute the following with `sqlite3 -bail /Users/ctao/.hermes/kanban.db`; `.bail on` plus the guard constraint ensures any mismatch leaves the open transaction uncommitted and therefore rolled back when sqlite exits:

```sql
.bail on
BEGIN IMMEDIATE;
CREATE TEMP TABLE recovery_guard(ok INTEGER CHECK (ok = 1));
INSERT INTO recovery_guard
SELECT CASE WHEN
    (SELECT COUNT(*) FROM tasks
     WHERE id IN ('t_ae459f66', 't_8e6ecc25', 't_f148f1cb', 't_92182918')
       AND created_by = 'auto-decomposer'
       AND workspace_kind = 'worktree'
       AND workspace_path IS NULL) = 4
    AND (SELECT workspace_path FROM tasks WHERE id = 't_61bf1456') =
        '/Users/ctao/code/work/ylopo/apps/mf-business-api/.worktrees/t_61bf1456'
THEN 1 ELSE 0 END;
UPDATE tasks
SET workspace_path = (
    SELECT workspace_path FROM tasks WHERE id = 't_61bf1456'
)
WHERE id IN ('t_ae459f66', 't_8e6ecc25', 't_f148f1cb', 't_92182918')
  AND created_by = 'auto-decomposer'
  AND workspace_kind = 'worktree'
  AND workspace_path IS NULL;
INSERT INTO recovery_guard
SELECT CASE WHEN changes() = 4 THEN 1 ELSE 0 END;
COMMIT;
```

Verify all four rows now carry the root anchor, then use the Kanban `unblock` command only for blocked `t_ae459f66` and `t_8e6ecc25`. Leave todo `t_f148f1cb` and `t_92182918` untouched. The resolver must materialize unique `<repo>/.worktrees/<child-id>` checkouts at dispatch; the stored root path is an anchor, not the child runtime checkout.

- [ ] **Step 5: Resume only `kanban-blocked-watch` and observe at least two ticks**

Keep both `w` jobs paused. Verify cron status, no repeated `t_53752066` completion error, and no new wrong-session task attribution before declaring the recovery complete.
