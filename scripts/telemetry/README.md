# Hermes self-improvement telemetry scripts

This directory tracks the self-improvement telemetry/proposal automation scripts that are deployed under `$HERMES_HOME/scripts/telemetry` on Chad's Hermes host.

## Why this lives under `scripts/telemetry`

The repository already has a top-level `scripts/` directory for auxiliary operational and developer scripts, and there was no existing tracked telemetry package or app surface. Consolidating the live telemetry helpers here avoids creating another top-level path while keeping the whole self-improvement telemetry subsystem together:

- telemetry DB initialization and schema migration
- kanban-to-telemetry sync and daily metrics
- readiness/audit/scoring helpers
- proposal packet generation, decision capture, dry-run digest, and approved-proposal apply helpers
- script-level regression tests for those helpers

## Deployment/source-of-truth convention

Tracked source of truth: `hermes-agent/scripts/telemetry/`

Live deployment path used by current cron/manual commands: `$HERMES_HOME/scripts/telemetry/`

When changing these scripts, update the tracked copy first, run the relevant script-level tests from this directory, then mirror the reviewed changes to the live deployment path. Current cron jobs still execute the live deployment path, so tracking alone does not redeploy a change.

## Safety posture

The production proposal digest remains dry-run/non-mutating. Mutable proposal decision/apply helpers are manual-command surfaces and must keep explicit human approval provenance, backup, idempotency, and targeted tests.

## Proposal outcome-loop reconciliation (Phase 4)

`reconcile_proposal_outcomes.py` links applied proposals to their `proposal_apply_audit.kanban_task_id` and reconciles `proposals.status/outcome/verified_at/scored_at` when linked Kanban tasks reach terminal states.

- default mode: dry-run only (plan output, no DB mutation)
- execute mode: requires explicit `--operator --source --reason`
- execute mode safety: pre/post `PRAGMA quick_check`, execute backups for `experiments.db` and `kanban.db`, idempotent guarded updates, append-only `proposal_outcome_audit` transition evidence
- stale detection: missing linked task marks proposal `stale/needs_attention`

Examples:

- Dry-run scan of applied proposals:
  - `python3 scripts/telemetry/reconcile_proposal_outcomes.py --json`
- Execute one proposal reconciliation:
  - `python3 scripts/telemetry/reconcile_proposal_outcomes.py --proposal-id proposal:example --execute --operator "Chad Tao" --source "slack:thread-123" --reason "Phase 4 outcome reconciliation" --json`

Note: this card does not create or schedule watchdog cron jobs. Scheduling and cadence are intentionally deferred to later ops coordination.
