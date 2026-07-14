# Just-Do-It Blocked PR Re-review Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically create one read-only Boris re-review lane when a Slack-originated PR that previously received a structured blocking Boris verdict is pushed to a new head.

**Architecture:** Add one deterministic no-agent watchdog under `~/.hermes/scripts`, alongside the existing just-do-it reaction watchdog. It discovers structured blocked-review runs from Kanban, reads live PR state through `ghx.sh`, deduplicates by canonical PR URL and target head across both a state ledger and live Kanban, and creates a bounded read-only reviewer lane. A first-run baseline prevents historical backfill unless explicitly requested.

**Tech Stack:** Python 3.11 standard library, `unittest`, Hermes Kanban CLI JSON interface, `ghx.sh`, Hermes cron no-agent scheduler.

## Global Constraints

- Only completed reviewer lanes with structured `NEEDS-FIX` or `CHANGES_REQUESTED` outcomes qualify.
- Use `/Users/ctao/.hermes/scripts/pr/ghx.sh` for every GitHub call.
- Never edit, push, update, or create remediation work for a third-party PR; GitHub review submission is the only permitted PR write.
- Require `live_head != reviewed_head`, an open PR, and verified live metadata before dispatch.
- Deduplicate by canonical PR URL plus target head in both local state and live Kanban.
- Dispatch at most two lanes per tick and back failed boundaries off for one hour.
- Preserve the original source Slack permalink; never post into `#just-do-it` from this watchdog.
- Empty/no-op ticks produce no stdout and exit 0.
- The first live run establishes a completion baseline and does not backfill historical lanes; `--backfill-existing` is explicit and testable.

---

## File Structure

- Create `/Users/ctao/.hermes/scripts/just_do_it_blocked_review_requeue_watchdog.py`: candidate parsing, GitHub/Kanban adapters, idempotent dispatch loop, CLI, and state persistence.
- Create `/Users/ctao/.hermes/scripts/test_just_do_it_blocked_review_requeue_watchdog.py`: unit and integration-style fixture tests for every safety and idempotency rule.
- Create runtime state at `/Users/ctao/.hermes/state/just_do_it_blocked_review_requeue.json`: baseline, successful dispatch ledger, and retry ledger; created only by live execution.
- Modify `/Users/ctao/.hermes/cron/jobs.json` only through `hermes cron create`: register the 10-minute no-agent job.

### Task 1: Structured blocked-review candidate parsing

**Files:**
- Create: `/Users/ctao/.hermes/scripts/just_do_it_blocked_review_requeue_watchdog.py`
- Create: `/Users/ctao/.hermes/scripts/test_just_do_it_blocked_review_requeue_watchdog.py`

**Interfaces:**
- Produces: `canonical_pr_url(value: str) -> str | None`
- Produces: `blocked_candidate(show: dict[str, Any], allowed_channels: set[str]) -> dict[str, Any] | None`
- Candidate keys: `task_id`, `pr_url`, `reviewed_head`, `source_permalink`, `findings`, `completed_at`, `run_id`

- [ ] **Step 1: Write failing candidate-parser tests**

Add fixture builders and tests proving:

```python
def test_needs_fix_run_becomes_candidate():
    show = blocked_show(
        verdict="NEEDS-FIX",
        github_state="CHANGES_REQUESTED",
        reviewed_head="a" * 40,
        findings=[{"severity": "blocking", "issue": "wrong docs", "recommended_fix": "correct docs"}],
    )
    candidate = m.blocked_candidate(show, {"C08K8FQKX9S"})
    assert candidate["pr_url"] == "https://github.com/Ylopo/example/pull/42"
    assert candidate["reviewed_head"] == "a" * 40

def test_approved_run_is_not_candidate():
    assert m.blocked_candidate(approved_show(), {"C08K8FQKX9S"}) is None

def test_unstructured_or_non_slack_run_is_not_candidate():
    assert m.blocked_candidate(show_without_reviewed_sha(), {"C08K8FQKX9S"}) is None
    assert m.blocked_candidate(show_from_other_channel(), {"C08K8FQKX9S"}) is None
```

