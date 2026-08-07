# Kanban Task 13 Boris Boundary Revision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the interrupted blocked-recovery Task 13 without any
just-do-it candidate transport or on-demand Boris changes, while adding a real
typed review-target producer, exclusion-first Boris lifecycle protection, and
an atomic current-authority final-write fence.

**Architecture:** Core Kanban owns typed managed-review writes and the final
claim/lease/authority transaction. The staged router reads only the latest
structured block event, while the shared ownership resolver excludes
Boris-created tasks before every positive predicate. Adjudicator mutation
intents reconcile by replaying the same idempotent operation; the deprecated
watchdog is excluded from the future install bundle rather than rewritten.

**Tech Stack:** Python 3.11 standard library, SQLite, Hermes Kanban JSON CLI,
checksummed JSON sidecar, pytest through `scripts/run_tests.sh`, staged local
automation under the existing SDD workspace.

## Global Constraints

- Design authority is
  `docs/superpowers/specs/2026-08-06-kanban-boris-on-demand-recovery-boundary-design.md`.
- Logical `/Users/ctao/.hermes/scripts/...` implementation paths map only to
  `.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/...`.
- Never edit, copy over, migrate, reinstall, or reinterpret anything under
  `/Users/ctao/.hermes/scripts/boris_intake/`, Boris config/state/schema, or
  Boris daemon/service/gate/dispatch/dedup/template/Slack/security behavior.
- Do not change the exact `Source Slack message: <permalink>` contract.
- Do not edit the retained reaction watcher or its tests. Its Kanban access
  remains read-only; Slack reactions and private cursor/backup/telemetry remain
  its only writes.
- Do not edit either deprecated live CRON or the staged
  `just_do_it_blocked_review_requeue_watchdog.py`; exclude it from the install
  allowlist and tests instead.
- Do not read or mutate live Kanban, CRON registry, gateway, config, state, or
  dashboard. No job disablement, source installation, or rollback occurs in
  this plan.
- Preserve the four existing unstaged Task 13 core/test files and every ignored
  staged artifact. Reconcile them file-by-file; never reset or broadly discard
  the worktree.
- Manual lifecycle changes win. Unknown claim or lifecycle outcomes halt later
  mutation until the exact operation reconciles.
- Extend the existing `kanban_block` model tool; do not add a new model tool.
- Use `scripts/run_tests.sh`, isolated `HOME`/`HERMES_HOME`, staged-first
  `PYTHONPATH`, and `--file-retries 0` for every acceptance gate.
- The `.superpowers/` tree is intentionally ignored. Do not force-add it.
  Staged-only tasks end with focused tests plus refreshed SHA-256/diff evidence,
  not a git commit.
- Live retirement of `ee4582d0598f` and `ff865c33c5ea` is a separate plan.
  That rollout must disable and prove both jobs quiescent before installing
  modules that remove `GenerationCandidate`.

---

## File Structure

Tracked core boundary:

- Modify `hermes_cli/kanban_db.py`: retain tenant-scoped claims; add
  task-scoped authority signatures, exact-one-current-claim final fencing,
  the Boris creator guard, and atomic managed-review blocking.
- Modify `hermes_cli/kanban.py`: finish the guarded lifecycle JSON command and
  add the board-explicit `block-managed-review` JSON command.
- Modify `tools/kanban_tools.py`: extend existing `kanban_block` with an
  optional typed `review_target`; compute a stable operation id and call the
  managed-review core API.
- Modify `tests/hermes_cli/test_kanban_recovery_core.py`: current-authority,
  claim race, Boris guard, managed-target atomicity, replay, and legacy tests.
- Modify `tests/hermes_cli/test_kanban_cli.py`: exact JSON CLI contracts.
- Modify `tests/tools/test_kanban_tools.py`: existing-tool schema and behavior.

Staged router boundary:

- Modify staged `review_delegator/models.py`: remove `GenerationCandidate`,
  `CandidateValidation`, candidate keys, and the candidate mapping.
- Modify staged `review_delegator/state_store.py`: restore the closed schema-v1
  state shape and remove candidate decode/encode/publication/reconciliation.
- Modify staged `review_delegator/router.py`: remove candidate validation and
  removal helpers.
- Modify staged `review_delegator/runner.py`: resolve only the top-level target
  on the latest structured block event and reject Boris-created rows before
  typed or legacy routing.
- Modify staged router tests: `test_models.py`, `test_state_store.py`,
  `test_generation_router.py`, and `test_router.py`.

Staged ownership/remediation boundary:

- Modify staged `review_delegator/ownership.py`: add the exclusion-first Boris
  disposition, excluded census accounting, and core authority projection.
- Modify staged `kanban_remediation/kanban_engine.py` and `reconcile.py`: filter
  blocked and failed Boris tasks before action or packet routing.
- Modify staged `kanban_remediation/spine.py`: enforce exclusion before verdict
  claims, carry authority signatures, and journal mutation intents.
- Modify staged `kanban_remediation/adjudicator_runner.py`: refuse Boris packets
  before adjudication or claim publication.
- Modify staged `kanban_remediation/state.py`: durable bounded mutation intents.
- Modify staged remediation tests: `test_state.py`, `test_spine.py`,
  `test_spine_exclusion.py`, `test_adjudicator_runner.py`, and
  `test_kanban_engine.py`.

Evidence/deployment boundary:

- Create ignored
  `.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/task-13-install-allowlist.txt`:
  exact active runtime files; no deprecated watcher/test and no Boris path.
- Update ignored `task-13-report.md`, `task-13-sha256.txt`,
  `task-13-staged.diff`, and `progress.md` without deleting historical evidence.
- Create ignored `task-13-protected-baseline.sha256`: immutable Boris/reaction
  compatibility baseline for the implementation session.

### Task 1: Finish the interrupted core final-authority boundary

**Files:**

- Modify: `hermes_cli/kanban_db.py`
- Modify: `hermes_cli/kanban.py`
- Modify: `tests/hermes_cli/test_kanban_recovery_core.py`
- Modify: `tests/hermes_cli/test_kanban_cli.py`

**Interfaces:**

- Retains: `ControllerClaim`, `publish_controller_claim(...)`, tenant-scoped
  claim rows, and board-explicit claim publication.
- Produces:
  `recovery_task_authority_signature(snapshot: RecoverySnapshot, task_id: str) -> str`.
- Produces: `ControllerAuthorityPreconditions.authority_signature: str`.
- Produces:
  `apply_controller_lifecycle_mutation(conn, request: ControllerLifecycleMutation) -> GuardedMutationResult`.
- CLI produces: `hermes kanban --board <board> controller-claim mutate ...
  --authority-signature <sha256> --json`.
- Removes: superseded independent-watchdog task-creation tests and any
  `ControllerTaskCreate`/`create_controller_task` surface if present.

- [ ] **Step 1: Preserve and classify the interrupted delta**

Run these read-only checks and save their output in the Task 13 report before
editing:

```bash
git status --short
git diff --stat
git diff --name-only
protected_baseline=.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/task-13-protected-baseline.sha256
find /Users/ctao/.hermes/scripts/boris_intake -type f \
  ! -path '*/__pycache__/*' -print0 | sort -z | xargs -0 shasum -a 256 \
  > "$protected_baseline"
shasum -a 256 \
  /Users/ctao/.hermes/scripts/just_do_it_review_reaction_watchdog.py \
  /Users/ctao/.hermes/scripts/test_just_do_it_review_reaction_watchdog.py \
  >> "$protected_baseline"
shasum -a 256 "$protected_baseline"
```

