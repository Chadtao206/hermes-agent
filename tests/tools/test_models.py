from tools.claude_session.models import TurnResult, SessionRecord


def test_turnresult_to_dict_has_consumed_fields():
    d = TurnResult(result="done", session_id="abc", num_turns=2,
                   total_cost_usd=0.01, usage={"output_tokens": 9},
                   subtype="success").to_dict()
    assert d == {
        "type": "result", "subtype": "success", "result": "done",
        "session_id": "abc", "num_turns": 2, "total_cost_usd": 0.01,
        "usage": {"output_tokens": 9},
    }


def test_sessionrecord_roundtrips_through_json():
    rec = SessionRecord(name="t1", uuid="u1", pid=123, workdir="/tmp/x",
                        deadline=1000.0, turns=0, cost=0.0, started_at=1.0,
                        log_path="/tmp/x.log")
    assert SessionRecord.from_dict(rec.to_dict()) == rec
