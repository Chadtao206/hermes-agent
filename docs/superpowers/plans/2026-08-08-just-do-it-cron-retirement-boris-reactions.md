# Just-Do-It CRON Retirement and Boris Reactions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove both legacy just-do-it lane-producing CRONs and their runnable entrypoints while migrating only `#boris-reviews` to `:rubbing:`, `:doge_notes:`, `:face_vomiting:`, and `:blinkapproved:`.

**Architecture:** Preserve the proven reaction classifier but separate its four semantic states from channel-specific emoji rendering. Keep scheduler ID `6daa151da351` as the only reaction writer, add a tested one-shot retirement controller for exact-ID backup/quiescence/removal/rollback, and deploy only after isolated compatibility and rollback gates pass.

**Tech Stack:** Python 3.11, `unittest`/pytest through `scripts/run_tests.sh`, SQLite, Hermes `cron.jobs`, Slack Web API through existing helpers, macOS launchd, JSON state ledgers.

## Global Constraints

- Authoritative design: `docs/superpowers/specs/2026-08-08-just-do-it-cron-retirement-boris-reactions-design.md`.
- Retire only `ee4582d0598f` and `ff865c33c5ea`; retain exact ID `6daa151da351` enabled at `* * * * *` after installation.
- Preserve completed/archived tasks, CRON output, three channel-watch watermarks, and the blocked-review requeue ledger as audit history.
- Migrate existing reactions only for `C0BPCKPEFCG`; do not rewrite legacy-channel Slack posts or ledger records.
- Preserve precedence exactly: `blocked > notes > active > approved`.
- Preserve add-before-remove, retry, quarantine, hold, compaction, and fail-closed behavior.
- The retained watchdog may write Slack reactions and its private state only; it must never create or mutate a Kanban task.
- Boris behavior may change only at the initial reaction seam: `thinkies` becomes `rubbing`.
- Preserve Boris authorization, grammar, exact-head deduplication, dispatch, audit, static-review guard, COMMENT-only policy, catch-up, and exact `Source Slack message: <permalink>` output.
- Never run tests against live Slack, the live board, the live CRON registry, or default Hermes state.
- Run repository tests only through `scripts/run_tests.sh` with `--file-retries 0` for rollout gates.
- Do not use a single blocking wait longer than 60 seconds during live quiescence; poll in separate bounded steps and report progress.
- Restore compatible source before restoring or resuming a retired registry record.

---

## File map

### Versioned runtime source

- Create: `cron/scripts/just_do_it_review_reaction_watchdog.py` — durable source for retained job `6daa151da351`; the deployed filename stays compatible.
- Create: `cron/scripts/boris_intake/` — durable copy of the complete live Boris intake package.
- Create: `scripts/ops/retire_just_do_it_crons.py` — exact-ID backup, quiescence, retirement, verification, and rollback controller.
- Delete after gates: `cron/scripts/just_do_it_blocked_review_requeue_watchdog.py` — retired automatic lane creator.

### Tests

- Create: `tests/cron/test_just_do_it_review_reaction_watchdog.py` — durable reaction classifier/rendering/migration suite.
- Create: `tests/cron/boris_intake/` — durable Boris compatibility suite.
- Create: `tests/cron/test_retire_just_do_it_crons.py` — isolated registry/execution/board/archive/rollback tests.
- Delete after gates: `tests/cron/test_just_do_it_blocked_review_requeue_watchdog.py` — tests a removed runtime path.

### Live rollout targets

- Modify: `/Users/ctao/.hermes/cron/jobs.json` through locked Hermes APIs.
- Replace: `/Users/ctao/.hermes/scripts/just_do_it_review_reaction_watchdog.py`.
- Replace: `/Users/ctao/.hermes/scripts/boris_intake/daemon.py`.
- Replace: `/Users/ctao/.hermes/scripts/boris_intake/slack_io.py`.
- Archive/remove: `/Users/ctao/.hermes/scripts/slack_channel_watch.py` and its live test.
- Archive/remove: `/Users/ctao/.hermes/scripts/just_do_it_blocked_review_requeue_watchdog.py` and its live test.
- Migrate: `/Users/ctao/.hermes/state/just_do_it_review_reactions.json`, after an exact backup.
- Restart: `gui/$(id -u)/ai.hermes.boris-intake` only after the retained watchdog is healthy.

---

### Task 1: Capture the live compatible source as a versioned baseline

**Files:**
- Create: `cron/scripts/just_do_it_review_reaction_watchdog.py`
- Create: `cron/scripts/boris_intake/*.py`
- Create: `tests/cron/test_just_do_it_review_reaction_watchdog.py`
- Create: `tests/cron/boris_intake/*.py`
- Create: `tests/cron/boris_intake/conftest.py`

**Interfaces:**
- Consumes: the currently deployed scripts under `/Users/ctao/.hermes/scripts`.
- Produces: a byte-identical versioned runtime baseline and a hermetic compatibility suite for later tasks.

- [ ] **Step 1: Mechanically copy only source and tests, excluding runtime caches**

Run from the implementation worktree:

```bash
mkdir -p cron/scripts/boris_intake tests/cron/boris_intake
rsync -a --exclude '__pycache__' --exclude 'tests' \
  /Users/ctao/.hermes/scripts/boris_intake/ cron/scripts/boris_intake/
rsync -a --exclude '__pycache__' --exclude '.pytest_cache' \
  /Users/ctao/.hermes/scripts/boris_intake/tests/ tests/cron/boris_intake/
cp /Users/ctao/.hermes/scripts/just_do_it_review_reaction_watchdog.py \
  cron/scripts/just_do_it_review_reaction_watchdog.py
cp /Users/ctao/.hermes/scripts/test_just_do_it_review_reaction_watchdog.py \
  tests/cron/test_just_do_it_review_reaction_watchdog.py
```

Expected: no `__pycache__`, `.pyc`, or `.pytest_cache` path appears under the new trees.

- [ ] **Step 2: Point the copied reaction test at versioned source**

Add `import os`, then apply this exact replacement near the top of
`tests/cron/test_just_do_it_review_reaction_watchdog.py`:

```python
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(
    os.environ.get(
        "HERMES_REACTION_WATCHDOG_SCRIPT",
        REPO_ROOT / "cron" / "scripts" / "just_do_it_review_reaction_watchdog.py",
    )
)
```

- [ ] **Step 3: Add the Boris test import root**

Create `tests/cron/boris_intake/conftest.py`:

```python
from __future__ import annotations

import os
import sys
from pathlib import Path

SOURCE_ROOT = Path(
    os.environ.get(
        "BORIS_INTAKE_SOURCE_ROOT",
        Path(__file__).resolve().parents[3] / "cron" / "scripts",
    )
)
sys.path.insert(0, str(SOURCE_ROOT))
```

The copied tests may retain their existing `sys.path` insertions; this fixture
ensures `cron/scripts/boris_intake` remains importable after those insertions.

- [ ] **Step 4: Prove the runtime copies match the live source before editing**

Run:

```bash
cmp -s cron/scripts/just_do_it_review_reaction_watchdog.py \
  /Users/ctao/.hermes/scripts/just_do_it_review_reaction_watchdog.py
for source in cron/scripts/boris_intake/*.py; do
  cmp -s "$source" "/Users/ctao/.hermes/scripts/boris_intake/${source##*/}"
done
```

Expected: exit 0. The test files are intentionally path-adjusted and are not
byte-compared.

- [ ] **Step 5: Run the copied baseline suites**

Run:

```bash
scripts/run_tests.sh \
  tests/cron/test_just_do_it_review_reaction_watchdog.py \
  tests/cron/boris_intake \
  -q --file-retries 0
```

Expected: PASS with no live Slack or live state access.

- [ ] **Step 6: Commit the baseline separately**

```bash
git add cron/scripts/just_do_it_review_reaction_watchdog.py \
  cron/scripts/boris_intake tests/cron/test_just_do_it_review_reaction_watchdog.py \
  tests/cron/boris_intake
git commit -m "chore(cron): capture live Boris reaction sources"
```

