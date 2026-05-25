# Phase 3A approved-proposal apply pilot: ops safety envelope

## Objective
Approve or block the smallest safe Phase 3A pilot that turns one explicitly approved self-improvement proposal into a durable follow-through lane without introducing automatic source/runtime mutation or dashboard write controls.

## Current live state

Observed 2026-05-25 from the real default-profile Hermes home (`HOME=/Users/ctao`, `HERMES_HOME=/Users/ctao/.hermes`):

- `experiments.db` exists at `/Users/ctao/.hermes/telemetry/experiments.db` and `PRAGMA quick_check` returns `ok`.
- `kanban.db` exists at `/Users/ctao/.hermes/kanban.db` and `PRAGMA quick_check` returns `ok`.
- `experiments.db` proposal tables are structurally present but empty:
  - `proposals = 0`
  - `proposal_decision_audit = 0`
  - `proposal_evidence_links = 0`
  - `schema_meta.schema_version = 3`
- Telemetry proposal artifacts already exist under `/Users/ctao/.hermes/telemetry/proposals`:
  - `11` `*.row.json` proposal rows
  - matching packet `.json` / `.md` artifacts
  - all sampled rows are currently `status=proposed`
- Digest state at `/Users/ctao/.hermes/state/self_improvement_proposal_digest.json` currently says:
  - `overall_verdict = NOT_COMPLETE`
  - `proposal_count = 11`
  - `suppressed_count = 1`
  - `updated_at = 2026-05-25T07:10:22+00:00`
- Existing human approval capture artifact exists at `/Users/ctao/.hermes/telemetry/proposals/human_approvals/2026-05-24_approve-phase2-structured-capture-and-kanban-durability.json`.
- Existing mutable decision-capture helper exists at `/Users/ctao/.hermes/scripts/telemetry/capture_proposal_decision.py`.
  - It runs `quick_check`, takes an `experiments.db` backup, opens a `BEGIN IMMEDIATE` transaction, imports a missing proposal row from a packet if needed, updates only proposal-ledger tables, and writes `proposal_decision_audit.backup_path`.
  - Its declared safety is `ledger_only_no_implementation_runtime_cron_or_kanban_mutation`.
- Current digest cron remains dry-run only:
  - job `2f1788c594bd` / `self-improvement-proposal-dry-run-digest`
  - schedule `0 22 * * *`
  - script `/Users/ctao/.hermes/scripts/telemetry/cron_generate_proposals_digest.py`
  - that script explicitly fails if proposal-table counts change across the run.
- Dashboard process is alive on `127.0.0.1:9119` and the proposal API responds `401 Unauthorized` when unauthenticated, which is consistent with a live but auth-protected dashboard.
- `/proposals` remains read-only in code:
  - backend path is `GET /api/control-center/proposals`
  - frontend copy says approval/deny remains in Slack and action controls are intentionally omitted.

## Operational reading of the empty ledger
The empty `proposals` / `proposal_decision_audit` tables are not a corruption signal.

They are the expected result of the current production posture:
- proposal generation writes packet artifacts to `telemetry/proposals`
- nightly digest stays `--dry-run`
- proposal ledger rows are only created once a human decision is explicitly captured

That means Phase 3A must treat packet artifacts as the pre-decision source of truth and the ledger as the post-decision/apply state overlay.

## Coexistence assessment: dry-run digest + approved-proposal apply helper
Safe coexistence is possible, with one important boundary:

1. Safe today
   - packet generation remains file-based
   - decision capture imports approved rows into the ledger on demand
   - dashboard reads packet rows first, then overlays ledger status/audit
   - nightly digest verifies that dry-run did not mutate the ledger

2. Unsafe if the generator is later switched to mutating mode without further work
   - `generate_proposals.py::persist_proposals()` uses `INSERT ... ON CONFLICT(proposal_id) DO UPDATE`
   - that update path overwrites `status` and packet-derived fields from fresh packet output
   - it does not preserve approved/applied state as a protected overlay
   - if someone enables non-dry-run generation later, approved/applied ledger state could be clobbered back toward `proposed`

