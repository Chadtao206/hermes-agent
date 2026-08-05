# Task 13 Cross-Controller Transport Design

**Status:** Approved on 2026-08-05

## Context

Task 13 of the blocked-recovery router plan requires the remediation actuator,
adjudicator, and specialized PR watchdog to share the ownership and generation
contracts built by earlier tasks. Implementation exposed two interfaces that
the approved plan named but did not transport:

- the watchdog can construct an immutable `GenerationCandidate`, but the
  router has no candidate inbox;
- the adjudicator can construct a core `ControllerClaim`, but its runner has no
  explicit board/tenant snapshot boundary or fenced lease/publish path.

The design remains governed by the original architecture: board-scoped SQLite
claims and leases are ownership truth, while the checksummed shared-root router
sidecar is a recoverable cursor. Manual lifecycle changes always win, and no
live controller or rollout state changes in this task.

## Goals

- Let the specialized watchdog publish exact-head discovery for a managed
  blocked handoff without creating a reviewer lane.
- Let the router consume that discovery without granting it ownership or
  trusting stale external state.
- Let the adjudicator publish an exact-input claim under a fresh fenced lease.
- Let the remediation spine execute an adjudicated decision only while that
  adjudicator claim remains current.
- Preserve explicit board and tenant scope, idempotency, unknown-outcome
  reconciliation, and manual precedence.

## Non-Goals

- No new lifecycle owner, reviewer lane creator, retry budget, or completion
  path.
- No direct watchdog access to router-owned board mutations.
- No live CRON, config, gateway, Kanban, state, or source installation.
- No general event bus or reusable cross-controller messaging framework.
- No migration of the watchdog's unrelated independent-PR discovery state.

## Chosen Architecture

### 1. Shared candidate type

`review_delegator.models` owns the shared immutable candidate type. The
watchdog imports it rather than defining a private copy.

```python
@dataclass(frozen=True)
class GenerationCandidate:
    board: str
    tenant: str
    task_id: str
    pr_url: str
    exact_head: str
    input_signature: str
    candidate_signature: str
```

The constructor requires an exact normalized board and tenant, nonempty task
id, canonical GitHub PR URL, lowercase 40-character head SHA, and lowercase
64-character core task-input signature. `candidate_signature` is the SHA-256
of canonical JSON containing a schema tag plus every preceding field. Prose,
assignee, creator, comments, and ambient board defaults are not candidate
inputs.

### 2. Checksummed candidate inbox

`RouterState` gains a bounded `generation_candidates` mapping. Its key is a
SHA-256 of canonical `{board, tenant, task_id}` scope, so each managed task has
at most one current candidate. A new head replaces that task's prior candidate
through the existing StateStore lock, revision compare-and-swap, checksum,
atomic replace, and file/directory `fsync` protocol.

The watchdog publishes through a focused StateStore API using the candidate's
explicit board and tenant. It does not write the candidate into its private
watchdog sidecar as the router transport. Failure to acquire/commit the shared
state is nonmutating and retryable on a later watchdog tick. If a write raises
after a possible replace, the publisher reloads the checksummed state and
reconciles the exact candidate signature before deciding whether another
attempt is safe.

The router sidecar schema advances by one version. A valid prior-version state
loads with an empty candidate inbox and is rewritten at the new version on its
next successful commit. An empty inbox is not positive evidence that no work
exists: a candidate-dependent handoff waits for watchdog republication.
Reconstruction from core Kanban cannot invent an external exact head and
therefore also reconstructs an empty inbox.

Candidate retention is bounded to one record per scoped task. The router drops
the candidate after the exact candidate has been materialized into a durable
current generation, or when a fresh core snapshot proves that manual lifecycle
state removed the task from the managed review handoff. A new exact head is a
new candidate and follows the existing five-minute stability window.

### 3. Router consumption is advisory and fail-closed

Before a candidate can affect target resolution, the router compares its
board, tenant, task id, and `input_signature` with one freshly projected
`RecoveryTaskSnapshot`. It then resolves the candidate PR through the existing
bounded target provider and requires the canonical live PR identity and exact
head to match the candidate.

A missing, stale, conflicting, malformed, or provider-unavailable candidate
creates no lane and grants no authority. It produces a bounded diagnostic or
the existing target-unavailable/stabilizing state. The router never falls back
from malformed managed-candidate evidence to unconstrained prose. Candidate
presence does not change the ownership resolver: the blocked router still
needs its fresh lease, healthy census, live feature gate, allowlisted reviewer,
unchanged parent lifecycle, and available mutation budget.

The permanent generation key and guarded lane create remain the sole lane
creation boundary. A candidate race can therefore produce zero or one exact
generation lane, never one lane from the watchdog plus another from the router.

### 4. Tenant-guarded core controller claims

`ControllerClaim` and `ControllerClaimRecord` gain required tenant scope. The
claim table stores the normalized tenant so recovery snapshots and their
signatures preserve the scope that authorized the claim.

Claim publication atomically:

1. requires the explicit board-bound connection;
2. loads the exact task and compares its current normalized tenant;
3. validates the operation signature including tenant;
4. validates the controller id, holder epoch, lease expiry, claim expiry,
   generation signature, authority, and task input signature;
5. only then performs replay or appends the claim/event.

A tenant or lifecycle precondition change wins over replay. Existing claim
tables migrate by adding the tenant column; legacy empty-tenant claims never
grant authority and expire/quarantine normally.

