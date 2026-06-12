import json

from tools.claude_session.cli import build_parser, cmd_send_result_json
from tools.claude_session.models import TurnResult, SUBTYPE_BUDGET


def test_parser_has_all_verbs():
    choices = build_parser()._subparsers._group_actions[0].choices
    for v in ("start", "send", "capture", "steer", "slash",
              "status", "stop", "list", "gc", "run"):
        assert v in choices


def test_run_defaults():
    a = build_parser().parse_args(["run", "--task", "x", "--workdir", "/w"])
    assert a.oneshot is False and a.no_tmux is False


def test_budget_exceeded_sets_subtype():
    tr = TurnResult(result="r", session_id="s", num_turns=3,
                    total_cost_usd=0.99, usage={}, subtype="success")
    out = cmd_send_result_json(tr, max_budget_usd=0.50)
    assert out["subtype"] == SUBTYPE_BUDGET
    assert json.loads(json.dumps(out))
