import os
import re
from dataclasses import replace
from types import SimpleNamespace

from pr_remediation import lanes
from pr_remediation.boris_source import EvidenceAnchor, Issue, Verdict
from pr_remediation.lanes import (
    MAX_BOT_THREADS_SECTION_CHARS, MAX_BRIEF_CHARS, TERMINAL_LANE_STATUSES,
    LaneIO, build_brief, idempotency_key,
)

VERDICT = Verdict("br_1", "Ylopo", "example", 42, "a" * 40, "1.0",
                  "ISSUES_FOUND", None,
                  (Issue(title="Null deref in handler"), Issue(title="Race in seed")))

# An issue carrying the full structure boris_intake produces, used by the
# tests below that check what build_brief does with fields beyond the
# title -- VERDICT above deliberately stays title-only so the plain
# "every blocking issue is named" tests aren't coupled to this shape.
RICH_ISSUE = Issue(
    title="Null deref in handler",
    expected_behavior="Handler returns 400 on a missing field",
    changed_mechanism="The new guard short-circuits before validation",
    trigger="POST with a missing `id` field",
    consequence="500 instead of 400, leaking a stack trace",
    impact_scope="Every caller of /webhook",
    action="Move the guard after validation",
    evidence_anchors=(
        EvidenceAnchor(kind="diff", reference="src/webhook.py", detail="line 42"),
        EvidenceAnchor(kind="test", reference="tests/test_webhook.py::test_missing_id"),
    ),
)
RICH_VERDICT = replace(VERDICT, blocking_issues=(RICH_ISSUE,))

# Deliberately big enough to overflow BOTH the cap this change lowered
# MAX_BRIEF_CHARS to and the 20,000 it used to be, so the truncation tests
# below fail for a truncation reason rather than for the cap's value.
_OVERFLOW_ISSUE_COUNT = 30
_OVERFLOW_FILLER = 700


def _overflowing_issues(count: int = _OVERFLOW_ISSUE_COUNT) -> tuple[Issue, ...]:
    """`count` issues, each identifiable on its own and each carrying an
    evidence anchor whose reference is a real-looking repo path.

    The path is the tell: a render that cuts mid-way leaves something like
    `src/path_number_18.p` -- a broken path that reads like a complete one --
    which is exactly what the tests below look for.
    """
    return tuple(
        Issue(title=f"Issue number {n}",
              expected_behavior="e" * _OVERFLOW_FILLER,
              evidence_anchors=(
                  EvidenceAnchor(kind="diff", reference=f"src/path_number_{n}.py"),))
        for n in range(1, count + 1)
    )


# --- a fixture the size boris_intake's output actually is --------------------
#
# RICH_ISSUE above is a stub: ~40 characters a field, and a three-issue
# verdict built from it renders at 2,324 characters, 28% of the cap. It cannot
# tell you anything about what the cap does, and a "live-sized" test built on
# it reported a brief comfortably inside a limit the real thing sits at 88% of.
#
# These numbers are the real ones, read off the live verdict this module's cap
# was measured against -- br_732853620fd14dfb8f6c5db13d8fb1d2
# (Ylopo/million-stars#199), whose three blocking issues carry 1,271 / 1,328 /
# 1,414 characters of prose across the same seven fields, plus 3-4 evidence
# anchors on 29-113-character references with 50-168-character details. Each
# length below is that verdict's per-field MEDIAN, and three of these issues
# render as 6,234 characters of issue blocks against the live verdict's 6,252
# (a 6,889-character brief against its 6,914) -- within 0.4%, which is the
# whole point: this fixture has to be the size of the thing the cap is about.
_LIVE_FIELD_CHARS = {
    "expected_behavior": 158, "changed_mechanism": 326, "trigger": 195,
    "consequence": 229, "impact_scope": 124, "action": 284,
}
_LIVE_ANCHOR_REF_CHARS = 84
_LIVE_ANCHOR_DETAIL_CHARS = 85
_LIVE_ANCHOR_PATH = ("src/app/(dashboard)/design-studio/site/(workspace)/"
                     "_components/workspace-shell-{n:03d}.tsx")


def _live_sized_issue(number: int) -> Issue:
    """One blocking issue at the size Boris really produces them.

    Each field is padded to its live median length but stays individually
    identifiable, so a test can assert a specific issue survived rather than
    only that something did.
    """
    fields = {name: f"[{number}:{name}] ".ljust(length, "z")
              for name, length in _LIVE_FIELD_CHARS.items()}
    detail = f"anchor detail for issue {number}".ljust(_LIVE_ANCHOR_DETAIL_CHARS, ".")
    return Issue(
        title=f"Issue {number}: a seeded value passes the first-publish gate",
        evidence_anchors=(
            EvidenceAnchor(
                kind="diff",
                reference=_LIVE_ANCHOR_PATH.format(n=number).ljust(
                    _LIVE_ANCHOR_REF_CHARS, "x"),
                detail=detail),
            EvidenceAnchor(
                kind="ticket",
                reference=f"PLAT-{2300 + number} acceptance".ljust(
                    _LIVE_ANCHOR_REF_CHARS, "x"),
                detail=detail),
            EvidenceAnchor(
                kind="test",
                reference=f"tests/publish_gate_{number:03d}.test.tsx".ljust(
                    _LIVE_ANCHOR_REF_CHARS, "x"),
                detail=detail),
        ),
        **fields,
    )


LIVE_SIZED_VERDICT = replace(
    VERDICT, blocking_issues=tuple(_live_sized_issue(n) for n in (1, 2, 3)))


def _hostile_paths(count: int, length: int = 140) -> list[dict]:
    """`count` DISTINCT paths of exactly `length` characters each.

    Distinct paths, not thread count, are what drive the section's size --
    the renderer dedups -- and 140 characters is the 90th percentile of real
    Ylopo repo paths (the thousand-stars tree: median 61, p90 140, max 246).
    """
    threads = []
    for n in range(count):
        suffix = f"/component_{n:05d}.tsx"
        path = "src/" + "d" * (length - len("src/") - len(suffix)) + suffix
        assert len(path) == length
        threads.append({"path": path, "outdated": n % 2 == 0})
    return threads