---

### Task 2: Establish the semantic state and reaction-rendering boundary

**Files:**
- Modify: `cron/scripts/just_do_it_review_reaction_watchdog.py:121-140`
- Modify: `cron/scripts/just_do_it_review_reaction_watchdog.py:851-1012`
- Modify: `tests/cron/test_just_do_it_review_reaction_watchdog.py`

**Interfaces:**
- Consumes: the existing rendered-emoji classifier outputs and the exact source permalink.
- Produces:
  - `normalize_review_state(value: object) -> str | None`
  - `source_channel(permalink: str) -> str | None`
  - `render_reaction(permalink: str, state: str, boris_style: str = "modern") -> str`
  - `owned_reactions(permalink: str) -> tuple[str, ...]`
  - `aggregate_state(states: Iterable[str]) -> str | None`

- [ ] **Step 1: Add failing channel/rendering tests**

Add a Boris permalink fixture and these assertions:

```python
BORIS_CHANNEL = "C0BPCKPEFCG"
BORIS_PERMALINK = (
    f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000003"
)


class ReactionSchemeTest(unittest.TestCase):
    def test_boris_channel_uses_modern_reactions(self):
        expected = {
            m.STATE_ACTIVE: "rubbing",
            m.STATE_NOTES: "doge_notes",
            m.STATE_BLOCKED: "face_vomiting",
            m.STATE_APPROVED: "blinkapproved",
        }
        self.assertEqual(
            {state: m.render_reaction(BORIS_PERMALINK, state) for state in expected},
            expected,
        )

    def test_legacy_channel_keeps_legacy_reactions(self):
        expected = {
            m.STATE_ACTIVE: "thinkies",
            m.STATE_NOTES: "doge_notes",
            m.STATE_BLOCKED: "review-no",
            m.STATE_APPROVED: "white_check_mark",
        }
        self.assertEqual(
            {state: m.render_reaction(PERMALINK, state) for state in expected},
            expected,
        )

    def test_reverse_style_renders_legacy_only_for_boris(self):
        self.assertEqual(
            m.render_reaction(BORIS_PERMALINK, m.STATE_BLOCKED, "legacy"),
            "review-no",
        )
        self.assertEqual(
            m.render_reaction(PERMALINK, m.STATE_BLOCKED, "legacy"),
            "review-no",
        )

    def test_boris_owns_old_and_new_names_but_legacy_does_not(self):
        self.assertEqual(
            set(m.owned_reactions(BORIS_PERMALINK)),
            {
                "thinkies", "doge_notes", "review-no", "white_check_mark",
                "rubbing", "face_vomiting", "blinkapproved",
            },
        )
        self.assertEqual(
            set(m.owned_reactions(PERMALINK)),
            {"thinkies", "doge_notes", "review-no", "white_check_mark"},
        )
```

- [ ] **Step 2: Run the focused tests and verify red**

```bash
scripts/run_tests.sh \
  tests/cron/test_just_do_it_review_reaction_watchdog.py::ReactionSchemeTest \
  -q --file-retries 0
```

Expected: FAIL because the semantic constants and rendering functions do not
exist.

- [ ] **Step 3: Add semantic and rendering tables without changing the live pipeline yet**

Add these definitions alongside the existing legacy constants. Task 3 removes
the temporary emoji-as-state path only when every classifier, state reader,
transition, and persistence call site changes in one independently passing
commit.

```python
BORIS_CHANNEL = "C0BPCKPEFCG"

STATE_BLOCKED = "blocked"
STATE_NOTES = "notes"
STATE_ACTIVE = "active"
STATE_APPROVED = "approved"

STATE_PRECEDENCE = {
    STATE_BLOCKED: 4,
    STATE_NOTES: 3,
    STATE_ACTIVE: 2,
    STATE_APPROVED: 1,
}

LEGACY_REACTIONS = {
    STATE_BLOCKED: "review-no",
    STATE_NOTES: "doge_notes",
    STATE_ACTIVE: "thinkies",
    STATE_APPROVED: "white_check_mark",
}
BORIS_REACTIONS = {
    STATE_BLOCKED: "face_vomiting",
    STATE_NOTES: "doge_notes",
    STATE_ACTIVE: "rubbing",
    STATE_APPROVED: "blinkapproved",
}
REACTION_TO_STATE = {
    reaction: state
    for table in (LEGACY_REACTIONS, BORIS_REACTIONS)
    for state, reaction in table.items()
}


def normalize_review_state(value: object) -> str | None:
    text = str(value or "")
    if text in STATE_PRECEDENCE:
        return text
    return REACTION_TO_STATE.get(text)


def source_channel(permalink: str) -> str | None:
    match = PERMALINK_RE.search(str(permalink or ""))
    return match.group("channel").upper() if match else None


def render_reaction(
    permalink: str,
    state: str,
    boris_style: str = "modern",
) -> str:
    if state not in STATE_PRECEDENCE:
        raise ValueError(f"unknown review state: {state}")
    if source_channel(permalink) == BORIS_CHANNEL and boris_style == "modern":
        return BORIS_REACTIONS[state]
    return LEGACY_REACTIONS[state]


def owned_reactions(permalink: str) -> tuple[str, ...]:
    tables = (
        (LEGACY_REACTIONS, BORIS_REACTIONS)
        if source_channel(permalink) == BORIS_CHANNEL
        else (LEGACY_REACTIONS,)
    )
    return tuple(dict.fromkeys(reaction for table in tables for reaction in table.values()))
```

- [ ] **Step 4: Add a pure semantic aggregator**

Add `aggregate_state` without replacing `aggregate_emoji` yet:

```python
def aggregate_state(states) -> str | None:
    known = [normalize_review_state(state) for state in states]
    known = [state for state in known if state in STATE_PRECEDENCE]
    if not known:
        return None
    return max(known, key=lambda state: STATE_PRECEDENCE[state])
```

Add direct tests proving both semantic values and old/new rendered names
normalize to the same state using this exact table:

```python
def test_normalizes_legacy_modern_and_semantic_values(self):
    expected = {
        "review-no": m.STATE_BLOCKED,
        "face_vomiting": m.STATE_BLOCKED,
        "doge_notes": m.STATE_NOTES,
        "thinkies": m.STATE_ACTIVE,
        "rubbing": m.STATE_ACTIVE,
        "white_check_mark": m.STATE_APPROVED,
        "blinkapproved": m.STATE_APPROVED,
    }
    for rendered, state in expected.items():
        self.assertEqual(m.normalize_review_state(rendered), state)
        self.assertEqual(m.normalize_review_state(state), state)


def test_aggregate_state_preserves_precedence(self):
    self.assertEqual(
        m.aggregate_state(
            [m.STATE_APPROVED, "rubbing", "doge_notes", "face_vomiting"]
        ),
        m.STATE_BLOCKED,
    )
```

Keep existing classifier and Slack-call expectations unchanged in this task.
That deliberate compatibility seam makes Task 2 independently green and
prevents semantic strings from accidentally being sent to Slack.

- [ ] **Step 5: Run the complete reaction suite**

```bash
scripts/run_tests.sh tests/cron/test_just_do_it_review_reaction_watchdog.py \
  -q --file-retries 0
```

Expected: PASS with the production pipeline still byte-for-byte equivalent in
its persisted values and Slack calls.

- [ ] **Step 6: Commit the semantic boundary**

```bash
git add cron/scripts/just_do_it_review_reaction_watchdog.py \
  tests/cron/test_just_do_it_review_reaction_watchdog.py
git commit -m "refactor(cron): separate review state from reactions"
```

---

### Task 3: Add channel-scoped transitions, state compatibility, and reversible migration

**Files:**
- Modify: `cron/scripts/just_do_it_review_reaction_watchdog.py:1000-1888`
- Modify: `tests/cron/test_just_do_it_review_reaction_watchdog.py`