Conclusion:
- Phase 3A is safe only if the digest/generator path stays dry-run.
- Enabling generator persistence is out of scope and should remain forbidden until conflict behavior is made decision-aware.

## Allowed mutations for the first Phase 3A pilot
The first live pilot may mutate only the following, and only after explicit human approval has already been captured:

1. `kanban.db`
   - create at most one durable root task (or one tightly bounded lane) for the approved proposal
   - task creation must use deterministic `idempotency_key` values derived from `proposal_id`

2. `experiments.db`
   - read the approved proposal row
   - record application state for that same proposal after successful lane creation
   - minimally allowed row changes:
     - `status`: `approved` -> `applied` (recommended), or keep `approved` only if the implementation deliberately wants no new terminal status yet
     - `applied_at`
     - `updated_at`
   - do not alter unrelated proposal rows

3. audit/plan artifact files
   - write one apply-run manifest under telemetry (recommended)
   - write one human-readable apply-plan artifact if the lane is plan-first rather than multi-task-first

## Forbidden mutations for Phase 3A
These remain out of bounds:

- no mutation from proposal generation alone
- no auto-apply behavior triggered by the digest cron
- no dashboard approve/deny/apply buttons or pseudo-controls
- no direct source-code edits, runtime restarts, cron edits, DB repairs, or telemetry backfills as part of the apply helper itself
- no forced decision rewrites as part of apply
- no bulk/multi-proposal apply in the first pilot
- no generator-mode change from `--dry-run` to persistent ledger writes

## Required preflight gates before any live apply
The apply helper should refuse to proceed unless all of the following are true:

1. Human approval already exists in the ledger
   - required state: proposal row exists in `proposals`
   - required state: `status = 'approved'`
   - required state: `approved_at IS NOT NULL`
   - required state: `applied_at IS NULL`
   - do not let the apply helper perform packet import or decision capture itself; that remains a separate explicit step

2. Packet identity is still anchored
   - matching packet artifact exists under `/Users/ctao/.hermes/telemetry/proposals`
   - packet `proposal_id` matches the approved ledger row exactly

3. Storage health is clean
   - `PRAGMA quick_check` on both `experiments.db` and `kanban.db` returns `ok`

4. Backup manifest has been created before mutation
   - `experiments.db` backup
   - `kanban.db` backup
   - JSON manifest capturing proposal id, preflight row snapshot, planned idempotency keys, and timestamps

5. Dry-run preview succeeds first
   - the exact same helper must support `--dry-run`
   - dry-run output must show the proposed kanban root task/lane, assignee, title/body source, idempotency key(s), and the exact proposal-row before/after diff

## Recommended execution order for the live pilot
Because `experiments.db` and `kanban.db` are separate SQLite files, there is no single atomic cross-DB transaction. The safe order is therefore:

1. Preflight checks
2. Create both DB backups + manifest
3. Run dry-run preview and require it to match operator intent
4. Create the kanban root task/lane using deterministic `idempotency_key`
5. Persist an apply-run artifact/manifest that records created task ids
6. Update the proposal row in `experiments.db` to mark it applied
7. Re-run `quick_check` on both DBs
8. Verify dashboard-visible status overlay via the read-only proposal loader/API

Why this order:
- if kanban creation succeeds but ledger update fails, a re-run can safely converge using idempotency keys
- the opposite order is worse because the dashboard could show `applied` before a real execution lane exists

## Idempotency requirements
Mandatory for the first pilot.

Minimum contract:
- root kanban task must carry `idempotency_key = proposal-apply:<proposal_id>`
- if child tasks are created, each must derive deterministic keys from the same proposal id plus a stable suffix
- the helper must check for existing tasks/artifacts before creating new ones
- if `applied_at` is already set for the same proposal, the helper must exit as a no-op unless an explicit rollback/recovery mode is invoked

