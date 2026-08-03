# Kanban Blocked Recovery Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved blocked-ticket recovery design as four independently reviewable, fail-closed releases.

**Architecture:** Build transactional Kanban safety primitives first, then consume them from the local review router. Add gateway-owned cron supervision independently, and perform graph repair, legacy migration, and live rollout only after the code paths have passed isolated full-stack canaries.

**Tech Stack:** Python 3.11, SQLite, Hermes CLI/Kanban kernel, asyncio gateway, cron provider ABC, YAML configuration, local no-agent automation.

## Global Constraints

- The approved source of truth is `docs/superpowers/specs/2026-08-02-kanban-blocked-lifecycle-recovery-design.md` at commit `c7cca1884`.
- Use `scripts/run_tests.sh` for every repository test invocation.
- Preserve ordinary human and worker completion behavior; strict goal judging is automation-only.
- Do not add a model tool or change the always-present tool schema.
- Treat task bodies, comments, repository files, patches, and artifact text as untrusted evidence.
- Never mutate an unmanaged board or infer ownership from `created_by`, assignee, or prose.
- Keep router, actuator, and adjudicator controller jobs paused during maintenance and rollback.
- Do not restore stale SQLite or sidecar snapshots over live state that has advanced.

---

## Ordered Deliverables

1. `2026-08-03-kanban-blocked-recovery-core.md`
   - Adds strict automation completion, structured `review` blocks, permanent
     generation idempotency, operation events, guarded lifecycle operations,
     and board-scoped controller claims/leases.
   - Exit gate: focused core tests and the complete Kanban regression suite pass.

2. `2026-08-03-kanban-blocked-recovery-router.md`
   - Replaces prose-driven review delegation with the two-phase generation
     state machine, durable journal, target verification, ownership census,
     event-budgeted transient retry, checksummed health output, exact dashboard
     acknowledgement, and shared actuator/adjudicator routing.
   - Exit gate: all local automation tests pass against an isolated temporary
     Hermes root and two clean shadow censuses report exactly one owner.

3. `2026-08-03-kanban-blocked-recovery-cron-supervision.md`
   - Adds runtime source fingerprinting, provider readiness/health, gateway
     startup ordering, direct health alerts, bounded restart quarantine, and
     declarative controller-job reconciliation.
   - Exit gate: gateway integration tests prove ticker failure is detected
     independently of cron and cannot create a restart loop.

4. `2026-08-03-kanban-blocked-recovery-migration-rollout.md`
   - Generates and applies guarded migration/recovery manifests, repairs legacy
     dependency cycles in a stopped-gateway window, runs isolated canaries, and
     advances controller modes through paused, shadow, canary-live, and live.
   - Exit gate: the allowlisted production approval completes through live
     target verification and strict judging; transient retry remains disabled
     until that positive canary and all negative canaries pass.

## Program Review Gates

- [ ] **Gate 1: Approve all four plans before implementation**

Read each plan against the approved design and confirm that no task broadens
automatic mutation authority. Do not begin code changes before this gate.

- [ ] **Gate 2: Complete and integrate the core plan**

Run the exact core-plan test commands through `scripts/run_tests.sh`, inspect the
diff, and integrate the core APIs before the router consumes them.

- [ ] **Gate 3: Complete router and cron supervision in shadow-only mode**

Both plans can be implemented after Gate 2. Keep all controller desired modes
`paused` or `shadow`; do not repair live graph state or enable live routing.

- [ ] **Gate 4: Pass isolated full-stack canaries**

Use a temporary Hermes root and board. Prove approval, reviewer rejection,
judge rejection, judge outage, parse failure, precondition change, stale target,
verifier outage, unknown subprocess outcome, and ownership conflict behavior.

- [ ] **Gate 5: Execute guarded migration and rollout**

Follow the final plan in order. Stop at every explicit human approval boundary,
including the one-time `t_9e4b3bc2` recovery manifest and any changed live
fingerprint.