**Interfaces:**
- Consumes: Task 2 semantic state and rendering functions.
- Produces:
  - `apply_transition(permalink, desired, record, reactor, owned) -> dict[str, Any]`
  - `reconcile_sweep(permalink, desired, reactor, owned) -> dict[str, Any]`
  - CLI option `--boris-reaction-style modern|legacy`
  - process lock at `STATE_PATH.with_suffix(".lock")`

- [ ] **Step 1: Add failing transition and migration tests**

Add tests covering the exact approved behavior:

```python
class BorisMigrationTest(unittest.TestCase):
    def test_modern_transition_adds_before_removing_legacy(self):
        slack = FakeSlack()
        record = {"emoji": "review-no", "state": m.STATE_BLOCKED}
        result = m.apply_transition(
            BORIS_PERMALINK,
            "face_vomiting",
            record,
            slack,
            m.owned_reactions(BORIS_PERMALINK),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(slack.calls[0], (BORIS_PERMALINK, "face_vomiting", False))
        self.assertIn((BORIS_PERMALINK, "review-no", True), slack.calls[1:])

    def test_failed_modern_add_retains_legacy_reaction(self):
        slack = FakeSlack(fail_add={"face_vomiting"})
        result = m.apply_transition(
            BORIS_PERMALINK,
            "face_vomiting",
            {"emoji": "review-no", "state": m.STATE_BLOCKED},
            slack,
            m.owned_reactions(BORIS_PERMALINK),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(slack.removes, [])

    def test_legacy_post_never_removes_modern_manual_emoji(self):
        slack = FakeSlack()
        m.reconcile_sweep(
            PERMALINK,
            "review-no",
            slack,
            m.owned_reactions(PERMALINK),
        )
        self.assertNotIn((PERMALINK, "face_vomiting", True), slack.calls)
        self.assertNotIn((PERMALINK, "rubbing", True), slack.calls)
        self.assertNotIn((PERMALINK, "blinkapproved", True), slack.calls)

    def test_reverse_style_removes_modern_after_adding_legacy(self):
        state = m.STATE_BLOCKED
        desired = m.render_reaction(BORIS_PERMALINK, state, "legacy")
        slack = FakeSlack()
        result = m.apply_transition(
            BORIS_PERMALINK,
            desired,
            {"emoji": "face_vomiting", "state": state},
            slack,
            m.owned_reactions(BORIS_PERMALINK),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(slack.calls[0], (BORIS_PERMALINK, "review-no", False))
        self.assertIn((BORIS_PERMALINK, "face_vomiting", True), slack.calls[1:])

    def test_kanban_adapter_exposes_only_list_and_show(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )
        with mock.patch.object(m, "run", return_value=completed) as run:
            m.kanban_reviewer_lanes()
            completed.stdout = json.dumps({"id": "lane-1"})
            m.kanban_show("lane-1")
        verbs = [call.args[0][2] for call in run.call_args_list]
        self.assertEqual(verbs, ["list", "show"])
```

Also add a fixture containing one Boris record and one legacy record. Run a
Boris-only backfill and assert the legacy record is deeply equal to its
pre-run copy. Import `json`, `subprocess`, and `mock` from `unittest` for the
adapter test.

- [ ] **Step 2: Verify the new tests fail**

```bash
scripts/run_tests.sh \
  tests/cron/test_just_do_it_review_reaction_watchdog.py::BorisMigrationTest \
  -q --file-retries 0
```

Expected: FAIL because transitions still use one global owned set and have no
rendering style.

- [ ] **Step 3: Convert classifiers and aggregation to semantic state**

Replace classifier returns as follows:

```python
# blocking signal
return "signal", STATE_BLOCKED, why

# non-blocking comment signal
return "signal", STATE_NOTES, why

# exact-head approval signal
return "signal", STATE_APPROVED, "approval submitted/existing for head"

# active lane contribution
{"state": STATE_ACTIVE, "reason": f"active lane status={row.get('status')}"}
```

Replace `aggregate_emoji` call sites with `aggregate_state`; render only at the
Slack boundary:

```python
desired_state = aggregate_state(list(lanes.values()))
desired_reaction = (
    render_reaction(permalink, desired_state, args.boris_reaction_style)
    if desired_state is not None
    else None
)
```

Update approval comparisons to `STATE_APPROVED`, update classifier assertions
using the exact mapping from Task 2, and remove the old `PRECEDENCE`/
`OWNED_EMOJI` globals only after all call sites use `STATE_PRECEDENCE` and
`owned_reactions(permalink)`. Do not change marker vocabulary or classifier
ordering.

- [ ] **Step 4: Make transition ownership explicit**

Change the transition signatures and loops to use the passed channel-scoped
tuple:

```python
def apply_transition(permalink, desired, record, reactor, owned):
    prior = record.get("emoji") if isinstance(record, dict) else None
    pending = [
        reaction
        for reaction in ((record or {}).get("pending_removals") or [])
        if reaction in owned
    ]
    add = reactor(permalink, desired, False)
    if not add.get("ok"):
        return {
            "ok": False,
            "emoji": desired,
            "prior": prior,
            "error": add.get("error") or add.get("raw") or "reaction add failed",
            "calls": 1,
            "pending_removals": pending,
        }
    stale = set(pending)
    if prior in owned:
        stale.add(prior)
    else:
        stale.update(owned)
    stale.discard(desired)
    # Keep the existing deterministic remove/error accumulation logic,
    # iterating `owned`, not a global tuple.
```

Apply the same explicit `owned` parameter to `reconcile_sweep` and every call
site.

- [ ] **Step 5: Persist semantic state only on migrated Boris records**

Add:

```python
def record_state(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    return normalize_review_state(record.get("state") or record.get("emoji"))


def lane_states(record: object) -> dict[str, str]:
    lanes = record.get("lanes") if isinstance(record, dict) else None
    return {
        str(task_id): state
        for task_id, raw in (lanes.items() if isinstance(lanes, dict) else [])
        if (state := normalize_review_state(raw)) is not None
    }
```

For each post, pass `owned_reactions(permalink)` into the transition. On a
successful Boris transition persist:

```python
{
    "state": desired_state,
    "emoji": desired_reaction,
    "lanes": semantic_lanes,
}
```

When a Boris post already has the desired rendered emoji (notably
`doge_notes`) but still has legacy lane values, persist the same semantic
`state` and `lanes` as a ledger-only migration with zero Slack calls. For a
converged legacy post, leave the entire original record and lane values deeply
equal. Do not globally normalize records inside `migrate_state`.

- [ ] **Step 6: Add a reversible operator style and invocation lock**

Add the parser option:

```python
ap.add_argument(
    "--boris-reaction-style",
    choices=("modern", "legacy"),
    default="modern",
    help="Render #boris-reviews with modern reactions (default) or legacy reactions for rollback.",
)
```

Add a nonblocking process lock around load/process/save so a scheduled tick
and a manual backfill cannot overwrite each other's state:

```python
@contextlib.contextmanager
def exclusive_run_lock(path: Path | None = None):
    path = path or STATE_PATH.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
```

If the lock is busy, return 0 without loading, reacting, or saving. Add a test
that holds the lock from one descriptor and proves a second invocation performs
zero reactor and state writes.

- [ ] **Step 7: Run migration, restart, and regression coverage**

```bash
scripts/run_tests.sh tests/cron/test_just_do_it_review_reaction_watchdog.py \
  -q --file-retries 0
```

Expected: PASS, including existing quarantine, unresolved-hold, reconciliation,
compaction, and failure-retry tests.

- [ ] **Step 8: Commit the reversible migration**

```bash
git add cron/scripts/just_do_it_review_reaction_watchdog.py \
  tests/cron/test_just_do_it_review_reaction_watchdog.py
git commit -m "feat(cron): migrate Boris review reactions"
```

---

### Task 4: Change only Boris intake's initial reaction

**Files:**
- Modify: `cron/scripts/boris_intake/slack_io.py`
- Modify: `cron/scripts/boris_intake/daemon.py`
- Modify: `tests/cron/boris_intake/test_slack_io.py`
- Modify: `tests/cron/boris_intake/test_daemon.py`

