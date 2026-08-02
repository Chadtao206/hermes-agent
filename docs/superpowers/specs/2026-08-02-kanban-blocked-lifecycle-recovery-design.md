# Kanban Blocked Lifecycle Recovery Design

- **Date:** 2026-08-02
- **Author:** Chad Tao + Codex
- **Status:** Approved design
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
  `transient` block semantics.
- Cron health reports process completion, not whether the blocked backlog has
  an owner or is advancing.
- The long-lived gateway predates relevant installed Kanban changes, so the
  running dispatcher and fresh CLI subprocesses use mixed code generations.

At design time the live board has 25 blocked cards. Sixteen are untyped, and
all eight blocked cards with nonzero dispatcher failure counts are untyped.

## 2. Goals

- Give every blocked card exactly one active lifecycle owner.
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
  type to core Hermes.
- Turning the exact-head PR re-review watchdog into a general blocked-card
  cleaner.

## 4. Chosen Architecture

Use a two-phase, unlinked review handoff.

### Phase 1: create runnable review

When a non-reviewer implementation card is blocked with a valid
`review-required` handoff:

1. Re-read the card and its latest block event.
2. Build the reviewer brief from the implementation body and handoff evidence.
3. Create a reviewer lane without a dependency parent. The existing
   `review-lane:<implementation-id>` idempotency key remains the durable dedupe
   boundary.
4. Persist the implementation-to-reviewer association in router state.
5. Leave the implementation card blocked while review is in progress.

The reviewer lane is immediately eligible for dispatch. Its body contains the
implementation card id, repository/PR references, the engineer handoff verbatim,
and the independent review contract, so it does not depend on parent-context
inheritance.

### Phase 2: consume review outcome

On later ticks the router re-reads both cards:

- **Reviewer `todo`, `ready`, or `running`:** keep the implementation blocked
  and record `awaiting_review`.
- **Reviewer `done`:** require a nonempty reviewer result, combine it with the
  original implementation evidence, and submit the implementation card for
  completion through the normal CLI. The goal judge remains authoritative.
- **Goal judge accepts:** record `resolved`; no further action.
- **Goal judge rejects:** retain the implementation block, record
  `approved_pending_completion` with the visible judge error, and exit the tick
  nonzero. Never loop a completion attempt every minute against unchanged
  evidence.
- **Reviewer `blocked`:** record `rejected`, retain the implementation block,
  and surface the reviewer findings. A later exact-head or explicit remediation
  workflow may produce a new review; this router does not erase the verdict.
- **Reviewer terminal without usable evidence, missing, or unreadable:** fail
  closed, retain the implementation block, and surface an error.

The completion attempt is evidence-signature gated. The router retries only if
the implementation handoff or reviewer result changes, preventing repeated
judge rejection spam.

## 5. Persisted Handoff State

`state/kanban_blocked_watch.json` gains a schema version and a
`review_handoffs` map keyed by implementation task id. Each record contains:

```json
{
  "reviewer_lane_id": "t_...",
  "idempotency_key": "review-lane:t_...",
  "phase": "awaiting_review",
  "evidence_signature": "...",
  "created_at": 0,
  "updated_at": 0,
  "last_completion_attempt_at": null,
  "last_error": null
}
```

Allowed phases are `awaiting_review`, `approved_pending_completion`,
`rejected`, and `resolved`.

State writes become atomic (`temporary file` plus `os.replace`). A malformed
state file fails closed for mutations. Lost handoff state is recoverable because
the Kanban idempotency key returns the existing non-archived reviewer lane.

## 6. Ownership Matrix

| Card state | Sole owner | Behavior |
|---|---|---|
| Non-reviewer implementation with active review handoff | Blocked router | Two-phase review lifecycle |
| Typed `transient` or verified dispatcher transient | Blocked router | Retry within the existing per-card budget |
| Typed `dependency` | Core Kanban | Keep in `todo`; promote when parents finish |
| Typed `needs_input` or `capability` | Human, monitored by blocked router | Remain blocked; escalate at age thresholds |
| Deterministic reconcile action or decision packet | Remediation actuator/adjudicator | Existing signature-gated lifecycle |
| Exact-head opted-in PR re-review | Specialized PR watchdog | Create/coalesce a reviewer lane only |
| Ambiguous legacy/untyped block | Actuator/adjudicator | Escalate; no guessed mutation |

The remediation actuator excludes a card only when an active handoff record
proves the blocked router owns it. A prose prefix alone is not an ownership
lease.

Reviewer-assigned cards are never interpreted as sources for another review
handoff. A reviewer card blocked with `review-required` is a protocol/error
class: it remains blocked, is assigned an owner, and is surfaced rather than
silently skipped.

Every census row must resolve to one and only one owner. Zero owners and
multiple owners are both health failures.

## 7. Structured Block Migration

Perform a one-time, guarded migration after the new router is deployed:

- A leading `review-required:` on a non-reviewer card becomes an explicit
  review-handoff record; it is not forced into an unrelated core `block_kind`.
- Verified dispatcher crash/timeout/idle signatures become `transient`.
- Explicit machine-readable dependency references become `dependency` and use
  core parent gating.