The core CLI gains a board-explicit JSON `controller-claim publish` boundary.
It requires task id, expected tenant, controller id, generation signature,
authority, input signature, lease epoch, expiry, and operation id. The local
adjudicator uses this argument-list boundary rather than opening the board DB
for mutation itself.

### 5. Adjudicator lease and publication flow

Pending adjudication packets carry exact board, tenant, task id, and core input
signature. The spine derives those fields only from a typed shared recovery
snapshot when it queues the packet. Legacy packets missing this scope are
escalation-only and cannot be upgraded from prose.

The adjudicator runner is invoked with an explicit board. For each packet it:

1. reads one board-scoped recovery snapshot and projects the exact packet
   tenant/task;
2. rejects a stale packet before the agent call;
3. obtains the bounded verdict;
4. rereads the snapshot and requires the same tenant/task input signature;
5. acquires or renews the `adjudicator` controller lease with a process-local
   holder token and the fresh census signature;
6. publishes the tenant-guarded claim with expiry no later than the lease;
7. records the exact operation result for the spine.

Paused mode performs no work. Shadow mode computes and reports the would-claim
but does not publish a core claim, because even a claim without a lifecycle
write changes ownership. Live claim publication remains unavailable until the
separate rollout plan enables the controller.

A timeout, killed process, malformed JSON, or unexpected nonzero result is an
unknown outcome. The runner performs no later mutation for that packet until a
fresh snapshot/event read reconciles the same operation id as committed,
definitely not applied, superseded, or conflicting. Only an unchanged
precondition with no matching claim/event can permit the existing single
same-operation retry rule.

### 6. Spine execution under adjudicator authority

The deterministic actuator path remains unchanged in ownership meaning:
`spine.plan()` may authorize only a resolver decision owned by
`remediation_actuator` with `mutate` authority and deterministic-action source.

The adjudicated path is separate. `spine.consume_verdict()` may return an
executable plan only when one fresh snapshot contains exactly one matching
`adjudicator` claim and lease whose board, tenant, task, generation, input
signature, authority, and expiry cover the signed verdict. The spine is the
mechanical executor in this path; it does not relabel the decision as actuator
ownership. A stale verdict, stale/missing lease, changed task input, unsupported
verb, multiple claim, manual lifecycle change, or unhealthy census is
escalation-only.

The final lifecycle command remains operation-keyed and guarded through the
existing reconciliation boundary. Claim publication alone never unblocks,
closes, retries, completes, or reclassifies a task.

## Error and Concurrency Rules

- StateStore CAS conflict: publish nothing; reload and retry only with the same
  still-current candidate.
- Corrupt candidate/state: disable candidate-driven mutation; preserve
  last-known-good only for observation.
- Watchdog/router race: watchdog only publishes; router alone creates.
- Candidate target race: provider mismatch prevents lane creation and starts no
  approval path.
- Adjudicator packet race: post-agent snapshot mismatch prevents claim publish.
- Lease takeover/expiry: core claim publish returns a nonmutating precondition
  change.
- Multiple fresh owners or claims: ownership quarantine and nonzero health.
- Manual unblock, completion, archive, reblock, or tenant change: automation
  releases/refuses authority and never restores the old state.

## Testing Strategy

All tests run through `scripts/run_tests.sh` with staged scripts first on
`PYTHONPATH`, isolated `HOME`/`HERMES_HOME`, and explicit
`--file-retries 0`.

Required coverage includes:

- candidate canonicalization, signature binding, deep immutability, bounded
  replacement, schema migration, checksum failure, CAS conflict, and
  commit-after-error reconciliation;
- managed-watchdog publication with zero lane-create calls, plus preserved
  independent-PR creation only under an explicit fresh claim;
- router rejection of stale board, tenant, task input, PR identity, and head;
- exactly one router lane under watchdog/router and head-change races;
- tenant-atomic claim publication, replay, stale tenant, stale epoch, expiry,
  and recovery-snapshot signature coverage;
- adjudicator paused/shadow nonpublication, stale-before-agent and
  stale-after-agent rejection, lease fencing, unknown-result reconciliation,
  and exact live claim request construction;
- spine deterministic-only actuator planning and separately guarded current
  adjudicator-claim execution;
- manual lifecycle precedence and unhealthy/multiple-owner quarantine across
  every controller race fixture.

The Task 13 focused controller files run first for RED/GREEN evidence, followed
by every staged review-delegator/remediation/watchdog test and the affected core
claim/CLI suites. Task 14 remains the full isolated integration gate.

## Alternatives Considered

### New core SQLite candidate registry

This offers stronger independent durability, but candidates do not grant
ownership and are revalidated before use. A new board schema, event family,
snapshot projection, and CLI would add permanent core surface without changing
the authority model. It is deferred unless sidecar republication proves
insufficient.

### Router reads the watchdog's private sidecar

This is smaller but couples the router to a separate controller's unchecksummed
private schema and makes private discovery state look authoritative. It is
rejected.

## Rollout Boundary

Task 13 changes staged sources, tests, and the minimal tracked core claim/CLI
tenant guard required by the global contract. Controller desired modes stay
paused/shadow. Installation, CRON command changes, source copying, live board
claims, and feature-mode changes remain exclusively in the guarded rollout
plan.
