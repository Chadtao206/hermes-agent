# Kanban Blocked Lifecycle Recovery Design

- **Date:** 2026-08-02
- **Author:** Chad Tao + Codex
- **Status:** Approved
- **Scope:** Restore reliable blocked-ticket advancement, remove review-handoff
  deadlocks, make ownership explicit, and add effectiveness health signals.

## 1. Problem

Hermes' cron scheduler and the deterministic remediation actuator are running,
but blocked-ticket cleanup is incomplete:

- `kanban-blocked-watch` has been paused since 2026-07-30 after repeatedly
  trying to complete a goal-mode implementation card merely to release its
  dependency-gated reviewer child. The goal judge correctly rejected that
  premature completion.
- The active remediation actuator still excludes `review-required` cards and
  delegates transient retries to the paused watcher, leaving those classes
  without an active general owner.
- Reviewer-assigned cards with a `review-required` reason are excluded by the
  actuator and skipped by the watcher.
- Custom automation still infers ownership from prose prefixes while core
  Kanban supports typed `dependency`, `needs_input`, `capability`, and
  `transient` block semantics but has no structured review-handoff kind.
- Cron health reports process completion, not whether the blocked backlog has
  an owner or is advancing.
- Existing CLI goal completion fails open when the judge is unavailable or
  raises, and ignores parse/transport failure flags. Automation therefore lacks
  a strict judge-required completion path, including for ordinary non-goal cards.
- All current blocked-card health jobs depend on the same in-process cron ticker
  they are meant to assess. The gateway declares readiness after merely starting
  that daemon thread, so ticker death can be silent and cannot self-report.
- The scheduler may be healthy while a required controller job is disabled,
  missing, duplicated, or stale. `hermes cron list` hides disabled jobs by
  default; live inspection confirmed `kanban-blocked-watch` is disabled and has
  not run since 2026-07-30 even though the ticker and other jobs are active.
- The long-lived gateway predates relevant installed Kanban changes, so the
  running dispatcher and fresh CLI subprocesses use mixed code generations.

At design time the live board has 25 blocked cards. Sixteen are untyped, and
all eight blocked cards with nonzero dispatcher failure counts are untyped.

## 2. Goals

- Give every blocked card on an explicitly managed board exactly one effective
  lifecycle owner and authority.
- Let reviewer lanes run without falsely completing their implementation card.
- Automatically submit an approved implementation for completion through the
  existing goal judge.
- Preserve human-only blocks, the goal judge, retry budgets, mutation caps,
  signature gates, and fail-closed behavior.
- Repair existing review dependency cycles without direct ad hoc board writes.
- Migrate legacy block kinds only when classification is deterministic.
- Measure remediation effectiveness rather than merely cron process health.
- Detect when installed Kanban/gateway code is newer than the running gateway.

## 3. Non-goals

- Bypassing or weakening goal completion validation.
- Blanket-unblocking `needs_input` or `capability` cards.
- Auto-closing ambiguous legacy cards.
- Replacing the existing remediation engine or adjudicator.
- Broadly redesigning Kanban dependency semantics or adding a new relationship
  type to core Hermes. A narrow guarded block-reclassification lifecycle
  operation is in scope because existing blocked cards cannot be migrated safely
  through `unblock` plus `block`.
- Turning the exact-head PR re-review watchdog into a general blocked-card
  cleaner.
- Adding a new model tool or widening the always-present tool schema; all new
  lifecycle surfaces are guarded CLI/core operations used by local automation.

## 4. Chosen Architecture

Use a two-phase, unlinked review handoff.

### Phase 1: create runnable review

When a non-reviewer implementation card is blocked with typed `review` (or a
legacy-compatible valid `review-required:` handoff):

1. Re-read the card and its latest block event.
2. Build the reviewer brief from the implementation body and handoff evidence.
3. Resolve an immutable review target and generation signature. PR-backed work
   uses the canonical PR identity and exact live head SHA; Git work without a PR
   uses repository identity and exact commit SHA; deployment/configuration work
   uses an immutable evidence or artifact identifier plus digest. If no stable
   target can be established, do not create an automatically completable
   generation—record `target_unavailable` and surface `ACTION_REQUIRED` instead.
   A newly observed target records `target_stabilizing` and must remain unchanged
   for five minutes before its lane is created.
   The generation also binds a review-input digest over the task id, title,
   body, latest review block event, and relevant parent links; a generic
   `edited` event alone is not a sufficient version signal.
4. Create a reviewer lane without a dependency parent. The
   `review-lane:<board>:<tenant>:<implementation-id>:<generation-signature>`
   idempotency key is the permanent dedupe boundary: retries of one generation
   resolve to its original lane even after that lane is archived, while changed
   code or evidence creates one new lane. Core creation enforces uniqueness
   atomically across all task states; concurrent callers cannot create duplicate
   lanes, and an archived generation is returned as terminal rather than reopened.
   This uses a dedicated permanent idempotency registry for the `review-lane:`
   namespace, keyed to the original task id in the same creation transaction. It
   does not impose a new blanket uniqueness constraint on legacy generic
   `tasks.idempotency_key` values; any pre-existing duplicate review-generation
   keys fail migration preflight and are quarantined for explicit repair.
   Each lane is created with `max_runtime_seconds=3600` and `max_retries=1`
   (current core semantics: block on the first failed attempt), so the dispatcher
   cannot independently requeue it.
5. Persist the implementation-to-reviewer generation association in router
   state.
6. Leave the implementation card blocked while review is in progress.

The reviewer lane is immediately eligible for dispatch. Its body contains the
implementation card id, repository/PR references, the engineer handoff verbatim,
and the independent review contract, so it does not depend on parent-context
inheritance.

The independent review contract is trusted router-authored instruction; the
implementation body, handoff, comments, patches, repository files, and external
artifact text are delimited as untrusted review evidence. They may not redefine
the verdict schema, target, scope, or approval criteria. The reviewer profile
loads the mandatory contract independently of the copied evidence and resolves
the immutable target from canonical providers rather than trusting commands or
claimed SHAs in prose. Control characters and delimiter collisions are encoded
without altering the auditable evidence bytes.

### Phase 2: consume review outcome

On later ticks the router re-reads both cards:

- **Reviewer `todo`, `ready`, or `running`:** keep the implementation blocked
  and record `awaiting_review`.
- **Reviewer `done`:** require structured completion metadata with the exact
  immutable review target, an explicit `APPROVED` verdict, and no conflicting
  blocking verdict or findings. Approval evidence must be written atomically on
  the exact terminal run/completion event; mutable task result fields, prose
  comments, or post-completion edits are not approval evidence. Re-verify the
  target immediately before
  completion: a PR head must still match, a Git commit must match the reviewed
  repository/commit identity, and an evidence artifact must still match its
  immutable identifier/digest. Verification runs before judging and again after
  an accepted judge verdict immediately before the local completion transaction;
  either mismatch invalidates the review. Only then combine the structured
  reviewer result with the original implementation evidence and submit the
  implementation card through an automation-only
  `hermes kanban complete --require-goal-judge`
  path. This mode requires a configured judge, a successful judge transport, a
  parseable structured result, and an exact `done` verdict before mutation; it
  never inherits the ordinary CLI's fail-open behavior.
