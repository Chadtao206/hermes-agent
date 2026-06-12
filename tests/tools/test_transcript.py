from pathlib import Path

from tools.claude_session.models import SUBTYPE_NO_TRANSCRIPT, SUBTYPE_SUCCESS
from tools.claude_session.transcript import parse_transcript

FIX = Path(__file__).parent / "fixtures" / "transcript_interactive.jsonl"


def test_parse_real_fixture():
    tr = parse_transcript(FIX, session_id="probe")
    assert tr.subtype == SUBTYPE_SUCCESS
    assert "hello world" in tr.result.lower()
    assert tr.num_turns >= 1
    assert tr.usage  # non-empty


def test_thinking_plus_text_share_one_turn(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"type":"assistant","requestId":"r1","message":{"content":'
        '[{"type":"thinking","thinking":"hmm"}],"usage":{"input_tokens":10,'
        '"output_tokens":41},"model":"claude-opus-4-5-20251101"}}\n'
        '{"type":"assistant","requestId":"r1","message":{"content":'
        '[{"type":"text","text":"final answer"}],"usage":{"input_tokens":10,'
        '"output_tokens":41},"model":"claude-opus-4-5-20251101"}}\n'
    )
    tr = parse_transcript(p, session_id="x")
    assert tr.result == "final answer"
    assert tr.num_turns == 1
    assert tr.usage["output_tokens"] == 41
    assert tr.total_cost_usd > 0


def test_two_turns_sum_across_requestids(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"type":"assistant","requestId":"r1","message":{"content":'
        '[{"type":"text","text":"A"}],"usage":{"input_tokens":1,"output_tokens":2},'
        '"model":"claude-opus-4-5"}}\n'
        '{"type":"assistant","requestId":"r2","message":{"content":'
        '[{"type":"text","text":"B"}],"usage":{"input_tokens":3,"output_tokens":4},'
        '"model":"claude-opus-4-5"}}\n'
    )
    tr = parse_transcript(p, session_id="x")
    assert tr.result == "B"
    assert tr.num_turns == 2
    assert tr.usage["output_tokens"] == 6


def test_missing_transcript_is_graceful(tmp_path):
    tr = parse_transcript(tmp_path / "nope.jsonl", session_id="x")
    assert tr.subtype == SUBTYPE_NO_TRANSCRIPT
    assert tr.result == "" and tr.num_turns == 0