**Interfaces:**
- Consumes: an accepted `ReviewCommand` after successful lane dispatch.
- Produces: `SlackIO.add_reviewing_reaction(channel: str, ts: str) -> None`, which best-effort adds only `rubbing`.

- [ ] **Step 1: Change the tests first**

Replace the old initial-reaction test with:

```python
def test_adds_only_rubbing():
    calls, call = recorder()
    SlackIO(call, "t", dry_run=False).add_reviewing_reaction("C1", "1.0")
    assert calls == [
        (
            "reactions.add",
            {"channel": "C1", "timestamp": "1.0", "name": "rubbing"},
        )
    ]
```

Update dry-run and failure-swallowing tests to call
`add_reviewing_reaction`. In `test_live_creates_lane_reacts_and_acks`, assert
the reaction call's name is exactly `rubbing`, not merely that
`reactions.add` occurred.

- [ ] **Step 2: Verify the Boris tests fail**

```bash
scripts/run_tests.sh \
  tests/cron/boris_intake/test_slack_io.py \
  tests/cron/boris_intake/test_daemon.py \
  -q --file-retries 0
```

Expected: FAIL because `add_reviewing_reaction` does not exist.

- [ ] **Step 3: Implement the narrow intake change**

In `slack_io.py`:

```python
def add_reviewing_reaction(self, channel: str, ts: str) -> None:
    self._post("reactions.add", channel=channel, timestamp=ts, name="rubbing")
```

Update the module and `reply_ack` docstrings to name `rubbing`, then change the
single daemon call after successful lane persistence:

```python
self.slack.add_reviewing_reaction(cmd.channel_id, cmd.message_ts)
```

Do not reorder lane creation, `record_lane`, `record_request`, reaction, or
acknowledgement.

- [ ] **Step 4: Run the full Boris compatibility gate**

```bash
scripts/run_tests.sh tests/cron/boris_intake -q --file-retries 0
```

Expected: PASS. No authorization, grammar, template, guard, dispatch, dedup,
audit, store, catch-up, or GitHub behavior changes.

- [ ] **Step 5: Run Boris plus reaction together**

```bash
scripts/run_tests.sh \
  tests/cron/boris_intake \
  tests/cron/test_just_do_it_review_reaction_watchdog.py \
  -q --file-retries 0
```

Expected: PASS.

- [ ] **Step 6: Commit the intake seam**

```bash
git add cron/scripts/boris_intake tests/cron/boris_intake
git commit -m "feat(boris): use rubbing for active reviews"
```

---

### Task 5: Build the exact-ID retirement controller

**Files:**
- Create: `scripts/ops/retire_just_do_it_crons.py`
- Create: `tests/cron/test_retire_just_do_it_crons.py`

**Interfaces:**
- Consumes: a Hermes home, source root, archive root, execution DB, board DB,
  exact job IDs, and an explicit immutable recovery commit.
- Produces:
  - `preflight(paths, now, recovery_ref) -> Manifest`
  - `pause_producers(paths, manifest, now) -> Manifest`
  - `verify_quiescent(paths, manifest, now, grace_seconds=180) -> list[str]`
  - `checkpoint_component(paths, manifest, component, now) -> Manifest`
  - `finalize(paths, manifest, now) -> Manifest`
  - `verify_retired(paths, manifest, now) -> list[str]`
  - `rollback(paths, manifest, now) -> Manifest`
  - `rehearse_reaction_roundtrip(state_path, watchdog_path) -> dict[str, Any]`

- [ ] **Step 1: Write isolated failure-first tests**

Build each fixture under `tmp_path`; never reference `/Users/ctao/.hermes` in
the tests. Cover these exact cases:

```python
RETIRE_IDS = {"ee4582d0598f", "ff865c33c5ea"}
RETAIN_ID = "6daa151da351"


def test_preflight_refuses_name_schedule_or_script_drift(retirement_fixture):
    fixture = retirement_fixture
    fixture.jobs[0]["script"] = "unexpected.py"
    fixture.write_jobs()
    with pytest.raises(RetirementError, match="fingerprint mismatch"):
        preflight(fixture.paths, fixture.now, fixture.recovery_ref)


def test_quiescence_rejects_running_or_post_pause_execution(retirement_fixture):
    manifest = retirement_fixture.preflight_and_pause()
    retirement_fixture.insert_execution(
        "ff865c33c5ea",
        status="running",
        claimed_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 1),
    )
    errors = verify_quiescent(
        retirement_fixture.paths,
        manifest,
        manifest.cutoff_epoch + 181,
    )
    assert any("post-pause execution" in error for error in errors)


def test_finalize_removes_only_retired_ids_and_archives_entrypoints(retirement_fixture):
    manifest = retirement_fixture.preflight_pause_and_age()
    finalize(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181)
    ids = {job["id"] for job in retirement_fixture.read_jobs()}
    assert ids == {RETAIN_ID}
    assert retirement_fixture.retired_live_paths_are_absent()
    assert retirement_fixture.archived_paths_match_hashes(manifest)


def test_finalize_preserves_outputs_ledgers_and_tasks(retirement_fixture):
    before = retirement_fixture.audit_hashes()
    manifest = retirement_fixture.preflight_pause_and_age()
    finalize(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181)
    assert retirement_fixture.audit_hashes() == before
    assert retirement_fixture.output_paths_are_present_for(RETIRE_IDS)


def test_finalize_preserves_a_pre_pause_execution_output(retirement_fixture):
    manifest = retirement_fixture.preflight_and_pause()
    retirement_fixture.finish_pre_pause_execution_and_write_output()
    expected = retirement_fixture.audit_hashes()
    finalize(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181)
    assert retirement_fixture.audit_hashes() == expected


def test_rollback_restores_sources_before_jobs(retirement_fixture):
    manifest = retirement_fixture.complete_retirement()
    events = []
    rollback(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240, events.append)
    assert events.index("sources_restored") < events.index("jobs_restored")
    assert retirement_fixture.all_fingerprints_match(manifest)


def test_reaction_checkpoint_requires_retained_job_paused(retirement_fixture):
    manifest = retirement_fixture.preflight_only()
    with pytest.raises(RetirementError, match="retained job must be paused"):
        checkpoint_component(
            retirement_fixture.paths,
            manifest,
            "retained-reaction",
            retirement_fixture.now,
        )


def test_rollback_uses_latest_component_checkpoints(retirement_fixture):
    manifest = retirement_fixture.complete_retirement()
    manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)
    manifest = retirement_fixture.checkpoint_and_change_boris(manifest)
    rollback(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240)
    assert retirement_fixture.reaction_matches_latest_checkpoint()
    assert retirement_fixture.boris_matches_latest_checkpoint()


def test_reaction_rehearsal_roundtrips_only_boris_records(retirement_fixture):
    before_legacy = retirement_fixture.legacy_reaction_records()
    report = rehearse_reaction_roundtrip(
        retirement_fixture.reaction_state_path,
        retirement_fixture.watchdog_source_path,
    )
    assert report["modern_second_pass_calls"] == 0
    assert report["legacy_second_pass_calls"] == 0
    assert report["legacy_records_unchanged"] is True
    assert retirement_fixture.legacy_reaction_records() == before_legacy
```

- [ ] **Step 2: Verify the retirement tests fail**

```bash
scripts/run_tests.sh tests/cron/test_retire_just_do_it_crons.py \
  -q --file-retries 0
```

Expected: FAIL because the controller does not exist.

- [ ] **Step 3: Implement exact constants and manifest types**

The controller must declare exact expected identities:

```python
RETIRE = {
    "ee4582d0598f": {
        "name": "just-do-it-channel-watch",
        "script": "slack_channel_watch.py",
        "schedule": {"kind": "interval", "minutes": 2},
    },
    "ff865c33c5ea": {
        "name": "just-do-it-blocked-review-requeue-watchdog",
        "script": "just_do_it_blocked_review_requeue_watchdog.py",
        "schedule": {"kind": "cron", "expr": "*/2 * * * *"},
    },
}
RETAIN_ID = "6daa151da351"
RETAIN_SCRIPT = "just_do_it_review_reaction_watchdog.py"
DEPRECATED_CREATOR = "just-do-it-blocked-review-requeue-watchdog"
DEPRECATED_IDEMPOTENCY_PREFIX = "just-do-it-rereview:"
```

