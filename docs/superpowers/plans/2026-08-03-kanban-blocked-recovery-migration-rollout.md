# Kanban Blocked Recovery Migration and Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair existing review deadlocks, migrate only deterministic block evidence, and activate recovery through paused, shadow, canary-live, and live gates without exposing duplicate work.

**Architecture:** Generate signed read-only manifests first; perform graph repair only in a stopped-gateway window through guarded CLI operations. Restart with every controller paused, prove health and ownership in shadow, require isolated negative canaries plus one allowlisted positive production completion, then enable review routing, transient retry, and deterministic legacy migration in that order.

**Tech Stack:** Python 3.11, Hermes Kanban/cron CLI, SQLite online backup, canonical JSON manifests, gateway runtime fingerprints, pytest through `scripts/run_tests.sh`.

## Global Constraints

- Complete and review the core, router, and cron-supervision plans first.
- Use `scripts/run_tests.sh` for every test command.
- Set all three controller manifest modes to `paused` before pausing jobs or mutating live board state.
- Stop the gateway/dispatcher before unlinking review cycles or applying recovery manifests.
- Re-read and re-sign every live precondition immediately before mutation.
- Prefix text alone cannot classify a block.
- Dependency migration is graph-first and atomic.
- Prose-only reviewer outcomes are never automatically completed.
- `t_9e4b3bc2` requires one explicit approval of its fresh exact manifest signature.
- Finalizing `t_9e4b3bc2` and parking `t_358c0763` occur in one transaction.
- Never restore older SQLite/sidecar snapshots over normally advanced live state.
- Never manufacture a rejection on a production PR.
- Keep transient retry shadowed until review completion has passed its positive canary.
- Begin transient live mode with one mutation per tick.

---

## File Structure

- Create `/Users/ctao/.hermes/scripts/review_delegator/migration.py`: deterministic evidence classifier and reclassification-manifest generator.
- Create `/Users/ctao/.hermes/scripts/review_delegator/recovery_manifest.py`: review-cycle discovery and legacy outcome manifests.
- Create `/Users/ctao/.hermes/scripts/review_delegator/maintenance.py`: stopped-gateway preflight, online backup, guarded repair, and postcondition verification.
- Add local tests `test_migration.py`, `test_recovery_manifest.py`, and `test_maintenance.py`.
- Create `tests/integration/test_kanban_blocked_recovery_fullstack.py`: isolated CLI canary matrix.
- Create `/Users/ctao/.hermes/state/kanban-blocked-recovery-rollout.json` only when rollout begins.
- Modify live config, cron jobs, and Kanban only through supported guarded commands.

### Task 1: Generate graph-first block migration manifests

**Files:**
- Create: `/Users/ctao/.hermes/scripts/review_delegator/migration.py`
- Create: `/Users/ctao/.hermes/scripts/review_delegator/tests/test_migration.py`

**Interfaces:**
- Produces: `classify_candidate(show: dict, graph: GraphSnapshot) -> MigrationDecision`
- Produces: `build_migration_manifest(snapshot: RecoverySnapshot) -> MigrationManifest`

- [ ] **Step 1: Write deterministic-versus-ambiguous tests**

```python
def test_structured_review_handoff_migrates_to_review():
    decision = classify_candidate(structured_review_show(), graph())
    assert (decision.kind, decision.automatic) == ("review", True)

def test_prefix_only_review_stays_untyped():
    decision = classify_candidate(prose_show("review-required: looks good"), graph())
    assert (decision.kind, decision.automatic) == (None, False)

def test_dependency_requires_all_existing_parents_and_edges():
    decision = classify_candidate(machine_dependency_show(["t_a", "t_b"]), graph(existing={"t_a"}))
    assert decision.reason == "missing_dependency_parent:t_b"

def test_human_kind_requires_resolved_owner():
    decision = classify_candidate(structured_human_show("needs_input", owner=None), graph())
    assert decision.reason == "human_owner_unresolved"
```

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=/Users/ctao/.hermes/scripts scripts/run_tests.sh /Users/ctao/.hermes/scripts/review_delegator/tests/test_migration.py -q`

Expected: no graph-first migration generator exists.

- [ ] **Step 3: Implement exact machine-readable rules**

```python
@dataclass(frozen=True)
class MigrationDecision:
    task_id: str
    expected_event_signature: str
    kind: Literal["review", "transient", "dependency", "needs_input", "capability"] | None
    automatic: bool
    evidence: Mapping[str, object]
    required_parent_ids: tuple[str, ...] = ()
    reason: str = ""
