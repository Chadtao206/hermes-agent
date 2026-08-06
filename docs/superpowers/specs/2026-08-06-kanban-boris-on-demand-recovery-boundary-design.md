# Kanban / Boris On-Demand Recovery Boundary Design

**Date:** 2026-08-06
**Status:** Approved design, pending written-spec review
**Scope:** Controller ownership, Task 13 revision, and CRON deprecation boundary
**Rollout:** Documentation and staged implementation only; no live CRON, Kanban,
gateway, config, or source mutation is authorized by this design

## Authority and supersession

This document is the authority when the blocked-recovery router, Boris Slack
intake, or the just-do-it CRON suite overlap.

It supersedes only these earlier decisions:

- the portions of
  `2026-08-05-kanban-task13-cross-controller-transport-design.md` and
  `2026-08-05-kanban-task13-cross-controller-transport.md` that add a
  `GenerationCandidate`, a router candidate inbox, watchdog candidate
  publication, or independent-watchdog lane creation;
- Task 13 and Task 14 references in
  `2026-08-03-kanban-blocked-recovery-router.md` that make the specialized
  just-do-it requeue watchdog a router input or required test dependency; and
- the statement in `2026-08-05-boris-slack-remote-control-design.md` that both
  just-do-it watchdogs remain active and that the blocked-review requeue
  watchdog owns Boris follow-up behavior.

It retains:

- the rest of the Boris Slack intake design, including its authorization,
  typed command, static-review, terminal-guard, audit, and catch-up contracts;
- the per-minute reaction reconciler and the exact
  `Source Slack message: <permalink>` lane-body contract it consumes;
- the blocked-recovery router architecture, structured target resolution,
  generation stability, strict review judgement, operation journal, ownership
  census, and manual-precedence rules; and
- Task 13's tenant-scoped adjudicator claim publication, fenced lease,
  unknown-outcome reconciliation, and separate deterministic-actuator path.

The two superseded CRONs remain live until a separate guarded retirement
rollout disables them. This design records desired state; it does not itself
change runtime state.

## Problem

The original Task 13 design made the specialized just-do-it blocked-review
requeue watchdog an upstream discovery controller for the recovery router. It
published exact-head candidates and retained an independently guarded lane
creation path. That made sense while the just-do-it CRON suite was the review
intake mechanism.

Boris Slack intake now provides a safer on-demand intake path. Keeping the old
watchdog transport would create two authorities for review creation and would
couple general blocked-ticket recovery to a suite that is being retired. It
would also preserve candidate state, claim APIs, and race handling whose only
consumer is deprecated.

The desired system has three narrow responsibilities:

1. Boris intake authorizes explicit team requests and creates Boris review
   lanes.
2. The reaction watchdog projects reviewer-lane state back into Slack.
3. The blocked-recovery router advances only its own managed Kanban handoffs.

No controller may infer that it owns work merely because another controller
observed it.

## Decisions

| # | Decision |
|---|---|
| D1 | Boris intake is the exclusive authorizer and creator for team-triggered Boris review lanes. |
| D2 | The per-minute reaction watchdog is retained as a Slack reaction reconciler only. |
| D3 | `ee4582d0598f` (`just-do-it-channel-watch`) and `ff865c33c5ea` (`just-do-it-blocked-review-requeue-watchdog`) are deprecated. |
| D4 | The blocked-recovery router never consumes just-do-it candidates, sidecar state, enrollments, claims, or fallback prose. |
| D5 | The router resolves review targets from its own structured managed-handoff evidence and fails closed when that evidence is absent, ambiguous, malformed, stale, or unavailable. |
| D6 | Boris-intake lanes are outside router and remediation lifecycle ownership. Positive router ownership evidence is required; creator, assignee, title, body, permalink, or reactions never grant it. |
| D7 | Boris has no automatic requeue. A later authorized request for a new PR head is a new lane; a same-head retry requires an explicit human operator action. Merely observing a new head never creates a lane. |
| D8 | Adjudicator publication and execution keep exact input/generation fencing, and the final lifecycle write atomically rechecks the exact current claim and lease. |
| D9 | Manual task lifecycle changes always win, and an unknown mutation outcome must reconcile before any later mutation. |
| D10 | Live CRON retirement is a separate, backed-up, fingerprinted, reversible rollout. |

## Controller boundaries

