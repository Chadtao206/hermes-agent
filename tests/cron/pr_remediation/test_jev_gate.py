import pytest

from pr_remediation import jev_gate
from pr_remediation.boris_source import EvidenceAnchor, Issue

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

    monkeypatch.setattr(jev_gate, "judge_issues", lambda issues: [{**MECHANICAL, "author_decision": 0.9}])
    deps = build(tmp_path, SourceFake([verdict()]))
    assert tick(deps) == 1
    assert deps.lanes.created == [("Ylopo/example#42", 1)]
    (note,) = notes(deps, "jev_gate_shadow")
    assert note["would_open"] is False
    assert note["issues"][0]["author_decision"] == 0.9


def test_api_failure_is_recorded_and_never_blocks_the_lane(tmp_path, gate_on, monkeypatch):
    from pr_remediation.loop import tick

    def boom(issues):
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
    monkeypatch.setattr(jev_gate, "judge_issues", lambda issues: pytest.fail("must not call the API"))
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