Expected: only the four known tracked Task 13 files are dirty; the Boris and
reaction hashes become the immutable compatibility baseline for Task 6. Save
the exact sorted output in ignored
`.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/task-13-protected-baseline.sha256`
and record its own SHA-256 in `task-13-report.md`.

- [ ] **Step 2: Remove only the superseded watchdog-create test slice**

Delete `_watchdog_task_creation_fixture`, `_watchdog_created_tasks`, and the
five `test_guarded_watchdog_create_*` tests from
`tests/hermes_cli/test_kanban_recovery_core.py`. Retain and rename generic claim
tests that happen to use `pr-watchdog` fixture strings; they still prove current
input/generation publication fencing.

Also delete `ControllerTaskCreate` and `create_controller_task` only if the
interrupted implementation added those definitions.

Expected: no core API can independently create a reviewer task for a watchdog.

- [ ] **Step 3: Write failing current-authority and Boris guard tests**

Add tests using the existing `_adjudicator_lifecycle_fixture` helper. Capture a
post-claim snapshot and place its authority signature in the request:

```python
def _with_current_authority(conn, request, task_id, now=100):
    snapshot = kb.read_recovery_snapshot(conn, now=now)
    guard = replace(
        request.preconditions,
        authority_signature=kb.recovery_task_authority_signature(snapshot, task_id),
    )
    return replace(request, preconditions=guard)


def test_guarded_lifecycle_rejects_competing_claim_after_planning(conn, monkeypatch):
    _, task_id, _, request = _adjudicator_lifecycle_fixture(conn, monkeypatch)
    request = _with_current_authority(conn, request, task_id)
    lease = next(
        lease for lease in kb.read_recovery_snapshot(conn, now=100).leases
        if lease.controller_id == "adjudicator"
    )
    assert kb.publish_controller_claim(
        conn,
        kb.ControllerClaim(
            controller_id="competing-controller",
            board="default",
            expected_tenant="alpha",
            task_id=task_id,
            generation_signature=request.preconditions.generation_signature,
            authority="mutate",
            input_signature=request.preconditions.input_signature,
            lease_epoch=lease.holder_epoch,
            expires_at=lease.expires_at,
            operation_id="competing-claim-after-plan",
        ),
    ).outcome == "committed"

    result = kb.apply_controller_lifecycle_mutation(conn, request)

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, task_id).status == "blocked"


def test_guarded_lifecycle_rejects_boris_creator_even_with_exact_claim(conn, monkeypatch):
    _, task_id, _, request = _adjudicator_lifecycle_fixture(conn, monkeypatch)
    conn.execute(
        "UPDATE tasks SET created_by = ? WHERE id = ?",
        ("boris-slack-intake", task_id),
    )
    request = _with_current_authority(conn, request, task_id)

    result = kb.apply_controller_lifecycle_mutation(conn, request)

    assert result.outcome == "precondition_changed"
    assert result.reason == "task is excluded from controller automation"
    assert kb.get_task(conn, task_id).status == "blocked"
```

Add a signature test proving a second current claim changes the task-scoped
authority signature, while an unrelated task comment does not:

```python
def test_task_authority_signature_tracks_only_relevant_authority(conn, monkeypatch):
    _, task_id, _, request = _adjudicator_lifecycle_fixture(conn, monkeypatch)
    before = kb.read_recovery_snapshot(conn, now=100)
    first = kb.recovery_task_authority_signature(before, task_id)
    other = kb.create_task(conn, title="unrelated", tenant="alpha")
    kb.add_comment(conn, other, "operator", "unrelated evidence")
    unchanged = kb.read_recovery_snapshot(conn, now=100)
    assert kb.recovery_task_authority_signature(unchanged, task_id) == first
```

- [ ] **Step 4: Run the focused tests to verify RED**

```bash
verify_home=$(mktemp -d -t hermes-task13-authority-red.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts:$PWD" \
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_recovery_core.py \
  tests/hermes_cli/test_kanban_cli.py -q --file-retries 0
```

Expected: the new tests fail because the authority signature field/helper,
exact-one-current-claim check, CLI option, and creator guard are absent. Remove
the temporary directory after the command exits.

- [ ] **Step 5: Implement the task-scoped authority signature**

Add the field to the retained preconditions type:

```python
@dataclass(frozen=True)
class ControllerAuthorityPreconditions:
    controller_id: str
    board: str
    expected_tenant: str
    task_id: str
    generation_signature: str
    input_signature: str
    authority_signature: str
    claim_operation_id: str
    claim_expires_at: int
    lease_holder_token: str
    lease_epoch: int
    lease_issued_at: int
    lease_expires_at: int
    lease_census_signature: str
    lease_operation_id: str
```

Compute a canonical, task-scoped signature from the already typed snapshot.
Only claims unexpired at `snapshot.observed_at` are current; include every
current task claim and the leases for their controller ids:

```python
def recovery_task_authority_signature(snapshot: RecoverySnapshot, task_id: str) -> str:
    task = _recovery_snapshot_task(snapshot, task_id)
    tenant = str(task.get("tenant") or "").strip()
    now = snapshot.observed_at
    claims = tuple(sorted(
        (
            claim for claim in snapshot.claims
            if claim.board == snapshot.board
            and claim.tenant == tenant
            and claim.task_id == task_id
            and claim.expires_at > now
        ),
        key=lambda claim: (
            claim.controller_id, claim.generation_signature,
            claim.authority, claim.operation_id,
        ),
    ))
    controller_ids = {claim.controller_id for claim in claims}
    leases = tuple(sorted(
        (
            lease for lease in snapshot.leases
            if lease.board == snapshot.board
            and lease.controller_id in controller_ids
        ),
        key=lambda lease: (lease.controller_id, lease.holder_epoch),
    ))
    return canonical_payload_signature({
        "schema_version": 1,
        "board": snapshot.board,
        "tenant": tenant,
        "task_id": task_id,
        "created_by": task.get("created_by"),
        "input_signature": recovery_task_input_signature(snapshot, task_id),
        "generation_signature": recovery_task_generation_signature(snapshot, task_id),
        "claims": [asdict(claim) for claim in claims],
        "leases": [asdict(lease) for lease in leases],
    })
```

- [ ] **Step 6: Enforce the final transaction fence**

Inside the existing `write_txn(conn)` in
`apply_controller_lifecycle_mutation`, capture `_controller_epoch_now()` once,
read the recovery snapshot inside that transaction, and enforce this order
before any run/task/event write:

```python
if task["created_by"] == "boris-slack-intake":
    return _guarded_result(
        "precondition_changed",
        task_id=task_id,
        operation_id=operation_id,
        reason="task is excluded from controller automation",
    )

current_authority = recovery_task_authority_signature(snapshot, task_id)
if current_authority != guard.authority_signature:
    return _guarded_result(
        "precondition_changed",
        task_id=task_id,
        operation_id=operation_id,
        reason="task authority changed",
    )

current_mutate_claims = tuple(
    claim for claim in snapshot.claims
    if claim.board == board
    and claim.tenant == tenant
    and claim.task_id == task_id
    and claim.authority == "mutate"
    and claim.expires_at > now
)
if len(current_mutate_claims) != 1:
    return _guarded_result(
        "precondition_changed",
        task_id=task_id,
        operation_id=operation_id,
        reason="task does not have exactly one current mutate claim",
    )
```

Then require that sole claim to match every existing expected claim field and
require the exact lease as the interrupted implementation already does. Select
`created_by` with tenant/status/current-run in the task query. Keep operation-id
replay inside the same transaction and before lifecycle mutation.

