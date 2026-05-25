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