| Controller | May read | May write | Must never do |
|---|---|---|---|
| Boris intake | Authorized Slack event, verified identity, canonical GitHub PR and head, Boris dedup state | One Boris reviewer lane per authorized exact head; intake audit; initial best-effort `thinkies` | Advance or clean blocked recovery tasks; publish router candidates or claims; automatically retry the same head |
| Reaction watchdog `6daa151da351` | Reviewer lanes carrying the exact source-permalink contract; Slack reaction state | Son-of-Chad-owned reaction state only | Create, requeue, archive, unblock, complete, or reclassify Kanban tasks; publish candidates or claims |
| Blocked-recovery router | Board-scoped recovery snapshot, its own checksummed handoff cursor, structured managed-review target, provider verification | Router generations and guarded lifecycle operations for positively owned managed handoffs | Read Slack, reaction state, just-do-it state, or watchdog candidates; adopt Boris lanes |
| Remediation actuator | Typed deterministic facts and the shared ownership decision | A guarded deterministic action while its authority remains current | Execute an adjudicator decision under actuator identity; infer authority from prose |
| Adjudicator | Scoped packet plus fresh board/tenant/task snapshots | One input-signed claim under a fresh fenced lease | Create reviewer lanes; mutate lifecycle merely by publishing a claim |
| Human operator | Escalations and explicit operational controls | Manual lifecycle changes, same-head Boris retry, guarded rollout actions | None imposed by automation; manual changes take precedence immediately |

The boundary is inclusion-based. A task is not router-owned unless one fresh
snapshot proves the exact structured router handoff or other approved ordered
ownership predicate. `created_by`, assignee, title, body text, Slack permalink,
and reactions are diagnostic fields, not ownership grants.

## Architecture

```text
Authorized Slack request
  -> Boris intake gate
  -> exact (repository, PR, head) dedup
  -> Boris reviewer lane with Source Slack message permalink
  -> static Boris review
  -> per-minute reaction watchdog
  -> one converged Slack reaction

Board-scoped blocked task
  -> typed recovery snapshot
  -> exactly-one-owner resolver and healthy census
  -> structured managed-review target
  -> canonical provider resolution and exact-head stability
  -> router-owned reviewer generation
  -> strict terminal review judgement
  -> guarded completion/retry/escalation

Scoped adjudication packet
  -> pre-agent snapshot check
  -> bounded adjudication
  -> post-agent snapshot check
  -> fresh fenced lease
  -> exact input/generation claim publication
  -> final atomic claim + lease + lifecycle recheck
  -> guarded lifecycle mutation or escalation
```

There is deliberately no arrow from either just-do-it watchdog into the
blocked-recovery router.

## Boris on-demand lifecycle

The Boris intake design remains the sole authority for team-triggered review.
The accepted command is normalized to validated identifiers; Slack prose is
discarded before lane rendering. The lane is pinned to the canonical PR and
exact GitHub-resolved head.

Deduplication remains keyed by `(repository, pull_request, head_sha)`:

- Repeating a request for the same head points to the existing lane and does
  not create or requeue another lane.
- An authorized later request after the PR head changes creates a distinct
  lane for the new exact head.
- Detecting a changed head in GitHub, Kanban, or a CRON is not itself a review
  request.
- If review of the same head timed out, crashed, or was otherwise
  inconclusive, an operator decides whether to requeue or create a replacement.
  No background controller spends a retry automatically.

This removes automatic Boris requeue without weakening the intake daemon's own
Socket Mode reconnect and catch-up behavior. Intake outage recovery may replay
authorized Slack events; it does not fall back to either deprecated CRON.

## Reaction projection

The retained job is:

- `6daa151da351` — `just-do-it-review-reaction-state-watchdog`
- schedule: `* * * * *`

Boris intake writes `Source Slack message: <permalink>` into the lane body and
may add the initial `thinkies` reaction on a best-effort basis. The reaction
watchdog is the authoritative convergence writer for the later state machine:

```text
active review                    -> thinkies
non-blocking review comments     -> doge_notes
blocked / not-ready verdict      -> review-no
clean exact-head approval        -> white_check_mark
```

Existing precedence, idempotent add-before-remove behavior, quarantine, and
retry semantics remain unchanged. A missing or malformed source-permalink
contract causes no Slack write for that lane. Reaction failure never blocks,
requeues, or changes a review; it is an observability/UX failure only.

The watchdog's name may remain for compatibility during this change. Its
authority is defined by behavior, not by the legacy `just-do-it` name.

## Blocked-recovery target resolution

The router already has a direct structured target path and keeps it. For a
managed review block, exactly one `review_target` descriptor must come from the
task's typed record or the latest structured block metadata in the fresh core
snapshot. The descriptor names the target kind and canonical identity; for a
pull request, the router resolves the live exact head through its bounded
provider before materializing a generation.

The router must not:

- parse a PR URL, SHA, target kind, or ownership grant from title, body,
  reason, comments, assignee, or creator text;