- [ ] **Step 7: Finish the CLI contract**

Add required `--authority-signature` parsing to `controller-claim mutate`, pass
it into `ControllerAuthorityPreconditions`, and keep `--json` required:

```python
claim_mutate.add_argument("--authority-signature", required=True)

# in _cmd_controller_claim
authority_signature=args.authority_signature,
```

Add CLI tests for missing/malformed signature, a competing claim inserted after
planning, Boris creator exclusion, and exact replay. Parse the JSON and assert
`outcome`, `reason`, `task_id`, and `operation_id`; do not assert formatting.

- [ ] **Step 8: Run GREEN and commit the reconciled core boundary**

```bash
verify_home=$(mktemp -d -t hermes-task13-authority-green.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts:$PWD" \
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_recovery_core.py \
  tests/hermes_cli/test_kanban_cli.py \
  tests/hermes_cli/test_kanban_db_init.py -q --file-retries 0
git diff --check
git add hermes_cli/kanban_db.py hermes_cli/kanban.py \
  tests/hermes_cli/test_kanban_recovery_core.py \
  tests/hermes_cli/test_kanban_cli.py
git commit -m "feat(kanban): fence controller lifecycle authority"
```

Expected: all focused tests pass with no retry summary; the commit contains the
preserved tenant-claim work and final-lifecycle implementation, but no
watchdog-create surface and no Boris file.

### Task 2: Add the atomic managed-review target producer

**Files:**

- Modify: `hermes_cli/kanban_db.py`
- Modify: `hermes_cli/kanban.py`
- Modify: `tools/kanban_tools.py`
- Modify: `tests/hermes_cli/test_kanban_recovery_core.py`
- Modify: `tests/hermes_cli/test_kanban_cli.py`
- Modify: `tests/tools/test_kanban_tools.py`

**Interfaces:**

- Produces:
  `block_managed_review(conn, task_id, *, board, expected_tenant,
  expected_status, expected_run_id, review_target, operation_id,
  reason=None) -> GuardedMutationResult`.
- CLI produces: `hermes kanban --board <board> block-managed-review <task_id>
  --expected-tenant <tenant> --expected-status <running|ready>
  [--expected-run-id <id>] --review-target <json> --operation-id <id>
  [--reason <text>] --json`.
- Existing model tool consumes optional
  `kanban_block(review_target: object, kind="review")`.
- Persists event envelope:
  `{"schema_version":1,"reason":...,"kind":"review","recurrences":...,
  "review_target":{...}}`.
- Leaves `block_task()` and `hermes kanban block --kind review` targetless.

- [ ] **Step 1: Write failing atomic producer tests**

Add a running-task fixture and test the exact event:

```python
def test_managed_review_block_commits_one_typed_target(conn):
    task_id = kb.create_task(conn, title="implementation", tenant="alpha")
    assert kb.claim_task(conn, task_id) is not None
    run = kb.latest_run(conn, task_id)
    target = {
        "kind": "pull_request",
        "url": "https://github.com/Ylopo/forge/pull/123",
    }

    result = kb.block_managed_review(
        conn,
        task_id,
        board="default",
        expected_tenant="alpha",
        expected_status="running",
        expected_run_id=run.id,
        review_target=target,
        operation_id="managed-review-block-1",
        reason="implementation complete; review required",
    )

    assert result.outcome == "committed"
    assert kb.get_task(conn, task_id).status == "blocked"
    event = kb.latest_event(conn, task_id)
    assert event.kind == "blocked"
    assert event.payload == {
        "schema_version": 1,
        "reason": "implementation complete; review required",
        "kind": "review",
        "recurrences": 1,
        "review_target": target,
        "operation_id": "managed-review-block-1",
        "operation_signature": event.payload["operation_signature"],
    }
```

Add parameterized rejection coverage. Take task/run/event counts before the
call and assert all counts and lifecycle fields are unchanged after rejection:

```python
@pytest.mark.parametrize("target", [
    {"kind": "pull_request", "url": "http://github.com/Ylopo/forge/pull/123"},
    {"kind": "pull_request", "url": "https://github.com/Ylopo/forge/pull/123", "head": "a" * 40},
    {"kind": "git", "repository": "relative/repo"},
    {"kind": "artifact", "identifier": "build", "path": "/tmp/x", "sha256": "BAD"},
    {"kind": "unknown", "url": "https://github.com/Ylopo/forge/pull/123"},
])
def test_managed_review_block_rejects_malformed_target_without_partial_write(
    conn, target,
):
    task_id, run_id, before = _running_task_state(conn, tenant="alpha")
    with pytest.raises(ValueError):
        kb.block_managed_review(
            conn, task_id, board="default", expected_tenant="alpha",
            expected_status="running", expected_run_id=run_id,
            review_target=target, operation_id="invalid-managed-review",
        )
    assert _task_run_event_state(conn, task_id) == before
```

Add stale board, tenant, status, run, Boris creator, exact replay, and
operation-id/different-target conflict tests. Add this legacy assertion:

```python
def test_generic_review_block_remains_targetless(conn):
    task_id = kb.create_task(conn, title="legacy handoff", tenant="alpha")
    assert kb.block_task(conn, task_id, kind="review", reason="PR is in the body")
    event = kb.latest_event(conn, task_id)
    assert event.payload["kind"] == "review"
    assert "review_target" not in event.payload
```

- [ ] **Step 2: Write failing CLI and existing-tool tests**

Add CLI coverage that passes an exact JSON target and asserts the event through
the real board connection. Add malformed JSON, wrong kind, missing operation
id, stale tenant, and bulk-inapplicability cases.

Extend the existing tool test without introducing a new registry name:

```python
def test_block_with_review_target_uses_managed_transition(worker_env):
    from tools import kanban_tools as kt
    result = json.loads(kt._handle_block({
        "kind": "review",
        "reason": "ready for PR review",
        "review_target": {
            "kind": "pull_request",
            "url": "https://github.com/Ylopo/forge/pull/123",
        },
    }))
    assert result["success"] is True
    assert result["status"] == "blocked"
    assert result["block_kind"] == "review"
    assert result["managed_review"] is True


def test_block_rejects_review_target_for_non_review_kind(worker_env):
    from tools import kanban_tools as kt
    result = json.loads(kt._handle_block({
        "kind": "transient",
        "reason": "network error",
        "review_target": {
            "kind": "pull_request",
            "url": "https://github.com/Ylopo/forge/pull/123",
        },
    }))
    assert result["success"] is False
```

Retain the existing expected tool-name sets verbatim. The test must prove no
new model tool was registered.

- [ ] **Step 3: Run the producer tests to verify RED**

```bash
verify_home=$(mktemp -d -t hermes-task13-target-red.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD" scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_recovery_core.py \
  tests/hermes_cli/test_kanban_cli.py \
  tests/tools/test_kanban_tools.py -q --file-retries 0
```

Expected: new tests fail because the guarded producer, CLI command, and
optional tool field do not exist. Remove the temporary directory afterward.

- [ ] **Step 4: Implement closed descriptor validation**

Add a pure core validator; do not import the ignored staged router into core:

```python
def normalize_review_target(value: Mapping[str, object]) -> dict[str, object]:
    if type(value) is not dict or type(value.get("kind")) is not str:
        raise ValueError("review_target must be an exact object with a kind")
    kind = value["kind"]
    expected = {
        "pull_request": {"kind", "url"},
        "git": {"kind", "repository"},
        "artifact": {"kind", "identifier", "path", "sha256"},
    }.get(kind)
    if expected is None or set(value) != expected:
        raise ValueError("review_target has an unsupported or open schema")
    if kind == "pull_request":
        return {"kind": kind, "url": _canonical_github_pull_url(value["url"])}
    if kind == "git":
        return {"kind": kind, "repository": _canonical_absolute_path(value["repository"])}
    return {
        "kind": kind,
        "identifier": _bounded_review_identifier(value["identifier"]),
        "path": _canonical_absolute_path(value["path"]),
        "sha256": _lower_sha256(value["sha256"]),
    }
```

Use `urllib.parse.urlsplit`, existing control-character checks, `os.path`, and
bounded regexes. Require GitHub HTTPS URLs with no credentials, query, fragment,
or trailing components; require exact real absolute paths; require lowercase
64-hex artifact hashes. Validation performs no network access.

- [ ] **Step 5: Implement the one-transaction managed transition**

Validate all caller inputs before opening `write_txn`. Inside it, perform
operation replay/conflict validation, select the exact task row, reject
`created_by=boris-slack-intake`, and compare tenant/status/current-run. Reuse
existing run-ending and lifecycle-hook semantics, but append the typed event in
the transaction:

```python
payload = {
    "schema_version": 1,
    "reason": reason,
    "kind": "review",
    "recurrences": recurrences,
    "review_target": normalized_target,
    "operation_id": operation_id,
    "operation_signature": operation_signature,
}
event_id = _append_event(conn, task_id, "blocked", payload, run_id=run_id)
```

The operation signature binds board, tenant, task, expected status/run, reason,
and the normalized descriptor. Return `precondition_changed` for stale task
state, `conflict` for operation-id reuse with different content, and the exact
prior `committed` result for replay. Never call `block_task()` and then patch
the event; that would create a partial-write interval.

- [ ] **Step 6: Add the CLI and extend the existing tool**

Register `block-managed-review` in the existing Kanban argparse tree. Parse
`--review-target` with `json.loads`, require an exact object, and print
`asdict(result)` only under required `--json`.

Extend `KANBAN_BLOCK_SCHEMA.kind` with `review` and add a closed optional
`review_target` schema using `oneOf`. In `_handle_block`:

```python
review_target = args.get("review_target")
if review_target is not None and kind != "review":
    return tool_error("review_target requires kind='review'")
if review_target is None:
    ok = kb.block_task(
        conn, tid, reason=reason, kind=kind,
        expected_run_id=_worker_run_id(tid),
    )
else:
    task = kb.get_task(conn, tid)
    run_id = _worker_run_id(tid)
    operation_id = "managed-review-block:" + kb.canonical_payload_signature({
        "schema_version": 1,
        "board": kb.get_current_board(),
        "tenant": task.tenant,
        "task_id": tid,
        "expected_status": task.status,
        "expected_run_id": run_id,
        "review_target": review_target,
        "reason": reason,
    })
    result = kb.block_managed_review(
        conn, tid, board=kb.get_current_board(),
        expected_tenant=task.tenant, expected_status=task.status,
        expected_run_id=run_id, review_target=review_target,
        operation_id=operation_id, reason=reason,
    )
    ok = result.outcome == "committed"
```

Return `managed_review=True` only for the structured branch. Keep the existing
worker-task ownership gate, goal-mode gate, redaction, board routing, and
generic block behavior unchanged.

- [ ] **Step 7: Run GREEN and commit the producer**

```bash
verify_home=$(mktemp -d -t hermes-task13-target-green.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD" scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_recovery_core.py \
  tests/hermes_cli/test_kanban_cli.py \
  tests/tools/test_kanban_tools.py -q --file-retries 0
git diff --check
git add hermes_cli/kanban_db.py hermes_cli/kanban.py tools/kanban_tools.py \
  tests/hermes_cli/test_kanban_recovery_core.py \
  tests/hermes_cli/test_kanban_cli.py tests/tools/test_kanban_tools.py
git commit -m "feat(kanban): persist managed review targets"
```

Expected: focused tests pass; ordinary targetless blocks still behave exactly
as before; the existing tool name set is unchanged.

### Task 3: Remove candidate transport and route from the latest structured event

**Files:**

- Modify staged `review_delegator/models.py`
- Modify staged `review_delegator/state_store.py`
- Modify staged `review_delegator/router.py`
- Modify staged `review_delegator/runner.py`
- Modify staged `review_delegator/tests/test_models.py`
- Modify staged `review_delegator/tests/test_state_store.py`
- Modify staged `review_delegator/tests/test_generation_router.py`
- Modify staged `review_delegator/tests/test_router.py`

**Interfaces:**

- Removes: `GenerationCandidate`, `CandidateValidation`,
  `CandidatePublishResult`, `generation_candidate_key`,
  `RouterState.generation_candidates`,
  `StateStore.publish_generation_candidate`,
  `validate_generation_candidate`, and `remove_generation_candidate`.
- Restores: checksummed router state schema version `1`, with the exact state
  keys `schema_version`, `revision`, `last_observed_at`, and
  `review_handoffs`.
- Consumes: exactly `latest_block_event.payload["review_target"]`.
- Does not consume: task fields, nested `block_metadata`, older events,
  watchdog state, Slack data, prose, or any candidate sidecar.

- [ ] **Step 1: Replace candidate tests with direct structured-target tests**

Delete only candidate-transport test helpers and assertions from the four
router test files. Add tests that construct a fresh `RecoveryTaskSnapshot`
whose latest block event contains the producer envelope from Task 2:

```python
def test_latest_top_level_review_target_routes_without_candidate_state(monkeypatch):
    row = _row(latest_block_payload={
        "schema_version": 1,
        "reason": "ready for review",
        "kind": "review",
        "recurrences": 1,
        "review_target": {
            "kind": "pull_request",
            "url": "https://github.com/Ylopo/forge/pull/123",
        },
        "operation_id": "managed-review-block-1",
        "operation_signature": "a" * 64,
    })
    monkeypatch.setattr(T, "_resolve_pull_request", _resolved_head("b" * 40))

    resolution = RUN._resolve_routing_target(row)

    assert resolution.outcome == "resolved"
    assert resolution.target == ReviewTarget(
        "pull_request", "https://github.com/Ylopo/forge/pull/123", "b" * 40,
    )
```

Add fail-closed cases for:

- no top-level `review_target`;
- a target only in `row.task`;
- a target only under `payload.block_metadata`;
- an open or malformed descriptor;
- an older valid block event followed by a latest targetless review block;
- provider unavailable; and
- a task whose reason/body/title contains a valid PR URL but whose latest
  event has no structured target.

Each case asserts no `RoutedMutation`, no created lane, no lifecycle call, and
no fallback provider call when the descriptor itself is absent or malformed.

- [ ] **Step 2: Write the schema-closure tests**

Update model and store tests to construct `RouterState` without a candidate
mapping. Assert the encoded state is exactly schema version 1:

```python
assert set(envelope) == {"schema_version", "revision", "checksum", "state"}
assert envelope["schema_version"] == 1
assert set(envelope["state"]) == {
    "schema_version", "revision", "last_observed_at", "review_handoffs",
}
```

Add rejection tests for a schema-2 candidate envelope and for a schema-1 state
containing `generation_candidates`. This implementation has no live state
migration: incompatible installed state is handled only by the separately
guarded retirement/install rollout after the deprecated CRONs are quiescent.

- [ ] **Step 3: Run the focused tests to verify RED**