- **Target verifier unavailable:** preserve the otherwise valid approval as
  `verification_deferred`. After the initial failed verification, make at most
  four delayed retries, waiting 5, 15, 30, then 60 minutes after the preceding
  failure (cumulative offsets approximately +5, +20, +50, and +110 minutes),
  with a hard deadline two hours after the initial failure. Re-read parent,
  reviewer, and target before every retry. Recovery continues automatically;
  budget or deadline exhaustion records `verification_exhausted` and emits one
  change-gated `ACTION_REQUIRED` outcome.
- **Target definitely changed or mismatched:** record `invalid_review`
  immediately. This is not a retryable verifier outage; a new review generation
  is required.
- **Goal judge returns `accepted`:** after the completion transaction rechecks
  the exact review block-event signature, review-input digest, target generation,
  reviewer approval signature, and judge-input signature, complete the
  implementation and record `resolved`.
- **Goal judge returns `rejected`:** retain the implementation block, record
  `approved_pending_completion` with the visible judge decision, emit a
  change-gated `ACTION_REQUIRED` outcome, and exit zero. Never loop a completion
  attempt every minute against unchanged evidence.
- **Goal judge returns `unavailable` or `parse_failed`:** retain the
  implementation block and record `judge_deferred`. Use the same four delayed
  retries and two-hour deadline as target verification, re-reading all bound
  inputs before each attempt. Judge configuration absence, transport failure,
  timeout, and malformed or unparseable output can never be treated as
  acceptance; budget or deadline exhaustion records `judge_exhausted` and emits
  one change-gated `ACTION_REQUIRED` outcome.
- **Goal judge returns `precondition_changed`:** retain the live parent state,
  reconcile the operation intent, and re-enter routing from a fresh snapshot;
  never apply the now-stale verdict.
- **Reviewer explicit non-approval or conflicting findings:** record `rejected`,
  retain the implementation block, and surface the reviewer findings. A later
  exact-head or explicit remediation workflow may produce a new review; this
  router does not erase the verdict.
- **Reviewer operational `timed_out`, `crashed`, or dispatcher `gave_up`:** retain
  the implementation block. The dispatcher never retries. If this remains the
  newest generation, its target and review-input digest are unchanged, no newer
  lane exists, and the generation has not consumed its one router retry, record
  `review_retry_pending` and guardedly requeue the same lane with the exact
  terminal run/event signature and operation id. The requeue clears only that
  lane's dispatcher failure/claim fields, preserves both run records, and does
  not create a new task or generation. A second operational failure records
  `review_exhausted` and emits one change-gated `ACTION_REQUIRED`. Superseded
  generations are archived and never retried.
- **Reviewer terminal with missing, malformed, contradictory, stale-head, or
  unreadable evidence:** fail closed, retain the implementation block, and
  record `invalid_review`, then emit a change-gated `ACTION_REQUIRED` outcome
  with exit zero. A `done` status or nonempty prose alone is never approval.

`--require-goal-judge` accepts exactly one task id and requires machine-readable
output. Its immutable judge input contains the task id/title/body and goal-mode
contract when present, the proposed result/summary/metadata, exact reviewer
completion-event evidence, target-verification evidence, and all completion
precondition signatures. Ordinary non-goal parents are therefore judged against
their task objective rather than silently skipping the gate. The command emits
one structured outcome—`accepted`, `rejected`, `unavailable`, `parse_failed`, or
`precondition_changed`. Handled outcomes exit zero; only operational or invariant
failures exit nonzero.

The strict judge prompt treats task/reviewer/artifact prose as quoted untrusted
evidence under a fixed judge contract and verdict schema. The parser accepts only
the strict structured verdict object for this mode; legacy booleans, prose,
fences, extra verdicts, or conflicting fields are `parse_failed` even though
ordinary interactive goal-loop parsing remains backward compatible.

An external PR/artifact service cannot participate in the local SQLite
transaction. Completion therefore attests only the recorded immutable revision,
not that a mutable PR will remain on that revision forever. The router records
both verifier observations and performs an immediate post-commit read. A change
detected between the last pre-commit check and that read emits
`post_completion_target_drift` and routes the new revision to the specialized
generation-discovery watchdog. A later head change does the same. Automation
never claims the new revision was approved and never reopens or rewrites the
completed parent automatically; the new generation remains separately visible
for review/escalation.

Review generation churn uses newest-generation-wins coalescing. If the target
changes while an older generation is active, the router immediately records the
older generation as `superseded`; its eventual verdict can never complete the
implementation. An old `todo` or `ready` lane is retired through a guarded
archive that requires its expected status and generation signature. A `running`
lane is not killed or raced by a second reviewer: its current attempt drains for
at most 60 minutes, after which `blocked`, `done`, or `archived` are terminal for
coalescing and a superseded generation cannot consume its router retry. Reviewer
instructions require a fresh target check before any
PR-visible findings or final submission; a changed target produces structured
`STALE_TARGET` and no visible findings or remediation. Once the old lane is
terminal, the router creates only the newest target after its five-minute
stability window, skipping intermediate revisions.

Manual implementation-card lifecycle changes always take precedence. If the
implementation becomes `done` or `archived`, the router records
`resolved_external`, supersedes any active reviewer generation, and stops. If
it leaves the matching review block for `todo`, `ready`, `running`, or another
block classification, the router releases ownership and never restores the old
block. If it remains review-blocked but its review-input digest changes, the old
generation is superseded and the changed input becomes a new generation after
the normal stability window. Automatic completion has one atomic precondition:
inside the same transaction that would mark the implementation complete, it
must still be blocked on the exact review block-event signature, review-input
digest, target generation, reviewer approval signature, and judge-input
signature that the approval and judge verdict cover. Drift returns structured
`precondition_changed` without mutation.

Completion mutation and judge-rejection notification are evidence-signature
gated. An unchanged rejection is not retried every minute. Scheduled target or
judge availability probes are the sole unchanged-evidence exception and remain
strictly bounded by their attempt/deadline budgets; they cannot mutate without a
fresh accepted result and transactional precondition recheck.

Every mutation is also operation-keyed. Before invoking a mutating CLI command,
the router durably records an intent containing a stable operation id, the
expected live preconditions, and the intended postcondition. A timeout, killed
subprocess, malformed response, or unexpected nonzero exit is an unknown
outcome, not proof of failure. The router performs no further mutation for that
handoff until it reconciles the intent against fresh task, link, and event data:

- A matching operation event and postcondition marks the intent `committed`.
- A successful idempotency-registry replay that returns another caller's exact
  generation lane marks reviewer creation `deduplicated`; the router may adopt
  that lane only after every generation/board/tenant/workspace guard matches.
- A definite structured nonmutation response such as judge rejection, bounded
  deferral, or `precondition_changed` marks the intent `not_applied`; it follows
  its domain state-machine gate and is never treated as an unknown timeout.
- A recognizable manual lifecycle change that removes the card from the exact
  handoff marks the intent `superseded_external`; automation records that it did
  not perform the mutation, releases ownership, and honors the manual state.
- Only after an actually unknown result, an unchanged expected precondition with
  no matching operation event or postcondition marks it safe to retry at most
  once, using the same operation id.
- Any other state marks it `conflict`, fails closed, and emits a change-gated
  `ACTION_REQUIRED` outcome.

The same protocol covers reviewer creation, parent completion, dependency link
or unlink, guarded reviewer requeue/archive, and block reclassification,
including the atomic multi-task recovery-manifest operation used for legacy
downstream containment. CLI lifecycle events carry the operation id so a desired
postcondition caused by another actor is not mistaken for this router's
successful operation. Sidecar-only generation supersession is also
preconditioned and journaled, but does not pretend to be a board mutation.

## 5. Persisted Handoff State

The shared recovery sidecar is resolved from the default Hermes root (the same
root that owns the shared Kanban board), never from an active secondary
profile's home. `state/kanban_blocked_watch.json` gains a schema version and a
`review_handoffs` map keyed by board, normalized tenant identity, and
implementation task id. Each implementation record identifies its current
generation and retains bounded prior-generation audit records. It also contains
a mutation-intent journal;
terminal intents are retention-bounded, while unresolved intents are never
pruned, remain until reconciled, and prevent subsequent mutation of that
handoff. A generation record contains:

```json
{
  "current_generation": {
    "generation_signature": "...",
    "review_target": {
      "kind": "pull_request",
      "identity": "https://github.com/owner/repo/pull/123",
      "revision": "exact-head-sha-or-artifact-digest"
    },
    "reviewer_lane_id": "t_...",
    "idempotency_key": "review-lane:<board>:<tenant>:<task>:<generation>",
    "review_input_digest": "...",
    "phase": "awaiting_review",
    "evidence_signature": "...",
    "target_first_seen_at": 0,
    "stable_after": 0,
    "created_at": 0,
    "updated_at": 0,
    "verification_attempts": 0,
    "next_verification_at": null,
    "verification_deadline": null,
    "verifier_health_generation": "...",
    "judge_attempts": 0,
    "next_judge_at": null,
    "judge_deadline": null,
    "judge_health_generation": "...",
    "review_retry_count": 0,
    "last_completion_attempt_at": null,
    "last_error": null
  },
  "prior_generations": [],
  "mutation_intents": [],
  "escalations": []
}
```

Allowed phases are `target_stabilizing`, `target_unavailable`, `repair_pending`,
`awaiting_review`, `review_retry_pending`, `review_exhausted`, `superseded`,
`verification_deferred`,
`verification_exhausted`, `judge_deferred`, `judge_exhausted`,
`approved_pending_completion`, `rejected`, `invalid_review`,
`resolved_external`, and `resolved`. Exhausted phases remain nonmutating until a
bound task/evidence input or the corresponding verifier/judge health-generation
fingerprint changes. Acknowledgement changes notification state only and never
reopens a handoff retry budget.

Phase transitions are explicit and guarded. A missing target enters
`target_unavailable`; a newly resolvable or changed target enters
`target_stabilizing`; five minutes of unchanged evidence permits lane creation
and `awaiting_review`. A repaired legacy lane moves from `repair_pending` to
`awaiting_review` only after the unlink operation reconciles and dispatchability
is verified. An operational reviewer failure enters `review_retry_pending`, then
returns to `awaiting_review` only after the guarded requeue commits; its next
operational failure enters `review_exhausted`. A usable approval enters
`verification_deferred` on verifier outage, then proceeds to judging after a
successful retry or enters `verification_exhausted` when its budget ends. Judge
outage follows the equivalent `judge_deferred` to resumed judging or
`judge_exhausted` path. Judge rejection enters `approved_pending_completion`;
review non-approval or unusable evidence enters `rejected` or `invalid_review`.
Manual terminal parent changes enter `resolved_external`; accepted guarded
completion enters `resolved`; replaced generations enter `superseded`. Any
bound target or review-input change supersedes the old nonterminal generation
and begins a new stability window. Only a corresponding verifier/judge health
generation change may reopen its exhausted availability budget without changed
task evidence; neither an escalation acknowledgement nor a cron-health
acknowledgement reopens reviewer, verifier, or judge budgets.

State snapshots are schema-versioned and carry a monotonic revision and
checksum. The router holds a nonblocking OS advisory lock for its whole tick;
the kernel releases it on process death, so automation never deletes a lockfile
based on elapsed time. Migration and maintenance tools acquire the same lock.
Writes use a unique same-directory temporary file, flush and `fsync` the file,
atomically replace the destination, and `fsync` the parent directory. The last
validated snapshot is retained as a backup.

Persisted leases, stability windows, retry deadlines, and escalation ages use
centralized UTC epoch time from `hermes_time.py`. A backward wall-clock jump past
the recorded last-observed time fails closed for mutation and is surfaced; it
cannot make an expired lease fresh or reset a retry budget. Tests inject time
rather than sleeping.

A malformed snapshot fails closed for mutations. The router quarantines it and
runs one nonmutating reconstruction tick from live Kanban tasks, operation
events, and generation idempotency keys. It may resume mutation on the next tick
only after every outstanding mutation intent reconciles; an actual conflict is
the only reconstruction outcome requiring human action. Lost handoff state is
therefore recoverable without trusting an empty default. A terminal prior
generation is never accepted as evidence for a different generation.

Current and nonterminal generations, unresolved mutation intents, and open
escalations are never retention-pruned. Full terminal generation records remain
for 90 days or the newest 20 generations per implementation, whichever retains
more. Older records compact to immutable tombstones containing the generation
signature, lane id, terminal phase, and terminal timestamp. Kanban lifecycle
events—not the sidecar—remain the durable audit trail.

Committed or otherwise terminal mutation intents use the same 90-day rule, with
the newest 100 per implementation retained when that is larger. Older terminal
intents compact to operation-id, operation-kind, terminal outcome, and timestamp
tombstones. Unresolved intents never compact.

## 6. Ownership Matrix

The matrix summarizes the ordered resolver below; it does not grant overlapping
claims.