Use dataclasses with JSON serialization for `Paths`, `FileFingerprint`, and
`Manifest`. `Paths` resolves these locations from `hermes_home`, with explicit
overrides accepted by tests:

```python
@dataclass(frozen=True)
class Paths:
    hermes_home: Path
    source_root: Path
    archive_root: Path
    jobs_path: Path
    executions_path: Path
    board_path: Path
    live_scripts: Path
    state_dir: Path
    cron_output: Path
    boris_launchd_plist: Path
```

The manifest records:

- UTC creation and pause cutoff as both integer epoch and ISO-8601 text;
- full backed-up job records for all three IDs;
- SHA-256, mode, size, and active/archive paths for scripts and tests;
- latest execution identity/timestamp per retired job;
- task census for deprecated creator/idempotency provenance and legacy source
  permalinks; and
- hashes plus snapshot paths for CRON outputs, three channel watermarks, both
  review ledgers, the live reaction script/test, the complete Boris source and
  test tree, a consistent SQLite backup of `boris_intake.db`, Boris audit/
  watermark/heartbeat files, `config.yaml`, and
  `/Users/ctao/Library/LaunchAgents/ai.hermes.boris-intake.plist`.

Snapshot `boris_intake.db` through `sqlite3.Connection.backup`, not by copying
its live database/WAL/SHM files independently. Copy secret-bearing config as
an opaque mode-preserving file, hash it, and never print or embed its contents
in the manifest or command output. Require an explicit full 40-hex recovery
commit ID, verify that it is the exact commit object and an ancestor of current
`HEAD`, and record that resolved commit plus blob/tree hashes for every durable
recovery path. Never search history or fall back to `HEAD`. The two deprecated
requeue paths must exist at the pinned commit but may be absent at `HEAD` after
Task 6. Retained reaction and Boris durable paths must have identical committed
objects at the pin and `HEAD`, and every durable path must be clean in the
worktree. The pinned Git objects are the recoverable backup while retained
`HEAD` identity proves the deployable retained code has not diverged.

- [ ] **Step 4: Implement guarded operations through `cron.jobs`**

Use `cron.jobs.use_cron_store(paths.hermes_home)` for locked job mutations.
`preflight` requires the explicit immutable recovery commit, then enters that
store and the registry lock while loading the three records, validating exact
fingerprints, and copying the raw `jobs.json` bytes; it releases the lock before
hashing unrelated files.
`pause_producers` calls `pause_job(id, reason="guarded just-do-it retirement")`
by exact ID and asserts `enabled is False` and `state == "paused"` afterward.

`pause_producers` records `cutoff_epoch` and `cutoff_iso` only after both pause
writes have been verified. `verify_quiescent` must return errors unless:

```python
now - manifest.cutoff_epoch >= grace_seconds
```

and both execution queries are empty:

```sql
SELECT job_id, status, claimed_at, started_at, finished_at, pid, process_started_at
FROM executions
WHERE job_id IN (?, ?)
  AND (
    status IN ('claimed', 'running', 'unknown')
    OR unixepoch(claimed_at) > ?
  )
```

Bind `manifest.cutoff_epoch` as the final SQL parameter; never compare the ISO
text column to an integer directly. It must also reject a live owner for any
matching execution using PID plus process-start identity, reject an exact
script-name match in an injected process listing, and compare a fresh board
census against the manifest. Reject new rows with the deprecated creator,
idempotency prefix, or a legacy-channel `Source Slack message:` permalink.
Tests inject the process listing; they never inspect unrelated host processes.

`finalize` first calls `verify_quiescent`, then records a second, quiescent
hash/snapshot set for output, watermarks, and ledgers. This permits a
pre-pause invocation to finish normally without treating its legitimate final
audit write as rollout drift; all post-quiescence comparisons use this second
set. It then uses `os.replace` to move only the four exact live script/test
paths into the archive and verifies their hashes. Because
`cron.jobs.remove_job` normally deletes a job's output
directory, protect each retiring output directory with `os.replace` into a
manifested temporary archive path before calling `remove_job`; immediately
restore it to the exact original output path and re-verify the tree hash after
each successful registry removal. A failed remove also restores the protected
output before returning an error. It never deletes output or state.

`rollback` restores and hash-verifies source first, then restores the exact
backed-up job records under `cron.jobs.use_cron_store` plus the registry lock
using `load_jobs`/`save_jobs`; it must not call `resume_job`, which would
rewrite the backed-up record. It restores optional retained-reaction, ledger,
and Boris snapshots before their job metadata when those components were
marked changed in the manifest. It refuses to restore either retired job
record when a source hash differs, and it never rewrites preserved output,
watermark, or legacy ledger paths when their current hashes still equal the
manifest.

`verify_retired` fails unless both retired IDs and their four active
entrypoints are absent, their post-cutoff execution count and new task census
are zero, both retired output directories, the three retired channel
watermarks, and the blocked-review requeue ledger match the quiescent hashes,
and `6daa151da351` has the exact retained ID/script, one-minute schedule,
`no_agent=true`, enabled/scheduled state, Boris display name, and a successful
latest execution. The reaction ledger is checked for Boris-only migration and
legacy projection equality rather than pre-migration hash equality. When a
Boris checkpoint exists, verification checks launchd running state, heartbeat
advancement, and logical audit/store continuity while allowing ordinary new
intake activity; it never reads or prints secret configuration.

`checkpoint_component` accepts only `retained-reaction` or `boris-intake`.
The retained-reaction checkpoint requires `6daa151da351` to be paused and
atomically appends fresh job/script/test/ledger snapshots to the manifest. The
Boris checkpoint requires the retained watchdog to be enabled and healthy,
then snapshots the complete Boris source tree, consistent SQLite database,
audit/watermark/heartbeat files, config, and launchd plist. Rollback always
uses the newest completed file/state checkpoint for a changed component, but
restores retained job `6daa151da351` from the original enabled preflight
record rather than from the temporary paused installation-lock record.

- [ ] **Step 5: Add a nonmutating CLI by default**

Expose explicit subcommands:

```text
preflight
pause
check-quiescence
checkpoint
finalize
verify
rollback
rehearse-reactions
```

Require `--manifest` with the absolute path printed by `preflight` for every
state-changing command after `preflight`.
Require preflight alone to receive `--recovery-ref` as an exact full 40-hex
commit ID. Subsequent manifest commands consume the persisted recovery identity
and must not require the option again.
Require `--apply` for `pause`, `checkpoint`, `finalize`, and `rollback`; without it, print the
validated action plan and exit without writes. Support `--hermes-home` and
`--source-root`, plus explicit `--board-path` and `--boris-launchd-plist`
overrides, so tests and rehearsal never resolve a live path. `verify` requires
`--expect retired|restored`. Accept `--now-epoch` only when the resolved
`--hermes-home` is not `/Users/ctao/.hermes`; reject it against the live home.
`checkpoint` requires `--component retained-reaction|boris-intake`.

`rehearse-reactions` imports the versioned watchdog, deep-copies the supplied
ledger, and uses an in-memory reactor seeded from each record's applied emoji.
It performs modern, settled-modern, legacy, and settled-legacy passes over
only Boris permalinks. It writes neither the supplied ledger nor Slack and
prints the four call counts plus a deep-equality result for legacy records.

- [ ] **Step 6: Run the retirement controller tests**

```bash
scripts/run_tests.sh tests/cron/test_retire_just_do_it_crons.py \
  -q --file-retries 0
```

Expected: PASS.

- [ ] **Step 7: Run scheduler regression tests**

```bash
scripts/run_tests.sh \
  tests/cron/test_jobs.py \
  tests/cron/test_execution_ledger.py \
  tests/cron/test_jobs_crossprocess_lock.py \
  tests/cron/test_retire_just_do_it_crons.py \
  -q --file-retries 0
```

