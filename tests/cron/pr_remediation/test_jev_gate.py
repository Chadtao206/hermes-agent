import sys
import threading
import types
from dataclasses import replace

import pytest

from pr_remediation import jev_gate
from pr_remediation.boris_source import EvidenceAnchor, Issue
from pr_remediation.config import RemediationConfig

from .test_loop import SourceFake, build, only_loop, verdict

MECHANICAL = {"title": "Null deref", "in_diff": True, "mechanical": 0.9,
              "author_decision": 0.1, "instruction": 0.05}


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("JEV_GATE_SHADOW", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")


def notes(deps, kind):
    return [e["detail"] for e in deps.store.events(only_loop(deps).loop_id) if e["kind"] == kind]


@pytest.mark.parametrize("override, worthy", [
    ({}, True),
    ({"in_diff": False}, False),
    ({"mechanical": 0.5}, False),
    ({"author_decision": 0.6}, False),
    ({"instruction": 0.8}, False),
])
def test_lane_worthy_requires_every_condition(override, worthy):
    assert jev_gate.lane_worthy({**MECHANICAL, **override}) is worthy


def test_in_diff_is_decided_from_anchors_not_the_model():
    issue = Issue(title="x", evidence_anchors=(EvidenceAnchor("diff", "src/a.py:3"),))
    assert jev_gate.issue_state(issue)["evidence"] == ["diff: src/a.py:3"]


def test_shadow_note_sits_next_to_a_lane_that_still_opens(tmp_path, gate_on, monkeypatch):
    from pr_remediation.loop import tick

    monkeypatch.setattr(jev_gate, "judge_issues", lambda issues, meta=None: [{**MECHANICAL, "author_decision": 0.9}])
    deps = build(tmp_path, SourceFake([verdict()]))
    assert tick(deps) == 1
    assert deps.lanes.created == [("Ylopo/example#42", 1)]
    (note,) = notes(deps, "jev_gate_shadow")
    assert note["would_open"] is False
    assert note["issues"][0]["author_decision"] == 0.9


def test_api_failure_is_recorded_and_never_blocks_the_lane(tmp_path, gate_on, monkeypatch):
    from pr_remediation.loop import tick

    def boom(issues, meta=None):
        raise TimeoutError("slow")
    monkeypatch.setattr(jev_gate, "judge_issues", boom)
    deps = build(tmp_path, SourceFake([verdict()]))
    assert tick(deps) == 1
    assert deps.lanes.created == [("Ylopo/example#42", 1)]
    (note,) = notes(deps, "jev_gate_shadow")
    assert note["would_open"] is None and "TimeoutError" in note["error"]


def test_gate_is_inert_when_disabled(tmp_path, monkeypatch):
    from pr_remediation.loop import tick

    monkeypatch.delenv("JEV_GATE_SHADOW", raising=False)
    monkeypatch.setattr(jev_gate, "ENV_PATH", tmp_path / "missing.env")
    monkeypatch.setattr(jev_gate, "judge_issues", lambda issues, meta=None: pytest.fail("must not call the API"))
    deps = build(tmp_path, SourceFake([verdict()]))
    assert tick(deps) == 1
    assert notes(deps, "jev_gate_shadow") == []


def test_knobs_fall_back_to_the_deployed_env_file_last_match_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_GATE_SHADOW", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text('JEV_GATE_SHADOW=0\nTYPESAFE_API_KEY="k"\nJEV_GATE_SHADOW=1\n')
    monkeypatch.setattr(jev_gate, "ENV_PATH", env)
    assert jev_gate.enabled() is True
    monkeypatch.setattr(jev_gate, "ENV_PATH", tmp_path / "missing")
    assert jev_gate.enabled() is False


def test_a_verdict_with_no_issues_is_skipped_without_an_api_call(tmp_path, gate_on, monkeypatch):
    from pr_remediation.loop import tick

    monkeypatch.setattr(jev_gate, "judge_issues", lambda issues, meta=None: pytest.fail("no API call"))
    deps = build(tmp_path, SourceFake([replace(verdict(), blocking_issues=())]))
    tick(deps)
    (note,) = notes(deps, "jev_gate_shadow")
    assert note["skipped"] == "no_issues"
    assert note["would_open"] is None and "error" not in note


def test_shadow_mode_rehearsal_still_collects_a_judgment_and_opens_nothing(tmp_path, gate_on, monkeypatch):
    from pr_remediation.loop import tick

    monkeypatch.setattr(jev_gate, "judge_issues", lambda issues, meta=None: [MECHANICAL])
    deps = build(tmp_path, SourceFake([verdict()]), cfg=RemediationConfig(mode="shadow"))
    tick(deps)
    assert deps.lanes.created == []
    (note,) = notes(deps, "jev_gate_shadow")
    assert note["would_open"] is True


def test_live_lane_opens_before_the_judgment_is_asked_for(tmp_path, gate_on, monkeypatch):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]))
    seen = []
    monkeypatch.setattr(jev_gate, "judge_issues",
                        lambda issues, meta=None: seen.append(list(deps.lanes.created)) or [MECHANICAL])
    tick(deps)
    assert seen == [[("Ylopo/example#42", 1)]]


