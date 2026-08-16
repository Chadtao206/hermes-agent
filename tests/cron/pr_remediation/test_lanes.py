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