- synthesize a target from Slack or reaction metadata;
- wait for or read a `GenerationCandidate` inbox;
- import or call the blocked-review requeue watchdog; or
- fall back to a just-do-it sidecar when structured evidence is unavailable.

Zero structured descriptors means target unavailable. Multiple or conflicting
descriptors mean invalid input. Provider outage means temporarily unavailable.
All three outcomes are nonmutating and surface a bounded diagnostic or
escalation. They never trigger a prose fallback.

Once resolved, the existing generation signature binds board, tenant, parent
task, target kind, canonical identity, exact revision, and review-input digest.
The existing five-minute stability window, permanent generation key,
single-attempt reviewer lane, strict terminal payload, guarded retry budget,
and manual-precedence rules remain unchanged.

## Adjudicator and final-mutation safety

Removing the watchdog candidate path does not weaken the generic controller
contract.

Claim publication remains board-explicit and occurs in one write transaction.
It validates the current task tenant, input signature, generation signature,
controller identity, authority, lease epoch, claim expiry, lease expiry, and
operation identity before replay or append. A task, tenant, generation, or
lease change returns a nonmutating precondition result.

Publishing a claim grants no lifecycle side effect. When the remediation spine
executes an adjudicated decision, the final lifecycle mutation must again
validate, atomically with the task write:

- the exact board, tenant, task, input signature, and generation signature;
- the exact `mutate` claim, including controller, operation id, epoch, and
  unexpired claim deadline;
- the exact lease holder token, epoch, issue/expiry bounds, census signature,
  and lease operation id;
- the still-blocked task lifecycle and supported verb; and
- operation-id replay against the exact mutation signature.

Any mismatch is nonmutating. This closes the interval between planning and the
write: claim expiry, lease takeover, a changed block, manual unblock/archive,
or a new task generation cannot be overwritten by a verdict computed earlier.

Unknown CLI, subprocess, or commit outcomes remain journaled. The next tick
must reconcile the exact operation against fresh claim/event/postcondition
evidence before it can retry or perform later work. A matching committed event
deduplicates; definite nonapplication may permit the existing bounded
same-operation retry; conflict or ambiguity remains fail-closed.

The deterministic actuator path stays separate. It can execute only its own
current signed deterministic decision. The adjudicated path remains identified
as `adjudicator_claim`; the spine is only its mechanical executor.

## Task 13 implementation consequence

The revised Task 13 keeps the adjudicator transport and safety work, including:

- tenant-scoped `ControllerClaim` records and snapshot signing;
- atomic current-input/current-generation validation at claim publication;
- board-explicit JSON claim publication;
- scoped adjudication packets and verdicts;
- fenced lease acquisition/renewal;
- durable claim-intent reconciliation; and
- exact claim-and-lease validation at the final lifecycle write.

It removes work whose only purpose was the deprecated watchdog integration:

- `GenerationCandidate` and candidate signatures;
- `RouterState.generation_candidates` and its schema migration;
- StateStore candidate publication, replacement, retention, reconciliation,
  and removal APIs;
- router candidate selection and validation;
- managed-handoff publication from the blocked-review requeue watchdog;
- the independent-watchdog guarded-create/claim API; and
- tests and Task 13 gates dedicated to those paths.

The implementation plan must inspect the existing interrupted Task 13 changes
file by file. It must retain adjudicator/final-write fencing and excise only
the candidate/watchdog surface; it must not reset or broadly discard the dirty
worktree or staged evidence.

## CRON deprecation and rollout boundary

| Job | Desired state | Reason |
|---|---|---|
| `ee4582d0598f` — `just-do-it-channel-watch` | Deprecated; disable in guarded rollout | Boris intake is the exclusive team-triggered review intake; non-review triage is intentionally not replaced. |
| `ff865c33c5ea` — `just-do-it-blocked-review-requeue-watchdog` | Deprecated; disable in guarded rollout | Automatic Boris requeue and router candidate publication are removed. |
| `6daa151da351` — `just-do-it-review-reaction-state-watchdog` | Retain enabled | It is the compatibility projector from Boris reviewer lanes to Slack reaction state. |

The retirement rollout must:

1. verify exact job ids, names, commands, schedules, and current fingerprints;
2. back up the CRON registry and affected local sources/state;
3. prove Boris intake health and reaction-watchdog compatibility;
4. disable the two deprecated jobs without deleting them;
5. verify they remain disabled while Boris intake and reactions continue; and
6. retain a documented rollback that re-enables only the exact backed-up jobs.

No source deletion or state cleanup occurs in the initial retirement. Removal
is a later decision after the rollback window. The reaction watchdog remains
enabled throughout.

## Human intervention