def _hostile_paths_with_duplicate_threads(
        path_count: int, threads_per_path: int, length: int = 140) -> list[dict]:
    """`path_count` DISTINCT paths, each carrying `threads_per_path` threads
    against it -- so `len(bot_threads) == path_count * threads_per_path`
    while the number of distinct paths stays `path_count`.

    Every other fixture in this file (`_hostile_paths`) uses exactly one
    thread per distinct path, so `len(bot_threads)` and the number of
    distinct paths are always numerically identical there -- a real PR with
    more review comments than touched files (a normal shape: bots post one
    thread per finding, not per file) is exactly what this fixture is for.
    """
    threads = []
    for n in range(path_count):
        suffix = f"/component_{n:05d}.tsx"
        path = "src/" + "d" * (length - len("src/") - len(suffix)) + suffix
        assert len(path) == length
        for t in range(threads_per_path):
            threads.append({"path": path, "outdated": t % 2 == 0})
    return threads


def _issues_rendered(brief: str, count: int = _OVERFLOW_ISSUE_COUNT) -> list[int]:
    """Which of `_overflowing_issues`'s issues actually reached the brief,
    matched on the whole heading line so `1` cannot match `11`."""
    lines = set(brief.splitlines())
    return [n for n in range(1, count + 1) if f"{n}. Issue number {n}" in lines]


