# Task 13 Cross-Controller Transport Implementation Plan

> **Do not execute as written — superseded in part on 2026-08-06.**
> `../specs/2026-08-06-kanban-boris-on-demand-recovery-boundary-design.md`
> removes the candidate/watchdog work in this plan, retains the adjudicator
> safety work, and adds structured-target production, Boris lifecycle
> exclusion, and a current-authority final-write fence. Preserve the partial
> implementation and evidence; use the replacement plan written after the new
> boundary receives written-spec approval.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Task 13 by transporting exact-head watchdog candidates through the checksummed router cursor and publishing tenant-guarded adjudicator claims under fenced core leases.

**Architecture:** `GenerationCandidate` is advisory state in the shared checksummed router sidecar; it never grants ownership, and the router revalidates it against a fresh core snapshot and provider target before creating a lane. Adjudicator ownership remains in board-scoped SQLite: scoped packets are rechecked after adjudication, a fresh lease fences claim publication, and the spine executes only an exact current claim. This supplement is one integrated reviewer unit under Task 13 of `2026-08-03-kanban-blocked-recovery-router.md`; preserve the existing partial staged work and review the complete Task 13 delta at the end.

**Tech Stack:** Python 3.11 standard library, SQLite, Hermes Kanban JSON CLI, checksummed JSON sidecar, pytest through `scripts/run_tests.sh`.

## Global Constraints

- Design authority is `docs/superpowers/specs/2026-08-05-kanban-task13-cross-controller-transport-design.md`.
- Logical `/Users/ctao/.hermes/scripts/...` paths map only to `/Users/ctao/.hermes/hermes-agent/.worktrees/kanban-blocked-recovery-router/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/...`.
- Never read or modify live `~/.hermes/scripts`, Kanban, CRON, config, gateway, state, or dashboard.
- Keep controller desired modes paused/shadow; do not install sources or alter live jobs.
- Use `scripts/run_tests.sh` with isolated `HOME`/`HERMES_HOME`, staged-first `PYTHONPATH`, and explicit `--file-retries 0`.
- Pass board and normalized tenant explicitly on every task read and mutation.
- Manual lifecycle changes win over replay, claims, candidates, and sidecar state.
- Unknown subprocess or file-commit outcomes halt later mutation until the exact operation reconciles.
- The watchdog never creates a reviewer lane for managed handoffs; the router remains the sole permanent-generation lane creator.
- A candidate is advisory cursor state. Only fresh board-scoped SQLite claims and leases grant ownership.
- Existing partial Task 13 staged edits are preserved and brought into conformance; do not delete or reset them.

---

## File Structure

Tracked core boundary:

- Modify `hermes_cli/kanban_db.py`: tenant-scoped claim records, migration, guarded publication, and snapshot signing.
- Modify `hermes_cli/kanban.py`: board-explicit `controller-claim publish --json` command.
- Modify `tests/hermes_cli/test_kanban_recovery_core.py`: atomic tenant, fencing, replay, and snapshot regressions.
- Modify `tests/hermes_cli/test_kanban_cli.py`: exact JSON CLI contract.
- Modify `tests/hermes_cli/test_kanban_db_init.py`: legacy claim-table tenant migration.

Staged router cursor:

- Modify staged `review_delegator/models.py`: shared immutable `GenerationCandidate` and candidate inbox on `RouterState`.
- Modify staged `review_delegator/state_store.py`: schema migration, candidate encoding, CAS publication, reconciliation, retention, and reconstruction behavior.
- Modify staged `review_delegator/router.py`: exact candidate validation and generation materialization.
- Modify staged `review_delegator/runner.py`: candidate selection, provider revalidation, persistence, and removal.
- Modify staged router tests: `test_models.py`, `test_state_store.py`, `test_generation_router.py`, and `test_router.py`.

Staged controllers:

- Modify staged `just_do_it_blocked_review_requeue_watchdog.py`: publish managed candidates through StateStore; preserve independent-PR behavior only with an explicit claim.
- Modify staged `test_just_do_it_blocked_review_requeue_watchdog.py`: managed no-lane and candidate race coverage.
- Modify staged `kanban_remediation/state.py`: exact scoped packet/verdict/claim-result persistence.
- Modify staged `kanban_remediation/spine.py`: queue typed scope and distinguish deterministic actuator execution from adjudicator-claim execution.
- Modify staged `kanban_remediation/adjudicator_runner.py`: explicit board snapshots, post-agent recheck, fenced lease, JSON claim publish, and reconciliation.
- Modify staged remediation tests: `test_spine.py` and `test_adjudicator_runner.py`.
- Create staged `kanban_remediation/tests/test_state.py`: scoped packet/verdict persistence and legacy-record rejection.

