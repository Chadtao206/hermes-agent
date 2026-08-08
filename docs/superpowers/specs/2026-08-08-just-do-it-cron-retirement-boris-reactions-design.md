# Just-Do-It CRON Retirement and Boris Reaction Migration Design

**Date:** 2026-08-08
**Status:** Approved; implementation plan committed
**Implementation plan:** `docs/superpowers/plans/2026-08-08-just-do-it-cron-retirement-boris-reactions.md`
**Scope:** Fully retire the two legacy just-do-it lane-producing CRONs and
adopt a Boris-channel-specific reaction vocabulary without changing review
authorization, review execution, or Kanban lifecycle ownership.

## Objective

The on-demand Boris Slack intake has run reliably long enough to become the
only team-triggered Boris review intake. The legacy just-do-it channel watcher
and automatic blocked-review requeue watchdog must stop producing work and be
removed from the live scheduler. Their completed work and state remain audit
history.

The retained reaction reconciler must project Boris review state using the
following vocabulary in `#boris-reviews`:

| Semantic state | Reaction |
|---|---|
| Review active | `:rubbing:` |
| Non-blocking review notes | `:doge_notes:` |
| Blocking/not-ready notes | `:face_vomiting:` |
| Approved exact PR head | `:blinkapproved:` |

The precedence remains:

```text
blocked > notes > active > approved
```

## Verified runtime baseline

The read-only inventory on 2026-08-07 found:

| Job ID | Name | Schedule | State | Desired state |
|---|---|---|---|---|
| `ee4582d0598f` | `just-do-it-channel-watch` | every two minutes | enabled and firing | retire |
| `ff865c33c5ea` | `just-do-it-blocked-review-requeue-watchdog` | `*/2 * * * *` | enabled and firing | retire |
| `6daa151da351` | `just-do-it-review-reaction-state-watchdog` | `* * * * *` | enabled and firing | retain and rename display metadata |

The Boris launchd service was running in `live` mode against only
`#boris-reviews` (`C0BPCKPEFCG`). Its intake, dispatch, exact-head deduplication,
and heartbeat were healthy.

Board and reaction state at inventory time:

- all 12 lanes created by the blocked-review requeue watchdog were terminal
  (`done` or `archived`);
- all 31 lanes created by `boris-slack-intake` were terminal;
- no active source-linked just-do-it or Boris lane existed;
- the reaction ledger contained no unresolved holds; and
- 30 Boris-channel posts were tracked: 24 `review-no`, five `doge_notes`, and
  one `thinkies`.

Those counts are observations, not migration constants. The rollout must take
a fresh census and operate on the live set it finds.

The custom emoji lookup confirmed `rubbing`, `doge_notes`, and
`blinkapproved` exist in the Ylopo workspace. `face_vomiting` is a standard
Slack emoji and therefore is not returned by `emoji.list`.

## Decisions

1. Boris Slack intake is the exclusive creator of team-triggered Boris review
   lanes.
2. `ee4582d0598f` and `ff865c33c5ea` are paused, proven quiescent, then removed
   from the live registry. Merely setting `enabled=false` is not full
   retirement.
3. The retired executable entrypoints and tests are removed from active
   deployment paths after an exact recoverable archive is created.
4. Completed/archived lanes, CRON execution output, the three channel-watch
   watermarks, and the blocked-review requeue ledger remain immutable audit
   history. The reaction ledger changes only for the approved Boris-channel
   migration. No task or legacy-state deletion is part of this rollout.
5. Job `6daa151da351` remains enabled under the display name
   `boris-review-reaction-state-watchdog`. Its stable ID and compatible script
   path remain unchanged.
6. The new reaction vocabulary applies only to source permalinks in
   `#boris-reviews`. Historical reactions in the three legacy watched channels
   are not rewritten.
7. Existing tracked Boris-channel posts are migrated in bounded batches.
8. Reaction classification remains independent of reaction rendering. Emoji
   names are presentation values, not review-state identifiers.
9. The retained reconciler remains read-only toward Kanban. It may write Slack
   reactions and its private cursor/backup/telemetry state only.
