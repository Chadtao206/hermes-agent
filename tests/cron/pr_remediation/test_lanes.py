from types import SimpleNamespace

from pr_remediation.boris_source import Verdict
from pr_remediation.lanes import (
    TERMINAL_LANE_STATUSES, LaneIO, build_brief, idempotency_key,
)

VERDICT = Verdict("br_1", "Ylopo", "example", 42, "a" * 40, "1.0",
                  "ISSUES_FOUND", None, ("Null deref in handler", "Race in seed"))


def test_brief_names_every_blocking_issue():
    brief = build_brief(VERDICT, 1)
    assert "Null deref in handler" in brief
    assert "Race in seed" in brief


def test_brief_pins_the_exact_head_so_andrej_cannot_drift():
    assert "a" * 40 in build_brief(VERDICT, 1)


def test_brief_names_the_pr():
    assert "Ylopo/example#42" in build_brief(VERDICT, 1)


def test_create_parses_the_lane_id_out_of_dispatch_output(tmp_path):
    def run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout="Created t_abc123 (engineer)\n",
                               stderr="")

    assert LaneIO(tmp_path / "kanban.db", runner=run).create(VERDICT, 1) == "t_abc123"


def test_create_uses_the_dispatch_interface_the_script_actually_accepts(tmp_path):
    """Guards against drifting back to invented flags like --profile/--pr.

    Also guards against a `--body @file` reference: `hermes_cli/kanban.py`'s
    `_cmd_create` stores `args.body` verbatim (confirmed by reading the source
    directly -- no `@`-prefix resolution exists anywhere in the CLI or in
    `kanban_dispatch.sh`), so a literal `@<path>` argv token would write the
    literal string "@/tmp/xyz.md" as the lane's body instead of the actual
    remediation brief. The body must be the real text, passed the same way
    `boris_intake/dispatch.py`'s `create_lane()` already does it.
    """
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


def test_create_returns_none_when_dispatch_refuses(tmp_path):
    """kanban_dispatch.sh exits non-zero when PR_REVIEW_AUTOFIX != 1."""
    def run(command, **kwargs):
        return SimpleNamespace(
            returncode=1, stdout="",
            stderr="kanban_dispatch.sh: REFUSED 'create' in SHADOW")

    assert LaneIO(tmp_path / "kanban.db", runner=run).create(VERDICT, 1) is None


def test_terminal_statuses_cover_the_ways_a_lane_can_end():
    assert {"done", "blocked", "failed", "cancelled"} <= TERMINAL_LANE_STATUSES