Review artifacts:

- Update `.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/task-13-report.md`.
- Create or refresh the Task 13 SHA-256 manifest and `diff --no-index` patch under the same SDD workspace.

### Task 1: Complete the integrated Task 13 transport boundary

**Interfaces:**

- Produces: `GenerationCandidate.create(*, board, tenant, task_id, pr_url, exact_head, input_signature) -> GenerationCandidate`
- Produces: `generation_candidate_key(candidate: GenerationCandidate) -> str`
- Produces: `StateStore.publish_generation_candidate(candidate: GenerationCandidate) -> CandidatePublishResult`
- Produces: `validate_generation_candidate(candidate, row, resolution) -> CandidateValidation`
- Produces: `remove_generation_candidate(state, candidate) -> RouterState`
- Consumes: `RouterState.generation_candidates: Mapping[str, GenerationCandidate]`
- Produces: `ControllerClaim.expected_tenant: str` and `ControllerClaimRecord.tenant: str`
- Produces CLI: `hermes kanban --board <slug> controller-claim publish ... --json`
- Produces: `claim_for(snapshot, verdict, *, lease_epoch, expires_at, operation_id) -> ControllerClaim`
- Consumes: one exact current adjudicator claim/lease in `spine.consume_verdict(...)`
- Produces: `watchdog_creation_authorized(enrollment, snapshot, now) -> bool`
- Produces: `RemediationState.record_claim_intent(operation_id: str, *, packet_signature: str, board: str, tenant: str, task_id: str, input_signature: str, claim_signature: str) -> None`
- Produces: `RemediationState.resolve_claim_intent(operation_id: str, outcome: str, *, reason: str = "") -> None`
- Produces: `RemediationState.unresolved_claim_intents() -> list[tuple[str, dict]]`

- [ ] **Step 1: Write tenant-bound core claim tests**

Add focused tests that construct a task in tenant `alpha`, acquire an
`adjudicator` lease, and publish a claim with required `expected_tenant`.
Include these behaviors:

```python
def test_controller_claim_rejects_atomic_tenant_change(conn, task, lease):
    claim = controller_claim(task, lease, expected_tenant="alpha")
    conn.execute("UPDATE tasks SET tenant = ? WHERE id = ?", ("beta", task))
    result = kb.publish_controller_claim(conn, claim)
    assert result.outcome == "precondition_changed"
    assert not claims_for(conn, task)


def test_controller_claim_replay_requires_current_tenant(conn, task, lease):
    claim = controller_claim(task, lease, expected_tenant="alpha")
    assert kb.publish_controller_claim(conn, claim).outcome == "committed"
    conn.execute("UPDATE tasks SET tenant = ? WHERE id = ?", ("beta", task))
    assert kb.publish_controller_claim(conn, claim).outcome == "precondition_changed"


def test_recovery_snapshot_signs_claim_tenant(conn, task, lease):
    publish_claim(conn, task, lease, expected_tenant="alpha")
    snapshot = kb.read_recovery_snapshot(conn, now=lease.issued_at)
    claim = next(item for item in snapshot.claims if item.task_id == task)
    assert claim.tenant == "alpha"
    assert snapshot.revision_signature == kb.snapshot_signature(snapshot)
```

Add a CLI test invoking `controller-claim publish` with every required field
and asserting the serialized `GuardedMutationResult`. Add missing-board,
missing-tenant, stale-tenant, and exact replay cases.

Add a DB-initialization fixture with the pre-tenant
`kanban_controller_claims` schema, initialize it, and assert the migrated table
has a non-null `tenant` column whose legacy rows carry the empty quarantine
value.

- [ ] **Step 2: Run the core tests to verify RED**

Run:

```bash
verify_home=$(mktemp -d -t hermes-task13-core-red.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts:$PWD" \
scripts/run_tests.sh tests/hermes_cli/test_kanban_recovery_core.py \
  tests/hermes_cli/test_kanban_cli.py tests/hermes_cli/test_kanban_db_init.py \
  -q --file-retries 0
```

Expected: new tests fail because controller claims lack tenant scope and the
`controller-claim publish` parser/handler does not exist. Remove the temporary
directory after the command exits.

- [ ] **Step 3: Implement the tenant-guarded core claim and CLI boundary**

Update the types:

