# Hermes Upstream Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge official `origin/main` into the customized Hermes fork without losing the blocked-review watchdog, Claude Kanban lane, supervisor safety contract, workspace storage controls, Codex proxy, or the corrected ten-worker concurrency behavior.

**Architecture:** Preserve the current dirty state on `preserve/pre-upstream-2026-07-28`, then perform the merge on `merge/upstream-2026-07-28` in the ignored `.worktrees/upstream-2026-07-28` workspace so the live editable checkout stays coherent for active workers. Adopt upstream's SQLite safety and modern Kanban architecture, then port the local behavioral invariants into that architecture using the existing local tests as acceptance contracts.

**Tech Stack:** Git, Python 3, SQLite/WAL, pytest, ruff, ty, Hermes CLI, Node/npm, React dashboard, tmux-based Claude lane.

## Global Constraints

- Never run the applying `hermes update` command for this checkout.
- Never merge directly on `main`; fast-forward it only after every verification gate passes.
- Never resolve the merge with `-X ours`, `-X theirs`, or `-s ours`.
- Preserve `kanban.max_in_progress=10` and `kanban.max_in_progress_per_profile=10` as live concurrency caps.
- Treat `kanban.max_spawn` as a per-dispatch-tick spawn budget, enforced independently from live concurrency.
- Preserve exact run fencing, compare-and-set worker ownership, supervisor adoption, atomic heartbeat/TTL renewal, fresh-heartbeat no-PID protection, deliberate `active_pr` reruns, and respawn-event throttling.
- Adopt upstream commits `fbd5e5772`, `899513145`, `49828a3fd`, and `8fb3cc1b1` without restoring the unsafe raw live-database file probe.
- Keep the blocked-review watchdog, Claude lane, workspace storage management, and Codex proxy capabilities.
- Regenerate `package-lock.json` from the merged package manifests.
- Keep local `main`, the primary editable runtime tree, and `chad/main` unchanged until the full gate is green.

---

### Task 1: Preserve runtime state and current source work

**Files:**
- Preserve: `/Users/ctao/.hermes/config.yaml`
- Preserve: active Kanban SQLite database plus matching `-wal` and `-shm`
- Preserve: `/Users/ctao/.hermes/plugins/claude-kanban-lane/**`
- Preserve: `/Users/ctao/.hermes/.claude-lane-install/**`
- Create: an external dated backup directory under `/Users/ctao/.hermes/backups/`

**Interfaces:**
- Consumes: current running Hermes configuration and databases.
- Produces: a restorable offline snapshot and a dated integration branch.

- [ ] **Step 1: Resolve the live gateway, database, and lane paths without modifying them**

Run `hermes gateway status`, inspect `config.yaml`, and list board database files, WAL/SHM sidecars, installed lane artifacts, and open file holders.

- [ ] **Step 2: Create SQLite-consistent online backups without interrupting active workers**

Use the external `sqlite3` process and its `.backup` API for the active Kanban and state databases. Require `PRAGMA quick_check` to return exactly `ok` for source and backup databases. Do not raw-copy live database/WAL/SHM families while the gateway is active.

- [ ] **Step 3: Create the dated offline snapshot**

Copy `config.yaml`, the SQLite-consistent backup databases, installed Claude-lane artifacts, and install transaction manifests into the dated backup directory. Save the binary Git diff and record SHA-256 checksums for configuration, databases, and preserved artifacts.

- [ ] **Step 4: Create the preservation branch**

Run `git switch -c preserve/pre-upstream-2026-07-28` from current local `main`; verify `main` still points to `483bbe612`. The working-tree bytes remain unchanged for the live editable runtime.

### Task 2: Preserve the dirty work as coherent commits

**Files:**
- Modify: `cron/scripts/just_do_it_blocked_review_requeue_watchdog.py`
- Modify: `tests/cron/test_just_do_it_blocked_review_requeue_watchdog.py`
- Modify: `hermes_cli/config.py`
- Modify: `hermes_cli/kanban.py`
- Modify: `hermes_cli/kanban_db.py`
- Modify: `tests/hermes_cli/test_kanban_core_functionality.py`
- Modify: `tests/hermes_cli/test_kanban_db.py`
- Create: `docs/kanban/storage-management.md`
- Modify: `agent/auxiliary_client.py`
- Modify: `hermes_cli/proxy/adapters/__init__.py`
- Modify: `hermes_cli/proxy/adapters/base.py`
- Create: `hermes_cli/proxy/adapters/codex.py`
- Modify: `hermes_cli/proxy/server.py`
- Modify: `package-lock.json`