Expected: PASS.

- [ ] **Step 8: Commit the controller**

```bash
git add scripts/ops/retire_just_do_it_crons.py \
  tests/cron/test_retire_just_do_it_crons.py
git commit -m "feat(cron): add guarded just-do-it retirement"
```

---

### Task 6: Remove the deprecated durable source and close documentation contracts

**Files:**
- Delete: `cron/scripts/just_do_it_blocked_review_requeue_watchdog.py`
- Delete: `tests/cron/test_just_do_it_blocked_review_requeue_watchdog.py`
- Modify: `docs/superpowers/specs/2026-08-08-just-do-it-cron-retirement-boris-reactions-design.md`

**Interfaces:**
- Consumes: the tested retirement archive/rollback controller.
- Produces: a deployment tree with no durable automatic requeue source and a spec status that points to this plan.

- [ ] **Step 1: Remove only the deprecated tracked runtime and test**

Use `apply_patch` to delete these exact files:

```text
cron/scripts/just_do_it_blocked_review_requeue_watchdog.py
tests/cron/test_just_do_it_blocked_review_requeue_watchdog.py
```

Do not delete the retained reaction source, Boris intake, historical root-state
files, or CRON output.

- [ ] **Step 2: Update the spec status**

Change only the status line to:

```markdown
**Status:** Approved; implementation plan committed
```

Add below it:

```markdown
**Implementation plan:** `docs/superpowers/plans/2026-08-08-just-do-it-cron-retirement-boris-reactions.md`
```

- [ ] **Step 3: Verify no runtime import depends on the retired module**

```bash
git grep -n 'just_do_it_blocked_review_requeue_watchdog' -- \
  ':!docs/superpowers/**' ':!scripts/ops/retire_just_do_it_crons.py' \
  ':!tests/cron/test_retire_just_do_it_crons.py'
```

Expected: no output.

- [ ] **Step 4: Run all versioned focused gates**

```bash
scripts/run_tests.sh \
  tests/cron/test_just_do_it_review_reaction_watchdog.py \
  tests/cron/boris_intake \
  tests/cron/test_retire_just_do_it_crons.py \
  tests/cron/test_jobs.py \
  tests/cron/test_execution_ledger.py \
  tests/cron/test_jobs_crossprocess_lock.py \
  -q --file-retries 0
```

Expected: PASS.

- [ ] **Step 5: Commit the durable retirement**

Because `docs/superpowers` is intentionally ignored, force-add only the two
approved documents:

```bash
git add cron/scripts/just_do_it_blocked_review_requeue_watchdog.py \
  tests/cron/test_just_do_it_blocked_review_requeue_watchdog.py
git add -f \
  docs/superpowers/specs/2026-08-08-just-do-it-cron-retirement-boris-reactions-design.md \
  docs/superpowers/plans/2026-08-08-just-do-it-cron-retirement-boris-reactions.md
git commit -m "chore(cron): remove deprecated just-do-it requeue"
```

---

### Task 7: Rehearse deployment and rollback entirely against isolated copies

**Files:**
- No source changes expected.
- Create runtime-only evidence under a temporary directory.

**Interfaces:**
- Consumes: Tasks 1-6 and copies of live registry/state/scripts.
- Produces: a passing rehearsal transcript proving migration and retirement rollback before production writes.

- [ ] **Step 1: Create an isolated Hermes home**

```bash
rehearsal_home=$(mktemp -d -t hermes-jdi-retirement-rehearsal.XXXXXX)
mkdir -p "$rehearsal_home/cron/output" "$rehearsal_home/scripts" "$rehearsal_home/state"
cp /Users/ctao/.hermes/cron/jobs.json "$rehearsal_home/cron/jobs.json"
sqlite3 /Users/ctao/.hermes/cron/executions.db \
  ".backup '$rehearsal_home/cron/executions.db'"
sqlite3 /Users/ctao/.hermes/kanban.db ".backup '$rehearsal_home/kanban.db'"
cp /Users/ctao/.hermes/config.yaml "$rehearsal_home/config.yaml"
cp /Users/ctao/Library/LaunchAgents/ai.hermes.boris-intake.plist \
  "$rehearsal_home/ai.hermes.boris-intake.plist"
cp /Users/ctao/.hermes/state/just_do_it_review_reactions.json "$rehearsal_home/state/"
cp /Users/ctao/.hermes/state/just_do_it_blocked_review_requeue.json "$rehearsal_home/state/"
cp /Users/ctao/.hermes/state/slack_channel_watch_C08K8FQKX9S.json "$rehearsal_home/state/"
cp /Users/ctao/.hermes/state/slack_channel_watch_C07BU8AAPB3.json "$rehearsal_home/state/"
cp /Users/ctao/.hermes/state/slack_channel_watch_C09339EGZ9U.json "$rehearsal_home/state/"
cp /Users/ctao/.hermes/state/boris_intake_audit.jsonl "$rehearsal_home/state/"
cp /Users/ctao/.hermes/state/boris_intake_heartbeat "$rehearsal_home/state/"
cp /Users/ctao/.hermes/state/boris_intake_watermark_C0BPCKPEFCG.txt "$rehearsal_home/state/"
sqlite3 /Users/ctao/.hermes/state/boris_intake.db \
  ".backup '$rehearsal_home/state/boris_intake.db'"
cp /Users/ctao/.hermes/scripts/slack_channel_watch.py "$rehearsal_home/scripts/"
cp /Users/ctao/.hermes/scripts/test_slack_channel_watch.py "$rehearsal_home/scripts/"
cp /Users/ctao/.hermes/scripts/just_do_it_blocked_review_requeue_watchdog.py "$rehearsal_home/scripts/"
cp /Users/ctao/.hermes/scripts/test_just_do_it_blocked_review_requeue_watchdog.py "$rehearsal_home/scripts/"
cp /Users/ctao/.hermes/scripts/just_do_it_review_reaction_watchdog.py "$rehearsal_home/scripts/"
cp /Users/ctao/.hermes/scripts/test_just_do_it_review_reaction_watchdog.py "$rehearsal_home/scripts/"
cp -a /Users/ctao/.hermes/scripts/boris_intake "$rehearsal_home/scripts/"
for job_id in ee4582d0598f ff865c33c5ea 6daa151da351; do
  cp -a "/Users/ctao/.hermes/cron/output/$job_id" "$rehearsal_home/cron/output/"
done
```

Expected: all copies exist; no live path is modified.

- [ ] **Step 2: Run preflight and pause against the isolated home**

Pin recovery to `7f5bc40da3f97e181bfc00ba1688cd0f065ff10a`, the reviewed
Task 5 commit immediately before Task 6 deleted the two deprecated versioned
paths. Preflight recovers those deleted artifacts from that exact ancestor,
while requiring the retained reaction/Boris Git objects to remain identical at
the pin and current deployable `HEAD`. It never searches history or falls back
to another commit.

```bash
rehearsal_preflight=$(python scripts/ops/retire_just_do_it_crons.py preflight \
  --hermes-home "$rehearsal_home" \
  --source-root "$PWD" \
  --recovery-ref 7f5bc40da3f97e181bfc00ba1688cd0f065ff10a \
  --board-path "$rehearsal_home/kanban.db" \
  --boris-launchd-plist "$rehearsal_home/ai.hermes.boris-intake.plist" \
  --archive-root "$rehearsal_home/forensics")
rehearsal_manifest=$(printf '%s' "$rehearsal_preflight" | jq -r '.manifest_path')
test -f "$rehearsal_manifest"
python scripts/ops/retire_just_do_it_crons.py pause \
  --hermes-home "$rehearsal_home" \
  --manifest "$rehearsal_manifest" \
  --apply
```

Expected: the two exact jobs are paused and `6daa151da351` remains enabled.

- [ ] **Step 3: Age only the isolated fixture and complete retirement**

Use the controller's test-only `--now-epoch` override, which is accepted only
when `--hermes-home` differs from the real default home:

```bash
cutoff=$(jq -r '.cutoff_epoch' "$rehearsal_manifest")
python scripts/ops/retire_just_do_it_crons.py check-quiescence \
  --hermes-home "$rehearsal_home" \
  --manifest "$rehearsal_manifest" \
  --now-epoch "$((cutoff + 181))"
python scripts/ops/retire_just_do_it_crons.py finalize \
  --hermes-home "$rehearsal_home" \
  --manifest "$rehearsal_manifest" \
  --now-epoch "$((cutoff + 181))" \
  --apply
```

Expected: both retired IDs and four active entrypoints are absent; audit files
and retained job remain.

- [ ] **Step 4: Rehearse modern and reverse reaction migration without Slack**

Run the controller's nonmutating reaction rehearsal against the copied ledger:

```bash
python scripts/ops/retire_just_do_it_crons.py rehearse-reactions \
  --state-path "$rehearsal_home/state/just_do_it_review_reactions.json" \
  --watchdog-path "$PWD/cron/scripts/just_do_it_review_reaction_watchdog.py"
```

Expected report:

```text
modern pass: only C0BPCKPEFCG calls; desired modern emojis; add-before-remove
second modern pass: reaction_calls=0
reverse pass: only C0BPCKPEFCG calls; desired legacy emojis; add-before-remove
second reverse pass: reaction_calls=0
legacy records: deep-equal before and after all four passes
```

Use the existing `FakeSlack`; do not invoke `slack_react.py`.

- [ ] **Step 5: Rehearse retirement rollback**

```bash
python scripts/ops/retire_just_do_it_crons.py rollback \
  --hermes-home "$rehearsal_home" \
  --manifest "$rehearsal_manifest" \
  --apply
python scripts/ops/retire_just_do_it_crons.py verify \
  --hermes-home "$rehearsal_home" \
  --manifest "$rehearsal_manifest" \
  --expect restored
```

Expected: exact source hashes and all three backed-up job records are restored.

- [ ] **Step 6: Remove the temporary rehearsal home**

Resolve and print the exact temporary path first. Then remove that one explicit
path only after all evidence has been captured. This is disposable test data;
it contains copies, not authoritative state.

```bash
printf '%s\n' "$rehearsal_home"
case "$rehearsal_home" in
  /var/folders/*/T/hermes-jdi-retirement-rehearsal.*) rm -rf -- "$rehearsal_home" ;;
  *) printf 'refusing unexpected rehearsal path: %s\n' "$rehearsal_home"; exit 2 ;;
esac
```

---

### Task 8: Back up and quiesce the two live lane producers

**Files:**
- Create: `/Users/ctao/.hermes/forensics/just-do-it-retirement-YYYYMMDDTHHMMSSZ/`
- Modify: `/Users/ctao/.hermes/cron/jobs.json` through the controller.

**Interfaces:**
- Consumes: a passing Task 7 rehearsal and the fresh live fingerprint census.
- Produces: exact backups plus a minimum 180-second proven quiescent cutoff.

- [ ] **Step 1: Re-run all focused gates immediately before live mutation**

```bash
scripts/run_tests.sh \
  tests/cron/test_just_do_it_review_reaction_watchdog.py \
  tests/cron/boris_intake \
  tests/cron/test_retire_just_do_it_crons.py \
  -q --file-retries 0
```

Expected: PASS.

- [ ] **Step 2: Take the live preflight backup**

Use the same reviewed immutable recovery commit rehearsed in Task 7. The pin
supplies only the deleted deprecated recovery objects; exact retained
reaction/Boris object equality against current `HEAD` proves it cannot bless
stale deployable code.

```bash
retirement_preflight=$(python scripts/ops/retire_just_do_it_crons.py preflight \
  --hermes-home /Users/ctao/.hermes \
  --source-root "$PWD" \
  --recovery-ref 7f5bc40da3f97e181bfc00ba1688cd0f065ff10a \
  --archive-root /Users/ctao/.hermes/forensics)
retirement_manifest=$(printf '%s' "$retirement_preflight" | jq -r '.manifest_path')
test -f "$retirement_manifest"
printf '%s\n' "$retirement_manifest"
```

Expected: a timestamped archive and manifest containing the exact three job
records, source hashes, execution cutoff, board census, and audit hashes. The
command is nonmutating toward jobs, board, Slack, and state.

- [ ] **Step 3: Pause only the two exact live IDs**

First run without `--apply` and review the printed targets, then run:

```bash
python scripts/ops/retire_just_do_it_crons.py pause \
  --hermes-home /Users/ctao/.hermes \
  --manifest "$retirement_manifest" \
  --apply
```

Expected: `ee4582d0598f` and `ff865c33c5ea` are paused;
`6daa151da351` is still enabled and scheduled.

- [ ] **Step 4: Poll after the first bounded wait**

Wait no more than 45 seconds, then query executions and jobs through the
controller's `check-quiescence` command. Expected: it reports that the required
180 seconds have not elapsed and reports no new retired-job execution.

- [ ] **Step 5: Poll after the second bounded wait**

Wait no more than 45 seconds and repeat. Expected: still paused; no post-cutoff
execution or new deprecated-provenance task.

- [ ] **Step 6: Poll after the third bounded wait**

Wait no more than 45 seconds and repeat. Expected: still paused and healthy;
the retained reaction job and Boris daemon continue normally.

- [ ] **Step 7: Poll after the fourth bounded wait and prove quiescence**

Wait no more than 45 seconds and run:

```bash
python scripts/ops/retire_just_do_it_crons.py check-quiescence \
  --hermes-home /Users/ctao/.hermes \
  --manifest "$retirement_manifest"
```

Expected: PASS with at least 180 seconds elapsed, no claimed/running/unknown or
post-cutoff execution, no new deprecated-provenance lane, and no relevant live
process.

- [ ] **Step 8: Finalize full retirement**

Run once without `--apply`, review the exact move/remove actions, then run:

```bash
python scripts/ops/retire_just_do_it_crons.py finalize \
  --hermes-home /Users/ctao/.hermes \
  --manifest "$retirement_manifest" \
  --apply
```

Expected: both exact IDs are absent; channel-watch and requeue live script/test
paths are absent; their archive hashes match; retained ID, outputs, task rows,
watermarks, and ledgers remain.

---

### Task 9: Deploy and migrate the retained reaction path

**Files:**
- Replace: `/Users/ctao/.hermes/scripts/just_do_it_review_reaction_watchdog.py`
- Replace: `/Users/ctao/.hermes/scripts/test_just_do_it_review_reaction_watchdog.py`
- Modify: job `6daa151da351` metadata.
- Migrate: `/Users/ctao/.hermes/state/just_do_it_review_reactions.json`.

**Interfaces:**
- Consumes: retired/quiescent producers and versioned tested reaction source.
- Produces: an enabled one-minute Boris reaction watchdog and migrated Boris-only posts.

- [ ] **Step 1: Pause the retained job only as an installation lock**

```bash
hermes cron pause 6daa151da351
```

Expected: exact ID is paused. Confirm the Boris daemon remains running.

- [ ] **Step 2: Back up the live reaction script, test, job record, and ledger**

Run the guarded checkpoint only after Step 1 has paused the retained ID:

```bash
python scripts/ops/retire_just_do_it_crons.py checkpoint \
  --hermes-home /Users/ctao/.hermes \
  --manifest "$retirement_manifest" \
  --component retained-reaction \
  --apply
```

Expected: the existing retirement archive contains fresh reaction script,
test, exact job record, and ledger snapshots with verified SHA-256/mode/size
records. Nothing in the live source, registry, ledger, or Slack has changed.

- [ ] **Step 3: Install the tested reaction source and test**

```bash
install -m 755 cron/scripts/just_do_it_review_reaction_watchdog.py \
  /Users/ctao/.hermes/scripts/just_do_it_review_reaction_watchdog.py
install -m 644 tests/cron/test_just_do_it_review_reaction_watchdog.py \
  /Users/ctao/.hermes/scripts/test_just_do_it_review_reaction_watchdog.py
```

Expected: installed hashes equal the worktree hashes.