```python
@dataclass(frozen=True)
class ControllerClaim:
    controller_id: str
    board: str
    expected_tenant: str
    task_id: str
    generation_signature: str
    authority: str
    input_signature: str
    lease_epoch: int
    expires_at: int
    operation_id: str


@dataclass(frozen=True)
class ControllerClaimRecord:
    controller_id: str
    board: str
    tenant: str
    task_id: str
    generation_signature: str
    authority: str
    input_signature: str
    lease_epoch: int
    expires_at: int
    operation_id: str
```

Add a `tenant TEXT NOT NULL DEFAULT ''` migration to
`kanban_controller_claims`. In `publish_controller_claim`, normalize and bind
`expected_tenant` into the operation signature, load the task tenant inside the
same write transaction, and return `precondition_changed` before replay when it
does not match. Store tenant in the row and event. Include it in claim snapshot
encoding and `snapshot_signature`.

Add the parser and handler:

```text
hermes kanban --board BOARD controller-claim publish TASK \
  --expected-tenant TENANT --controller-id adjudicator \
  --generation-signature DIGEST_OR_EMPTY --authority mutate \
  --input-signature DIGEST --lease-epoch N --expires-at EPOCH \
  --operation-id ID --json
```

The handler must require the global board, connect only to that board, construct
the exact `ControllerClaim`, print JSON, and return nonzero only for invariant
or transport failures, not handled domain outcomes.

- [ ] **Step 4: Run core GREEN and commit the tracked boundary**

Run the Step 2 command again. Expected: all selected tests pass with zero
failures and no flaky retry summary. Then run `git diff --check` and commit only
the tracked core/CLI/test files:

```bash
git add hermes_cli/kanban_db.py hermes_cli/kanban.py \
  tests/hermes_cli/test_kanban_recovery_core.py tests/hermes_cli/test_kanban_cli.py \
  tests/hermes_cli/test_kanban_db_init.py
git commit -m "feat(kanban): bind controller claims to tenant"
```

- [ ] **Step 5: Write candidate model and sidecar tests**

Add tests with hand-derived signatures and exact observable behavior:

```python
def test_generation_candidate_binds_all_scope_and_target_fields():
    first = GenerationCandidate.create(
        board="default", tenant="alpha", task_id="t_parent",
        pr_url="https://github.com/acme/repo/pull/7",
        exact_head="a" * 40, input_signature="b" * 64,
    )
    second = GenerationCandidate.create(
        board="default", tenant="alpha", task_id="t_parent",
        pr_url="https://github.com/acme/repo/pull/7",
        exact_head="c" * 40, input_signature="b" * 64,
    )
    assert first.candidate_signature != second.candidate_signature


def test_candidate_publish_replaces_only_same_scoped_task(store):
    first = candidate(task_id="t_parent", exact_head="a" * 40)
    second = candidate(task_id="t_parent", exact_head="b" * 40)
    store.publish_generation_candidate(first)
    store.publish_generation_candidate(second)
    loaded = store.load().state
    assert tuple(loaded.generation_candidates.values()) == (second,)


def test_candidate_publish_reconciles_commit_then_error(store, monkeypatch):
    candidate_value = candidate()
    original = store._atomic_write
    calls = 0
    def commit_then_raise(path, contents):
        nonlocal calls
        calls += 1
        original(path, contents)
        if calls == 1:
            raise OSError("lost acknowledgement after primary replace")
    monkeypatch.setattr(store, "_atomic_write", commit_then_raise)
    result = store.publish_generation_candidate(candidate_value)
    assert result.outcome == "committed"
    assert candidate_value in store.load().state.generation_candidates.values()
```

Also cover invalid/corrupt fields, deep immutability, v1-to-v2 decode with an
empty inbox, checksum failure, CAS conflict, one-candidate-per-scope bounding,
candidate retention for unresolved work, manual-resolution pruning, and
reconstruction with an empty non-authoritative inbox.

- [ ] **Step 6: Run the candidate tests to verify RED**

Run:

```bash
verify_home=$(mktemp -d -t hermes-task13-candidate-red.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts:$PWD" \
scripts/run_tests.sh \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator/tests/test_models.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator/tests/test_state_store.py" \
  -q --file-retries 0
```

Expected: fail because the shared model, RouterState inbox, migration, and
StateStore publication API are absent. Remove the temporary directory after the
command exits.

- [ ] **Step 7: Implement the shared candidate inbox**

Move candidate construction into `review_delegator.models` and validate exact
types, canonical URL, SHA lengths, and canonical signature. Add:

```python
@dataclass(frozen=True)
class RouterState:
    schema_version: int
    revision: int
    last_observed_at: int
    review_handoffs: Mapping[str, ReviewHandoff]
    generation_candidates: Mapping[str, GenerationCandidate]


CandidatePublishOutcome = Literal[
    "committed", "deduplicated", "precondition_changed", "unavailable"
]

@dataclass(frozen=True)
class CandidatePublishResult:
    outcome: CandidatePublishOutcome
    candidate_signature: str
    current_revision: int
    reason: str = ""

@dataclass(frozen=True)
class CandidateValidation:
    outcome: Literal["current", "stale", "unavailable"]
    reason: str
```

Implement exact encode/decode/domain validation, schema migration, canonical
candidate keying, bounded compaction, and a StateStore publisher that uses the
existing retained lock and commit protocol. On an ambiguous write failure,
reload and compare the exact signature before returning or retrying.

- [ ] **Step 8: Write router candidate-consumption tests**

Cover the provider and lifecycle boundary:

```python
def test_current_candidate_materializes_one_stabilizing_generation(router):
    router.seed_candidate(candidate(input_signature=router.row.core_input_signature))
    result = router.tick()
    assert result.current.review_target.revision == "a" * 40
    assert result.current.phase.value == "target_stabilizing"
    assert result.created_lanes == 0


@pytest.mark.parametrize("change", ["board", "tenant", "task", "input", "pr", "head"])
def test_stale_candidate_never_creates_lane(router, change):
    router.seed_candidate(candidate_with(change))
    assert router.tick().created_lanes == 0


def test_watchdog_router_race_has_single_lane(router, watchdog):
    watchdog.publish(candidate())
    first = router.tick(after_stability=True)
    second = router.tick(after_stability=True)
    assert first.created_lanes + second.created_lanes == 1
```

Add manual unblock/reblock/complete precedence, provider unavailable, corrupt
candidate, head change during verification, candidate replacement during the
five-minute window, and exact candidate removal after durable generation
materialization.

- [ ] **Step 9: Run router tests to verify RED, then implement GREEN**

Run staged `test_generation_router.py` and `test_router.py` through the same
isolated/no-retry command pattern. Expected RED: candidate state is ignored.

Implement `validate_generation_candidate` in `router.py` as the pure exact
board/tenant/task/input/provider comparison and `remove_generation_candidate`
as the immutable scoped-map update. Update `runner.py` to select only the exact
scoped candidate, pass its canonical PR descriptor through
`targets.resolve_target`, require `CandidateValidation("current", ...)`, and
persist removal only after materialization/manual release. Candidate presence
must not bypass `_mutation_gate` or ownership census checks. Re-run the two
router files plus Steps 5-6 files and require zero failures.

- [ ] **Step 10: Replace watchdog-local transport with the shared publisher**

Write a failing managed-handoff test that injects a real temporary StateStore
and asserts:

```python
result = process_managed_handoff(candidate, publisher, forbidden_create_lane)
assert result.published_candidate is True
assert result.lane_task_id is None
assert state_store_contains(candidate)
```

Add stale-head, CAS contention, commit-after-replace reconciliation, dry-run
nonpublication, and malformed managed metadata cases. Preserve independent PR
lane creation only when the enrollment is not a managed handoff and its
explicit controller claim is current.

Add `watchdog_creation_authorized(enrollment, snapshot, now)` and require the
independent enrollment to contain exact board, tenant, and source task id. It
returns true only for exactly one fresh `pr-watchdog` claim with `mutate`
authority whose tenant, task, generation/input signatures, lease epoch, and
expiry match the same signed snapshot. Legacy independent enrollments without
that scope remain enrolled but do not create a lane.

Delete the private candidate class and private `generation_candidates` registry
as a transport. Import the shared type, construct StateStore with the exact
candidate board/tenant, and publish there. Run
`test_just_do_it_blocked_review_requeue_watchdog.py` with explicit no-retry;
expected GREEN is zero managed-path lane creates.

- [ ] **Step 11: Write scoped adjudication packet and claim-flow tests**

Extend remediation state tests so pending packets and verdicts preserve exact
`board`, `tenant`, `task_id`, and `input_signature`. Legacy unscoped records
must return an escalation-only result.

Add a bounded `claim_intents` registry keyed by deterministic operation id.
`record_claim_intent` persists the packet signature, board, tenant, task input
signature, intended claim signature, outcome `pending`, retry count zero, and
timestamps before the CLI call. `resolve_claim_intent` accepts only
`committed`, `deduplicated`, `not_applied`, `superseded_external`, `conflict`,
or `unknown`. `unresolved_claim_intents` returns pending/unknown records oldest
first so every tick reconciles them before processing another packet.