Cover metadata aliases already present in live tasks: `pr_url`, `reviewed_head_oid`, `reviewed_head_sha`, `reviewed_commit`, `review_verdict`, `github_review.state`, `github_review_state`, `findings`, `blockers`, `source_slack_permalink`, and `source_permalink`.

- [ ] **Step 2: Run the parser tests and verify RED**

Run:

```bash
cd /Users/ctao/.hermes/scripts
python3 -m unittest \
  test_just_do_it_blocked_review_requeue_watchdog.BlockedCandidateTest -v
```

Expected: import/file failure because the watchdog module does not exist.

- [ ] **Step 3: Implement the minimum parser**

Implement strict URL/SHA regexes and newest-run-first parsing. Accept a run only when it has a canonical PR URL, a 40-character reviewed SHA, an allowed Ylopo Slack permalink, and either `NEEDS-FIX` or `CHANGES_REQUESTED`. Normalize findings to JSON-serializable dictionaries/strings without inventing missing content.

Use these constants:

```python
DEFAULT_ALLOWED_CHANNELS = {"C08K8FQKX9S"}
PR_RE = re.compile(r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)")
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
PERMALINK_RE = re.compile(r"https://ylopo\.slack\.com/archives/(?P<channel>[A-Z0-9]+)/p\d{16}", re.I)
```

- [ ] **Step 4: Run parser tests and verify GREEN**

Run the command from Step 2. Expected: all `BlockedCandidateTest` tests pass.

### Task 2: Live PR checks, lane deduplication, and safe task construction

**Files:**
- Modify: `/Users/ctao/.hermes/scripts/just_do_it_blocked_review_requeue_watchdog.py`
- Modify: `/Users/ctao/.hermes/scripts/test_just_do_it_blocked_review_requeue_watchdog.py`

**Interfaces:**
- Consumes: candidate dictionaries from Task 1
- Produces: `fetch_live_pr(pr_url: str) -> dict[str, Any]`
- Produces: `target_lane_exists(tasks: list[dict[str, Any]], pr_url: str, target_head: str) -> str | None`
- Produces: `build_rereview_body(candidate: dict[str, Any], live: dict[str, Any]) -> str`
- Produces: `create_rereview(candidate: dict[str, Any], live: dict[str, Any]) -> str`

- [ ] **Step 1: Write failing adapter and body tests**

Add tests that monkeypatch `run()` and assert:

```python
def test_live_head_change_builds_third_party_read_only_lane():
    body = m.build_rereview_body(candidate(), live(author="barwin", head="b" * 40))
    assert "Old reviewed head: " + "a" * 40 in body
    assert "New live head: " + "b" * 40 in body
    assert "STRICTLY READ-ONLY" in body
    assert "Do NOT edit code" in body
    assert candidate()["source_permalink"] in body
    assert "wrong docs" in body

def test_existing_lane_for_target_head_is_detected():
    assert m.target_lane_exists([existing_lane("b" * 40)], PR_URL, "b" * 40) == "t_existing"

def test_closed_or_unchanged_pr_is_not_dispatchable():
    assert not m.should_dispatch(candidate(), live(state="CLOSED", head="b" * 40))
    assert not m.should_dispatch(candidate(), live(state="OPEN", head="a" * 40))
```

Also assert the `ghx.sh` command requests `state,isDraft,headRefOid,author,headRepositoryOwner,headRefName` and that malformed/auth-failed JSON raises a boundary-specific `RuntimeError`.

- [ ] **Step 2: Run the adapter tests and verify RED**

Run:

```bash
cd /Users/ctao/.hermes/scripts
python3 -m unittest \
  test_just_do_it_blocked_review_requeue_watchdog.LivePrAndDispatchTest -v
```