- [ ] **Step 4: Prove installed bytes and run the hermetic reaction tests**

The canonical runner intentionally strips arbitrary environment variables, so
do not rely on `HERMES_REACTION_WATCHDOG_SCRIPT` crossing its clean-env
boundary. Prove both installed files are byte-identical, then test those exact
versioned bytes:

```bash
cmp -s cron/scripts/just_do_it_review_reaction_watchdog.py \
  /Users/ctao/.hermes/scripts/just_do_it_review_reaction_watchdog.py
cmp -s tests/cron/test_just_do_it_review_reaction_watchdog.py \
  /Users/ctao/.hermes/scripts/test_just_do_it_review_reaction_watchdog.py
scripts/run_tests.sh \
  tests/cron/test_just_do_it_review_reaction_watchdog.py \
  -q --file-retries 0
```

Expected: PASS and no Slack calls.

- [ ] **Step 5: Rename and resume the retained job**

Use this exact prompt:

```text
No-agent authoritative state machine for source-linked Boris reviewer lanes and historical legacy reviewer lanes. In #boris-reviews it maintains exactly one owned reaction with precedence face_vomiting > doge_notes > rubbing > blinkapproved. Legacy watched-channel reactions retain their prior vocabulary. Quiet when settled; emits compact transition, hold, reconciliation, or failure output. It never creates or mutates Kanban tasks. Do not schedule additional cron jobs.
```

Run:

```bash
boris_reaction_prompt='No-agent authoritative state machine for source-linked Boris reviewer lanes and historical legacy reviewer lanes. In #boris-reviews it maintains exactly one owned reaction with precedence face_vomiting > doge_notes > rubbing > blinkapproved. Legacy watched-channel reactions retain their prior vocabulary. Quiet when settled; emits compact transition, hold, reconciliation, or failure output. It never creates or mutates Kanban tasks. Do not schedule additional cron jobs.'
hermes cron edit 6daa151da351 \
  --name boris-review-reaction-state-watchdog \
  --prompt "$boris_reaction_prompt"
hermes cron resume 6daa151da351
```

Expected: same ID, `* * * * *`, `no_agent=true`, compatible script path,
enabled/scheduled, new display name.

- [ ] **Step 6: Take a fresh Boris/legacy ledger census**

Record current per-channel post counts and reaction distribution. This is the
comparison baseline; do not assert the earlier observed count of 30.

- [ ] **Step 7: Run bounded modern Boris-only migration passes**

Run one pass at a time:

```bash
/Users/ctao/.hermes/hermes-agent/venv/bin/python \
  /Users/ctao/.hermes/scripts/just_do_it_review_reaction_watchdog.py \
  --channel C0BPCKPEFCG \
  --backfill-existing \
  --boris-reaction-style modern \
  --verbose
```

After each pass inspect `METRICS`, failures, holds, and pending removals. Repeat
only while bounded eligible work remains. The process lock serializes manual
and scheduled invocations.

Expected: old `thinkies`, `review-no`, and `white_check_mark` on eligible Boris
posts are replaced add-before-remove; `doge_notes` remains `doge_notes`; no
legacy-channel permalink is touched.

- [ ] **Step 8: Prove a second settled pass is a zero-write no-op**

Run the same command after convergence.

Expected: `reaction_calls=0`, `posts_transitioned=0`, no failure/hold output,
and the legacy-channel ledger projection is deep-equal to the Step 6 baseline.

---

### Task 10: Deploy the Boris initial reaction and verify the entire cutover

**Files:**
- Replace: `/Users/ctao/.hermes/scripts/boris_intake/slack_io.py`
- Replace: `/Users/ctao/.hermes/scripts/boris_intake/daemon.py`
- Restart: `ai.hermes.boris-intake`.

**Interfaces:**
- Consumes: a healthy modern retained watchdog.
- Produces: natural new Boris requests start at `:rubbing:`; all later states converge through the watchdog.

- [ ] **Step 1: Reconfirm retained-watchdog health before Boris restart**

Verify ID `6daa151da351` is enabled, its last status is `ok`, no reaction failures
or holds are pending, and the migration is settled. If not, stop without
touching Boris.

- [ ] **Step 2: Install only the two changed Boris files**

First take a fresh consistent Boris checkpoint:

```bash
python scripts/ops/retire_just_do_it_crons.py checkpoint \
  --hermes-home /Users/ctao/.hermes \
  --manifest "$retirement_manifest" \
  --component boris-intake \
  --apply
```

Expected: the controller confirms the retained watchdog is enabled and healthy
and appends verified Boris source/state/config snapshots. Then run:

```bash
install -m 644 cron/scripts/boris_intake/slack_io.py \
  /Users/ctao/.hermes/scripts/boris_intake/slack_io.py
install -m 644 cron/scripts/boris_intake/daemon.py \
  /Users/ctao/.hermes/scripts/boris_intake/daemon.py
```

Expected: installed hashes match the tested versioned source; all other Boris
source hashes remain equal to the preflight manifest.

- [ ] **Step 3: Prove installed Boris bytes and run the compatibility suite**

Compare every live Boris source file (excluding runtime caches and the live
test copy) with the versioned tree, then run the hermetic versioned suite. This
avoids relying on a custom environment variable that `scripts/run_tests.sh`
intentionally strips:

```bash
for source in cron/scripts/boris_intake/*.py; do
  cmp -s "$source" "/Users/ctao/.hermes/scripts/boris_intake/${source##*/}"
done
scripts/run_tests.sh tests/cron/boris_intake -q --file-retries 0
```

Expected: PASS; no live Slack, GitHub, board, or Boris state writes.

- [ ] **Step 4: Restart Boris intake**

```bash
launchctl kickstart -k gui/$(id -u)/ai.hermes.boris-intake
```

Expected: a new PID, running state, no new stderr error, and heartbeat
advancement. Do not synthesize a production review request.

- [ ] **Step 5: Verify intake state and configuration are unchanged**

Compare preflight/postrestart hashes or logical snapshots for configuration,
dedup lane table, audit record count, watermarks, and terminal-guard source.
Expected changes are limited to normal heartbeat/audit activity; no dedup or
authorization reset.

- [ ] **Step 6: Poll one retired schedule interval plus grace**

Use separate waits no longer than 45 seconds. After at least 180 seconds, run:

```bash
python scripts/ops/retire_just_do_it_crons.py verify \
  --hermes-home /Users/ctao/.hermes \
  --manifest "$retirement_manifest"
```

Expected:

```text
retired IDs absent
post-cutoff retired executions = 0
deprecated active entrypoints absent
new deprecated-provenance lanes = 0
retained ID enabled and healthy
Boris launchd service and heartbeat healthy
modern migration settled; zero-write second pass
legacy posts and audit-only state preserved
```

- [ ] **Step 7: Inspect the final diff and evidence**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -8
```

Record the final source commit, live installed hashes, retirement manifest
path, migration before/after counts, test results, job state, service state,
and rollback archive path in the handoff. Do not include tokens, Slack message
content, or secret-bearing config values.

---

## Stop and rollback conditions

Immediately stop forward rollout when any of these occurs:

- a job ID/name/script/schedule fingerprint differs from the manifest;
- a retired job executes after the pause cutoff;
- a relevant execution remains claimed/running/unknown after grace;
- a new deprecated-provenance or legacy-channel source-linked lane appears;
- a migration pass targets a non-Boris permalink;
- an add failure would require removing the previous reaction;
- the retained watchdog writes or attempts a Kanban mutation;
- Boris compatibility tests fail outside the intentional reaction assertion;
- Boris heartbeat stops or launchd restart fails; or
- exact source/state rollback hashes cannot be proven.

Rollback order is:

1. preserve current failure evidence;
2. use the modern watchdog's `legacy` style to reverse-migrate Boris reactions;
3. restore and verify reaction/Boris source, state, and retained job metadata;
4. restore and verify deprecated sources from the archive; and only then
5. restore exact deprecated job records if the old intake must be re-enabled.

Never restore a deprecated job record before its compatible source has been
hash-verified.