def _valid_anchor(tmp_path, monkeypatch, repo=VERDICT.repo):
    """Point ANCHOR_ROOT at a temp dir with a valid git anchor for `repo`.

    Mirrors how the module itself resolves ANCHOR_ROOT (via the
    PR_FIX_ANCHOR_ROOT env var, read live off the module global rather than
    copied at import time) so tests can redirect it without touching a real
    anchor under /Users/ctao/.hermes/state/pr-review-anchors.
    """
    anchor_root = tmp_path / "anchors"
    (anchor_root / repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(lanes, "ANCHOR_ROOT", str(anchor_root))


def test_brief_names_every_blocking_issue():
    brief = build_brief(VERDICT, 1)
    assert "Null deref in handler" in brief
    assert "Race in seed" in brief


def test_brief_pins_the_exact_head_so_andrej_cannot_drift():
    assert "a" * 40 in build_brief(VERDICT, 1)


def test_brief_names_the_pr():
    assert "Ylopo/example#42" in build_brief(VERDICT, 1)


# --- the full issue structure, not only the title --------------------------


def test_brief_carries_the_full_issue_structure_not_only_the_title():
    """The fix this whole change exists for: build_brief must stop
    discarding everything Boris found except the title. Every field on
    RICH_ISSUE has to reach the rendered brief."""
    brief = build_brief(RICH_VERDICT, 1)
    assert RICH_ISSUE.title in brief
    assert RICH_ISSUE.expected_behavior in brief
    assert RICH_ISSUE.changed_mechanism in brief
    assert RICH_ISSUE.trigger in brief
    assert RICH_ISSUE.consequence in brief
    assert RICH_ISSUE.impact_scope in brief
    assert RICH_ISSUE.action in brief


def test_diff_anchors_are_surfaced_as_the_file_paths_they_are():
    """The `diff` anchor's `reference` IS a real repo-relative file path
    (see the module docstring) -- it must appear in the brief labelled as a
    file, which is what lets the agent go straight to the code instead of
    rediscovering where the finding lives."""
    brief = build_brief(RICH_VERDICT, 1)
    assert "file: src/webhook.py" in brief
    assert "line 42" in brief, "the anchor's detail must not be dropped"


def test_non_diff_anchors_are_labelled_by_their_own_kind_not_as_a_file():
    """A `test` anchor's reference is a test id, not a path -- mislabelling
    it as `file:` would send the agent looking for a file that does not
    exist at that name."""
    brief = build_brief(RICH_VERDICT, 1)
    assert "test: tests/test_webhook.py::test_missing_id" in brief
    assert "file: tests/test_webhook.py::test_missing_id" not in brief


def test_an_issue_with_no_evidence_anchors_still_renders():
    """RICH_ISSUE's siblings in VERDICT carry no evidence at all -- the
    common case for a title-only issue -- and must not blow up or leave a
    dangling 'Evidence:' header with nothing under it.

    The absence assertion is the one that matters and it was missing: with
    only the two title checks, making `_issue_block`'s
    `lines.append("   - Evidence:")` unconditional -- producing precisely the
    dangling header this docstring names -- left the whole suite green. A
    dangling `Evidence:` tells the agent evidence was recorded and then sends
    it looking for anchors that were never there.
    """
    brief = build_brief(VERDICT, 1)
    assert "Null deref in handler" in brief and "Race in seed" in brief
    assert "Evidence:" not in brief, (
        "no issue in VERDICT carries an anchor, so an 'Evidence:' header here "
        "is a label with nothing under it")


def test_an_untitled_issue_renders_the_placeholder_not_a_blank_heading():
    """`_issue_block`'s `issue.title or '(untitled)'` fallback, pinned. It is
    the reason boris_source must coerce a null title to "" rather than to the
    literal string "None" -- see test_boris_source.py's null-title test: a
    truthy "None" walks straight past this fallback and the brief reads
    `1. None`."""
    verdict = replace(VERDICT, blocking_issues=(Issue(title=""),))
    assert "1. (untitled)" in build_brief(verdict, 1)


# --- acceptance criteria ----------------------------------------------------


def test_brief_states_acceptance_criteria():
    """The old brief said what to fix and what not to do, but never what
    'done' means -- the agent had no stopping condition. This is the fix."""
    brief = build_brief(RICH_VERDICT, 1)
    assert "Acceptance criteria" in brief
    assert "cleared at a pushed head" in brief
    assert "tests covering them" in brief


def test_acceptance_criteria_count_matches_the_number_of_blocking_issues():
    brief = build_brief(VERDICT, 1)
    assert "All 2 listed blocking issue" in brief


def test_prohibitions_are_kept():
    """No new PR, no force-push, no base-branch change -- unchanged
    substance, now living inside the acceptance-criteria section."""
    brief = build_brief(RICH_VERDICT, 1)
    assert "Do not open a new PR" in brief
    assert "Do not force-push" in brief
    assert "Do not change the PR's base branch" in brief


# --- base and head branch ---------------------------------------------------


def test_brief_names_the_base_and_head_branch_when_known():
    """Label-to-value binding, not mere presence.

    Asserting only that both names appear *somewhere* left the header
    completely unpinned: swapping the two values (so the brief reads
    `Base branch: chad/fix-webhook` / `Head branch: main`) and deleting the
    `Head branch:` line outright both kept the suite green. Either one makes
    the brief contradict its own push instruction about which branch is
    which -- the exact ambiguity this enrichment existed to remove.
    """
    brief = build_brief(RICH_VERDICT, 1, base_branch="main",
                        head_branch="chad/fix-webhook")
    assert "Base branch: main" in brief
    assert "Head branch: chad/fix-webhook" in brief


def test_brief_tells_the_agent_to_push_the_actual_head_branch_not_the_worktree_s():
    """The real ambiguity this closes: the old brief said 'push' without
    saying where, and the worktree a lane runs in is checked out on a
    kanban-generated branch that is NOT the PR's. The head branch name must
    appear in the instruction that tells the agent where to push."""
    brief = build_brief(RICH_VERDICT, 1, base_branch="main",
                        head_branch="chad/fix-webhook")
    lowered = brief.lower()
    assert "push to" in lowered
    assert "chad/fix-webhook" in lowered.split("push to", 1)[-1], \
        "the head branch name must appear at or after the push instruction"


def test_brief_degrades_visibly_when_branches_are_unknown():
    """None (unreadable GitHub, or never asked) must render as a visible
    placeholder -- never a guessed name, and never silently omitted."""
    brief = build_brief(RICH_VERDICT, 1)
    assert "unknown" in brief.lower()
    assert "confirm" in brief.lower()


# --- Codex/CodeQL bot threads ------------------------------------------------
#
# GitHubIO.unresolved_bot_threads() only ever hands back an inventory --
# {"path": str, "outdated": bool} dicts, no comment text, no thread id -- so
# the brief carries a count and a distinct-path list plus a fetch
# instruction, never rendered finding bodies. See lanes.py's module
# docstring for why bodies are not inlined the way Boris's are.


def test_bot_threads_render_as_inventory_not_bodies():
    brief = build_brief(VERDICT, 1, bot_threads=[
        {"path": "src/a.ts", "outdated": False},
        {"path": "src/a.ts", "outdated": True},
        {"path": "src/b.ts", "outdated": False},
    ])
    assert "3 unresolved Codex/CodeQL review thread(s)" in brief
    assert "src/a.ts" in brief and "src/b.ts" in brief
    # the inventory names each path once, not once per thread
    assert brief.count("src/a.ts") == 1


def test_bot_threads_render_the_outdated_split_in_the_heading():
    """Matches the precedent `slack_io.py`'s `_thread_notice` sets ("N
    unresolved bot review thread(s) (M outdated) on: ...") and its reasoning:
    an outdated thread is not a resolved one, so it is counted, and the split
    is shown because it is real triage signal. `outdated` was being fetched
    and then thrown away here.

    Heading only, deliberately -- a per-path marker would grow the list, and
    the list is the thing MAX_BOT_THREADS_SECTION_CHARS is keeping small.
    """
    brief = build_brief(VERDICT, 1, bot_threads=[
        {"path": "src/a.ts", "outdated": True},
        {"path": "src/b.ts", "outdated": True},
        {"path": "src/c.ts", "outdated": False},
    ])
    assert "3 unresolved Codex/CodeQL review thread(s) (2 outdated)" in brief


def test_bot_threads_omit_the_outdated_split_when_none_are_outdated():
    brief = build_brief(VERDICT, 1, bot_threads=[
        {"path": "src/a.ts", "outdated": False}])
    assert "1 unresolved Codex/CodeQL review thread(s), across" in brief
    assert "outdated" not in brief


def test_bot_threads_tell_the_agent_how_to_fetch_the_thread_bodies():
    """The brief carries paths, not comment text, and this sentence is the
    entire justification for that: the lane runs in a worktree with `gh` in
    it, so it can read the threads itself. Without the sentence the inventory
    names files with no way to find out what was said about them.
    """
    brief = build_brief(VERDICT, 1, bot_threads=[
        {"path": "src/a.ts", "outdated": False}])
    assert "gh api graphql" in brief
    assert "gh pr view --comments" in brief
    assert "resolve each thread you addressed" in brief


def test_the_bot_thread_section_sits_before_the_acceptance_criteria():
    """Ordering is load-bearing, not cosmetic. kanban's `_cap` head-truncates
    the task body, so whatever sits last is what disappears first -- and the
    last thing in this brief has to be the stopping condition and the push
    target, never a file listing. Moving the section below the push-target
    line left the whole suite green.
    """
    brief = build_brief(VERDICT, 1, base_branch="main", head_branch="chad/fix",
                        bot_threads=[{"path": "src/a.ts", "outdated": False}])
    assert (brief.index("## Codex / CodeQL threads")
            < brief.index("## Acceptance criteria")
            < brief.index("Fix them at this head and push to"))


def test_bot_threads_none_says_the_check_failed():
    """None means 'could not be determined' and must never render as clean.

    Including in the acceptance criteria: an undetermined read still leaves
    the agent a check it owes, so the bullet stays. Only a verified-clean
    read owns nothing. Without this assertion, collapsing lanes.py's
    `bot_threads is not None and not bot_threads` down to `not bot_threads`
    -- which drops the bullet for None too, turning "we could not tell" into
    "nothing to do" in the one section that states what done means -- left
    the entire suite passing.
    """
    brief = build_brief(VERDICT, 1, bot_threads=None)
    assert "could not be determined" in brief
    assert "0 unresolved" not in brief
    assert "resolve every unresolved codex/codeql thread" in brief.lower()


def test_bot_threads_empty_says_clean():
    brief = build_brief(VERDICT, 1, bot_threads=[])
    assert "No unresolved Codex/CodeQL threads" in brief


def test_an_empty_tuple_is_read_as_clean_by_both_predicates_not_one():
    """`_bot_threads_section` tests `not bot_threads`; the acceptance bullet
    used to test `bot_threads == []`. They disagreed for an empty tuple,
    which rendered "No unresolved Codex/CodeQL threads at the time this lane
    opened" and then, three lines later, told the agent to resolve the
    threads that sentence had just said did not exist.

    `GitHubIO.unresolved_bot_threads` only returns `list | None`, so nothing
    in production reaches this -- but `test_loop.py`'s `GitHubFake` defaulted
    to `()`, so every existing loop test's "clean PR" case ran on a value
    production never produces. That is how a self-contradicting brief goes
    unnoticed, so the two predicates are pinned to agree.
    """
    brief = build_brief(VERDICT, 1, bot_threads=())
    assert "No unresolved Codex/CodeQL threads" in brief
    assert "resolve every unresolved codex/codeql thread" not in brief.lower()


def test_acceptance_criteria_require_bot_threads_resolved():
    brief = build_brief(VERDICT, 1, bot_threads=[{"path": "src/a.ts", "outdated": False}])
    assert "resolve every unresolved codex/codeql thread" in brief.lower()


def test_acceptance_criteria_do_not_mention_bot_threads_when_clean():
    """No unresolved threads means nothing to resolve -- the acceptance
    bullet must not appear when bot_threads is an empty list."""
    brief = build_brief(VERDICT, 1, bot_threads=[])
    assert "resolve every unresolved codex/codeql thread" not in brief.lower()


def test_create_threads_bot_threads_into_the_dispatched_body(tmp_path, monkeypatch):
    """LaneIO.create must hand bot_threads straight to build_brief, the same
    way it already does for base_branch/head_branch."""
    _valid_anchor(tmp_path, monkeypatch)
    seen = {}

    def run(command, **kwargs):
        seen["argv"] = command
        return SimpleNamespace(returncode=0, stdout="t_abc123", stderr="")

    LaneIO(tmp_path / "kanban.db", runner=run).create(
        VERDICT, 1, bot_threads=[{"path": "src/webhook.py", "outdated": False}])
    body_value = seen["argv"][seen["argv"].index("--body") + 1]
    assert "src/webhook.py" in body_value
    assert "unresolved Codex/CodeQL review thread(s)" in body_value


# --- size cap and truncation -------------------------------------------------


def test_our_brief_cap_does_not_exceed_the_kanban_body_cap_that_actually_binds():
    """The limit that binds is kanban's, not ours.

    `hermes_cli/kanban_db.py`'s `build_worker_context` -- what the engineer
    agent actually reads -- applies `_cap(task.body, _CTX_MAX_BODY_BYTES)`,
    and that `_cap` truncates from the HEAD:
    `s[:limit] + "… [truncated, N chars omitted]"`. The acceptance criteria,
    the three prohibitions and the push target all live at the END of the
    brief, so they are the first thing kanban drops. A cap of ours that sits
    above kanban's means our own truncation -- which protects the footer by
    construction and says what it dropped -- never gets to fire.

    Read off kanban's real constant rather than a literal 8192 on purpose:
    a copy of a contract can only ever confirm the copy. This must fail if
    EITHER side moves.
    """
    from hermes_cli.kanban_db_context import _CTX_MAX_BODY_BYTES

    assert MAX_BRIEF_CHARS <= _CTX_MAX_BODY_BYTES, (
        f"MAX_BRIEF_CHARS={MAX_BRIEF_CHARS} exceeds kanban's "
        f"_CTX_MAX_BODY_BYTES={_CTX_MAX_BODY_BYTES}; build_worker_context "
        "head-truncates task.body at that cap, so the brief would lose its "
        "acceptance criteria and push target before the agent ever read them")


def test_truncation_drops_whole_issues_instead_of_cutting_one_mid_render():
    """The unit of truncation is one issue, not one character.

    Cutting the rendered text mid-way produced lines like
    `- file: src/path_number_118.p` -- a broken repo path that reads exactly
    like a complete one, and that the agent would go looking for.
    """
    verdict = replace(VERDICT, blocking_issues=_overflowing_issues())
    brief = build_brief(verdict, 1, base_branch="main", head_branch="chad/fix")

    complete = re.findall(r"src/path_number_\d+\.py", brief)
    anything = re.findall(r"src/path_number_\S*", brief)
    assert complete == anything, (
        f"a path was cut mid-token: {sorted(set(anything) - set(complete))}")

    for n in _issues_rendered(brief):
        assert f"src/path_number_{n}.py" in brief, (
            f"issue {n}'s heading is rendered but its evidence anchor is not -- "
            "a rendered issue must be rendered whole")


def test_truncation_states_how_many_issues_it_omitted():
    """The footer counts every blocking issue in its acceptance criteria, so
    a brief that renders only some of them has to say how many are missing.
    Otherwise the agent gets a stopping condition it cannot even enumerate
    and no signal that anything is absent at all."""
    verdict = replace(VERDICT, blocking_issues=_overflowing_issues())
    brief = build_brief(verdict, 1, base_branch="main", head_branch="chad/fix")

    rendered = _issues_rendered(brief)
    assert rendered, "at least one issue must survive"
    assert len(rendered) < _OVERFLOW_ISSUE_COUNT, "this fixture must overflow the cap"
    assert rendered == list(range(1, len(rendered) + 1)), \
        "the issues kept must be the leading run, not an arbitrary subset"

    omitted = _OVERFLOW_ISSUE_COUNT - len(rendered)
    assert f"{omitted} further blocking issue(s)" in brief, (
        f"{omitted} issues were dropped and the brief never says so")
    assert f"All {_OVERFLOW_ISSUE_COUNT} listed blocking issue(s)" in brief, \
        "the footer still counts them all, which is what the notice reconciles"


def test_a_truncated_brief_still_fits_the_cap_and_keeps_its_footer():
    verdict = replace(VERDICT, blocking_issues=_overflowing_issues())
    brief = build_brief(verdict, 1, base_branch="main", head_branch="chad/fix")
    assert len(brief) <= MAX_BRIEF_CHARS
    assert "Acceptance criteria" in brief
    assert "Do not force-push" in brief
    assert "Head branch: chad/fix" in brief


def test_an_ordinary_brief_is_nowhere_near_the_cap():
    assert len(build_brief(RICH_VERDICT, 1)) < MAX_BRIEF_CHARS // 4


def test_an_oversized_brief_is_truncated_and_says_so():
    huge_issue = Issue(title="Huge issue",
                       expected_behavior="x" * (MAX_BRIEF_CHARS * 2))
    verdict = replace(VERDICT, blocking_issues=(huge_issue,))
    brief = build_brief(verdict, 1)
    assert len(brief) <= MAX_BRIEF_CHARS
    assert "TRUNCATED" in brief


def test_truncation_never_costs_the_acceptance_criteria_or_the_footer():
    """The part of the brief that answers 'what does done mean' and 'where
    do I push' must survive truncation intact -- only the issue detail may
    be cut. A brief that looks complete but silently dropped its stopping
    condition would be worse than a short one that admits it was cut."""
    huge_issue = Issue(title="Huge issue",
                       expected_behavior="x" * (MAX_BRIEF_CHARS * 3))
    verdict = replace(VERDICT, blocking_issues=(huge_issue,))
    brief = build_brief(verdict, 1, base_branch="main", head_branch="chad/fix")
    assert "Acceptance criteria" in brief
    assert "Do not force-push" in brief
    assert "chad/fix" in brief


def test_a_brief_within_the_cap_is_not_marked_truncated():
    assert "TRUNCATED" not in build_brief(RICH_VERDICT, 1)


def test_a_hostile_bot_thread_inventory_cannot_evict_a_live_sized_verdicts_payload():
    """The invariant: supporting context never evicts the payload.

    Boris's blocking issues are what this brief is FOR. The bot-thread
    inventory is context around them, and it lives in the footer `_bounded`
    deliberately refuses to trim -- so before `MAX_BOT_THREADS_SECTION_CHARS`
    existed, a PR touching enough distinct files pushed the issues out of the
    brief (`_bounded` subtracts the footer from the issue budget) and then
    pushed the brief itself past the cap, at which point kanban's own
    head-truncate took the acceptance criteria and the push target off the
    end too. Measured against this fixture's live-sized ancestor: 22 distinct
    140-character paths cost it an issue, 40 cost it all three, and 60 cost
    it the acceptance criteria and the push target.

    The assertion that bites is NOT "the brief fits the cap" -- the test this
    replaces asserted exactly that and passed with the whole section deleted,
    because a brief with no section trivially fits. It is that every issue
    Boris found, the stopping condition, and the push target are all still
    there with a hostile inventory attached.
    """
    threads = _hostile_paths(200)
    brief = build_brief(LIVE_SIZED_VERDICT, 1, base_branch="main",
                        head_branch="chad/fix", bot_threads=threads)

    for issue in LIVE_SIZED_VERDICT.blocking_issues:
        assert issue.title in brief, (
            f"{issue.title!r} was evicted by a 200-path bot-thread inventory")
    assert "TRUNCATED" not in brief, \
        "no issue may be dropped to make room for the inventory"
    assert "## Acceptance criteria" in brief
    assert "Do not force-push" in brief
    assert "push to the PR's head branch, `chad/fix`" in brief
    assert len(brief) <= MAX_BRIEF_CHARS
    # and the inventory is still there doing its job, not silently dropped
    assert "200 unresolved Codex/CodeQL review thread(s)" in brief


def test_a_hostile_bot_thread_inventory_cannot_evict_a_live_sized_verdicts_payload_when_branches_are_unknown():
    """Same invariant as the test above, swept against the header shape that
    actually decides where the budget's guard bites.

    `base_branch="main", head_branch="chad/fix"` renders the SHORT header.
    `branches()` returns `None, None` whenever `gh pr view` fails -- a real
    production path, not a hypothetical one -- and that renders a header 119
    characters longer via the "(unknown -- confirm with `gh pr view`...)"
    placeholders. `_bounded` computes the issue budget as
    `MAX_BRIEF_CHARS - len(header) - len(footer)`, so the longer header
    leaves less room before the same `MAX_BOT_THREADS_SECTION_CHARS`-sized
    inventory starts costing an issue. Sweeping only the known-branch shape
    left `MAX_BOT_THREADS_SECTION_CHARS` free to drift up to 1450 with
    nothing here noticing that 1308-1450 all lose a Boris issue on this
    header.
    """
    threads = _hostile_paths(200)
    brief = build_brief(LIVE_SIZED_VERDICT, 1, base_branch=None,
                        head_branch=None, bot_threads=threads)

    for issue in LIVE_SIZED_VERDICT.blocking_issues:
        assert issue.title in brief, (
            f"{issue.title!r} was evicted by a 200-path bot-thread inventory")
    assert "TRUNCATED" not in brief, \
        "no issue may be dropped to make room for the inventory"
    assert "## Acceptance criteria" in brief
    assert "Do not force-push" in brief
    assert ("push to the PR's head branch named above "
            "(NOT the worktree's own branch)") in brief
    assert len(brief) <= MAX_BRIEF_CHARS
    # and the inventory is still there doing its job, not silently dropped
    assert "200 unresolved Codex/CodeQL review thread(s)" in brief


def test_the_bot_thread_section_never_outgrows_its_own_budget():
    """Whatever `unresolved_bot_threads` hands back, including the 2,000
    threads `_MAX_THREAD_PAGES` x 100 allows at its ceiling, and including
    paths at the longest length the Ylopo tree contains (246 characters)."""
    for count, length in ((1, 61), (200, 140), (2000, 140), (2000, 246),
                          (50, 4000)):
        section = lanes._bot_threads_section(_hostile_paths(count, length))
        assert len(section) <= MAX_BOT_THREADS_SECTION_CHARS, (
            f"{count} paths of {length} chars rendered {len(section)} chars, "
            f"over the {MAX_BOT_THREADS_SECTION_CHARS} budget")


def test_the_bot_thread_inventory_lists_whole_paths_or_none_of_them():
    """Same rule as `_bounded`'s, for the same reason, learned the same way:
    a cut that lands mid-token leaves a broken repo path that reads exactly
    like a complete one and that the agent goes looking for."""
    threads = _hostile_paths(200)
    brief = build_brief(LIVE_SIZED_VERDICT, 1, bot_threads=threads)
    listed = re.findall(r"^- (\S+)$", brief, flags=re.MULTILINE)
    assert listed, "the inventory must still list something"
    known = {t["path"] for t in threads}
    assert set(listed) <= known, (
        f"a path was cut mid-token: {sorted(set(listed) - known)}")


def test_the_bot_thread_inventory_previews_the_lexicographically_first_paths():
    """`paths = sorted({...})` is a deliberate choice, not an artifact of set
    iteration order: preview the lexicographically-first N paths every time,
    not whichever N a set's iteration order (hash-randomized per process for
    strings, by default) happens to produce first. Without `sorted`, the same
    PR would preview a different N files on every render.
    """
    threads = _hostile_paths(200)
    brief = build_brief(LIVE_SIZED_VERDICT, 1, bot_threads=threads)
    listed = re.findall(r"^- (\S+)$", brief, flags=re.MULTILINE)
    assert listed, "the inventory must still list something"
    assert len(listed) < 200, "this fixture must overflow the section budget"
    expected = sorted({t["path"] for t in threads})[:len(listed)]
    assert listed == expected, (
        "the preview must be the sorted-first N paths, not an arbitrary N")


def test_the_bot_thread_inventory_says_how_many_paths_it_did_not_list():
    """A list that quietly stops is worse than a short one that admits it.

    The count in the heading stays the TRUE thread total -- it is what the
    agent is being held to -- so the omission line is the only thing that
    stops the heading and the list under it from contradicting each other.

    Fixture is 200 distinct paths carrying 10 threads each (2,000 threads),
    not `_hostile_paths`' one-thread-per-path shape: with one thread per
    path, `len(paths)` and `len(bot_threads)` are numerically identical, so
    `_paths_omission_line(len(paths) - index)` mutated to
    `len(bot_threads) - index` renders the same digits and the assertion
    below cannot tell the two apart. 200 paths / 2,000 threads makes the
    omission count PATHS, provably, not threads.
    """
    threads = _hostile_paths_with_duplicate_threads(200, 10)
    brief = build_brief(LIVE_SIZED_VERDICT, 1, bot_threads=threads)
    listed = re.findall(r"^- (\S+)$", brief, flags=re.MULTILINE)
    assert 0 < len(listed) < 200, "this fixture must overflow the section budget"
    assert f"{200 - len(listed)} further file(s) not listed" in brief, (
        f"{200 - len(listed)} paths were dropped and the brief never says so")
    assert "2000 unresolved Codex/CodeQL review thread(s)" in brief, \
        "the heading must keep the true thread total, not the listed-path count"


def test_a_short_bot_thread_inventory_is_not_marked_as_omitting_anything():
    brief = build_brief(VERDICT, 1, bot_threads=_hostile_paths(3, 61))
    assert "further file(s) not listed" not in brief


def test_create_parses_the_lane_id_out_of_dispatch_output(tmp_path, monkeypatch):
    _valid_anchor(tmp_path, monkeypatch)

    def run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout="Created t_abc123 (engineer)\n",
                               stderr="")

    assert LaneIO(tmp_path / "kanban.db", runner=run).create(VERDICT, 1) == "t_abc123"


