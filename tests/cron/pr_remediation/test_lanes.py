import os
from dataclasses import replace
from types import SimpleNamespace

from pr_remediation import lanes
from pr_remediation.boris_source import EvidenceAnchor, Issue, Verdict
from pr_remediation.lanes import (
    MAX_BRIEF_CHARS, TERMINAL_LANE_STATUSES, LaneIO, build_brief, idempotency_key,
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
    dangling 'Evidence:' header with nothing under it."""
    brief = build_brief(VERDICT, 1)
    assert "Null deref in handler" in brief and "Race in seed" in brief


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
    brief = build_brief(RICH_VERDICT, 1, base_branch="main",
                        head_branch="chad/fix-webhook")
    assert "main" in brief
    assert "chad/fix-webhook" in brief


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


# --- size cap and truncation -------------------------------------------------


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
