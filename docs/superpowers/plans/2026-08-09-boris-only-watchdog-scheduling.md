# Boris-Only Watchdog Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the retained per-minute reaction watchdog Boris-only by default,
restore the one proven legacy `alerted_at` drift under exact guards, and finish
the reviewed Boris `:rubbing:` intake rollout.

**Architecture:** Keep the existing channel-fenced watchdog and change only its
default channel set. Extend the one-shot retirement controller with two
paused-only operations: exact legacy alert compensation against the immutable
checkpoint and exact retained-prompt configuration. Deploy reviewed bytes,
verify one natural scheduled tick, then checkpoint and restart Boris intake.

**Tech Stack:** Python 3.11, pytest through `scripts/run_tests.sh`, JSON state,
Hermes CRON registry locking, POSIX `flock`, macOS launchd.

## Global Constraints

- Authoritative design: `docs/superpowers/specs/2026-08-09-boris-only-watchdog-scheduling-design.md`.
- Retain only exact job ID `6daa151da351`; never restore or recreate `ee4582d0598f` or `ff865c33c5ea`.
- Scheduled/no-argument reaction execution selects only `C0BPCKPEFCG`.
- Explicit `--channel` operation retains the three legacy channel IDs for rollback and forensics.
- Preserve strict legacy projection equality, including `alerted_at`; never rebaseline or exclude the field.
- Never mutate Kanban from the reaction watchdog or retirement controller.
- Preserve Boris authorization, grammar, exact-head deduplication, audit, dispatch, COMMENT-only review policy, catch-up, and exact `Source Slack message: <permalink>` behavior.
- Never run the global controller rollback during this rollout.
- Run repository tests only through `scripts/run_tests.sh` and always pass `--file-retries 0`.
- Use `/Users/ctao/.hermes/hermes-agent/venv/bin/python` for standalone Python commands.
- Do not block for more than 60 seconds in a single wait.

---

### Task 1: Make scheduled/default watchdog execution Boris-only

**Files:**
- Modify: `cron/scripts/just_do_it_review_reaction_watchdog.py`
- Modify: `tests/cron/test_just_do_it_review_reaction_watchdog.py`

**Interfaces:**
- Consumes: existing `--channel` replacement semantics and channel-fenced state paths.
- Produces: `SCHEDULED_DEFAULT_CHANNELS = {BORIS_CHANNEL}` while explicit channel arguments remain unchanged.

- [ ] **Step 1: Make unit helpers explicit and add failing no-argument behavior tests**

Change the test-only `args()` helper to pass all fixture channels explicitly,
so classifier/state-machine tests do not accidentally assert CLI defaults.
Replace the old scheduled-all-channel expectation with these behaviors:

```python
def legacy_pending_state(self):
    initial = m.migrate_state(None, NOW, backfill=True)[0]
    initial["machine_baseline_at"] = 0
    initial["posts"][PERMALINK_B] = {
        "emoji": "review-no",
        "applied_at": NOW - 600,
        "pending_removals": ["thinkies"],
        "lanes": {"t_legacy": "review-no"},
    }
    return initial


def test_scheduled_default_preserves_due_legacy_repair(self):
    initial = self.legacy_pending_state()
    before = json.loads(json.dumps(initial["posts"][PERMALINK_B]))
    legacy_reacted_before = json.loads(json.dumps(initial["reacted"]))
    slack, after, _before_bytes, _after_bytes = self.run_persisted_main(initial, [])
    self.assertEqual(slack.calls, [])
    self.assertEqual(after["posts"][PERMALINK_B], before)
    self.assertEqual(after["reacted"], legacy_reacted_before)


def test_explicit_legacy_channel_still_repairs_due_legacy_record(self):
    initial = self.legacy_pending_state()
    slack, after, _before_bytes, _after_bytes = self.run_persisted_main(
        initial, ["--channel", OTHER_WATCHED]
    )
    self.assertEqual(slack.calls, [
        (PERMALINK_B, "review-no", False),
        (PERMALINK_B, "thinkies", True),
    ])
    self.assertEqual(after["posts"][PERMALINK_B]["pending_removals"], [])
```

Update migration tests that deliberately need an all/legacy-channel operator
pass to provide explicit repeated `--channel` arguments.

- [ ] **Step 2: Run the focused tests and verify red**

Run:

```bash
scripts/run_tests.sh \
  tests/cron/test_just_do_it_review_reaction_watchdog.py \
  -q --file-retries 0
```

Expected: the no-argument legacy-preservation test fails because the current
default still admits all watched channels.

- [ ] **Step 3: Implement the minimal default-selection change**

Introduce an explicit supported/all set and a scheduled set:

```python
LEGACY_CHANNELS = {
    "C08K8FQKX9S",
    "C07BU8AAPB3",
    "C09339EGZ9U",
}
SUPPORTED_CHANNELS = LEGACY_CHANNELS | {BORIS_CHANNEL}
SCHEDULED_DEFAULT_CHANNELS = {BORIS_CHANNEL}
```

Use `SCHEDULED_DEFAULT_CHANNELS` at both `process_tick()` and `main()` channel
resolution boundaries when `args.channels` is empty. Keep explicit
`args.channels` replacement behavior. Update the module documentation and CLI
help to state that default execution is Boris-only and legacy IDs are explicit
operator tooling.

- [ ] **Step 4: Verify green and run the mutation check**

Run the same suite. Confirm the new test would fail if either default-selection
site were changed back to the all-channel set, while the explicit legacy test
would fail if operator support were removed.

- [ ] **Step 5: Commit Task 1**

```bash
git add cron/scripts/just_do_it_review_reaction_watchdog.py \
  tests/cron/test_just_do_it_review_reaction_watchdog.py
git commit -m "fix(cron): make scheduled reaction watchdog Boris-only"
```

---

### Task 2: Add exact legacy-alert compensation

**Files:**
- Modify: `scripts/ops/retire_just_do_it_crons.py`
- Modify: `tests/cron/test_retire_just_do_it_crons.py`

**Interfaces:**
- Consumes: `Manifest`, `_legacy_reaction_baseline()`, archived fingerprints, CRON store lock, and the watchdog ledger lock path.
- Produces: `compensate_legacy_alerted_at(paths, manifest, permalink, expected_current, expected_baseline, now, *, apply) -> dict[str, Any]` and CLI command `compensate-legacy-alerted-at`.

- [ ] **Step 1: Add failing compensation tests**

Create a finalized fixture with a retained-reaction checkpoint, pause the exact
retained ID, and change only the legacy fixture record's `alerted_at`. Add the
following exact test cases:

- `test_compensation_restores_only_checkpoint_alerted_at` builds an expected
  full ledger by deep-copying the live pre-compensation ledger and changing
  only the selected `alerted_at`, then asserts exact equality after apply.
- `test_compensation_dry_run_is_read_only` records ledger, registry, manifest,
  and archive bytes and asserts all remain byte-identical.
- `test_compensation_is_idempotent_at_exact_baseline` applies once, records all
  bytes, applies again, and requires `status == "already_compensated"` with no
  second evidence directory.
- `test_compensation_rejects_boris_permalink` passes the fixture Boris
  permalink and expects `RetirementError("selected permalink is not legacy")`.
- `test_compensation_rejects_extra_legacy_drift` changes a second legacy field
  and expects the exact-delta guard to reject without writes.
- `test_compensation_rejects_wrong_explicit_values` parametrizes a wrong
  current value and wrong baseline value; both must reject without writes.
- `test_compensation_requires_retained_job_paused` resumes the retained fixture
  before invoking compensation and expects a paused-state error.
- `test_compensation_rejects_missing_or_changed_retired_archive` parametrizes
  unlinking and modifying one retired archive and expects a fingerprint error.
- `test_compensation_rejects_busy_watchdog_lock` holds the exact ledger lock
  with `LOCK_EX | LOCK_NB` and expects a busy-lock error without waiting.

The success test must assert the full post-write ledger equals a hand-built
copy of the pre-write ledger with exactly one leaf changed, the Boris projection
is unchanged, strict `verify_retired()` projection validation passes after the
retained job is resumed in the fixture, and the evidence directory contains
hash-valid before/after snapshots plus JSON evidence.

- [ ] **Step 2: Run the new tests and verify red**

Run:

```bash
scripts/run_tests.sh \
  tests/cron/test_retire_just_do_it_crons.py \
  -q --file-retries 0
```

Expected: import/attribute failures because the compensation interface and CLI
subcommand do not exist.

- [ ] **Step 3: Add minimal projection and boundary helpers**

Add a Boris projection symmetric to `_legacy_projection()`, an exact paused
retained validator, and a non-blocking context manager for
`<state>/just_do_it_review_reactions.lock`. The paused validator must require:

```python
{
    "id": RETAIN_ID,
    "name": RETAIN_POST_MIGRATION_NAME,
    "script": RETAIN_SCRIPT,
    "schedule": {"kind": "cron", "expr": "* * * * *"},
    "no_agent": True,
    "enabled": False,
    "state": "paused",
}
```

It must separately prove both retired IDs are absent and every entry in
`ENTRYPOINTS` is absent at its active path and matches its archived
fingerprint.

- [ ] **Step 4: Implement dry-run validation and exact apply**

The function must validate `PERMALINK_RE.fullmatch(permalink)`, require the
source channel in `LEGACY_CHANNELS`, and compare projections as follows:

```python
expected = copy.deepcopy(current_raw)
expected["posts"][permalink]["alerted_at"] = expected_baseline
if _canonical_json_bytes(_legacy_projection(expected)) != \
        _canonical_json_bytes(checkpoint_legacy):
    raise RetirementError("legacy drift is not limited to the selected alerted_at")
```

On `apply=True`, snapshot the complete live ledger, atomically write `expected`
with the original mode, reload it under the same lock, and assert:

```python
reloaded == expected
_legacy_projection(reloaded) == checkpoint_legacy
_boris_projection(reloaded) == _boris_projection(current_raw)
```

Snapshot the result and write an evidence JSON file with the exact permalink,
before/after integers, immutable checkpoint fingerprint, and both full-ledger
fingerprints. If the baseline is already present and the full strict legacy
projection matches, return `already_compensated` without writing.

- [ ] **Step 5: Add the CLI boundary**

Add `compensate-legacy-alerted-at` with the standard manifest/path options plus:

```text
--permalink
--expected-current-alerted-at <int>
--expected-baseline-alerted-at <int>
--apply
```

Dry-run output must state `apply=false` and the single planned field change.
Apply output must include the evidence path. No command path may call rollback.

- [ ] **Step 6: Verify green**

Run the controller suite with retries disabled and confirm all failure cases are
read-only.

- [ ] **Step 7: Commit Task 2**

```bash
git add scripts/ops/retire_just_do_it_crons.py \
  tests/cron/test_retire_just_do_it_crons.py
git commit -m "fix(cron): guard legacy alert compensation"
```

---

### Task 3: Guard and verify the Boris-only retained prompt

**Files:**
- Modify: `scripts/ops/retire_just_do_it_crons.py`
- Modify: `tests/cron/test_retire_just_do_it_crons.py`

**Interfaces:**
- Produces: `configure_retained_boris_only_prompt(paths, manifest, *, apply) -> dict[str, Any]`, CLI command `configure-retained-prompt`, and prompt-aware post-checkpoint verification.

- [ ] **Step 1: Add failing prompt-configuration tests**

Cover exact paused-only mutation, dry-run, idempotence, unknown-current-prompt
rejection, wrong job name/script/schedule rejection, and preservation of every
non-prompt job field. Add a verification test proving that post-checkpoint
retired-state verification rejects the old all-channel prompt.

- [ ] **Step 2: Run the controller suite and verify red**

Run the Task 2 command. Expected: failures because the configure interface and
post-checkpoint prompt requirement do not exist.

- [ ] **Step 3: Implement exact prompt configuration**

Define exact constants for the current all-channel post-migration prompt and
the desired text:

```text
No-agent authoritative state machine for source-linked Boris reviewer lanes. In #boris-reviews it maintains exactly one owned reaction with precedence face_vomiting > doge_notes > rubbing > blinkapproved. Scheduled/default execution is Boris-only; explicit legacy-channel operation remains available only for rollback and forensics. Quiet when settled; emits compact transition, hold, reconciliation, or failure output. It never creates or mutates Kanban tasks. Do not schedule additional cron jobs.
```

Under the CRON registry lock, require the exact retained record to be paused
and require its prompt to equal either the known current prompt or the desired
prompt. Deep-copy the full record, change only `prompt`, save, reload, and
compare every other field exactly.

- [ ] **Step 4: Add prompt-aware verification and CLI**

Extend `_validate_retained_job()` with an optional exact prompt. Require the
Boris-only prompt whenever a retained-reaction checkpoint exists and before a
Boris-intake checkpoint. Add `configure-retained-prompt` with dry-run default
and `--apply`.

- [ ] **Step 5: Verify green and commit**

Run the controller suite, then:

```bash
git add scripts/ops/retire_just_do_it_crons.py \
  tests/cron/test_retire_just_do_it_crons.py
git commit -m "fix(cron): enforce Boris-only retained prompt"
```

---

### Task 4: Review and stage the retained watchdog rollout

**Files:**
- Modify: `.superpowers/sdd/2026-08-08-just-do-it-cron-retirement-boris-reactions/progress.md`
- Create: `.superpowers/sdd/2026-08-08-just-do-it-cron-retirement-boris-reactions/task-9d-report.md`

**Interfaces:**
- Consumes: Tasks 1-3 reviewed commits and live manifest `/Users/ctao/.hermes/forensics/just-do-it-retirement-20260808T204357Z/manifest.json`.
- Produces: exact reviewed source/test bytes and a live rollout evidence record.

- [ ] **Step 1: Run the full focused gate**

```bash
scripts/run_tests.sh \
  tests/cron/test_just_do_it_review_reaction_watchdog.py \
  tests/cron/test_retire_just_do_it_crons.py \
  tests/cron/boris_intake \
  -q --file-retries 0
```

- [ ] **Step 2: Obtain independent code review**

Review the Tasks 1-3 git range against this plan. Resolve every Critical or
Important finding and re-run the covering suites before any live write.