Routine eligible blocked-ticket recovery remains automatic. A human is not
required when the router has one healthy owner, a fresh lease, a valid
structured target, an exact stable generation, and a valid strict outcome.

Human action is intentionally required for:

- same-head Boris retry after a failed or inconclusive lane;
- ownership quarantine, malformed/ambiguous target evidence, exhausted retry
  budget, conflicting claims, or an unreconciled unknown outcome;
- policy/taste decisions that the deterministic actuator cannot make; and
- the guarded live CRON retirement and any rollback.

Manual lifecycle edits immediately supersede automation and are never undone
by later recovery ticks.

## Failure and concurrency rules

- Duplicate Slack delivery: Boris intake deduplicates the exact head; no
  second lane and no background requeue.
- New head without a new authorized request: no lane.
- Boris intake unavailable: its own reconnect/catch-up path recovers events;
  deprecated CRONs do not become fallback creators.
- Reaction watchdog unavailable: Slack state may be stale; Kanban and GitHub
  review state are unchanged.
- Malformed permalink contract: no reaction mutation for that lane.
- Missing/conflicting router target: no lane; no prose or watchdog fallback.
- Provider outage: defer without materializing a generation.
- Boris/router coincidence on the same PR: the lanes remain independent
  because their authorization and lifecycle domains differ; neither adopts or
  mutates the other's lane.
- Claim expiry or lease takeover before final write: no lifecycle mutation.
- Manual unblock, completion, archive, reblock, or tenant change: no later
  automated restoration of the previous state.
- Unknown operation result: no later mutation until exact reconciliation.

## Testing strategy

All repository and staged automation tests run through `scripts/run_tests.sh`
with isolated `HOME`/`HERMES_HOME`, staged-first `PYTHONPATH`, and explicit
`--file-retries 0` where required.

Required behavioral coverage:

- Boris intake creates one lane for an authorized exact head, deduplicates a
  repeated same-head request, and creates a distinct lane only after both a
  head change and a later authorized request.
- A failed same-head Boris lane is not automatically requeued by intake,
  router, remediation, or the reaction watchdog.
- The Boris lane template emits exactly one parseable
  `Source Slack message: <permalink>` line.
- The reaction watchdog recognizes Boris-created lanes through that contract,
  converges every supported verdict state, and performs zero Kanban lifecycle
  or creation calls.
- Reaction write failure leaves review state untouched and preserves the prior
  reaction according to the existing add-before-remove contract.
- The router creates a managed generation from one structured target without
  any candidate inbox or watchdog call.
- Missing, duplicate, conflicting, stale, or malformed structured targets
  produce zero lane and lifecycle mutations and never fall back to prose.
- Boris lanes without positive router ownership evidence are not adopted,
  requeued, archived, completed, or used as recovery parents.
- Claim publication rejects changed tenant, input, generation, lease epoch, or
  expiry atomically, including replay after a precondition change.
- Final adjudicated lifecycle mutation rejects a changed claim, expired or
  replaced lease, changed input/generation, manual lifecycle change, unhealthy
  census, unsupported verb, and operation-id conflict.
- A commit-with-lost-acknowledgement fixture reconciles the exact operation and
  never applies it twice.
- Deterministic actuator and adjudicator ownership remain distinct under race
  and multiple-claim fixtures.

After focused tests, run the revised Task 13 gate, the Task 14 isolated
full-stack matrix, and a whole-branch adversarial review. The gates must exclude
the deprecated requeue-watchdog suite and include Boris intake plus reaction
compatibility tests. No test may read or write the live board, CRON registry,
Slack workspace, or default Hermes state.

## Out of scope

- Changing Boris intake authorization, static-review security, or GitHub
  authority.
- Replacing or renaming the reaction watchdog.
- Automatic same-head Boris retry.
- Using Slack as a source for general Kanban remediation.
- Replacing the blocked-recovery router with Boris intake.
- Live CRON disablement, source installation, state deletion, or rollback
  execution in this implementation branch.
- A generalized event bus or reusable cross-controller candidate registry.

## Acceptance criteria

The design is satisfied when:

1. team-triggered Boris review lanes can originate only from Boris intake;
2. the retained per-minute watchdog can change only Slack reaction state;
3. neither deprecated job is a runtime dependency of blocked recovery;
4. the router resolves only its own structured targets and fails closed
   without them;
5. Boris lanes remain outside router/remediation lifecycle ownership;
6. routine eligible blocked recovery is automatic, while defined escalations
   and same-head Boris retry require human action;
7. adjudicator claim publication and final lifecycle mutation are both fenced
   against current task input, generation, claim, and lease state; and
8. the two deprecated jobs can be disabled later through a reversible rollout
   while the reaction watchdog remains operational.