Expected: failures for undefined live-check, dedupe, and task-body functions.

- [ ] **Step 3: Implement the live and Kanban adapters**

Use subprocess argument lists only. `fetch_live_pr()` must call:

```python
[str(GHX), "pr", "view", pr_url, "--json",
 "state,isDraft,headRefOid,author,headRepositoryOwner,headRefName"]
```

Create lanes with:

```python
[
    "hermes", "kanban", "create", title,
    "--body", body,
    "--assignee", "reviewer",
    "--workspace", "scratch",
    "--priority", "110",
    "--created-by", "just-do-it-blocked-review-requeue-watchdog",
    "--idempotency-key", f"just-do-it-rereview:{owner.lower()}/{repo.lower()}#{number}:{target_head.lower()}",
    "--json",
]
```

Parse the returned task ID, then verify it with `hermes kanban show <id> --json`; accept only `ready`, `todo`, or `running` with assignee `reviewer`.

- [ ] **Step 4: Run the adapter tests and verify GREEN**

Run the command from Step 2. Expected: all `LivePrAndDispatchTest` tests pass.

### Task 3: Idempotent bounded orchestration

**Files:**
- Modify: `/Users/ctao/.hermes/scripts/just_do_it_blocked_review_requeue_watchdog.py`
- Modify: `/Users/ctao/.hermes/scripts/test_just_do_it_blocked_review_requeue_watchdog.py`

**Interfaces:**
- Consumes: Task 1 candidates and Task 2 adapters
- Produces: `process_tick(args: argparse.Namespace, now: int) -> tuple[int, list[str]]`
- Produces: CLI flags `--dry-run`, `--backfill-existing`, `--lookback-seconds`, `--only-task`, and repeatable `--channel`
- State shape: `baseline_completed_at`, `dispatched`, and `failures`

- [ ] **Step 1: Write failing orchestration tests**

Use fixture Kanban list/show JSON and fake GitHub responses to verify:

```python
def test_new_head_dispatches_exactly_once():
    rc, output = run_fixture_tick(blocked_head="a" * 40, live_head="b" * 40)
    assert rc == 0
    assert output == [f"JUST_DO_IT_BLOCKED_REREVIEW dispatched pr={PR_URL} head={'b' * 7} task=t_new"]
    assert create_calls == 1
    rc, output = run_fixture_tick(blocked_head="a" * 40, live_head="b" * 40)
    assert create_calls == 1
    assert output == []

def test_head_changes_again_dispatches_new_lane():
    run_fixture_tick(blocked_head="a" * 40, live_head="b" * 40)
    run_fixture_tick(blocked_head="a" * 40, live_head="c" * 40)
    assert create_calls == 2

def test_first_run_baselines_without_historical_dispatch():
    rc, output = run_fixture_tick(state={})
    assert rc == 0 and output == [] and create_calls == 0

def test_dispatch_cap_and_failure_backoff():
    assert dispatch_count_for_three_candidates() == 2
    assert attempts_across_two_ticks_within_one_hour() == 1
```

Include quiet unchanged/closed/existing-lane cases and explicit list/show/GitHub/create/read-back failure messages.

- [ ] **Step 2: Run orchestration tests and verify RED**

Run:

```bash
cd /Users/ctao/.hermes/scripts
python3 -m unittest \
  test_just_do_it_blocked_review_requeue_watchdog.OrchestrationTest -v
```

Expected: failures for undefined orchestration and state behavior.

- [ ] **Step 3: Implement the bounded loop**

Set:

```python
STATE_PATH = HOME / "state" / "just_do_it_blocked_review_requeue.json"
DEFAULT_LOOKBACK_SECONDS = 7 * 24 * 3600
FAIL_RETRY_SECONDS = 60 * 60
MAX_DISPATCHES_PER_TICK = 2
```