| Card state | Effective owner | Authority and behavior |
|---|---|---|
| Eligible typed/legacy review candidate or active review handoff | Blocked router | `mutate` while its lease is fresh; two-phase review lifecycle |
| Typed `transient` or structurally verified dispatcher transient | Blocked router | `mutate` while its lease is fresh; retry within the existing per-card budget |
| Valid typed `dependency` in `todo` | Core Kanban | `mutate`; promote only when parents finish |
| Typed `needs_input` or `capability` | Configured human owner | `monitor`; retain the block and escalate at age thresholds |
| Deterministic reconcile action | Remediation actuator | `mutate`; existing signature-gated lifecycle |
| Current signed decision packet | Adjudicator | `mutate`; existing signature-gated lifecycle |
| Exact-head opted-in PR discovery | Specialized PR watchdog | Generation discovery only; never lifecycle ownership of the blocked card |
| Ambiguous legacy/untyped block or invariant violation | Actuator fallback | `escalate_only`; no guessed mutation |

Both loops use one shared ownership resolver. Live Kanban task/generation state
is durable truth; router sidecar state is a cursor/cache, not an ownership
authority. The blocked router renews a five-minute heartbeat lease on each
successful one-minute census tick.

Claims and leases live in board-scoped core Kanban tables, not only in either
watcher's sidecar. A lease records controller id, holder/epoch token, board,
issued/expiry times, last successful census signature, and operation id. Acquire
and renew use compare-and-swap transactions; a second router cannot silently
replace a fresh holder. The resolver reads task state, claims, and leases inside
the same SQLite read transaction. Sidecar disagreement therefore fails closed
and cannot grant mutation authority.

The resolver is a pure, board-scoped function over one consistent Kanban read
snapshot. It returns exactly one lifecycle owner, an authority (`mutate`,
`monitor`, or `escalate_only`), a reason, and an input signature. Its ordered
predicates are:

1. A matching explicit controller claim whose task/generation signature is
   current and whose five-minute controller lease is fresh owns the card.
2. An eligible typed/legacy review candidate or active review handoff belongs to
   the blocked router; a stale router lease routes it to the actuator's
   escalation-only fallback.
3. A typed or structurally verified transient belongs to the blocked router,
   with the same stale-lease fallback.
4. `needs_input` and `capability` belong to the configured human owner. The
   router monitors them; its stale lease transfers monitoring to the actuator
   fallback without granting mutation authority.
5. A valid dependency belongs to core Kanban in `todo`. A typed dependency
   found in `blocked` is an invariant violation owned by the escalation-only
   fallback.
6. A deterministic reconcile action belongs to the actuator.
7. A current signed adjudication packet belongs to the adjudicator.
8. An ambiguous or untyped block belongs to actuator escalation and never to a
   guessed mutation path.
9. A reviewer card requesting another review is a router-monitored protocol
   error and never a nested delegation source.

Human ownership resolves first from structured owner identity on the exact block
event, then from the managed board's `kanban.blocked_recovery.human_owner`
fallback. Task `assignee` and prose mentions are not human-owner claims. If
neither source resolves, the card enters escalation-only ownership quarantine
rather than being guessed.

Resolver inputs include board identity, task status and typed kind, latest block
and terminal events, dependency graph, run/failure state, current review
generation, unresolved operation intents, and controller claims/leases.
`created_by` alone is never an ownership claim. External controllers must
publish an exact input-signed task claim and fresh lease through a supported
core lifecycle operation or ownership falls back. Each claim records controller
id, board/task id, requested authority, task/generation input signature, lease
id, expiry, and operation id. “Signed” here means canonically bound to those live
inputs within Hermes' existing local-user trust boundary; it does not imply a new
cross-user cryptographic identity system.
Multiple valid claims or an unclassifiable snapshot enter ownership quarantine:
no process mutates, the health tick exits nonzero, and the conflict is surfaced.
The specialized exact-head PR watchdog owns generation discovery, not blocked
cards. For a router-managed blocked card it publishes a structured immutable
generation candidate and never creates the reviewer lane itself; the router is
the sole lane creator through the permanent generation key. The watchdog may
retain its existing creation behavior only for independent opted-in PRs that are
not attached to a managed blocked-card handoff and for which it holds an explicit
controller claim.

The remediation actuator excludes a handoff from mutation only when the shared
resolver sees a matching live review generation and a fresh router lease. A
prose prefix or stale handoff record alone is not an ownership lease. When the
lease expires, ownership transfers to an actuator fallback that may only
escalate and report the card; it cannot unblock, complete, close, or retry it.
This makes a paused or crashed router visible without allowing two mutation
paths to race.

Reviewer-assigned cards are never interpreted as sources for another review
handoff. A reviewer card blocked with typed `review` or legacy
`review-required:` is a protocol/error class: it remains blocked, is assigned an
owner, and is surfaced rather than silently skipped.

Every census row must resolve to one effective owner, including ownership
quarantine and the escalation-only fallback. Candidate zero/multiple-owner
states remain health failures even though quarantine provides the single safe
effective disposition.

The router operates only on an explicit managed-board allowlist, initially the
current default board. It enumerates each managed board independently and passes
that board identity explicitly to every read and mutation. Every blocked card on
a managed board must resolve through the ownership census; cards on unmanaged
boards are reported as outside automation scope rather than ownerless.
The allowlist and rollout mode live in nonsecret `config.yaml` under
`kanban.blocked_recovery`; no new environment-variable configuration is added.
An empty, malformed, or unknown-board allowlist fails closed for mutation and is
reported in health.

Only this recovery-control section is narrowly hot-reloaded by the gateway
supervisor on a validated config revision; it does not rebuild prompts, toolsets,
or conversation state. Guarded CLI mode/allowlist updates write config atomically.
A malformed reload retains the last validated control snapshot for observation,
disables new recovery mutation, and alerts. A newly added board begins in
`shadow` and must pass two clean ownership censuses before its mode can become
`live`.

The same section contains a declarative controller-job manifest. Each required
role (initially blocked router, remediation actuator, and adjudicator) binds one
cron job id, canonical role tag, explicit local source-manifest digest (entry
script plus required local helpers), schedule digest, delivery scope, and desired
mode (`paused`, `shadow`, or `live`). Missing/unreadable manifest sources are
drift, so changing an imported helper cannot bypass the check by leaving the
wrapper unchanged. The blocked-router closure includes its shell/Python entry
points plus `hermes_cli/kanban.py`, `hermes_cli/goals.py`, and the required core
Kanban modules that its fresh subprocess loads. The exact-head PR watchdog is
listed separately as an optional specialized role. Desired mode—not the current
`enabled` bit—is operational intent, so maintenance changes the manifest mode
before pausing a job.

Reviewer lanes inherit the implementation's exact tenant. A project-linked
implementation passes both project id and source-task identity so core resolves
the canonical repository while creating a separate reviewer task-id worktree.
The implementation's literal workspace path is never copied or shared. A
multi-repository or non-project review uses an isolated scratch workspace with
separate clones or detached worktrees at the immutable targets. After creation,
the router reads the lane back through the same board and verifies board,
tenant, reviewer assignee, generation key, and workspace isolation before
accepting it into the handoff.