```bash
verify_home=$(mktemp -d -t hermes-task13-direct-target-red.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts:$PWD" \
scripts/run_tests.sh \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator/tests/test_models.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator/tests/test_state_store.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator/tests/test_generation_router.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator/tests/test_router.py" \
  -q --file-retries 0
```

Expected: failures identify the schema-2 candidate mapping, nested metadata
fallback, task-field fallback, and candidate selection/removal paths.

- [ ] **Step 4: Restore the closed schema-v1 state model**

Delete the candidate dataclasses, candidate signature helper, publish result,
and validation result from `models.py`. Retain only these `RouterState` fields
and the existing immutable, alias-free handoff validation:

```python
@dataclass(frozen=True)
class RouterState:
    schema_version: Literal[1]
    revision: int
    last_observed_at: int
    review_handoffs: Mapping[str, ReviewHandoff] = field(default_factory=dict)
```

Set `SCHEMA_VERSION = 1` in `state_store.py`. Remove candidate imports,
decode/encode/domain validation, publication/CAS, compaction retention, merge,
and reconstruction fields. `_decode_envelope` must accept only version 1 and
the exact version-1 state keys; it must not silently discard candidate state.

- [ ] **Step 5: Remove candidate routing and bind the latest block event only**

Delete candidate imports and the two candidate helpers from `router.py`. In
`runner.py`, make the target handoff closed and top-level only:

```python
def _review_target_handoff(row: O.RecoveryTaskSnapshot) -> dict[str, object]:
    payload = row.latest_block_event.payload if row.latest_block_event else None
    if not isinstance(payload, Mapping):
        return {}
    target = payload.get("review_target")
    return {"review_target": dict(target)} if isinstance(target, Mapping) else {}


def _resolve_routing_target(
    row: O.RecoveryTaskSnapshot,
) -> T.TargetResolution:
    if type(row) is not O.RecoveryTaskSnapshot:
        raise TypeError("routing target resolution requires a typed row")
    # The empty task object makes fallback to task fields impossible.
    return T.resolve_target({}, _review_target_handoff(row))
```

Update call sites to carry only `TargetResolution`. Remove candidate cleanup
on ownership release and lane creation, remove candidates from state merge,
and remove candidate counts from typed routing diagnostics. Do not edit or run
the deprecated requeue-watchdog implementation or test.

- [ ] **Step 6: Run GREEN and prove the candidate surface is gone**

```bash
verify_home=$(mktemp -d -t hermes-task13-direct-target-green.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts:$PWD" \
scripts/run_tests.sh \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator/tests" \
  -q --file-retries 0
! rg -uu -n \
  'GenerationCandidate|CandidateValidation|CandidatePublishResult|generation_candidates|generation_candidate_key|publish_generation_candidate|candidate_signature' \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator"
```

Expected: all router tests pass and `rg` returns no source or test match. This
staged-only task is not committed; record its test result in the Task 13 report.

### Task 4: Enforce Boris exclusion before every ownership and remediation path

**Files:**

- Modify staged `review_delegator/ownership.py`
- Modify staged `review_delegator/runner.py`
- Modify staged `kanban_remediation/kanban_engine.py`
- Modify staged `kanban_remediation/reconcile.py`
- Modify staged `kanban_remediation/spine.py`
- Modify staged `kanban_remediation/adjudicator_runner.py`
- Modify staged `review_delegator/tests/test_ownership.py`
- Modify staged `review_delegator/tests/test_generation_router.py`
- Modify staged `review_delegator/tests/test_router.py`
- Modify staged `kanban_remediation/tests/test_kanban_engine.py`
- Modify staged `kanban_remediation/tests/test_reconcile.py`
- Modify staged `kanban_remediation/tests/test_spine.py`
- Modify staged `kanban_remediation/tests/test_spine_exclusion.py`
- Modify staged `kanban_remediation/tests/test_adjudicator_runner.py`

**Interfaces:**

- Produces: `BORIS_CREATED_BY = "boris-slack-intake"`.
- Produces:
  `is_automation_excluded(task: Mapping[str, object]) -> bool`.
- Adds ownership source and disposition: `outside_automation`.
- Adds: `RecoveryTaskSnapshot.authority_signature` from the core task-scoped
  projection.
- Adds: `OwnershipCensus.excluded_blocked_count` without treating exclusion as
  unowned, multiply owned, or unhealthy.

- [ ] **Step 1: Write exclusion-first ownership tests**

Add one parameterized test that overlays each otherwise-positive predicate on
a task with the exact Boris creator:

```python
@pytest.mark.parametrize("predicate", [
    "explicit_claim", "review_handoff", "transient", "deterministic_action",
    "adjudication_packet", "legacy_review", "dependency",
])
def test_boris_creator_is_outside_automation_before_positive_predicate(predicate):
    row = _row_for(predicate, created_by="boris-slack-intake")

    decision = O.resolve_owner(row, now=100)

    assert decision.owner == "outside_automation"
    assert decision.authority == "escalate_only"
    assert decision.source == "outside_automation"
    assert decision.reason == "boris_slack_intake_excluded"
```

Include forged current claims, a fresh router lease, and an invariant violation
in the matrix so exclusion demonstrably precedes claim and quarantine logic.
Add a near-match control (`"boris-slack-intake "`, different case, or another
creator) proving only the exact marker excludes.

Add census coverage with one ordinary owned block and one Boris block:

```python
assert census.blocked_count == 2
assert census.excluded_blocked_count == 1
assert census.singly_owned_blocked_count == 1
assert census.unowned_blocked_count == 0
assert census.multiply_owned_blocked_count == 0
assert census.healthy is True
```

Assert `project_task(...).authority_signature` exactly equals
`kb.recovery_task_authority_signature(core_snapshot, task_id)`.

- [ ] **Step 2: Write end-to-end nonmutation tests for blocked and failed Boris rows**

Across the runner, engine, reconcile seam, spine, and adjudicator tests, create
both `status="blocked"` and failed/terminal Boris fixtures. Assert:

- engine actions, safe buckets, decision packets, transient actions, and
  parent routing contain no Boris task id;
- reconcile signature refresh cannot reintroduce an excluded task;
- runner typed review, transient self-heal, dependency auto-unblock, legacy
  review, and triage paths perform zero Kanban lifecycle or creation calls;
- spine ignores a forged queued packet and a pre-existing verdict for a Boris
  row, even if both are otherwise scoped and claim-bearing;
- adjudicator rejects before the agent call, lease acquisition, claim
  publication, or verdict publication; and
- changing the creator to Boris between the pre-agent and post-agent snapshots
  prevents claim publication.

Count test doubles for every write boundary, not only the final summary.

- [ ] **Step 3: Run the focused tests to verify RED**

```bash
verify_home=$(mktemp -d -t hermes-task13-boris-exclusion-red.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts:$PWD" \
scripts/run_tests.sh \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator/tests/test_ownership.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator/tests/test_generation_router.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator/tests/test_router.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests/test_kanban_engine.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests/test_reconcile.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests/test_spine.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests/test_spine_exclusion.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests/test_adjudicator_runner.py" \
  -q --file-retries 0
```

Expected: the new tests expose late filtering, census-health distortion, and
entry points that currently bypass the shared ownership resolver.

- [ ] **Step 4: Implement the shared exact exclusion and authority projection**

In `ownership.py`:

```python
BORIS_CREATED_BY = "boris-slack-intake"


def is_automation_excluded(task: Mapping[str, object]) -> bool:
    return isinstance(task, Mapping) and task.get("created_by") == BORIS_CREATED_BY
```