Add adjudicator runner tests for:

```python
def test_shadow_adjudicator_never_publishes_claim(runner):
    result = runner.run("SHADOW", board="default")
    assert result["would_claim"] == 1
    assert runner.claim_publish_calls == []


def test_post_agent_snapshot_change_prevents_claim(runner):
    runner.change_task_after_agent()
    result = runner.run("LIVE", board="default")
    assert result["stale"] == 1
    assert runner.claim_publish_calls == []


def test_live_claim_uses_fresh_lease_and_exact_tenant(runner):
    result = runner.run("LIVE", board="default")
    claim = runner.only_claim_request()
    assert claim.expected_tenant == "alpha"
    assert claim.lease_epoch == runner.lease.holder_epoch
    assert claim.expires_at <= runner.lease.expires_at
    assert result["claims_published"] == 1
```

Also cover paused no-op, stale-before-agent, lease denial/takeover, malformed
CLI JSON, timeout after claim commit, exact operation replay, definite
nonmutation, multiple fresh claims producing unhealthy escalation-only
ownership, and conflict halting later packet mutation.

- [ ] **Step 12: Implement adjudicator lease/publication and spine execution**

Change remediation packet persistence to explicit scoped fields. In
`adjudicator_runner.run(mode, *, board)`, read and project exact snapshots,
perform before/after input checks, use the existing board-explicit
`controller-lease acquire|renew --json`, and publish through the new
`controller-claim publish --json` command. Commands use argument lists, bounded
timeouts/output, minimal environment, stable operation ids, and exact JSON
parsing. Before any CLI mutation, persist `record_claim_intent`. At the start of
every tick, reconcile every `unresolved_claim_intents()` record against fresh
claim/event evidence before adjudicating or publishing another packet. Resolve
the intent to the exact typed terminal outcome; leave conflict/unknown records
fail-closed and perform no later mutation.

Keep `spine.plan()` deterministic-actuator-only. Update
`spine.consume_verdict()` to require one matching current adjudicator
claim/lease and return escalation-only for stale input, manual lifecycle,
unsupported verbs, unhealthy census, or ambiguous claims. The existing spine
may mechanically execute a current adjudicator plan, but the plan's ownership
source remains `adjudicator_claim`, never `remediation_actuator`.

- [ ] **Step 13: Run focused cross-controller GREEN**

Run:

```bash
verify_home=$(mktemp -d -t hermes-task13-focused.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts:$PWD" \
scripts/run_tests.sh \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests/test_spine.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests/test_adjudicator_runner.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/test_just_do_it_blocked_review_requeue_watchdog.py" \
  tests/hermes_cli/test_kanban_recovery_core.py \
  tests/hermes_cli/test_kanban_cli.py -q --file-retries 0
```

Expected: all selected tests pass, no file retry or live path access. Remove the
temporary directory after the command exits.

- [ ] **Step 14: Run the complete staged Task 13 gate**

Run:

```bash
verify_home=$(mktemp -d -t hermes-task13-full.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts:$PWD" \
scripts/run_tests.sh \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator/tests" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/test_just_do_it_blocked_review_requeue_watchdog.py" \
  tests/hermes_cli/test_kanban_recovery_core.py \
  tests/hermes_cli/test_kanban_cli.py -q --file-retries 0
```

Expected: zero failures, no flaky summary, no test reads or writes a live
Hermes path. Remove the temporary directory after the command exits.

- [ ] **Step 15: Compile, inspect scope, and freeze review evidence**

Run `python -m compileall -q` on the staged `review_delegator` and
`kanban_remediation` directories, `python -m py_compile` on the staged watchdog,
`git diff --check`, and tracked status inspection.

Create a Task 13 `diff --no-index` artifact from the read-only source snapshot
to the staged tree for every changed local source/test. Create a SHA-256
manifest with exact relative paths and hashes. The manifest must not include
live files or unbounded state artifacts.

Append `task-13-report.md` with the approved design/plan references, every RED
and GREEN command/result, tracked commit(s), staged hashes, artifact paths,
scope proof, and concerns. Do not claim any live run or rollout.

Staged automation remains outside Git; its manifest and review patch are the
reviewable record until guarded rollout. Tracked Task 13 code must be clean
after the Step 4 commit; any later tracked change is a scope violation and must
be explained and reviewed before proceeding.