```

Review requires a complete structured immutable target/handoff. Transient
requires vetted dispatcher events. Dependency requires explicit complete parent
ids. Human kinds require structured kind plus structured or board-default owner.
All other cases remain untyped and reviewable.

- [ ] **Step 4: Run migration and ownership tests**

Run: `PYTHONPATH=/Users/ctao/.hermes/scripts scripts/run_tests.sh /Users/ctao/.hermes/scripts/review_delegator/tests/test_migration.py /Users/ctao/.hermes/scripts/review_delegator/tests/test_ownership.py -q`

Expected: all pass; freeform inference never produces an automatic mutation.

- [ ] **Step 5: Dry-run against a copied board**

Require canonical JSON with task id, event signature, proposed kind, exact
evidence, parent plan, and automatic/manual disposition. Do not use the live board.

### Task 2: Generate guarded legacy review recovery manifests

**Files:**
- Create: `/Users/ctao/.hermes/scripts/review_delegator/recovery_manifest.py`
- Create: `/Users/ctao/.hermes/scripts/review_delegator/tests/test_recovery_manifest.py`

**Interfaces:**
- Produces: `discover_review_cycles(snapshot) -> tuple[ReviewCycle, ...]`
- Produces: `build_cycle_repair(cycle) -> CycleRepairManifest`
- Produces: `build_legacy_outcome_manifest(case) -> LegacyRecoveryManifest`

- [ ] **Step 1: Write cycle and `t_9e4b3bc2` fixture tests**

```python
def test_unique_cycle_is_repairable():
    manifest = build_cycle_repair(discover_review_cycles(cycle_snapshot())[0])
    assert manifest.phase == "repair_pending"
    assert manifest.unlink == {"parent": "t_impl", "child": "t_review"}

def test_ambiguous_or_changed_cycle_is_not_automatic():
    assert build_cycle_repair(ambiguous_cycle()).automatic is False
    assert build_cycle_repair(changed_cycle()).automatic is False

def test_t9_manifest_parks_downstream_atomically():
    manifest = build_legacy_outcome_manifest(t9_fixture())
    assert manifest.reviewer_task_id == "t_9e4b3bc2"
    assert manifest.child_task_id == "t_358c0763"
    assert manifest.child_transition == {"status": "blocked", "block_kind": "needs_input"}
    assert manifest.human_approval_required is True
```

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=/Users/ctao/.hermes/scripts scripts/run_tests.sh /Users/ctao/.hermes/scripts/review_delegator/tests/test_recovery_manifest.py -q`

Expected: current recovery has no canonical graph/target/downstream manifest.

- [ ] **Step 3: Implement exact signatures and effects**

Cycle repair requires current review block, unique reviewer, exact edge, exact
events, and no unresolved intent. Automatic legacy finalization additionally
requires structured run metadata satisfying target/verdict/no-findings and safe
downstream effects. Otherwise require human approval. The `t_9e4b3bc2` shape
binds reviewer, child, graph, adopted targets, events, both transitions, and one
operation id. Canonical JSON SHA-256 is the approval signature.

- [ ] **Step 4: Run manifest and core transaction tests**

Run: `PYTHONPATH=/Users/ctao/.hermes/scripts scripts/run_tests.sh /Users/ctao/.hermes/scripts/review_delegator/tests/test_recovery_manifest.py tests/hermes_cli/test_kanban_recovery_core.py -q`

Expected: manifest shape matches `apply-recovery-manifest`.

- [ ] **Step 5: Keep the live manifest unsigned**

Do not obtain approval until a stopped-gateway preflight freshly verifies every
live target and board fingerprint.

### Task 3: Build stopped-gateway maintenance orchestration

**Files:**
- Create: `/Users/ctao/.hermes/scripts/review_delegator/maintenance.py`
- Create: `/Users/ctao/.hermes/scripts/review_delegator/tests/test_maintenance.py`

**Interfaces:**
- Produces: `preflight(root: Path, board: str) -> MaintenancePreflight`
- Produces: `online_backup(board_path: Path, output: Path) -> BackupRecord`
- Produces: `apply_cycle_repair(manifest) -> OperationResult`