## 7. Structured Block Migration

Perform a one-time, guarded migration only after existing review cycles are
repaired, the restarted gateway fingerprint is verified, and live review routing
has passed its canary and stability window. The router's legacy compatibility
path owns eligible handoffs until this final migration stage:

- A leading legacy `review-required:` on a non-reviewer card becomes typed
  `review` plus an explicit review-handoff generation only when the legacy
  protocol parser yields a complete immutable target and exact structured
  handoff fields; prose that merely resembles the prefix stays untyped.
- Vetted structured dispatcher crash/timeout/idle event classes become
  `transient`; freeform failure prose does not qualify.
- Explicit machine-readable dependency references are migrated graph-first:
  every referenced task id must exist, the complete parent set must be
  unambiguous, and each required `task_links` edge must be created or verified
  before the card is routed to typed `dependency`/`todo`. Missing or
  contradictory references remain untyped and escalated. A dependency kind
  without a verified parent edge is invalid migration output.
- Existing structured event metadata may migrate to the matching human-blocked
  kind only when a structured owner or configured board fallback also resolves.
  Prefix text alone is not migration evidence.
- Every other prose-only or semantically inferred classification stays untyped
  and is emitted in a dry-run migration manifest for human review.

Migration uses a narrow supported `hermes kanban reclassify-block` lifecycle
operation. It works only when the task is still blocked and its latest block
event matches the caller's expected signature. For human-blocked kinds it
changes only `block_kind`, preserves failure/recurrence/run state, and appends an
auditable `block_reclassified` event containing the caller's stable operation
id. For `dependency`, one transaction verifies
or creates the complete parent edge set before moving the task to `todo`.
Precondition drift changes nothing. Migration never exposes a card to the ready
queue between classification steps and never uses an unguarded SQLite update.
The migration manifest records the task id, current event signature, proposed
kind, and exact machine-readable evidence; applying it revalidates that same
signature.

Core Kanban adds `review` to its valid block kinds and worker/CLI schemas. It is
a sticky blocked state owned by the review router, not a human-only
`needs_input` alias. Future worker instructions and local automations must pass
structured block kinds. Freeform `review-required:` parsing remains a legacy
compatibility path, not the primary routing contract.

## 8. Existing Review-Cycle Recovery

Repair existing cycles inside a controlled gateway/dispatcher maintenance
window:

1. Set the blocked router, remediation actuator, and adjudicator manifest modes
   to `paused`, pause those jobs, then stop the gateway so neither cron catch-up
   nor the dispatcher can mutate or claim a newly promoted reviewer between
   repair steps.
2. Take an SQLite online backup and snapshot the two watcher state files plus
   `cron/jobs.json`.
3. Re-read each candidate implementation and reviewer lane.
4. Require: implementation is blocked with a current review handoff; reviewer
   lane is the unique active reviewer associated with it; the dependency edge
   is still present; neither card changed since preflight.
5. Seed the handoff generation as `repair_pending` before changing its graph.
6. Remove only that dependency edge through `hermes kanban unlink`. Core
   `unlink_tasks` immediately recomputes reviewer readiness and records the
   repair operation id on its event. An unknown command outcome is reconciled
   before the repair proceeds or the gateway restarts.
7. Verify each repaired reviewer lane becomes dispatchable while the gateway
   remains stopped.
8. Apply any approved legacy recovery manifest, including the atomic
   `t_9e4b3bc2` finalization plus `t_358c0763` parking operation, and verify all
   intended postconditions.
9. Start the gateway with current installed code while all three
   blocked-lifecycle jobs remain paused.
10. Activate the router in lease/health-only mode before enabling the actuator,
    adjudicator, or automatic parent completion. A reviewer may dispatch first,
    but the blocked parent cannot be completed until the router owns the handoff
    under a fresh lease and the shared ownership census is clean.

If any guard differs, do not mutate that pair. Report it for explicit review.
Reviewer-assigned legacy cards receive separate case-by-case recovery; they are
not transformed into implementation cards by heuristic. Normal rollback may
re-link a reviewer only if it has never started a run; once review work has
begun, preserve its evidence and escalate rather than rewinding the board.

Routine automation never completes a blocked reviewer card from prose. A legacy
reviewer outcome may be finalized automatically only if existing structured run
metadata independently satisfies the current immutable-target, explicit-verdict,
and no-conflicting-findings contract, and every resulting child-readiness change
is explicitly safe. Otherwise, prose-only or downstream-ambiguous cases produce
a signed recovery manifest containing the exact task, run, block-event, proposed
targets, intended transitions, and complete downstream readiness effect; applying
that manifest requires one human approval and revalidates every fingerprint.

`t_9e4b3bc2` is such a prose-only case: its run metadata is null and its approval
and target SHAs exist only in comments. It is also the sole non-done parent of
`t_358c0763`, so completing it alone would make that implementation root ready
and risk duplicate work. Its approved recovery manifest must, during the stopped
gateway maintenance window, adopt and freshly verify the explicit targets, then
atomically finalize the reviewer only if they still match while parking
`t_358c0763` as typed `needs_input` for its documented human merge/deployment
gate. Because that downstream card is currently `todo`
and ordinary `block` accepts only `ready`/`running`, a guarded
`hermes kanban apply-recovery-manifest` operation performs reviewer finalization
and downstream parking in one transaction. It requires the expected reviewer,
child, graph, target, and event fingerprints, records one operation id on both
tasks, and exposes no intermediate ready state. Any precondition drift changes
nothing. If a target changed, the old verdict is not adopted; route the current
target through a fresh normal review generation or escalate if no immutable
target can be established.

## 9. Health and Observability

Each router/actuator health snapshot contains:

- Blocked count by structured kind and age bucket.
- Active handoffs by phase and oldest handoff age.
- Eligible, actioned, retried, escalated, and deferred counts.
- Deterministic mutations over the last 24 hours.
- Unacknowledged human escalations.
- Acknowledged-but-still-open human escalations.
- `ownerless_blocked_count` and `multiply_owned_blocked_count`.
- Router lease age and escalation-only fallback count.
- Unresolved mutation intents by age and operation kind.
- Gateway PID/start time and installed-code drift status.
- Cron provider readiness, thread/provider liveness, last heartbeat and last
  successful-tick ages, consecutive tick failures, automatic-restart count, and
  restart-quarantine state.
- Required controller jobs by role, desired/observed mode, exact spec-digest
  and source-manifest agreement, enabled state, last-success age, next due time,
  and reconciliation outcome.
- Validated recovery-control config revision/digest, reload error if any, and
  per-board shadow/live eligibility.