10. Boris authorization, normalized commands, exact-head deduplication,
    static-review security, review template, COMMENT-only GitHub policy,
    catch-up, rate limits, and the exact
    `Source Slack message: <permalink>` contract do not change.

## Rejected approaches

### Global emoji replacement

Replacing the four emoji constants globally is mechanically simple, but it
would reinterpret or rewrite historical posts from `#just-do-it`,
`#maverickre-eng-infra`, and `#eng-midnight-vulcan`. Those channels are being
retired as intake sources, so changing their history provides no benefit and
creates unnecessary Slack writes.

### A second Boris-only reaction watchdog

A new job and state ledger would produce clean naming, but the cutover would
temporarily create two writers for the same Boris posts. Cursor transfer,
reaction ownership, and rollback would become more complex. The existing
watchdog already has the necessary exact permalink boundary and hardened
reconciliation behavior.

### Disable without removal

Leaving the two jobs disabled in the live registry is useful as an initial
quiescence step, but it does not meet the requested full retirement. It leaves
an easy accidental resume path and continues to present deprecated automation
as installed behavior.

## Controller boundaries

### Boris intake

For each accepted exact-head review command, Boris intake creates or reuses the
deduplicated reviewer lane, persists its intake state, and best-effort adds
`:rubbing:` to the source post. It does not write any later reaction state.

Changing the initial reaction is the only deliberate Boris intake behavior
change. Intake must continue to create the lane before attempting the reaction;
a Slack failure never cancels or blocks the review.

### Retained reaction watchdog

The watchdog reads reviewer lanes containing the exact source-permalink
contract and classifies each lane into one stable semantic state:

- `active`;
- `notes`;
- `blocked`; or
- `approved`.

It aggregates semantic state using the existing precedence, then selects a
reaction scheme from the permalink channel:

| Source channel | Active | Notes | Blocked | Approved |
|---|---|---|---|---|
| `C0BPCKPEFCG` (`#boris-reviews`) | `rubbing` | `doge_notes` | `face_vomiting` | `blinkapproved` |
| Legacy watched channels | `thinkies` | `doge_notes` | `review-no` | `white_check_mark` |

The watchdog owns both the old and new Boris reaction names while migrating a
Boris post. For a legacy-channel post it owns only the legacy set. This
channel-scoped ownership prevents migration code from removing a manually
added modern emoji in a historical channel.

Every transition keeps the existing add-before-remove invariant:

1. add the desired reaction;
2. if the add succeeds or Slack reports it already exists, remove stale owned
   reactions in deterministic order; and
3. persist the new applied state only after the Slack operations succeed.

If the add fails, the previous reaction remains and the transition is retried.
If a stale-reaction removal fails, the desired reaction remains and the
pending removal is persisted for retry. A reaction failure never changes a
Kanban task, requeues a review, or changes a GitHub review.

### Retired jobs

After retirement, no installed scheduler job may invoke
`slack_channel_watch.py` or
`just_do_it_blocked_review_requeue_watchdog.py`. No retained source may expose
an automatic same-head or changed-head Boris requeue path. A later authorized
request for a new PR head remains a new on-demand Boris lane; same-head recovery
remains an explicit generic Kanban operation.

## State and migration

The reaction ledger currently persists rendered emoji names. The
implementation must normalize those names into semantic state in memory at its
read boundary, then render through the channel-specific scheme. The rollout
does not globally rewrite the ledger: only a Boris post that is migrated is
persisted with the updated representation. Legacy-channel records remain
unchanged by this migration. This keeps old ledgers readable and prevents a
future display change from altering classification or precedence.

The compatibility normalization and each Boris-record update are idempotent:

- legacy `thinkies` becomes semantic `active`;
- `doge_notes` becomes `notes`;
- `review-no` becomes `blocked`;
- `white_check_mark` becomes `approved`;
- the three new Boris-only names normalize to their corresponding semantic
  states; and
- unknown values are quarantined under the existing fail-closed behavior.

The migration command targets only `C0BPCKPEFCG` and runs bounded passes using
the existing show and reaction caps. It continues until every eligible tracked
Boris post is converged and a second pass performs zero Slack writes. It must
not use a hardcoded count of 30.