**Interfaces:**
- Consumes: the current 14-path dirty worktree.
- Produces: reviewable commits for watchdog, Kanban/storage/concurrency, Codex proxy, plan documentation, and generated lockfile state.

- [ ] **Step 1: Run focused pre-merge tests against the current state**

Run:

```bash
venv/bin/python -m pytest -q \
  tests/cron/test_just_do_it_blocked_review_requeue_watchdog.py \
  tests/hermes_cli/test_kanban_core_functionality.py \
  tests/hermes_cli/test_kanban_db.py \
  tests/hermes_cli/test_kanban_heartbeat_ttl_and_respawn_guard.py \
  tests/hermes_cli/test_kanban_supervisor_contract.py
```

Expected: all selected tests pass before preservation commits.

- [ ] **Step 2: Commit the watchdog follow-on independently**

Stage only the watchdog implementation and test, then commit `fix(cron): harden blocked review reconciliation`.

- [ ] **Step 3: Commit storage management and concurrency correction**

Stage the Kanban configuration, CLI, database, storage documentation, and Kanban tests, then commit `fix(kanban): harden storage and concurrency limits`.

- [ ] **Step 4: Commit the Codex proxy adapter**

Stage the auxiliary-client alias and proxy adapter/server files, then commit `feat(proxy): add Codex OAuth upstream adapter`.

- [ ] **Step 5: Preserve plan and lockfile state separately**

Commit this plan as `docs: plan Hermes upstream integration`. Commit the generated lockfile delta separately as `chore(web): preserve pre-merge lockfile state`; it will be regenerated after the merge.

- [ ] **Step 6: Verify the branch is clean**

Run `git status --short`; expected output is empty.

- [ ] **Step 7: Create the isolated integration worktree**

Run `git worktree add .worktrees/upstream-2026-07-28 -b merge/upstream-2026-07-28 preserve/pre-upstream-2026-07-28`. All remaining merge and source edits occur in that worktree until deployment.

### Task 3: Refresh and audit official upstream

**Files:**
- Read: every path reported by `audit-upstream.sh`
- Read: upstream history for `hermes_cli/kanban.py`, `hermes_cli/kanban_db.py`, and `tools/kanban_tools.py`

**Interfaces:**
- Consumes: clean integration branch and fetched `origin/main`.
- Produces: conflict, silent-break, and path-collision checklists recorded in the integration report.

- [ ] **Step 1: Fetch official upstream**

Run `git fetch origin --prune` and record `origin/main` SHA and ahead/behind counts.

- [ ] **Step 2: Run the prescribed audit**

Run `/Users/ctao/.agents/skills/merging-hermes-upstream/scripts/audit-upstream.sh` and save the findings in the working notes.

- [ ] **Step 3: Inspect every overlap and silent-break**

Use `git log -p HEAD..origin/main -- <path>` for each reported path. Classify each behavior as upstream-only, local-only, composable, or requiring a user decision.

### Task 4: Merge upstream and accept SQLite safety architecture

**Files:**
- Merge: all paths in `origin/main`
- Reconcile: `hermes_cli/kanban_db.py`
- Accept/Create: `hermes_cli/sqlite_safe_read.py`
- Reconcile: `hermes_cli/backup.py`
- Reconcile: `hermes_state.py`
- Test: `tests/test_sqlite_lock_safe_inspection.py`
- Test: `tests/hermes_cli/test_kanban_db_repair.py`

**Interfaces:**
- Consumes: audited integration branch and `origin/main`.
- Produces: a merged tree that retains upstream lock-safe inspection, index repair, WAL checkpointing, and corruption-backup retention.

- [ ] **Step 1: Merge without blanket strategy flags**

Run `git merge origin/main`; leave conflicts unresolved until both stages and the merge-base version have been inspected.

- [ ] **Step 2: Resolve non-customized paths in favor of upstream**

For paths outside local customization dependencies, retain upstream changes and record any mechanical call-site adaptations.

- [ ] **Step 3: Preserve upstream's safe SQLite contract**