- [ ] **Step 3: Reconfirm live preconditions read-only**

Require exact retained ID paused, retired IDs absent, four retired active paths
absent and archives valid, Boris service healthy, live source matching the
previous reviewed checkpoint, and only the documented legacy `alerted_at`
difference.

- [ ] **Step 4: Install exact reviewed watchdog bytes while paused**

Atomically install:

```text
cron/scripts/just_do_it_review_reaction_watchdog.py
  -> /Users/ctao/.hermes/scripts/just_do_it_review_reaction_watchdog.py
tests/cron/test_just_do_it_review_reaction_watchdog.py
  -> /Users/ctao/.hermes/scripts/test_just_do_it_review_reaction_watchdog.py
```

Verify SHA-256 equality immediately.

- [ ] **Step 5: Dry-run and apply compensation and prompt configuration**

Use permalink
`https://ylopo.slack.com/archives/C07BU8AAPB3/p1785529492858409`, current
`1786256846`, and baseline `1786223639`. Run each controller command without
`--apply`, then with `--apply`. Record the evidence path and final strict
legacy hash.

- [ ] **Step 6: Resume and verify one natural scheduled tick**

Resume only `6daa151da351`. Poll in intervals no longer than 60 seconds until a
new completed execution is recorded. Compare before/after legacy projections
exactly, confirm zero Slack calls/zero settled Boris writes from job output,
and run controller `verify --expect retired`.

- [ ] **Step 7: Commit evidence bookkeeping**

Append Task 9d completion and evidence locations to the existing ledger and
commit only the report/ledger changes.

---

### Task 5: Deploy the reviewed Boris `:rubbing:` intake

**Files:**
- Live replace: `/Users/ctao/.hermes/scripts/boris_intake/daemon.py`
- Live replace: `/Users/ctao/.hermes/scripts/boris_intake/slack_io.py`
- Live replace as needed: the remaining reviewed `cron/scripts/boris_intake/*.py`
- Modify: `.superpowers/sdd/2026-08-08-just-do-it-crons-boris-reactions/progress.md`
- Create: `.superpowers/sdd/2026-08-08-just-do-it-crons-boris-reactions/task-10-report.md`

**Interfaces:**
- Consumes: healthy retained watchdog and reviewed versioned Boris package.
- Produces: live Boris intake whose accepted-review seam adds only `:rubbing:`.

- [ ] **Step 1: Run the Boris compatibility suite**

```bash
scripts/run_tests.sh tests/cron/boris_intake -q --file-retries 0
```

- [ ] **Step 2: Create the Boris intake checkpoint**

Use controller `checkpoint --component boris-intake --apply` only after the
retained job is enabled and its latest execution is completed.

- [ ] **Step 3: Capture continuity evidence and install exact bytes**

Record PID, heartbeat mtime/content, config fingerprint, watermark, audit line
count/prefix hash, SQLite integrity/schema/counts, and launchd plist hash.
Atomically install the reviewed versioned package and verify every installed
file hash. Do not synthesize a production review request.

- [ ] **Step 4: Restart and verify launchd service**

Restart only `ai.hermes.boris-intake`. Require a new PID, running launchd
state, advancing heartbeat, unchanged config/plist, readable audit/database,
non-regressing watermark, and preserved deduplication rows.

- [ ] **Step 5: Wait the 180-second retirement grace in bounded intervals**

Use separate waits of at most 60 seconds, reporting intermediate health. Then
run controller retired-state verification and the focused watchdog/controller/
Boris suites again.

- [ ] **Step 6: Commit Task 10 evidence bookkeeping**

Append Task 10 completion and evidence locations to the ledger and commit the
report/ledger changes.

---

### Task 6: Final branch verification and review

**Files:**
- Review all branch changes and reports.

**Interfaces:**
- Produces: a fully verified branch ready for the user's integration choice.

- [ ] **Step 1: Run fresh full relevant verification**

```bash
scripts/run_tests.sh \
  tests/cron/test_just_do_it_review_reaction_watchdog.py \
  tests/cron/test_retire_just_do_it_crons.py \
  tests/cron/boris_intake \
  -q --file-retries 0
```

Also run controller `verify --expect retired`, hash comparisons for all live
installed sources, launchd health, exact retired-ID absence, and active-path
absence.

- [ ] **Step 2: Obtain whole-branch independent review**

Review from the branch merge base through `HEAD`, including the original design,
this amendment, both plans, the SDD ledger, and Task 9d/10 live evidence. One
fix wave is allowed; every fix must receive a scoped re-review.

- [ ] **Step 3: Use the branch-finishing workflow**

After fresh green evidence, detect the worktree/base branch and present the
standard merge, PR, or keep-branch integration choices. Do not merge, push, or
remove the worktree without the user's choice.