def test_create_uses_the_dispatch_interface_the_script_actually_accepts(tmp_path, monkeypatch):
    """Guards against drifting back to invented flags like --profile/--pr.

    Also guards against a `--body @file` reference: `hermes_cli/kanban.py`'s
    `_cmd_create` stores `args.body` verbatim (confirmed by reading the source
    directly -- no `@`-prefix resolution exists anywhere in the CLI or in
    `kanban_dispatch.sh`), so a literal `@<path>` argv token would write the
    literal string "@/tmp/xyz.md" as the lane's body instead of the actual
    remediation brief. The body must be the real text, passed the same way
    `boris_intake/dispatch.py`'s `create_lane()` already does it.
    """
    _valid_anchor(tmp_path, monkeypatch)
    seen = {}

    def run(command, **kwargs):
        seen["argv"] = command
        return SimpleNamespace(returncode=0, stdout="t_abc123", stderr="")

    LaneIO(tmp_path / "kanban.db", runner=run).create(VERDICT, 2)
    argv = seen["argv"]
    assert argv[1] == "create"
    assert "--assignee" in argv and argv[argv.index("--assignee") + 1] == "engineer"
    assert any(a.startswith("worktree:") for a in argv)
    assert "--body" in argv
    body_value = argv[argv.index("--body") + 1]
    assert not body_value.startswith("@"), "the real CLI never resolves @file references"
    assert "Null deref in handler" in body_value and "Race in seed" in body_value
    assert "--profile" not in argv and "--brief" not in argv