Add `outside_automation` to `DecisionSource`. At the very start of
`resolve_owner`, before invariant, explicit-claim, review, transient, actuator,
adjudicator, or legacy predicates, return:

```python
if is_automation_excluded(row.task):
    return _decision(
        row, "outside_automation", "escalate_only",
        "boris_slack_intake_excluded", "outside_automation",
    )
```

Set `RecoveryTaskSnapshot.authority_signature` during `project_task` by calling
the core helper on the same immutable snapshot. Add `excluded_blocked_count`
to the census signature and counts. Excluded rows remain visible, but subtract
them before calculating singly/unowned/multiply-owned counts and do not make
`healthy` false.

- [ ] **Step 5: Put the same gate at every staged ingress**

Use only the shared helper/decision; do not add creator aliases or heuristics.

- In `runner.py`, skip excluded rows before typed routing and before the legacy
  review/transient/dependency loop calls any classifier or provider.
- In `kanban_engine.py`, derive excluded ids from exact task rows and remove
  them before forming actions, systemic failure groups, buckets, or decision
  packets. Include `created_by` in the read-only census projection.
- In `reconcile.py`, make `_exclude_owned`, `gather`, `_live_signatures`, and
  census preserve `outside_automation` as nonactionable even though its
  authority is `escalate_only`.
- In `spine.py`, filter `_list_blocked`, planned safe actions, decision packets,
  transient sweep input, queued packets, and existing verdicts before facts,
  planning, or CLI execution.
- In `adjudicator_runner.py`, check both pre-agent and post-agent projected
  tasks. Drop the local pending packet and count it as excluded; do not invoke
  the agent, acquire a lease, publish a claim, or write an authoritative
  verdict.

These are defense-in-depth gates. The final tracked core writer from Task 1 is
still authoritative against stale planners or injected local state.

- [ ] **Step 6: Run GREEN and record the exclusion matrix**

Rerun the Step 3 command. Then run the complete staged router/remediation
suites, still excluding the deprecated watchdog test:

```bash
verify_home=$(mktemp -d -t hermes-task13-boris-exclusion-green.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts:$PWD" \
scripts/run_tests.sh \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator/tests" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests" \
  -q --file-retries 0
```

Expected: all staged tests pass; the Task 13 report contains a table showing
zero calls at every Boris-facing mutation boundary. No Boris source or test is
edited.

### Task 5: Finish adjudicated execution and durable unknown-outcome reconciliation

**Files:**

- Modify staged `review_delegator/ownership.py`
- Modify staged `kanban_remediation/state.py`
- Modify staged `kanban_remediation/spine.py`
- Modify staged `kanban_remediation/reconcile.py`
- Modify staged `kanban_remediation/tests/test_state.py`
- Modify staged `kanban_remediation/tests/test_spine.py`
- Modify staged `kanban_remediation/tests/test_reconcile.py`
- Modify staged `kanban_remediation/tests/test_reconcile_seam.py`

**Interfaces:**

- Adds bounded `RemediationState.lifecycle_mutation_intents` records containing
  the exact serialized `ControllerLifecycleMutation` and packet signature.
- Adds `--authority-signature` to every adjudicated lifecycle CLI vector.
- Adds the existing runtime source `adjudicator_claim` to the closed
  `DecisionSource` literal and removes its type-ignore escape hatch.
- First attempt requires a still-live packet signature; later reconciliation
  replays only the exact idempotent operation and relies on core replay and
  current atomic guards rather than requiring the now-completed packet to
  remain in the engine feed.
- An unresolved lifecycle intent halts all later remediation work.

- [ ] **Step 1: Write failing state-journal tests**

Mirror the existing claim-intent tests for lifecycle mutations. Require:

- exact board/tenant/task, verb, operation id, input/generation/authority
  signatures, claim fields, and lease fields;
- idempotent record of the same operation;
- conflict on any attempted operation-id rebind;
- `pending -> unknown -> committed|superseded_external|conflict` transitions;
- at most one retry count increment;
- deterministic oldest-resolved eviction at 256 records; and
- refusal to evict or overwrite unresolved records when the bounded registry
  is full.

The record must be sufficient to reconstruct the exact dataclasses after a
process restart; do not persist an opaque in-memory argv only.

- [ ] **Step 2: Write authority-vector and lost-ack tests**

Add a reconcile seam assertion:

```python
assert [
    "--authority-signature", mutation.preconditions.authority_signature,
] == argv[argv.index("--authority-signature"):argv.index("--authority-signature") + 2]
```

Add a two-tick spine fixture. On the first LIVE tick, the fake CLI executes the
real core mutation and commits `controller_lifecycle_mutated`, but simulates a
lost acknowledgement. Assert the spine:

- saved a durable `unknown` lifecycle intent before returning;
- retained the packet/verdict binding;
- halted the batch; and
- did not execute a second planned item.

On the second tick, assert it reconstructs and invokes the identical operation
before gathering or planning later work. Core replay returns the original
event id. Final assertions:

```python
assert lifecycle_event_count(operation_id) == 1
assert task_mutation_count(task_id) == 1
assert invoked_operation_ids == [operation_id, operation_id]
assert state.unresolved_lifecycle_mutation_intents() == []
assert later_operation_calls == []  # reconciliation tick performs no later work
```

Add unknown-again, conflict, manual lifecycle change, authority change, lease
expiry/takeover, and Boris-creator-change cases. All halt or resolve
nonmutating; none derive a new operation id.

- [ ] **Step 3: Run the focused tests to verify RED**

```bash
verify_home=$(mktemp -d -t hermes-task13-lifecycle-journal-red.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts:$PWD" \
scripts/run_tests.sh \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests/test_state.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests/test_spine.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests/test_reconcile.py" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests/test_reconcile_seam.py" \
  -q --file-retries 0
```

Expected: tests fail because lifecycle intents are currently memory-only and
the CLI vector does not yet carry current task-scoped authority.

- [ ] **Step 4: Add the authority signature to spine planning and execution**

In `consume_verdict`, use the post-claim `RecoveryTaskSnapshot` produced from
the same fresh core snapshot and set:

```python
preconditions = kb.ControllerAuthorityPreconditions(
    controller_id=claim.controller_id,
    board=claim.board,
    expected_tenant=claim.tenant,
    task_id=claim.task_id,
    generation_signature=claim.generation_signature,
    input_signature=claim.input_signature,
    authority_signature=snapshot.authority_signature,
    claim_operation_id=claim.operation_id,
    claim_expires_at=claim.expires_at,
    lease_holder_token=lease.holder_token,
    lease_epoch=lease.holder_epoch,
    lease_issued_at=lease.issued_at,
    lease_expires_at=lease.expires_at,
    lease_census_signature=lease.census_signature,
    lease_operation_id=lease.operation_id,
)
```

Keep the operation id derived from `asdict(preconditions)`, so current
authority is part of the idempotency identity. In `reconcile.py`, insert
`--authority-signature` into the exact board-explicit `controller-claim mutate`
argv. Preserve exact `conflict` and `precondition_changed` results instead of
collapsing them into generic rejection.

Add `adjudicator_claim` to `ownership.DecisionSource`, remove the type ignore,
and return the plan with named fields so constructor drift cannot misbind the
decision:

```python
return ActuatorPlan(
    authority="mutate",
    mutated=True,
    reason="adjudicator_claim_current",
    decision=decision,
    preconditions=preconditions,
    operation_id=operation_id,
    verb=verb,
)
```