No-op ticks remain silent. Runtime errors, malformed durable state, ownerless
cards, or multiple ownership violate automation invariants: they exit nonzero
and produce a compact watchdog failure. Handled domain outcomes—including
review rejection, invalid/stale review evidence, lease expiry handled by the
fallback, goal-judge rejection, and exhausted target-verifier or judge deferral—
emit change-gated `ACTION_REQUIRED` output with exit zero and remain visible in
persisted health until resolved. In-budget deferral remains visible but does not
page on every probe. Backlog size by itself is not failure because human-only
blocks may remain legitimately open.

Each escalation has a signature derived from task id, current block or review
generation signature, outcome class, and escalation tier. A guarded
`hermes kanban acknowledge-escalation <task> --signature <signature>` command
and equivalent dashboard action append a structured acknowledgement event with
actor and timestamp. Acknowledgement changes notification state only; it never
changes card status or ownership. Acknowledged open escalations remain visible
in health. A changed block, generation, outcome, or higher age tier produces a
new unacknowledged signature, while disappearance of the underlying condition
resolves it automatically. Prose comments are not acknowledgements. Initial,
24-hour, and 72-hour notices are change-gated rather than emitted every tick.

At startup the gateway atomically writes
`state/gateway-runtime-fingerprint.json`. One authoritative manifest contains
`gateway/run.py`, `gateway/kanban_watchers.py`, `gateway/status.py`,
`gateway/code_skew.py`, `gateway/systemd_notify.py`, the new
`gateway/runtime_fingerprint.py`, `gateway/cron_supervisor.py`, and
`gateway/recovery_control.py`,
`cron/scheduler.py`, `cron/scheduler_provider.py`, `cron/jobs.py`,
`cron/executions.py`, `hermes_cli/kanban_db.py`, `hermes_cli/config.py`,
`hermes_constants.py`, and `hermes_time.py`. Paths are resolved from the
imported modules rather than assumed from a checkout root.

The snapshot contains its schema version, PID, OS process-start fingerprint,
recording time, Python executable, Git commit, dirty-state marker, SHA-256 of
every manifest source file, and a nonsecret static automation-configuration
digest covering restart-sensitive cron provider/interval and served profile
scope. Dynamic `kanban.blocked_recovery` mode/allowlist/manifest revisions are
reported separately in health and are intentionally excluded from restart drift.
The fingerprint is written only
after the gateway owns its runtime lock/PID identity and before cron, notifier,
or dispatcher threads start, using file and directory `fsync` plus atomic
replacement. When cron or the embedded dispatcher is enabled, a
missing/unreadable required module or an unpersistable fingerprint aborts
startup before background work begins.

Gateway startup ordering becomes explicit: acquire gateway identity/lock, write
the runtime fingerprint, connect required adapters, start the cron provider and
await its readiness signal, then start Kanban notifier/dispatcher watchers and
other mutating background loops, and only then emit `READY=1`. Today the embedded
Kanban dispatcher starts inside `GatewayRunner.start()` before the top-level cron
thread; implementation must split that boundary so no dispatcher mutation or
worker spawn can precede cron readiness. Every background entry point also checks
the same startup gate, preventing a future ordering regression.

The existing gateway restart watchdog first requires PID/start-time agreement,
then compares current source hashes to that exact startup fingerprint and emits
a change-gated `restart_required` alert naming every changed module. Module
hashes are authoritative; Git state is diagnostic. It does not use commit
timestamps and excludes local cron scripts and the watchdog itself because
separate processes load them fresh each tick. It continues to report PID
changes and downtime.

Cron liveness is supervised by the gateway event loop, never by a cron job. The
cron-provider contract gains an explicit readiness signal and a read-only health
snapshot. The built-in provider is ready only after its initial durable ticker
heartbeat is recorded; an external provider is ready only after it confirms its
schedule is armed. The gateway does not emit `READY=1` until that signal arrives
within a configurable deadline (`cron.startup_ready_timeout_seconds`, default 30
seconds); expiry aborts startup before background mutation begins.

After startup, a gateway task checks provider health every 30 seconds. For the
built-in provider, an exited ticker thread, a heartbeat older than the greater
of three configured tick intervals or 180 seconds, or three consecutive failed
tick cycles is unhealthy. External providers supply equivalent armed/liveness
health without being required to keep their startup thread alive. Unhealthy cron
state marks application health unhealthy, persists a compact failure, and
delivers an alert directly through live gateway adapters, so detection and
notification do not depend on the failed ticker. The systemd watchdog continues
to represent process/event-loop liveness; cron application health cannot stop
its feed and accidentally create a supervisor restart loop.

The gateway requests at most one guarded automatic restart for an unchanged
runtime-source plus static automation-config fingerprint. Failure class is
recorded for diagnosis but does not create another restart budget; alternating
dead-thread/stale-heartbeat/failed-tick symptoms therefore cannot loop. If the
restarted process is unhealthy without an intervening source/config fingerprint
change or structured operator acknowledgement, it remains running for
diagnostics, withholds healthy application status, and enters restart quarantine.
A later fingerprint change or a guarded
`hermes cron acknowledge-health --signature <signature>` event clears quarantine
and permits one new automatic restart. The durable, atomically written
supervision record survives the restart and keys the failure to the
runtime-source digest, static automation-config digest, provider, and observed
failure classes. Cron-owned blocked-lifecycle mutation remains disabled whenever
scheduler health is quarantined or the router lease is stale. Ordinary human,
worker, and core dispatcher lifecycle semantics remain unchanged after startup.

The gateway supervisor also reconciles the declarative controller-job manifest
through the locked cron job store. It may automatically re-enable a job only when
exactly one record has the required role, every immutable job/spec digest and
delivery field matches, desired mode is `shadow` or `live`, and the sole drift is
`enabled=false`. The enable operation is operation-id journaled and attempted at
most once per unchanged job-state signature. A desired `paused` mode is never
re-enabled. Missing roles, duplicate roles, changed source manifests, schedules,
or delivery, unknown enable outcomes, or a last-success age beyond three expected
periods remain nonmutating, mark controller health unhealthy, and alert directly
through the gateway. An exact in-flight execution with a fresh execution
heartbeat defers the stale-success decision until its existing hard runtime bound;
it does not count as success. The CLI/status surfaces include disabled required
jobs instead of hiding them from the effective-health view.

Before an automatic enable, the gateway durably records the expected cron-record
signature and intended state in its supervision journal. The locked cron-store
update records the operation id on the job. A timeout or write error is reconciled
from the exact enabled state plus operation id before any further attempt; an
enabled bit changed by another actor is not claimed as the supervisor's success.

## 10. Safety and Error Handling

- All board mutations use supported `hermes kanban` CLI verbs.
- Every mutation uses a fresh read immediately before execution.
- Multi-task legacy recovery uses one guarded CLI transaction; reviewer
  finalization can never expose a downstream card to the ready queue before its
  required parking transition commits.