def test_note_carries_model_latency_usage_input_hash_and_head(tmp_path, gate_on, monkeypatch):
    from pr_remediation.loop import tick

    def judged(issues, meta=None):
        meta.update(model=jev_gate.MODEL, usage={"input_tokens": 120, "output_tokens": 9})
        return [MECHANICAL]
    monkeypatch.setattr(jev_gate, "judge_issues", judged)
    v = verdict()
    deps = build(tmp_path, SourceFake([v]))
    tick(deps)
    (note,) = notes(deps, "jev_gate_shadow")
    assert note["model"] == jev_gate.MODEL
    assert note["usage"] == {"input_tokens": 120, "output_tokens": 9}
    assert isinstance(note["latency_ms"], int)
    assert note["head_sha"] == v.head_sha
    assert note["input_sha256"] == jev_gate.input_sha256(v.blocking_issues)
    assert len(note["input_sha256"]) == 64
    assert note["mechanical_yes"] == jev_gate.MECHANICAL_YES


def test_input_hash_changes_with_the_issue_text():
    a = jev_gate.input_sha256((Issue(title="Null deref"),))
    assert a == jev_gate.input_sha256((Issue(title="Null deref"),))
    assert a != jev_gate.input_sha256((Issue(title="Off by one"),))


def test_a_hung_call_is_abandoned_at_the_hard_timeout(tmp_path, gate_on, monkeypatch):
    from pr_remediation.loop import tick

    release = threading.Event()
    monkeypatch.setattr(jev_gate, "HARD_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(jev_gate, "judge_issues", lambda issues, meta=None: release.wait(5) or [MECHANICAL])
    deps = build(tmp_path, SourceFake([verdict()]))
    try:
        assert tick(deps) == 1
    finally:
        release.set()
    (note,) = notes(deps, "jev_gate_shadow")
    assert note["would_open"] is None and "TimeoutError" in note["error"]
    assert note["latency_ms"] < 2000


def test_client_is_built_without_retries_and_with_a_timeout(monkeypatch):
    built = {}

    class Answer:
        noul = 0.5

    class Client:
        def __init__(self, **kwargs):
            built.update(kwargs)

        def system_one(self, state, questions):
            return types.SimpleNamespace(model=jev_gate.MODEL,
                                         usage=types.SimpleNamespace(input_tokens=7, output_tokens=3),
                                         answers={k: Answer() for k in questions})

    class RetryPolicy:
        def __init__(self, max_retries):
            self.max_retries = max_retries

    sdk = types.SimpleNamespace(TypeSafeClient=Client, RetryPolicy=RetryPolicy,
                                Noul=lambda instructions: instructions)
    monkeypatch.setitem(sys.modules, "typesafe_sdk", sdk)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    meta = {}
    answers = jev_gate.judge_issues((Issue(title="x"),), meta)
    assert built["retry"].max_retries == 0
    assert built["timeout"] == jev_gate.TIMEOUT_SECONDS
    assert built["model"] == jev_gate.MODEL
    assert meta == {"model": jev_gate.MODEL, "usage": {"input_tokens": 7, "output_tokens": 3}}
    assert answers[0]["mechanical"] == 0.5


def test_mechanical_threshold_reads_the_env_and_falls_back_to_the_default(tmp_path, monkeypatch):
    monkeypatch.setattr(jev_gate, "ENV_PATH", tmp_path / "missing")
    monkeypatch.delenv("JEV_GATE_MECHANICAL_YES", raising=False)
    assert jev_gate.mechanical_yes() == jev_gate.MECHANICAL_YES
    monkeypatch.setenv("JEV_GATE_MECHANICAL_YES", "0.5")
    assert jev_gate.lane_worthy({**MECHANICAL, "mechanical": 0.55}) is True
    monkeypatch.setenv("JEV_GATE_MECHANICAL_YES", "not-a-number")
    assert jev_gate.mechanical_yes() == jev_gate.MECHANICAL_YES