- Clear access/credential limitations become `capability`.
- Clear questions or product decisions become `needs_input`.
- Ambiguous prose stays untyped and is reported.

Migration uses Kanban lifecycle CLI operations with an immediate fresh-state
guard. It does not update live task rows through an unguarded SQLite statement.

Future worker instructions and local automations must pass structured block
kinds. Freeform reason parsing remains a legacy compatibility path, not the
primary routing contract.

## 8. Existing Review-Cycle Recovery

Before resuming the watcher:

1. Take an SQLite online backup and snapshot the two watcher state files plus
   `cron/jobs.json`.
2. Re-read each candidate implementation and reviewer lane.
3. Require: implementation is blocked with a current review handoff; reviewer
   lane is the unique active reviewer associated with it; the dependency edge
   is still present; neither card changed since preflight.
4. Remove only that dependency edge through `hermes kanban unlink`. Core
   `unlink_tasks` immediately recomputes reviewer readiness.
5. Seed the handoff record and verify the reviewer lane becomes dispatchable.

If any guard differs, do not mutate that pair. Report it for explicit review.
Reviewer-assigned legacy cards receive separate case-by-case recovery; they are
not transformed into implementation cards by heuristic.

## 9. Health and Observability

Each router/actuator health snapshot contains:

- Blocked count by structured kind and age bucket.
- Active handoffs by phase and oldest handoff age.
- Eligible, actioned, retried, escalated, and deferred counts.
- Deterministic mutations over the last 24 hours.
- Unacknowledged human escalations.
- `ownerless_blocked_count` and `multiply_owned_blocked_count`.
- Gateway PID/start time and installed-code drift status.

No-op ticks remain silent. Runtime errors, malformed state, ownerless cards,
multiple ownership, or a stale completion failure exit nonzero and produce a
compact alert. Backlog size by itself is not failure because human-only blocks
may remain legitimately open.

The existing gateway restart watchdog gains a change-gated
`restart_required` alert when commits touching loaded gateway/Kanban runtime
paths are newer than the gateway process. It continues to report PID changes
and downtime.

## 10. Safety and Error Handling

- All board mutations use supported `hermes kanban` CLI verbs.
- Every mutation uses a fresh read immediately before execution.
- Reviewer creation remains idempotency-keyed.
- Parent completion always traverses the existing goal judge.
- Reviewer rejection and missing evidence fail closed.
- Completion rejection is signature-cached to prevent per-minute retries.
- Transient retries keep the existing budget and anti-stale checks.
- `needs_input` and `capability` are never automatically unblocked.
- Existing actuator signature gates and max-mutation anomaly cap remain.
- The two diagnostic `w` jobs stay paused.

## 11. Testing Strategy

Implementation follows red-green-refactor.

Local review-router behavioral tests cover:

- Creating an unparented, immediately runnable reviewer lane.
- Recovering the same lane through its idempotency key after state loss.
- Leaving the implementation blocked while review is pending.
- Completing through the goal judge only after a reviewer finishes with usable
  evidence.
- Caching a judge rejection until evidence changes.
- Preserving and surfacing reviewer rejection.
- Failing closed on malformed state, partial reads, or stale preconditions.
- Exactly-one-owner census behavior.
- High-confidence typed migration and ambiguous legacy retention.

Core regression tests cover unlink/recompute behavior, typed block routing,
goal completion, dispatcher recovery, and session attribution. Tests execute
through `scripts/run_tests.sh`; local automation tests run in the installed
Hermes virtual environment with an isolated temporary home/board.

Live verification uses shadow mode before any cron resume and inspects actual
CLI behavior rather than source text.

## 12. Guarded Rollout

1. Capture board, state, jobs, gateway PID/start time, and blocked census.
2. Implement and pass local automation tests.
3. Pass focused core Kanban/gateway regression suites.
4. Restart the gateway so installed code is loaded.
5. Run `kanban-blocked-watch` once with review delegation and self-heal both in
   shadow mode.
6. Repair guarded existing dependency cycles and verify reviewer readiness.
7. Resume only `kanban-blocked-watch`.
8. Observe at least two completed ticks with no repeated judge error, duplicate
   lane, wrong-session attribution, or ownership violation.
9. Apply the high-confidence legacy block migration and publish ambiguous
   residue.
10. Verify scheduler state, blocked ownership, handoff phases, mutation counts,
    and delivered health output.

Rollback pauses the watcher and restores its state snapshots. Board recovery
uses the pre-rollout online backup only if guarded CLI reversal cannot restore
the exact prior lifecycle.

## 13. Success Criteria

- `kanban-blocked-watch` is active and has completed at least two clean ticks.
- The running gateway contains current installed Kanban recovery code.
- No review lane is dependency-gated by the implementation card it must review.
- Reviewer approval can automatically complete an implementation card only via
  the goal judge.
- Reviewer rejection never completes or unblocks the implementation card.
- Every blocked card has exactly one active owner.
- Typed block coverage increases without guessing ambiguous legacy reasons.
- Health output distinguishes a healthy no-op from ineffective or ownerless
  automation.