- Reviewer creation remains generation-idempotency-keyed.
- Reviewer-generation idempotency is enforced atomically and permanently across
  active and archived tasks in core Kanban.
- Mutations use durable operation ids and reconcile unknown subprocess outcomes
  before any retry or later mutation.
- Manual parent lifecycle changes win; automation never reinstates an old block.
- Router parent completion always uses the automation-only
  `--require-goal-judge` path. Its only mutating outcome is a configured judge's
  successful, parseable, exact `done` verdict; unavailable, transport-failed,
  timed-out, or malformed judging defers and never fails open. Ordinary
  human/worker completion semantics remain unchanged.
- Exact parent, reviewer, target, and judge-input preconditions are rechecked
  inside the completion transaction after judging.
- Reviewer rejection and missing evidence fail closed.
- Reviewer attempts are capped at 60 minutes. Core dispatcher retries are
  disabled for reviewer lanes; only the router may authorize one guarded retry
  of the current unchanged generation after a structured operational failure.
- Completion rejection is signature-cached to prevent per-minute retries.
- Transient retries keep the existing budget and anti-stale checks.
- `needs_input` and `capability` are never automatically unblocked.
- Existing actuator signature gates and max-mutation anomaly cap remain.
- Scheduler supervision is gateway-owned: startup readiness is gated on cron
  readiness, unhealthy cron receives one guarded automatic restart, and any
  continued unhealthy state under the same runtime/config fingerprint is
  quarantined instead of restart-looped.
- Controller-job desired state is declarative. Exact disabled-only drift may be
  re-enabled once; missing, duplicate, modified, stale, or intentionally paused
  jobs are never guessed or recreated.
- The two diagnostic `w` jobs stay paused.

## 11. Testing Strategy

Implementation follows red-green-refactor.

Local review-router behavioral tests cover:

- Creating an unparented, immediately runnable reviewer lane.
- Preserving board and tenant identity while using an isolated project worktree
  or multi-repository scratch workspace, including rejection of cross-board,
  cross-tenant, idempotency-key, and shared-workspace mismatches.
- Shared-default-root state resolution across profiles, proving secondary
  profiles cannot fork router authority or reconstruct from a different sidecar.
- Managed-board allowlist enforcement and fail-closed behavior for empty,
  malformed, or unknown board configuration.
- Narrow recovery-control hot reload, atomic guarded config updates,
  malformed-revision mutation disablement with last-validated observation, and
  mandatory two-census shadow entry for newly managed boards.
- Recovering the same lane through its idempotency key after state loss.
- Creating exactly one new lane when the PR head or non-PR evidence generation
  changes, without reusing a prior generation's verdict.
- Leaving the implementation blocked while review is pending.
- Completing through the goal judge only after a reviewer finishes with usable
  structured approval evidence bound to a freshly verified immutable target.
- Exercising `--require-goal-judge` for both goal-mode and ordinary parents,
  including structured `accepted`, `rejected`, `unavailable`, `parse_failed`,
  and `precondition_changed` outcomes; only `accepted` may mutate, and ordinary
  human/worker completion behavior remains backward compatible.
- Strict-judge injection fixtures and parser rejection of legacy booleans,
  prose/fences, unknown or conflicting fields, and every non-exact verdict.
- Rejecting `done` lanes whose metadata is missing, contradictory, non-approved,
  or bound to a stale/mismatched target.
- Accepting approval only from the exact terminal run/completion event and
  rejecting mutable task-result fields, prose comments, or post-completion edits
  as approval evidence.
- Prompt-injection and delimiter-collision fixtures in handoffs, comments,
  patches, repository files, and artifact text, proving untrusted evidence cannot
  redefine the target, verdict schema, scope, or structured approval contract.
- Supporting PR-head, Git-commit, and immutable evidence/artifact targets while
  refusing mutable or unverifiable review targets.
- Target changes before judging, after judging/before transaction, in the
  post-commit observation window, and later: pre-commit changes prevent
  completion; post-completion changes produce a distinct generation/drift alert
  without claiming approval or automatically reopening the parent.
- Deferring temporarily unavailable target verification with bounded backoff,
  while immediately invalidating a definitely changed target.
- Distinguishing change-gated action-required domain outcomes (exit zero) from
  operational/invariant failures (nonzero).
- Escalation acknowledgement bound to the exact current signature, including
  automatic resolution, higher-tier re-escalation, and proof that an
  acknowledgement cannot mutate a task or carry forward to changed evidence.
- Gateway startup fingerprint creation and dirty/committed restart-sensitive
  module drift detection without docs-only or fresh-script false positives.
- Cron provider startup readiness, dead-thread/stale-heartbeat detection,
  consecutive failed-tick detection, adapter-direct alerting, systemd-watchdog
  separation, one guarded automatic restart, and same-fingerprint restart
  quarantine without a supervisor loop. External providers that arm and return
  must not produce a false dead-thread alarm, and alternating failure classes
  must not mint additional restart budgets.
- Required controller-job manifest validation: exact disabled-only self-heal,
  explicit pause preservation, role/source-closure/spec/schedule/delivery drift rejection,
  duplicate/missing-role rejection, unknown-enable reconciliation, stale-success
  detection with fresh in-flight execution handling, direct alerting, and
  disabled-job visibility in health/status.
- Caching a judge rejection until evidence changes.
- Preserving and surfacing reviewer rejection.
- A 60-minute reviewer attempt cap, no dispatcher retry, one guarded router retry
  only for the current unchanged generation after structured timeout/crash/give-up,
  and exhaustion after the second operational failure.
- Coalescing target churn without concurrent reviewers: guarded retirement of
  unclaimed stale lanes, bounded drain of running stale lanes, rejection of all
  superseded verdicts, and creation of only the newest five-minute-stable
  generation.
- Parent completion, archival, unblock, re-block, and review-input edits during
  a review, including proof that a stale approval cannot mutate the parent and
  that automation never reverses a manual lifecycle change.
- Unknown-operation reconciliation when a human concurrently completes,
  archives, unblocks, re-blocks, or edits the card, recording
  `superseded_external` without claiming the router committed the change.
- Legacy blocked-reviewer recovery with structured-metadata auto-finalization,
  prose-only manifest approval, and downstream readiness containment; fixture
  coverage includes the `t_9e4b3bc2`/`t_358c0763` shape.
- Atomic recovery-manifest application across the reviewer, downstream child,
  graph, events, and operation-id audit trail, including all-or-nothing behavior
  on every stale precondition and simulated crash boundary.
- Failing closed on malformed state, partial reads, or stale preconditions.
- Lock contention, torn/corrupt snapshots, last-known-good recovery, read-only
  reconstruction, intent reconciliation after state loss, and retention that
  never prunes live work or unresolved intents while bounding terminal history.