The watchdog exposes an operator-only reverse rendering mode for rollback. A
reverse pass renders Boris posts with the legacy scheme while still
recognizing and removing the modern Boris reactions. The scheduled default is
always the modern scheme after rollout.

No migration touches:

- completed or archived task rows;
- the blocked-review requeue ledger;
- legacy-channel reaction records or Slack posts;
- Boris intake deduplication, audit, watermark, or lane tables; or
- the exact source permalink stored in a task body.

## Full-retirement rollout

### 1. Preflight and backup

Under the CRON registry lock or supported CLI mutation surface:

- require the explicit immutable recovery commit
  `7f5bc40da3f97e181bfc00ba1688cd0f065ff10a` as an exact full commit ID and
  ancestor of current `HEAD`, never a symbolic/short ref or history-search
  fallback;
- require every durable recovery path at that pin, persist its exact blob/tree
  object ID, and permit only the two Task 6-deleted requeue paths to be absent
  from current `HEAD`;
- require retained reaction and Boris durable objects at current `HEAD` to be
  identical to the pin, and reject uncommitted changes anywhere in the durable
  path set;
- resolve all three exact IDs and verify their expected names, schedules,
  script paths, enabled state, and fingerprints;
- record current executions and prove whether any invocation is claimed or
  running;
- back up `jobs.json`, the three live scripts and tests, durable repository
  copies, CRON output, every channel-watch watermark, the blocked-review
  requeue ledger, the reaction ledger, and the Boris intake
  source/state/config needed to prove non-regression; and
- record cryptographic hashes and file metadata in a timestamped retirement
  manifest.

Backups live outside active script lookup paths. They are recoverable artifacts,
not runnable fallbacks.

### 2. Quiesce lane producers

Pause `ee4582d0598f` and `ff865c33c5ea` by exact ID. Wait at least one maximum
schedule interval (two minutes) plus a bounded grace period. During that window:

- neither job may acquire a new execution;
- any execution claimed before the pause must finish and be recorded;
- no relevant process may remain alive; and
- no new task may appear with deprecated creator or idempotency provenance.

If any condition fails, remain paused, diagnose, and do not remove registry or
source artifacts.

### 3. Remove registry and source entrypoints

After quiescence proof:

- remove both exact job IDs from the live registry;
- archive then remove `slack_channel_watch.py` and its active test from the
  live script path;
- archive then remove the blocked-review requeue script and test from the live
  path; and
- remove the blocked-review requeue source and test from the durable deployment
  source so a later install cannot resurrect it.

Historical job output, task rows, and ledgers remain in place. Documentation
may point to the retirement manifest for provenance.

### 4. Deploy the retained reaction change

Run focused and compatibility tests against staged source first. Briefly pause
job `6daa151da351` only for the atomic script/state installation, then resume it
under the same ID with:

- display name `boris-review-reaction-state-watchdog`;
- the updated Boris/legacy rendering schemes;
- prompt text describing
  `face_vomiting > doge_notes > rubbing > blinkapproved` for Boris; and
- the same one-minute schedule and no-agent execution mode.

The pause is an installation lock, not a retired state. The watchdog must be
enabled again before Boris intake is restarted.

### 5. Migrate Boris-channel reactions

Take a fresh census, run bounded modern-scheme passes for only
`C0BPCKPEFCG`, and verify:

- every eligible post has exactly one desired owned reaction;
- old Boris reactions are removed only after the desired reaction exists;
- failures remain queued rather than being marked converged;
- the state ledger remains valid across a process restart; and
- a second pass performs zero Slack writes.

### 6. Switch Boris initial reaction

Install the tested Boris intake source that adds `:rubbing:` and restart the
launchd service. The restart must preserve Socket Mode reconnect and catch-up.
Verify the service PID, heartbeat advancement, audit/state readability, and
unchanged intake configuration. Do not synthesize a production review request
merely to obtain a live reaction; the first natural accepted request is covered
by the deployed contract and dry-run/isolated execution tests.

### 7. Post-cutover proof

Wait another deprecated-job schedule interval plus grace, then prove:

- the two retired IDs remain absent;
- neither has a post-cutoff execution;
- their active script paths are absent;
- no new deprecated-provenance lane exists;
- `6daa151da351` remains enabled and healthy under its Boris display name;
- the Boris daemon and heartbeat remain healthy; and
- reaction migration remains settled and idempotent.