def test_create_threads_base_and_head_branch_into_the_dispatched_body(tmp_path, monkeypatch):
    """loop.py's _open_lane reads these off GitHubIO.branches() and hands
    them to create() as keyword-only args (see LaneIO.create) -- they must
    land in the actual --body text, not be silently dropped."""
    _valid_anchor(tmp_path, monkeypatch)
    seen = {}

    def run(command, **kwargs):
        seen["argv"] = command
        return SimpleNamespace(returncode=0, stdout="t_abc123", stderr="")

    LaneIO(tmp_path / "kanban.db", runner=run).create(
        VERDICT, 1, base_branch="main", head_branch="chad/fix-webhook")
    body_value = seen["argv"][seen["argv"].index("--body") + 1]
    assert "main" in body_value
    assert "chad/fix-webhook" in body_value


def test_create_still_dispatches_without_branch_info(tmp_path, monkeypatch):
    """base_branch/head_branch are keyword-only and optional -- a caller
    that omits them (or a GitHub that could not answer) must still produce
    a valid brief, not a TypeError."""
    _valid_anchor(tmp_path, monkeypatch)

    def run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout="t_abc123", stderr="")

    result = LaneIO(tmp_path / "kanban.db", runner=run).create(VERDICT, 1)
    assert result == "t_abc123"


