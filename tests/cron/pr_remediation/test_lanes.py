from dataclasses import replace
from types import SimpleNamespace

from pr_remediation import lanes
from pr_remediation.boris_source import Verdict
from pr_remediation.lanes import (
    TERMINAL_LANE_STATUSES, LaneIO, build_brief, idempotency_key,
)

VERDICT = Verdict("br_1", "Ylopo", "example", 42, "a" * 40, "1.0",
                  "ISSUES_FOUND", None, ("Null deref in handler", "Race in seed"))


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


def test_terminal_statuses_cover_the_ways_a_lane_can_end():
    assert {"done", "blocked", "failed", "cancelled"} <= TERMINAL_LANE_STATUSES


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