## Rollback

Rollback is ordered to avoid re-enabling a job against missing or incompatible
source:

1. stop new rollout mutations and preserve failure evidence;
2. while the modern watchdog is still installed, run its bounded reverse
   rendering pass for Boris posts and prove convergence;
3. restore the exact backed-up reaction script, ledger, Boris intake source,
   and job metadata, then restart/verify Boris intake and the retained job;
4. restore and verify the exact archived channel-watch and blocked-requeue
   source bundles before restoring either deprecated registry record; and
5. restore the exact backed-up jobs only if rollback requires the old intake,
   then prove their scripts import and fingerprints match before resuming.

A partial rollback must never leave duplicate old/new owned reactions or
re-enable the blocked-review watchdog against incompatible source.

## Testing strategy

### Reaction behavior

- semantic classification and precedence remain unchanged;
- Boris permalinks render the four modern reactions;
- legacy permalinks render the legacy reactions;
- mixed-channel inputs cannot cross schemes;
- active intake adds only `rubbing` and remains best-effort;
- desired reactions are added before stale ones are removed;
- failed adds retain the prior reaction and retry;
- failed removals persist and retry;
- old and new persisted names normalize idempotently;
- modern and reverse migration passes converge and become zero-write no-ops;
- quarantine, unresolved holds, compaction, and restart behavior remain
  fail-closed; and
- the retained job performs no Kanban create or lifecycle mutation.

### Boris compatibility

Run the existing Boris suite with only the intentional initial-reaction
assertions changed. Keep regression gates for:

- channel and employee authorization;
- typed grammar and untrusted-text elimination;
- canonical GitHub resolution and exact-head deduplication;
- rate/concurrency limits;
- static read-only terminal guard;
- COMMENT-only review behavior;
- lane template and exact source permalink;
- catch-up and watermark behavior; and
- dispatch and audit persistence.

### Retirement behavior

Use isolated registry, execution DB, board, state, and script fixtures to prove:

- exact-ID pause and remove operations cannot target the retained job;
- a pre-pause in-flight execution is allowed to finish but a post-pause one
  cannot start;
- source removal occurs only after quiescence proof;
- deprecated entrypoints cannot create a lane after removal;
- audit history is retained; and
- restore-before-resume rollback ordering is enforced.

Repository tests run through `scripts/run_tests.sh` with isolated `HOME` and
`HERMES_HOME`. Tests must never read or write the live Slack workspace, board,
CRON registry, or default Hermes state.

## Acceptance criteria

The rollout is complete when all of the following are true:

1. `ee4582d0598f` and `ff865c33c5ea` are absent from the live registry and
   have no post-cutoff execution.
2. Their active executable entrypoints are absent and preserved only in the
   timestamped retirement archive.
3. No new lane can be created by either retired path.
4. Historical tasks, outputs, and ledgers remain readable and unchanged except
   for the explicitly migrated Boris reaction ledger.
5. Job `6daa151da351` remains enabled every minute under its Boris-oriented
   display name and has no Kanban write authority.
6. Boris posts use `rubbing`, `doge_notes`, `face_vomiting`, and
   `blinkapproved` with the original semantic precedence.
7. Existing eligible Boris posts are migrated; legacy-channel posts are not.
8. A settled migration pass performs zero Slack writes.
9. Boris intake remains healthy and unchanged outside its initial reaction.
10. The backed-up rollback path has passed an isolated rehearsal.

## Human intervention

The rollout is a one-time operator action. After completion, ordinary on-demand
Boris review and reaction convergence require no human intervention. Human
action remains necessary only for explicit same-head review recovery, existing
watchdog quarantine/hold escalation, or a deliberate rollback.

## Out of scope

- Changing who may request Boris reviews.
- Adding automatic Boris retries or changed-head discovery.
- Changing Boris review execution, GitHub permissions, or verdict semantics.
- Replacing the retained reaction watchdog with a new service.
- Deleting historical Kanban tasks, CRON output, or legacy state ledgers.
- Rewriting reactions in the retired just-do-it source channels.