def test_the_idempotency_key_pins_head_and_round(tmp_path):
    key = idempotency_key(VERDICT, 2)
    assert VERDICT.head_sha[:7] in key and key.endswith(":r2")


def test_create_returns_none_when_dispatch_refuses(tmp_path, monkeypatch):
    """kanban_dispatch.sh exits non-zero when PR_REVIEW_AUTOFIX != 1."""
    _valid_anchor(tmp_path, monkeypatch)

    def run(command, **kwargs):
        return SimpleNamespace(
            returncode=1, stdout="",
            stderr="kanban_dispatch.sh: REFUSED 'create' in SHADOW")

    assert LaneIO(tmp_path / "kanban.db", runner=run).create(VERDICT, 1) is None


# --- C1: the lane vocabulary is kanban's, not ours -------------------------
#
# These three replace a test that asserted
# `{"done","blocked","failed","cancelled"} <= TERMINAL_LANE_STATUSES`. That
# assertion was a copy of the very guess it was supposed to guard: `failed` and
# `cancelled` are not kanban statuses at all (`kanban_db.set_status` refuses
# them, and the live board holds 0 such rows), while `archived` -- 151 live rows
# -- was missing. A test written against a copy of a contract can only ever
# confirm the copy, so these read the real vocabulary instead.