- [ ] **Step 1: Write gateway, drift, and unknown-outcome tests**

```python
def test_apply_refuses_running_gateway(maintenance):
    assert maintenance.apply_cycle_repair(cycle_manifest()).outcome == "gateway_running"
    assert unlink_calls == []

def test_precondition_drift_changes_nothing(maintenance):
    manifest = cycle_manifest()
    mutate_reviewer_event()
    assert maintenance.apply_cycle_repair(manifest).outcome == "precondition_changed"
    assert unlink_calls == []

def test_unlink_timeout_reconciles_committed_event(maintenance):
    result = maintenance.apply_cycle_repair(timeout_after_commit_manifest())
    assert result.outcome == "committed"
    assert unlink_calls == [result.operation_id]
```

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=/Users/ctao/.hermes/scripts scripts/run_tests.sh /Users/ctao/.hermes/scripts/review_delegator/tests/test_maintenance.py -q`

Expected: no single command enforces paused modes, stopped gateway, backup, and reconciliation.

- [ ] **Step 3: Implement exact preflight and supported operations**

Read desired/observed controller modes, jobs including disabled, gateway
PID/start/fingerprint, cron health, router lock, sidecar revision/checksum,
board revision, ownership census, cycles, and intents. Use SQLite online backup.
Apply only guarded `hermes kanban unlink` and `apply-recovery-manifest`; journal
and reconcile unknown results. Verify reviewer dispatchability while stopped.

- [ ] **Step 4: Run maintenance and rollback tests**

Run: `PYTHONPATH=/Users/ctao/.hermes/scripts scripts/run_tests.sh /Users/ctao/.hermes/scripts/review_delegator/tests/test_maintenance.py /Users/ctao/.hermes/scripts/review_delegator/tests/test_operation_journal.py /Users/ctao/.hermes/scripts/review_delegator/tests/test_state_store.py -q`

Expected: all pass, including refusal to restore stale snapshots.

- [ ] **Step 5: Preflight a copied production snapshot**

Require expected job ids: router `fef26eedc8da`, actuator `8d721850ca15`,
adjudicator `5a8468229522`, specialized watchdog `ff865c33c5ea`. Stop if copied
records do not match; do not adapt expected ids heuristically.

### Task 4: Build the isolated full-stack canary matrix

**Files:**
- Create: `tests/integration/test_kanban_blocked_recovery_fullstack.py`

**Interfaces:**
- Consumes: real Hermes CLI subprocesses against a temporary root/board.

- [ ] **Step 1: Write positive and negative cases**

```python
def test_approved_generation_completes_through_strict_judge(fullstack):
    case = fullstack.approved_case()
    case.run_router_to_terminal()
    assert case.parent()["status"] == "done"

@pytest.mark.parametrize("case_name, phase", [
    ("reviewer_rejected", "rejected"),
    ("judge_rejected", "approved_pending_completion"),
    ("judge_unavailable", "judge_exhausted"),
    ("judge_parse_failed", "judge_exhausted"),
    ("stale_target", "invalid_review"),
    ("verifier_outage", "verification_exhausted"),
])
def test_negative_cases_keep_parent_blocked(fullstack, case_name, phase):
    case = fullstack.case(case_name)
    case.run_router_to_terminal()
    assert case.parent()["status"] == "blocked"
    assert case.handoff_phase() == phase