Ensure no post-connection code opens/closes a raw descriptor for a live SQLite database. Keep live-connection tracking, existing-connection PRAGMA inspection, index-only `REINDEX` recovery, periodic WAL checkpointing, and backup retention.

- [ ] **Step 4: Run SQLite safety tests**

Run:

```bash
venv/bin/python -m pytest -q \
  tests/test_sqlite_lock_safe_inspection.py \
  tests/hermes_cli/test_kanban_db_repair.py \
  tests/hermes_cli/test_kanban_db.py
```

Expected: lock-preservation, repair, checkpoint, and Kanban database tests pass.

### Task 5: Port local Kanban lifecycle guarantees onto upstream

**Files:**
- Modify: `hermes_cli/kanban.py`
- Modify: `hermes_cli/kanban_db.py`
- Modify: `tools/kanban_tools.py`
- Preserve/Adapt: `hermes_cli/kanban_supervisor.py`
- Test: `tests/hermes_cli/test_kanban_heartbeat_ttl_and_respawn_guard.py`
- Test: `tests/hermes_cli/test_kanban_supervisor_contract.py`

**Interfaces:**
- Consumes: upstream Kanban store/schema plus the local acceptance tests.
- Produces: `heartbeat_worker(..., expected_run_id, worker_pid)` with atomic TTL renewal, run-fenced PID CAS/adoption, and safe supervisor helpers.

- [ ] **Step 1: Run local acceptance tests to expose merge regressions**

Run the two focused test files above. Expected before reconciliation: failures identify missing local guarantees; test collection itself must succeed.

- [ ] **Step 2: Restore atomic heartbeat renewal**

Update task and current-run heartbeat timestamps and non-null claim expiries in one run-fenced write transaction. Never shorten a longer expiry and never invent an expiry for an unexpiring claim.

- [ ] **Step 3: Restore run-fenced ownership CAS**

Keep first-live-owner-wins semantics, allow supervisor takeover only from a dead owner, reject superseded runs, and emit diagnostic ownership events without overwriting a successor.

- [ ] **Step 4: Restore active-PR rerun semantics**

Retain upstream scheduling behavior while permitting only deliberate manual reruns after the newest PR acknowledgement. Automatic reclaim/promotion must not disable the guard, and unchanged guard events must remain throttled.

- [ ] **Step 5: Run lifecycle acceptance tests**

Run:

```bash
venv/bin/python -m pytest -q \
  tests/hermes_cli/test_kanban_heartbeat_ttl_and_respawn_guard.py \
  tests/hermes_cli/test_kanban_supervisor_contract.py
```

Expected: all heartbeat, fencing, adoption, guard, and stand-down tests pass.

- [ ] **Step 6: Commit the reconciled lifecycle**

Commit `fix(kanban): reconcile fenced supervisor lifecycle with upstream`.

### Task 6: Make concurrency semantics explicit and verify ten-worker capacity

**Files:**
- Modify: `hermes_cli/kanban_db.py`
- Modify: `hermes_cli/kanban.py`
- Modify: `gateway/kanban_watchers.py`
- Modify: `hermes_cli/config.py`
- Test: `tests/hermes_cli/test_kanban_db.py`
- Test: `tests/gateway/test_kanban_watchers_mixin.py`
- Preserve: `/Users/ctao/.hermes/config.yaml`

**Interfaces:**
- Consumes: `max_spawn`, `max_in_progress`, and `max_in_progress_per_profile`.
- Produces: independent spawn-budget and live-concurrency gates that reach exactly ten workers when capacity and ready tasks exist.

- [ ] **Step 1: Add or retain the three-running-to-ten regression test**

The test must create three running tasks plus at least ten ready tasks, dispatch with `max_spawn=10`, `max_in_progress=10`, and `max_in_progress_per_profile=10`, then assert exactly seven new spawns and ten total running tasks.

- [ ] **Step 2: Verify the regression test fails against upstream's clamp**

Run the individual test and confirm upstream's `remaining` reuse underfills the live cap.

- [ ] **Step 3: Separate the three gates**

Enforce `spawned_this_tick < max_spawn`, `running_before_tick + spawned_this_tick < max_in_progress`, and per-profile running-plus-new-spawns below `max_in_progress_per_profile`.

- [ ] **Step 4: Run concurrency and gateway tests**

Run:

```bash
venv/bin/python -m pytest -q \
  tests/hermes_cli/test_kanban_db.py \
  tests/gateway/test_kanban_watchers_mixin.py
```

Expected: all dispatch and gateway tests pass, including exactly-ten capacity and no oversubscription.

- [ ] **Step 5: Commit explicit cap semantics**

Commit `fix(kanban): separate spawn budget from live concurrency caps`.

### Task 7: Reconcile local products and regenerate web dependencies

**Files:**
- Preserve/Adapt: `cron/scripts/just_do_it_blocked_review_requeue_watchdog.py`
- Preserve/Adapt: `plugins/kanban/claude_lane/**`
- Preserve/Adapt: `docs/kanban/storage-management.md`
- Preserve/Adapt: `hermes_cli/proxy/adapters/codex.py`
- Modify: `package-lock.json`

**Interfaces:**
- Consumes: merged upstream APIs and local product behavior.
- Produces: working watchdog, Claude lane, storage controls, Codex proxy, and a lockfile generated from merged manifests.

- [ ] **Step 1: Adapt local products to upstream APIs**

Make mechanical imports/signatures compatible while preserving watchdog dedupe/safety, Claude-lane fail-closed installation, storage deletion boundaries, and Codex credential rotation plus Cloudflare headers.

- [ ] **Step 2: Run focused local-product tests**

Run:

```bash
venv/bin/python -m pytest -q \
  tests/cron/test_just_do_it_blocked_review_requeue_watchdog.py \
  tests/hermes_cli/test_kanban_claude_lane_install.py \
  tests/plugins/test_kanban_claude_lane_rollout.py \
  tests/hermes_cli/test_kanban_core_functionality.py \
  tests/hermes_cli/test_kanban_db.py
```

Expected: all focused local-product tests pass.

- [ ] **Step 3: Add focused Codex proxy coverage if upstream has none**

Cover adapter registration, credential rotation on 401/429, required Cloudflare headers, header filtering, and refusal when no usable OAuth credential exists.

- [ ] **Step 4: Regenerate the lockfile**

Run `npm install --package-lock-only` from the repository root and confirm only dependency-resolution changes implied by merged manifests remain.

- [ ] **Step 5: Commit reconciled local products**

Commit product adaptations and regenerated lockfile in separately reviewable commits.

### Task 8: Full verification, canary, integration, and push

**Files:**
- Verify: complete repository
- Update: `/Users/ctao/.hermes/config.yaml` only if upstream migrations require mechanical key relocation without changing the approved values

**Interfaces:**
- Consumes: completed integration branch.
- Produces: green `main`, pushed `chad/main`, restarted gateway, and a clean live database integrity check.

- [ ] **Step 1: Run the required full Python gate**

Run `venv/bin/python -m pytest -q`, `venv/bin/ruff check .`, and `venv/bin/ty check`. Expected: zero failures and zero diagnostics.

- [ ] **Step 2: Run Hermes diagnostics**

Run `venv/bin/hermes doctor` or the checkout's equivalent CLI entry point. Expected: no update-introduced errors.

- [ ] **Step 3: Run the required web gate**

From `web/`, run `npm run build`, `npm run lint`, and `npm run test`. Expected: all succeed.

- [ ] **Step 4: Verify installed Claude-lane artifacts**

Run the versioned installer check, then the installed-runtime test suite with `PYTHONPATH` unset. Install updated artifacts only after the merged core contract is verified importable.

- [ ] **Step 5: Run an offline database canary**

Exercise initialization/migration and concurrent dispatch/heartbeat writes against a copy of the board database, then require `PRAGMA integrity_check` to return exactly `ok`.

- [ ] **Step 6: Fast-forward local main**

Run `git switch main` and `git merge --ff-only merge/upstream-2026-07-28`; verify the resulting tree matches the green integration branch.

- [ ] **Step 7: Push the fork**

Run `git push chad main` without force and verify the remote SHA matches local `main`.

- [ ] **Step 8: Restart and smoke-test the gateway**

Start the gateway, verify health, dispatcher startup, and a clean live Kanban integrity check. Confirm the active configuration still contains all three approved values of ten.

- [ ] **Step 9: Report the integration**

Report upstream SHA and commit count, resolved conflict decisions, silent-break adaptations, local behaviors retained, SQLite fixes adopted, every verification command and result, backup location, pushed SHA, and any deferred non-blocking cleanup.