Do not loosen any existing packet, claim, lease, or census check.

- [ ] **Step 5: Implement the bounded lifecycle-intent journal**

In `RemediationState.__init__`, add
`self._data.setdefault("lifecycle_mutation_intents", {})`. Implement APIs
parallel to claim intents:

```python
record_lifecycle_mutation_intent(packet_signature, mutation)
resolve_lifecycle_mutation_intent(operation_id, outcome, reason="")
increment_lifecycle_mutation_retry(operation_id)
lifecycle_mutation_intent(operation_id)
unresolved_lifecycle_mutation_intents()
reconstruct_lifecycle_mutation(record) -> kb.ControllerLifecycleMutation
```

Use a separate `MAX_LIFECYCLE_MUTATION_INTENTS = 256`. Persist the complete
closed `asdict(mutation)` structure plus packet signature, outcome, retry
count, reason, and timestamps. Validate all exact strings, positive epochs,
lowercase digests, verbs, and key sets on both write and reconstruction.

- [ ] **Step 6: Journal before execution and reconcile before later work**

Refactor `spine.run` so state loads before `reconcile.gather`. If any lifecycle
intent is unresolved:

1. reconstruct the exact mutation;
2. for `pending`, perform the first invocation; for `unknown` with retry count
   zero, durably increment the counter and perform the one permitted replay;
   for `unknown` with retry count one, halt without invoking again;
3. in LIVE mode invoke the same board-explicit core CLI operation with packet
   freshness bypassed only for this reconciliation call;
4. resolve `committed`, `precondition_changed`/`superseded_external`, or exact
   `conflict` durably;
5. retain `unknown` and halt; and
6. return after reconciliation without gathering, planning, facts calls, or a
   later mutation in that tick.

The reconciliation invocation is not a new mutation path: it uses the same
operation id, verb, and complete preconditions. Core operation replay handles
commit-with-lost-acknowledgement even though the original packet disappears
after the task leaves `blocked`.

For the first planned adjudicated mutation:

```python
state.record_lifecycle_mutation_intent(item.packet_signature, mutation)
state.save()  # durable replace before subprocess
outcome = apply_adjudicated_decision(mutation, item.packet_signature, mode)
state.resolve_lifecycle_mutation_intent(
    mutation.operation_id,
    _journal_outcome(outcome),
    reason=outcome.reason,
)
state.save()
```

If the outcome is unknown, stop without popping verdict/pending state. On a
resolved result, clean up only the exact bound packet records. Never create a
replacement operation after a manual change or authority loss.

- [ ] **Step 7: Run GREEN and the full adjudicator boundary gate**

Rerun the Step 3 command, then:

```bash
verify_home=$(mktemp -d -t hermes-task13-lifecycle-journal-green.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts:$PWD" \
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_recovery_core.py \
  tests/hermes_cli/test_kanban_cli.py \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests" \
  -q --file-retries 0
git diff --check
```

Expected: the lost-ack fixture proves one event and one lifecycle change; no
unknown intent permits later work. This staged-only task is not committed.

### Task 6: Build the deployment allowlist and run protected compatibility gates

**Files:**

- Create ignored `task-13-install-allowlist.txt`
- Create ignored `task-13-protected-final.sha256`
- Update ignored `task-13-report.md`
- Update ignored `task-13-sha256.txt`
- Update ignored `task-13-staged.diff`
- Update ignored `progress.md`

**Interfaces:**

- The allowlist contains only active runtime files under `review_delegator/`
  and `kanban_remediation/`.
- It contains no test, cache, backup, state, Boris, reaction-watchdog, channel
  watcher, or blocked-review requeue-watchdog path.
- This task compiles and imports a temporary bundle. It does not install that
  bundle and does not inspect or mutate the live CRON registry or board.

- [ ] **Step 1: Create and validate the exact install allowlist**

Create the file with one sorted path per active runtime file, relative to
`staged-hermes/scripts`. Include Python modules, active shell entrypoints, and
prompt resources actually imported/read by those modules. Do not generate the
list from a broad directory glob. Its exact contents are:

```text
kanban_remediation/__init__.py
kanban_remediation/adjudicator.py
kanban_remediation/adjudicator_prompt.md
kanban_remediation/adjudicator_runner.py
kanban_remediation/adjudicator_runner.sh
kanban_remediation/facts.py
kanban_remediation/kanban_engine.py
kanban_remediation/kanban_health.py
kanban_remediation/kanban_remediation.sh
kanban_remediation/locking.py
kanban_remediation/models.py
kanban_remediation/probes.py
kanban_remediation/reconcile.py
kanban_remediation/rollup.py
kanban_remediation/rules.py
kanban_remediation/spine.py
kanban_remediation/state.py
kanban_remediation/triage_advisor.py
kanban_remediation/triage_advisor_prompt.md
kanban_remediation/triage_advisor_runner.py
kanban_remediation/triage_advisor_runner.sh
review_delegator/__init__.py
review_delegator/delegator.py
review_delegator/escalation.py
review_delegator/health.py
review_delegator/models.py
review_delegator/operation_journal.py
review_delegator/ownership.py
review_delegator/review_contract.py
review_delegator/router.py
review_delegator/runner.py
review_delegator/runner.sh
review_delegator/self_heal.py
review_delegator/state_store.py
review_delegator/targets.py
```

Validate it:

```bash
allowlist=.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/task-13-install-allowlist.txt
staged=.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts
LC_ALL=C sort -c "$allowlist"
test "$(sort -u "$allowlist" | wc -l | tr -d ' ')" = "$(wc -l < "$allowlist" | tr -d ' ')"
while IFS= read -r path; do test -f "$staged/$path" || exit 1; done < "$allowlist"
if rg -n '(^|/)(tests?|__pycache__|boris_intake)(/|$)|just_do_it|\.bak|\.json$' "$allowlist"; then exit 1; fi
```

Also assert these exact deprecated paths are absent:

```bash
! rg -n 'just_do_it_blocked_review_requeue_watchdog|test_just_do_it_blocked_review_requeue_watchdog|just_do_it_channel_watch' "$allowlist"
```

- [ ] **Step 2: Compile and import only a temporary allowlisted bundle**

```bash
allowlist=.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/task-13-install-allowlist.txt
staged=.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts
bundle=$(mktemp -d -t hermes-task13-bundle.XXXXXX)
rsync -a --files-from="$allowlist" "$staged/" "$bundle/"
python3 -m compileall -q "$bundle/review_delegator" "$bundle/kanban_remediation"
verify_home=$(mktemp -d -t hermes-task13-bundle-home.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$bundle:$PWD" python3 -c \
  'import review_delegator.runner, review_delegator.state_store, kanban_remediation.spine, kanban_remediation.adjudicator_runner'
```

Expected: compilation and imports pass with no deprecated-watchdog or Boris
file in the temporary bundle. Remove both temporary directories afterward.

- [ ] **Step 3: Run the complete core, router, and remediation gates**

```bash
verify_home=$(mktemp -d -t hermes-task13-complete-green.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts:$PWD" \
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_recovery_core.py \
  tests/hermes_cli/test_kanban_cli.py \
  tests/hermes_cli/test_kanban_db_init.py \
  tests/tools/test_kanban_tools.py \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator/tests" \
  "$PWD/.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation/tests" \
  -q --file-retries 0
```

Expected: all tests pass with no deprecated requeue-watchdog test in the
command and no file-retry summary.