On a normal first run, save `baseline_completed_at = now` and skip tasks completed at or before the baseline. `--backfill-existing` bypasses only the baseline gate, never safety or dedupe. Save state atomically. Record `dispatched[key]` only after read-back verification. On create/read-back ambiguity, refresh the live task list before any future create. Return exit 1 only when a boundary was attempted and failed; backed-off/no-op ticks exit 0 silently.

- [ ] **Step 4: Run the complete test file and verify GREEN**

Run:

```bash
cd /Users/ctao/.hermes/scripts
python3 -m unittest test_just_do_it_blocked_review_requeue_watchdog -v
python3 -m py_compile \
  just_do_it_blocked_review_requeue_watchdog.py \
  test_just_do_it_blocked_review_requeue_watchdog.py
```

Expected: all tests pass and compilation exits 0.

### Task 4: Dry-run validation and cron rollout

**Files:**
- Runtime create: `/Users/ctao/.hermes/state/just_do_it_blocked_review_requeue.json`
- Runtime modify via CLI: `/Users/ctao/.hermes/cron/jobs.json`

**Interfaces:**
- Consumes: completed watchdog script and tests
- Produces: enabled no-agent cron `just-do-it-blocked-review-requeue-watchdog`

- [ ] **Step 1: Run a historical dry-run against PR #696**

Run:

```bash
/Users/ctao/.hermes/scripts/just_do_it_blocked_review_requeue_watchdog.py \
  --dry-run --backfill-existing --only-task t_b2eb8405
```

Expected: no task creation. Because target head `c40fb1d...` already has lane `t_fc423563`, output is empty or an explicit `WOULD_SKIP existing_lane=t_fc423563` diagnostic reserved for dry-run mode.

- [ ] **Step 2: Establish the live baseline**

Run:

```bash
/Users/ctao/.hermes/scripts/just_do_it_blocked_review_requeue_watchdog.py
```

Expected: exit 0, empty stdout, state file created with `baseline_completed_at`, no `dispatched` entries, and no Kanban task creation.

- [ ] **Step 3: Register the no-agent cron**

First verify no job with the same name exists, then run:

```bash
hermes cron create '10m' \
  --name just-do-it-blocked-review-requeue-watchdog \
  --script just_do_it_blocked_review_requeue_watchdog.py \
  --no-agent \
  --deliver slack:C0B5CT0TRFS \
  'No-agent watchdog: requeues one read-only Boris review when a Slack-originated structured NEEDS-FIX/CHANGES_REQUESTED PR receives a new head. Silent on no-op; bounded failures only.'
```

Expected: one enabled job ID. Save the ID in the verification notes; do not create a second job if registration output is ambiguous.

- [ ] **Step 4: Trigger and verify one scheduled-equivalent tick**

Run:

```bash
hermes cron run <new-job-id>
```

Expected: job `last_status` is `ok`, `last_error` is null, stdout is empty, and no new reviewer task was created.

- [ ] **Step 5: Run final regression and safety verification**

Run:

```bash
cd /Users/ctao/.hermes/scripts
python3 -m unittest \
  test_just_do_it_blocked_review_requeue_watchdog \
  test_just_do_it_review_reaction_watchdog -v
sqlite3 /Users/ctao/.hermes/kanban.db 'PRAGMA quick_check;'
hermes kanban list --json >/tmp/just-do-it-rereview-kanban.json
python3 -m json.tool /tmp/just-do-it-rereview-kanban.json >/dev/null
```

Expected: all tests pass, SQLite prints `ok`, and Kanban JSON parses successfully.

- [ ] **Step 6: Preserve implementation evidence**

Copy the final script, test output, dry-run output, cron job record, and state file into a timestamped directory under:

```text
/Users/ctao/.hermes/forensics/just-do-it-blocked-rereview-rollout-<timestamp>/
```

Do not copy tokens or `.env`. Record the new cron job ID and the exact verification commands in `verification.txt`.