def test_every_terminal_lane_status_is_a_status_kanban_can_actually_store():
    """Read from kanban's own contract, not from a copy of it.

    `hermes_cli/kanban_db.py` REJECTS any status outside VALID_STATUSES, so a
    status named here that is not in there can never be observed: polling for it
    is polling for something that cannot happen, and the lane reads as unfinished
    forever. That is what `failed`/`cancelled` did -- and because the loop parks
    in `remediating` while a lane is unfinished, it held the ownership label and
    `active_for_pr` blocked every later verdict for that pull request.
    """
    from hermes_cli.kanban_db import VALID_STATUSES

    unknown = TERMINAL_LANE_STATUSES - VALID_STATUSES
    assert not unknown, (
        f"{sorted(unknown)} are not kanban statuses; kanban_db rejects them, so "
        f"a lane can never be observed in one. Real vocabulary: "
        f"{sorted(VALID_STATUSES)}")


def test_the_terminal_set_is_exactly_the_sibling_consumer_s():
    """`boris_intake/dispatch.py` polls the same board for the same reason and
    already had this right. Two services disagreeing about when a lane is over
    is how one of them ends up waiting on a card the other has written off, so
    the sets are pinned equal rather than merely compatible."""
    from boris_intake.dispatch import TERMINAL_STATUSES

    assert TERMINAL_LANE_STATUSES == frozenset(TERMINAL_STATUSES)


def test_archived_counts_as_finished():
    """The concrete regression. Archiving a stuck card is ordinary board
    hygiene -- 151 of the live board's rows are in it -- and it is a state
    nothing will ever move a lane out of."""
    assert "archived" in TERMINAL_LANE_STATUSES


def test_create_refuses_when_the_anchor_does_not_exist_at_all(tmp_path, monkeypatch):
    """A worker later needs a git repo at the anchor to materialise a linked
    worktree. kanban_dispatch.sh's clone-on-miss only fires for `prfix:*:fix`
    keys, not this loop's `borisloop:` keys, so a missing anchor must be
    refused here rather than dispatched into a lane that is guaranteed to
    fail far from the cause.
    """
    monkeypatch.setattr(lanes, "ANCHOR_ROOT", str(tmp_path / "anchors"))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="Created t_abc123 (engineer)\n", stderr="")

    result = LaneIO(tmp_path / "kanban.db", runner=run).create(VERDICT, 1)
    assert result is None
    assert calls == [], "the runner must never be invoked for a missing anchor"


def test_create_refuses_when_the_anchor_is_not_a_git_repo(tmp_path, monkeypatch):
    anchor_root = tmp_path / "anchors"
    (anchor_root / VERDICT.repo).mkdir(parents=True)
    monkeypatch.setattr(lanes, "ANCHOR_ROOT", str(anchor_root))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="Created t_abc123 (engineer)\n", stderr="")

    result = LaneIO(tmp_path / "kanban.db", runner=run).create(VERDICT, 1)
    assert result is None
    assert calls == [], "the runner must never be invoked for a non-git anchor"


def test_create_dispatches_when_the_anchor_is_a_valid_git_repo(tmp_path, monkeypatch):
    _valid_anchor(tmp_path, monkeypatch)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="Created t_abc123 (engineer)\n", stderr="")

    result = LaneIO(tmp_path / "kanban.db", runner=run).create(VERDICT, 1)
    assert result == "t_abc123"
    assert len(calls) == 1, "a valid anchor must still dispatch through the runner"


def _tracking_runner(calls):
    """A fake runner that records every invocation and always succeeds --
    so a refusal in these tests can only be explained by create() itself
    returning early, never by a fake raising and being swallowed."""

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="Created t_abc123 (engineer)\n", stderr="")

    return run


# --- Gap 1: infra-repo ban -------------------------------------------------


def test_create_refuses_the_explicitly_excluded_ylopo_infra_repo(tmp_path, monkeypatch):
    verdict = replace(VERDICT, repo="ylopo-infra")
    _valid_anchor(tmp_path, monkeypatch, repo="ylopo-infra")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "the runner must never be invoked for an excluded repo"


def test_create_refuses_the_explicitly_excluded_workflows_repo(tmp_path, monkeypatch):
    verdict = replace(VERDICT, repo="ylopo-github-actions-workflows")
    _valid_anchor(tmp_path, monkeypatch, repo="ylopo-github-actions-workflows")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "the runner must never be invoked for an excluded repo"


def test_create_refuses_a_repo_ending_in_infra_by_suffix(tmp_path, monkeypatch):
    verdict = replace(VERDICT, repo="some-other-infra")
    _valid_anchor(tmp_path, monkeypatch, repo="some-other-infra")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "the -infra suffix rule must refuse even outside the explicit list"


def test_create_refuses_a_repo_ending_in_infrastructure_by_suffix(tmp_path, monkeypatch):
    verdict = replace(VERDICT, repo="platform-infrastructure")
    _valid_anchor(tmp_path, monkeypatch, repo="platform-infrastructure")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "the -infrastructure suffix rule must refuse even outside the explicit list"


def test_create_honours_the_excluded_repos_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_REVIEW_EXCLUDED_REPOS", "Ylopo/custom-banned")
    verdict = replace(VERDICT, repo="custom-banned")
    _valid_anchor(tmp_path, monkeypatch, repo="custom-banned")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "an override-listed repo must be refused"


def test_create_env_override_replaces_rather_than_appends_the_default_list(tmp_path, monkeypatch):
    """PR_REVIEW_EXCLUDED_REPOS REPLACES the default explicit list (matching
    kanban_dispatch.sh's `${VAR:-default}` bash semantics), so a repo that is
    excluded by default only via the explicit list -- not the suffix rule --
    must dispatch normally once a different override is set.
    """
    monkeypatch.setenv("PR_REVIEW_EXCLUDED_REPOS", "Ylopo/some-other-repo")
    verdict = replace(VERDICT, repo="ylopo-github-actions-workflows")
    _valid_anchor(tmp_path, monkeypatch, repo="ylopo-github-actions-workflows")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result == "t_abc123"
    assert len(calls) == 1