```

- [ ] **Step 2: Verify RED before implementation integration**

Run: `scripts/run_tests.sh tests/integration/test_kanban_blocked_recovery_fullstack.py -q`

Expected: failures until the three implementation plans are complete.

- [ ] **Step 3: Add deterministic target/judge boundary fixtures**

Cover accepted, rejected, unavailable, parse-failed, precondition change, target
change, create/complete timeout after commit, manual unblock, duplicate claim,
backward clock jump, and corrupt sidecar.

- [ ] **Step 4: Run the complete matrix**

Run: `scripts/run_tests.sh tests/integration/test_kanban_blocked_recovery_fullstack.py -q`

Expected: only exact approval plus unchanged target plus strict acceptance completes.

- [ ] **Step 5: Commit the canary harness**

```bash
git add tests/integration/test_kanban_blocked_recovery_fullstack.py
git commit -m "test(kanban): add blocked recovery full-stack canaries"
```

### Task 5: Install the controller manifest in paused mode

**Files:**
- Runtime guarded update: `/Users/ctao/.hermes/config.yaml`
- Runtime read-only verification: `/Users/ctao/.hermes/cron/jobs.json`

**Interfaces:**
- Produces: exact paused specs for router, actuator, and adjudicator.

- [ ] **Step 1: Capture canonical live state**

Save board/state/job/gateway/cron/census fingerprints in the rollout record.
Require the four expected job ids and exact observed specs.

- [ ] **Step 2: Calculate source, schedule, and delivery digests**

Use the supervisor's canonical digest functions. Router closure includes local
entry points/helpers and every Hermes CLI/core module its subprocess loads.

- [ ] **Step 3: Atomically set all controller desired modes paused**

Use guarded config update; verify validated revision/digest. Do not edit YAML directly.

- [ ] **Step 4: Pause router, actuator, and adjudicator jobs**

Use `hermes cron pause <job-id>`. Reconcile ambiguous responses from exact job
state before retry. Do not create missing jobs.

- [ ] **Step 5: Verify paused gate**

Require desired paused, observed disabled, exact immutable job fields, no active
controller execution, and no unresolved enable intent.

### Task 6: Stop gateway, back up, and repair cycles

**Files:**
- Runtime state only through `maintenance.py` and guarded Hermes commands.

- [ ] **Step 1: Stop gateway and prove dispatcher inactivity**

Use `hermes gateway stop`; require no live PID, new claims, controller execution,
or held router mutation lock.

- [ ] **Step 2: Take final online backup and checksummed snapshots**

Back up SQLite online and copy router/remediation state plus jobs.json. Label the
database backup disaster-recovery-only.

- [ ] **Step 3: Revalidate and seed each `repair_pending` generation**

Require exact implementation/reviewer/events/edge/target and no run or intent.

- [ ] **Step 4: Guard-unlink one cycle at a time**

Use stable operation id and exact preconditions. Reconcile unknown result, then
verify edge absent and reviewer dispatchable before the next cycle.

- [ ] **Step 5: Stop on any drift**

Leave a changed pair untouched and record human review. Do not rewrite its
manifest during maintenance.

### Task 7: Approve and apply the `t_9e4b3bc2` manifest

**Files:**
- Runtime manifest generated by `recovery_manifest.py`.

- [ ] **Step 1: Freshly verify targets and downstream effect**

Require unchanged prose evidence, null structured run metadata, adopted targets,
sole unfinished-parent relationship, and `t_358c0763` still `todo`.

- [ ] **Step 2: Generate final canonical JSON and SHA-256**

Bind reviewer, child, graph, targets, events, both transitions, and operation id.

- [ ] **Step 3: Obtain human approval for this exact signature**

Prior design/plan approval and prose comments do not count. Record actor/time
only after the exact fresh manifest signature is approved.

- [ ] **Step 4: Apply guarded atomic transaction**

Run `hermes kanban apply-recovery-manifest <path> --approval-signature <sha256> --json`.
Reconcile unknown output before retry; precondition drift changes neither task.

- [ ] **Step 5: Verify no ready-state window**

Require reviewer done, child blocked/needs_input, operation id on both events,
unchanged unrelated graph, and no ready/claim event for `t_358c0763`.

### Task 8: Restart paused and establish shadow health

**Files:**
- Runtime guarded config/cron state only.

- [ ] **Step 1: Start gateway with controllers paused**

Require current PID/start/module fingerprint, cron readiness before READY, and
healthy heartbeat/success.

- [ ] **Step 2: Set router shadow, then enable exact router job**

Set the board's `review_mode=shadow` and `transient_mode=shadow`, then update the
router job's desired mode to `shadow`. Resume `fef26eedc8da` or allow exact
disabled-only self-heal. Verify job operation id and spec.

- [ ] **Step 3: Observe two nonmutating router ticks**

Require fresh lease, clean census, no lane/task mutation, no unresolved intent,
and delivered health.

- [ ] **Step 4: Set actuator/adjudicator shadow, then enable exact jobs**

Run two shared-snapshot escalation-only ticks.

- [ ] **Step 5: Require shadow gate**

Stop for ownership conflict, stale lease, cron quarantine, controller drift,
reconstruction, or unknown intent.

### Task 9: Run one allowlisted positive production canary

**Files:**
- Runtime guarded config only.

- [ ] **Step 1: Select one safe current handoff**

Require immutable target, clean ownership, no active reviewer, isolated workspace,
and low downstream risk. Allowlist only that task; set `review_mode=live` while
keeping `transient_mode=shadow`.

- [ ] **Step 2: Verify stable single lane creation**

Require five unchanged minutes, permanent key, no parent link, exact tenant,
isolated workspace, 3600-second runtime, max retries one, and no concurrent lane.

- [ ] **Step 3: Require exact terminal structured approval**

Do not force/edit a verdict. Rejection/timeout/malformed/stale outcomes prove
safety but do not satisfy the positive canary.

- [ ] **Step 4: Verify target and strict completion**

Require pre-judge/post-judge target reads, exact done, transaction signatures,
post-commit read, parent done, and expected downstream readiness.

- [ ] **Step 5: Record pass only on actual acceptance**

Save task/generation/reviewer/target/judge/event/operation signatures.

### Task 10: Enable review routing, transient retry, then migration

**Files:**
- Runtime guarded config and manifests only.

- [ ] **Step 1: Re-run isolated negative canaries against installed code**

All negative branches must retain the parent with exact phase/health behavior.

- [ ] **Step 2: Remove review allowlist after positive and negative gates**

Keep managed-board allowlist exact and `review_mode=live`; verify
census/lease/controller health first.

- [ ] **Step 3: Enable transient retry last with cap one**

Set `transient_mode=live` only after review remains live and healthy. Only
typed/structured transient evidence qualifies. Keep the shared board mutation
cap at one and observe event-derived budgets, stale skips, manual precedence,
and exhaustion before proposing a higher cap.

- [ ] **Step 4: Regenerate and apply deterministic migration last**

Revalidate live signatures. Apply dependencies graph-first; use guarded
reclassify for other exact entries; leave ambiguous residue untyped.

- [ ] **Step 5: Publish effectiveness and residue**

Report owners, phases, mutations, deferrals, escalations, typed coverage, and
untyped ambiguity. Backlog size alone is not failure.

### Task 11: Verify success and rollback posture

**Files:**
- Runtime rollout record and read-only evidence.

- [ ] **Step 1: Run final repository tests**

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_*.py \
  tests/tools/test_kanban_*.py \
  tests/cron \
  tests/gateway/test_runtime_fingerprint.py \
  tests/gateway/test_recovery_control.py \
  tests/gateway/test_cron_supervisor.py \
  tests/gateway/test_gateway_cron_readiness.py \
  tests/integration/test_kanban_blocked_recovery_fullstack.py -q
```