Acceptable first-pilot behavior:
- exactly one proposal per run
- exactly one root task created per proposal id
- repeated runs converge to the same task ids / same applied state rather than duplicating work

## Exact backup manifest requirements
Recommended path:
- `/Users/ctao/.hermes/telemetry/backups/proposal_applies/<timestamp>-<safe_proposal_id>/`

Required contents:
- `experiments.db.bak`
- `kanban.db.bak`
- `manifest.json` containing:
  - `proposal_id`
  - `approved_row_before`
  - `packet_paths`
  - `preflight_quick_check = {experiments_db: ok, kanban_db: ok}`
  - `planned_idempotency_keys`
  - `created_task_ids` (empty in dry-run)
  - `db_paths`
  - `operator_source`
  - `started_at` / `completed_at`

Note:
- `/Users/ctao/.hermes/telemetry/backups/proposal_decisions` does not exist yet in the live tree; first mutable runs will create backup directories on demand.

## Row-level before/after requirements
Before mutation, capture and log this exact row snapshot:
- `proposal_id`
- `status`
- `approved_at`
- `applied_at`
- `approver`
- `linked_experiment_id`
- `updated_at`

Expected first-pilot after-state:
- `status = 'applied'` (preferred because `/proposals` already has an `applied` filter)
- `approved_at` unchanged
- `applied_at = <utc timestamp>`
- `approver` unchanged
- `updated_at = <utc timestamp>`

Caveat:
- the current proposal overlay code reads `status`, `approved_at`, `denied_at`, `approver`, `denial_reason`, and `updated_at`, but not `applied_at`
- so `/proposals` can show `applied` status today, but it will not separately display the application timestamp unless that read-only projection is later expanded

## First-live-run verification checklist
A live Phase 3A apply is only complete if all checks below pass:

1. Preflight evidence saved
   - backup manifest directory exists
   - both DB backups exist

2. `kanban.db` outcome
   - exactly one root task exists for `idempotency_key = proposal-apply:<proposal_id>`
   - created task id(s) recorded in the manifest

3. `experiments.db` outcome
   - proposal row is still the same `proposal_id`
   - row shows approved -> applied transition (or approved + applied_at if that variant is chosen deliberately)

4. DB health
   - `PRAGMA quick_check` returns `ok` for both DBs after the run

5. Read-only visibility
   - proposal loader / `/proposals` reads the post-apply status without requiring any dashboard mutation path

6. Digest coexistence remains safe
   - no change to nightly dry-run job config
   - next digest run must continue to pass its no-ledger-mutation invariant

## Rollback path
The rollback path for the first pilot should be simple and explicit, not clever.

Primary rollback:
1. stop after the failed/suspect run
2. restore `experiments.db` from the apply-run backup
3. restore `kanban.db` from the apply-run backup
4. remove any apply artifact/manifest created after the backup if it is now misleading
5. re-run `PRAGMA quick_check` on both restored DBs

Why full backup restore is preferred for the first pilot:
- cross-DB partial success is the most likely failure shape
- deterministic full restore is safer than inventing a complex compensating transaction protocol in the first live run

## Monitoring / follow-up
Watch after rollout:
- nightly dry-run digest job `2f1788c594bd`
- proposal-engine watchdog `c4fecd02af57`
- dashboard proposal queue status overlays
- duplicate-task creation attempts in `kanban.db` via `idempotency_key`

Trigger immediate investigation if:
- digest reports unexpected proposal-table mutation
- a proposal returns from `applied`/`approved` to `proposed`
- duplicate kanban root tasks appear for the same proposal id
- either DB fails `quick_check`

## Decision
Ops approval: safe to proceed with a bounded Phase 3A pilot only if implementation stays inside this envelope.

The smallest safe implementation target is:
- separate explicit approval capture first
- one-proposal-at-a-time apply helper
- dry-run mode required
- deterministic kanban idempotency keys required
- dual-DB backups required
- no runtime/source mutation in the apply helper
- keep generator/digest dry-run-only

If any of those constraints are removed, Phase 3A should be re-reviewed before implementation proceeds.
