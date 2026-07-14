# Just-Do-It Blocked PR Re-review Watchdog

## Goal

Automatically send a previously blocked, Slack-originated pull request back to Boris when its GitHub head changes, without requiring the author to repost the PR in `#just-do-it`.

The initial scope is deliberately narrow: only completed reviewer lanes whose structured metadata records `CHANGES_REQUESTED` or `NEEDS-FIX` qualify. A head change after an approval or non-blocking review does not trigger another review.

## Current behavior and gap

`just-do-it-channel-watch` polls Slack for new top-level human messages. A PR link in a new message wakes Jensen, which can create a read-only Boris lane. Once that lane finishes, the channel watcher has no durable GitHub lifecycle loop.

PR `Ylopo/npm-ylopo-models-bookshelf#696` demonstrated the gap:

1. Slack created reviewer lane `t_b2eb8405` for head `4455c6d`.
2. Boris submitted `CHANGES_REQUESTED` and recorded `NEEDS-FIX` plus structured findings.
3. The author pushed head `c40fb1d`.
4. No automation noticed the new head.
5. A second Slack post created lane `t_fc423563`, which approved the new head.

## Architecture

Add a separate no-agent cron script named `just_do_it_blocked_review_requeue_watchdog.py`. Keeping this separate from Slack ingestion and reaction delivery gives each loop one responsibility:

- `slack_channel_watch.py`: ingest new human Slack requests.
- blocked-review requeue watchdog: observe GitHub head changes after blocking Boris verdicts.
- `just_do_it_review_reaction_watchdog.py`: acknowledge successful approvals in Slack.

Run the new watchdog every 10 minutes. It should be quiet on no-op and print compact output only for dispatches or actionable failures.

## Candidate discovery

On each tick:

1. List recent completed Kanban tasks assigned to `reviewer`.
2. Inspect task runs newest-first and select a run only when its structured metadata contains:
   - a canonical GitHub PR URL;
   - the exact reviewed head SHA;
   - a blocking outcome represented by `review_verdict = NEEDS-FIX`, a GitHub review state of `CHANGES_REQUESTED`, or an equivalent structured blocking verdict;
   - at least one blocker/finding, or an explicit blocking verdict;
   - evidence that the lane originated from a watched Slack message, preferably `source_slack_permalink` in run metadata or the task body.
3. Ignore approved, informational, failed-infrastructure, incomplete, or unstructured historical lanes.
4. Bound discovery to a configurable lookback, initially seven days.

The script must use deterministic metadata inspection. It must not ask an agent to infer whether an old review was blocking.

## Live GitHub check

Use `/Users/ctao/.hermes/scripts/pr/ghx.sh` for all GitHub calls. For each candidate, fetch the live PR state, author, head repository owner, head SHA, and draft status.

Dispatch is allowed only when:

- the PR is still open;
- the live head SHA differs from the blocked reviewed SHA;
- the live head is non-empty and can be verified;
- no existing reviewer lane already targets the same canonical PR URL and live head SHA.

A draft may be re-reviewed because the requested behavior is review readiness, not merge readiness. The re-review task must record that it was draft at dispatch time.

GitHub authentication, network, malformed metadata, or ambiguous ownership failures produce a compact failure and no dispatch.

## Re-review lane

Create one Kanban task assigned to the canonical `reviewer` role. The body must include:

- canonical PR URL;
- old reviewed head and new live head;
- live author, head owner, state, and draft status;
- original blocking findings and recommended fixes;
- the original source Slack permalink;
- instructions to verify the new head still matches before submitting a review;
- duplicate-approval protection;
- structured completion metadata requirements;
- the third-party read-only safety gate.

For a PR not authored by `Chadtao206`, Boris may inspect, test, and submit a GitHub review as Chad, but must not edit code or PR metadata, update the branch, push, commit, or create an engineer/remediation lane. A remaining blocker is reported to the author; it is never auto-fixed.

## Idempotency and state

Persist state in `~/.hermes/state/just_do_it_blocked_review_requeue.json`.

Use `canonical PR URL + live head SHA` as the dispatch key. Before creation, check both:

1. the state ledger; and
2. live Kanban tasks for a reviewer lane containing that PR and target head.

Record a ledger entry only after task creation succeeds and the created task can be read back. If creation succeeds but verification is inconclusive, the next tick must discover the existing lane before attempting another create.

Limit each tick to two dispatches. Retry transient GitHub or Kanban failures no sooner than one hour. A head that changes again becomes a new key and may receive a new re-review lane.

## Slack behavior

The requeue watchdog does not post or react in `#just-do-it`. It preserves the original source permalink in the new lane. If Boris approves the new head, the existing reaction watchdog adds the check-mark reaction to that original request once Slack authorization permits it.

No new Slack repost, GitHub mention, or comment is required to trigger the re-review.

## Error handling

- No candidates or unchanged heads: exit 0 with empty stdout.
- Candidate skipped because a target-head lane already exists: update the local ledger if necessary, then remain silent.
- Transient GitHub/Kanban failure: persist bounded retry state, emit one compact failure when attempted, and exit 1.
- Invalid or incomplete historical metadata: skip safely; do not manufacture a task.
- Task creation or read-back failure: emit the PR, target head, and exact failing boundary; exit 1.

## Tests

Unit tests should cover:

- recognizing `NEEDS-FIX` and `CHANGES_REQUESTED` metadata;
- rejecting approved, non-blocking, infrastructure-failed, and unstructured runs;
- extracting and canonicalizing PR URLs and reviewed SHAs;
- triggering only when the live head changes;
- ignoring closed PRs;
- preserving third-party read-only instructions;
- deduplicating by PR and target head across both ledger and live Kanban tasks;
- allowing a second re-review when the head changes again;
- retry backoff and the two-dispatch-per-tick cap;
- quiet no-op behavior;
- reporting GitHub, Kanban creation, and read-back failures.

An integration-style test should use fixture Kanban JSON plus a fake `ghx.sh` response to prove that a blocked old head produces exactly one reviewer task for a new head.

## Success criteria

1. A Slack-originated Boris `CHANGES_REQUESTED`/`NEEDS-FIX` lane is automatically requeued once when the PR receives a different head SHA.
2. Repeated ticks do not duplicate the re-review lane for the same PR and head.
3. Approved or merely reviewed PRs are not monitored for arbitrary future changes.
4. Third-party PRs remain strictly read-only outside GitHub review submission.
5. The original Slack request need not be reposted, mentioned, or otherwise changed.
6. No-op ticks are silent; actionable failures are bounded and diagnosable.

## Out of scope

- Automatically fixing third-party PRs.
- Monitoring every previously reviewed PR.
- Re-reviewing after comment-only activity or GitHub mentions without a head change.
- Posting reminders or status messages into `#just-do-it`.
- Replacing the existing approval-reaction watchdog.