Expected: all pass with no flaky-file retry summary.

- [ ] **Step 2: Run all local recovery tests**

```bash
PYTHONPATH=/Users/ctao/.hermes/scripts scripts/run_tests.sh \
  /Users/ctao/.hermes/scripts/review_delegator/tests \
  /Users/ctao/.hermes/scripts/kanban_remediation/tests \
  /Users/ctao/.hermes/scripts/test_just_do_it_blocked_review_requeue_watchdog.py -q
```

Expected: all pass.

- [ ] **Step 3: Verify every design success criterion**

Require active/effective router, accepted production canary, negative canaries,
current runtime, independently supervised cron, exact controller specs, strict
completion, rejection safety, exactly one owner, typed growth, and health output.

- [ ] **Step 4: Exercise rollback on an isolated clone**

Pause mutation first. Restore a last-good sidecar only when its revision matches
all live events; otherwise reconstruct read-only. Re-link only never-run reviewers.
Never restore pre-rollout DB over advanced state.

- [ ] **Step 5: Request final operational review**

Require review of rollout record, remaining escalations, and rollback posture
before declaring the recovery system live.

## Remaining Human-Intervention Cases

- Ownership conflicts or zero/multiple candidate owners.
- Ambiguous legacy prose without deterministic evidence.
- Human blocks without structured or board-default owner.
- Exhausted reviewer, verifier, or judge budgets without changed evidence/health generation.
- Reviewer or strict-judge rejection requiring changed implementation evidence.
- Unknown operation conflicts not reconcilable from live events.
- Controller/runtime drift other than exact disabled-only drift.
- Cron unhealthy after one automatic restart until exact acknowledgement or fingerprint change.
- The fresh one-time `t_9e4b3bc2` manifest signature.
- Any live precondition drift during maintenance, canary, migration, or rollback.