- Backward/forward clock jumps across leases, stability windows, retry deadlines,
  and escalation tiers, proving no jump revives authority or resets a budget.
- Rollback refusal to restore a stale sidecar over newer revisions/events, with
  reconstruction used whenever exact snapshot-to-board agreement is absent.
- Reconciling create, complete, link/unlink, guarded archive, and reclassify or
  downstream-park commands that time out before returning, including committed,
  deduplicated, definite-not-applied, unchanged-safe-to-retry, and conflict
  outcomes; no test may infer failure from a timeout alone or retry a definite
  domain nonmutation as though it were unknown.
- Permanent reviewer-generation idempotency under concurrent creation attempts
  and after archival, including migration preflight for duplicate scoped keys and
  proof that unrelated legacy generic-key duplicates remain compatible.
- Specialized PR-watchdog generation publication racing the router, proving the
  router remains the sole lane creator for managed blocked-card handoffs.
- Exactly-one-owner census behavior.
- Ordered ownership predicates, snapshot/input signatures, fresh external
  controller claims, created-by non-ownership, and mutation quarantine for zero
  or multiple candidate owners.
- Structured human-block owner resolution, board-default fallback, assignee/prose
  non-ownership, and escalation-only handling when no human owner resolves.
- Fresh-lease router ownership, stale-lease fallback ownership, and proof that
  the fallback cannot mutate a handoff, including competing router acquisition,
  holder/epoch fencing, and sidecar-to-core disagreement.
- High-confidence typed migration and ambiguous legacy retention.
- Dependency migration that links every verified parent before routing to
  `todo`, and refuses missing or ambiguous references.
- Reclassifying an unchanged blocked card without an unblock window or counter
  mutation, plus atomic no-op behavior when the event signature is stale.
- Refusing to mutate block kinds from freeform semantic inference while
  producing a reviewable migration manifest for those cases.

Core regression tests cover unlink/recompute behavior, typed block routing,
guarded block reclassification, goal completion, dispatcher recovery, and
session attribution. Tests execute through `scripts/run_tests.sh`; local
automation tests run in the installed Hermes virtual environment with an
isolated temporary home/board.

Live verification uses shadow mode before any cron resume and inspects actual
CLI behavior rather than source text.

## 12. Guarded Rollout

1. Capture board, state, jobs, gateway PID/start time, and blocked census.
2. Implement and pass local automation tests.
3. Pass focused core Kanban/gateway regression suites. On an isolated full-stack
   board, prove: structured approval plus unchanged target plus judge acceptance
   completes the parent through `--require-goal-judge`; reviewer rejection
   retains the block and emits one exit-zero `ACTION_REQUIRED`; approval plus
   judge rejection retains the block and is not retried on unchanged evidence;
   judge unavailable/parse-failure/precondition-change, stale-target, and
   verifier-outage branches remain nonmutating.
4. Run `kanban-blocked-watch` once with review delegation and self-heal both in
   shadow mode.
5. Set all three controller-job manifest modes to `paused`, pause the jobs, stop
   the gateway/dispatcher, take the final backup/preflight, seed guarded
   `repair_pending` generations; repair existing dependency cycles; and apply
   approved atomic legacy recovery manifests.
6. Start the gateway with all three blocked-lifecycle jobs still paused so
   installed code is loaded; verify its PID/start fingerprint, cron-provider
   readiness/healthy heartbeat, and repaired reviewer readiness. Gateway
   readiness must not precede cron readiness.
7. Set only the blocked router's desired mode to `shadow`, enable it in
   lease/health-only mode, and observe two ticks. Review routing and self-heal
   remain nonmutating.
8. Set the actuator and adjudicator desired modes to `shadow` and run two
   shadow/escalation-only ticks against the same snapshot resolver. Require a
   clean exactly-one-owner census before restoring their existing live
   deterministic actions.
9. Set the router to `live` for one explicitly allowlisted handoff while
   self-heal stays SHADOW. This production canary must reach structured reviewer
   approval, live immutable-target verification, and actual parent goal-judge
   acceptance. Verify cron output/delivery, mutation journal, lease, ownership
   census, and downstream readiness after completion; an action-required
   rejection does not satisfy the positive canary.
10. Only after the isolated negative cases and the live positive completion pass,
   remove the review allowlist and enable review routing for every eligible
   handoff. Never manufacture a rejection on a real PR; deterministic isolated
   canaries and shadow replay of real evidence cover negative production paths.
11. Keep transient self-heal in SHADOW until review routing is stable, then
    enable it with a one-mutation-per-tick cap. Observe retry counter/budget
    behavior before increasing the cap.
12. Apply the high-confidence legacy block migration last and publish ambiguous
    residue.
13. Verify scheduler state, blocked ownership, handoff phases, mutation counts,
    and delivered health output.

Rollback pauses all blocked-lifecycle mutation. It never restores an older
sidecar snapshot over a board or sidecar revision that has advanced; that could
erase unresolved intents and replay mutations. A last-known-good snapshot may be
restored only when its monotonic revision still matches every live operation
event, otherwise recovery is read-only reconstruction from current board state.
Rollback re-links a repaired reviewer only when a fresh guard proves that lane
has never run. Once a reviewer has started, its evidence is preserved and the
handoff is escalated. The pre-rollout online database backup is disaster recovery
only; it is never restored over a live board that has advanced normally.

## 13. Success Criteria

- `kanban-blocked-watch` is active, has completed two health-only ticks, and has
  processed at least one allowlisted production review generation end-to-end
  through structured approval, immutable-target verification, and parent judge
  acceptance.
- Isolated full-stack canaries prove reviewer rejection, judge rejection,
  stale-target, and verifier-outage paths retain the parent block with the
  required retry and notification behavior.
- The running gateway contains current installed Kanban recovery code.
- Cron liveness is independently supervised by the gateway; a dead or repeatedly
  failing ticker is detected and directly reported, receives at most one guarded
  automatic restart per unchanged runtime/config fingerprint regardless of
  changing failure class, and cannot leave blocked-lifecycle mutation falsely
  healthy.
- Required controller roles match their declarative cron-job manifests; an exact
  unexpectedly disabled live/shadow job self-heals once, while missing,
  duplicate, modified, intentionally paused, or stale jobs are directly visible
  and cannot mutate under guessed ownership.
- No review lane is dependency-gated by the implementation card it must review.
- Reviewer approval can automatically complete an implementation card only via
  `--require-goal-judge`, after a configured judge returns a parseable exact
  `done` verdict and the transaction revalidates every bound precondition.
- Reviewer rejection never completes or unblocks the implementation card.
- Every blocked card on a managed board has exactly one effective owner and
  authority; unmanaged boards are explicitly reported outside automation scope.
- Typed block coverage increases without guessing ambiguous legacy reasons.
- Health output distinguishes a healthy no-op from ineffective or ownerless
  automation.