- [ ] **Step 4: Run unchanged Boris and reaction-watchdog compatibility gates**

Hash first, test second, and hash again. The suites are compatibility gates,
not implementation targets:

```bash
verify_home=$(mktemp -d -t hermes-task13-protected-green.XXXXXX)
HOME="$verify_home" HERMES_HOME="$verify_home/.hermes" \
PYTHONPATH="/Users/ctao/.hermes/scripts:$PWD" \
scripts/run_tests.sh \
  /Users/ctao/.hermes/scripts/boris_intake/tests \
  /Users/ctao/.hermes/scripts/test_just_do_it_review_reaction_watchdog.py \
  -q --file-retries 0
```

Do not modify Boris or reaction code if this fails. Record the failure and
block future rollout.

Regenerate a sorted protected manifest using the same commands and path order
as Task 1, then compare it byte-for-byte with
`task-13-protected-baseline.sha256`:

```bash
protected_final=.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/task-13-protected-final.sha256
find /Users/ctao/.hermes/scripts/boris_intake -type f \
  ! -path '*/__pycache__/*' -print0 | sort -z | xargs -0 shasum -a 256 \
  > "$protected_final"
shasum -a 256 \
  /Users/ctao/.hermes/scripts/just_do_it_review_reaction_watchdog.py \
  /Users/ctao/.hermes/scripts/test_just_do_it_review_reaction_watchdog.py \
  >> "$protected_final"
cmp \
  .superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/task-13-protected-baseline.sha256 \
  "$protected_final"
```

Expected: both suites pass and `cmp` exits 0. The report records the unchanged
exact `Source Slack message: <permalink>` compatibility coverage and reaction
watchdog zero-Kanban-write coverage from the existing tests.

- [ ] **Step 5: Refresh frozen staged evidence without including deprecated or Boris files**

Regenerate `task-13-sha256.txt` from the tracked core files, their tests, all
modified staged router/remediation files, and the install allowlist. Verify
every entry with `shasum -a 256 -c` from its recorded root.

```bash
evidence_root=.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router
staged="$evidence_root/staged-hermes/scripts"
allowlist="$evidence_root/task-13-install-allowlist.txt"
manifest="$evidence_root/task-13-sha256.txt"
{
  shasum -a 256 \
    hermes_cli/kanban.py hermes_cli/kanban_db.py tools/kanban_tools.py \
    tests/hermes_cli/test_kanban_cli.py \
    tests/hermes_cli/test_kanban_recovery_core.py \
    tests/tools/test_kanban_tools.py "$allowlist" \
    "$evidence_root/task-13-protected-baseline.sha256" \
    "$evidence_root/task-13-protected-final.sha256"
  find "$staged/review_delegator/tests" \
    "$staged/kanban_remediation/tests" -type f -name '*.py' -print0 \
    | sort -z | xargs -0 shasum -a 256
  while IFS= read -r path; do shasum -a 256 "$staged/$path"; done < "$allowlist"
} > "$manifest"
shasum -a 256 -c "$manifest"
```

Regenerate `task-13-staged.diff` from the existing read-only
`source-snapshot/scripts/{review_delegator,kanban_remediation}` directories to
their staged counterparts. Do not include the top-level deprecated watcher or
its test in the revised diff. Record the diff and manifest byte sizes and
SHA-256 values in `task-13-report.md`.

```bash
evidence_root=.superpowers/sdd/2026-08-03-kanban-blocked-recovery-router
staged="$evidence_root/staged-hermes/scripts"
review_diff=$(mktemp -t hermes-task13-review.XXXXXX)
remediation_diff=$(mktemp -t hermes-task13-remediation.XXXXXX)
diff -ruN -x '__pycache__' -x '*.pyc' \
  "$evidence_root/source-snapshot/scripts/review_delegator" \
  "$staged/review_delegator" > "$review_diff"
test "$?" -le 1
diff -ruN -x '__pycache__' -x '*.pyc' \
  "$evidence_root/source-snapshot/scripts/kanban_remediation" \
  "$staged/kanban_remediation" > "$remediation_diff"
test "$?" -le 1
cp "$review_diff" "$evidence_root/task-13-staged.diff"
cat "$remediation_diff" >> "$evidence_root/task-13-staged.diff"
shasum -a 256 "$manifest" "$evidence_root/task-13-staged.diff"
wc -c "$manifest" "$evidence_root/task-13-staged.diff"
```

Append, do not erase, the report with:

- design and replacement-plan authority;
- preserved interrupted core delta;
- every RED/GREEN command and result;
- candidate-removal scan;
- Boris exclusion matrix;
- lost-ack event/mutation counts;
- allowlist validation and temporary-bundle imports;
- protected baseline/final hash equality;
- explicit absence of live rollout; and
- the existing deployed `task_links.relation_type` prerequisite concern.

Update `progress.md` to mark revised Task 13 staged implementation complete
and live CRON retirement/install still pending under a separate plan.

- [ ] **Step 6: Perform the final adversarial scope and quality review**

```bash
git diff --check
git status --short
! rg -uu -n \
  'GenerationCandidate|CandidateValidation|CandidatePublishResult|generation_candidates|publish_generation_candidate' \
  .superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator
! rg -uu -n \
  'boris_intake|just_do_it_blocked_review_requeue_watchdog|just_do_it_channel_watch' \
  .superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/task-13-install-allowlist.txt
! rg -uu -n -g '*.py' 'TODO|TBD|NotImplementedError' \
  hermes_cli/kanban.py hermes_cli/kanban_db.py tools/kanban_tools.py \
  tests/hermes_cli/test_kanban_cli.py \
  tests/hermes_cli/test_kanban_recovery_core.py \
  tests/tools/test_kanban_tools.py \
  .superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/review_delegator \
  .superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/staged-hermes/scripts/kanban_remediation
cmp \
  .superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/task-13-protected-baseline.sha256 \
  .superpowers/sdd/2026-08-03-kanban-blocked-recovery-router/task-13-protected-final.sha256
```

Expected: formatting is clean; the two `rg` commands have no match; protected
paths have no tracked diff; only the intended core/test files are tracked; all
`.superpowers` implementation/evidence remains ignored. Review the complete
tracked plus staged delta against every acceptance criterion in the design.
No live source installation, CRON disablement, state migration, board write,
or rollback action is part of this plan.

## Plan Self-Review Checklist

Before implementation begins:

- [x] Every design decision D1–D10 maps to at least one task or explicit
  no-change constraint.
- [x] Every on-demand Boris path and the reaction watchdog are protected by
  both path exclusion and before/after hashes.
- [x] No step imports, calls, edits, or installs the deprecated requeue
  watchdog.
- [x] The managed target has exactly one atomic producer and the router has
  exactly one structured consumer.
- [x] Boris exclusion precedes every positive ownership predicate and is
  repeated at final core mutation.
- [x] The final mutation binds input, generation, task-scoped authority,
  exactly one current claim, exact lease, Boris exclusion, and operation
  replay in one writer transaction.
- [x] Unknown lifecycle results are durable before execution and block all
  later work until the same operation reconciles.
- [x] Every test command uses an isolated home, staged-first imports where
  needed, `scripts/run_tests.sh`, and `--file-retries 0`.
- [x] The install allowlist is exact and excludes tests, deprecated sources,
  Boris, caches, backups, and state.
- [x] Placeholder scan, constructor/signature scan, `git diff --check`, and
  complete focused/full gates are present.
- [x] Live retirement remains a separately authorized, reversible rollout
  with disable-and-quiesce ordering before any incompatible install.