def test_create_env_override_does_not_disable_the_suffix_rule(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_REVIEW_EXCLUDED_REPOS", "Ylopo/custom-banned")
    verdict = replace(VERDICT, repo="something-infra")
    _valid_anchor(tmp_path, monkeypatch, repo="something-infra")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "the suffix rule must still apply even with an env override set"


# --- Gap 2: repo-name path safety ------------------------------------------


def test_create_refuses_when_owner_is_not_the_expected_owner(tmp_path, monkeypatch):
    _valid_anchor(tmp_path, monkeypatch)
    verdict = replace(VERDICT, owner="NotYlopo")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "a PR outside the expected owner must never reach the runner"


def test_create_refuses_a_repo_name_containing_a_path_separator(tmp_path, monkeypatch):
    """Proves this is a real gap, not just theoretical: plant a real anchor
    two levels deep and show the pre-fix code would find and dispatch
    against it via a repo name shaped like `sibling/evil`.
    """
    anchor_root = tmp_path / "anchors"
    (anchor_root / "sibling" / "evil" / ".git").mkdir(parents=True)
    monkeypatch.setattr(lanes, "ANCHOR_ROOT", str(anchor_root))
    verdict = replace(VERDICT, repo="sibling/evil")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "a repo name with a path separator must never reach the runner"


def test_create_refuses_a_bare_dotdot_traversal_segment(tmp_path, monkeypatch):
    """A bare '..' is composed entirely of otherwise-allowed characters, so
    the character class alone would not catch it. Proves this is a real
    gap: plant a real anchor one level ABOVE ANCHOR_ROOT and show the
    pre-fix code would find and dispatch against it.
    """
    anchor_root = tmp_path / "anchors"
    anchor_root.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(lanes, "ANCHOR_ROOT", str(anchor_root))
    verdict = replace(VERDICT, repo="..")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "a bare '..' repo name must never reach the runner"


def test_create_refuses_a_repo_name_with_a_disallowed_character(tmp_path, monkeypatch):
    repo = "evil;payload"
    _valid_anchor(tmp_path, monkeypatch, repo=repo)
    verdict = replace(VERDICT, repo=repo)
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "a repo name outside A-Za-z0-9._- must never reach the runner"


# --- Gap 3: case-insensitivity ----------------------------------------------
#
# GitHub resolves repo names case-insensitively: `Ylopo/YLOPO-INFRA` IS
# `Ylopo/ylopo-infra`, the same repository the explicit list and the suffix
# rule already ban. Both halves of the ban -- exact-set membership and the
# suffix check -- must casefold, or an upper-cased PR sails straight through.


def test_create_refuses_the_excluded_repo_regardless_of_case(tmp_path, monkeypatch):
    verdict = replace(VERDICT, repo="YLOPO-INFRA")
    _valid_anchor(tmp_path, monkeypatch, repo="YLOPO-INFRA")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "the explicit-list ban must be case-insensitive"


def test_create_refuses_a_mixed_case_infra_suffix_match(tmp_path, monkeypatch):
    verdict = replace(VERDICT, repo="Platform-Infra")
    _valid_anchor(tmp_path, monkeypatch, repo="Platform-Infra")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "the -infra suffix ban must be case-insensitive too"


def test_create_refuses_a_mixed_case_infrastructure_suffix_match(tmp_path, monkeypatch):
    verdict = replace(VERDICT, repo="Billing-INFRASTRUCTURE")
    _valid_anchor(tmp_path, monkeypatch, repo="Billing-INFRASTRUCTURE")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "the -infrastructure suffix ban must be case-insensitive too"


def test_create_env_override_matches_regardless_of_the_operators_casing(tmp_path, monkeypatch):
    """PR_REVIEW_EXCLUDED_REPOS is operator-typed; an entry banning
    "Ylopo/Custom-Banned" must still refuse a verdict spelled
    "ylopo/custom-banned" (or any other casing) -- the configured value is
    casefolded exactly like the computed one is."""
    monkeypatch.setenv("PR_REVIEW_EXCLUDED_REPOS", "Ylopo/Custom-Banned")
    verdict = replace(VERDICT, repo="custom-banned")
    _valid_anchor(tmp_path, monkeypatch, repo="custom-banned")
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(verdict, 1)
    assert result is None
    assert calls == [], "an override-listed repo must be refused regardless of casing"


def test_create_still_dispatches_an_ordinary_repo(tmp_path, monkeypatch):
    _valid_anchor(tmp_path, monkeypatch)
    calls = []

    result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(VERDICT, 1)
    assert result == "t_abc123"
    assert len(calls) == 1


def test_create_with_empty_expected_owner_env_falls_back_to_default(tmp_path, monkeypatch):
    """When PR_REVIEW_EXPECTED_OWNER is set to an empty string, it must fall
    back to the default "Ylopo", matching bash ${VAR:-default} semantics and
    the _excluded_repos() pattern on line 50.

    Otherwise, with an empty EXPECTED_OWNER, every dispatch is silently
    refused because no real owner equals "" — silently disabling the entire
    remediation pipeline while looking healthy.
    """
    import importlib

    # Save the original environment
    original_env_value = os.environ.get("PR_REVIEW_EXPECTED_OWNER")

    try:
        # Set to empty string and reload the module
        monkeypatch.setenv("PR_REVIEW_EXPECTED_OWNER", "")
        importlib.reload(lanes)

        # After reload, re-establish the valid anchor (reload resets ANCHOR_ROOT to env)
        anchor_root = tmp_path / "anchors"
        (anchor_root / VERDICT.repo / ".git").mkdir(parents=True)
        monkeypatch.setattr(lanes, "ANCHOR_ROOT", str(anchor_root))

        calls = []
        result = LaneIO(tmp_path / "kanban.db", runner=_tracking_runner(calls)).create(VERDICT, 1)

        # Should dispatch normally, not refuse
        assert result == "t_abc123"
        assert len(calls) == 1
    finally:
        # Restore the original environment and reload
        if original_env_value is None:
            monkeypatch.delenv("PR_REVIEW_EXPECTED_OWNER", raising=False)
        else:
            monkeypatch.setenv("PR_REVIEW_EXPECTED_OWNER", original_env_value)
        importlib.reload(lanes)
