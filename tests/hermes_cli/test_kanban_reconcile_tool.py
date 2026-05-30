"""WS5: the kanban_reconcile orchestrator tool — list decision packets and
apply allowlisted options (with packet-signature re-validation), escalating
non-allowlisted options to a human."""
import json

import tools.kanban_tools as kt


def test_reconcile_list_returns_packets(monkeypatch):
    fake = {"packets": [{"task_id": "t1", "packet_signature": "sig1",
                         "suggested_options": ["unblock", "keep_blocked", "close"]}],
            "mode": "jensen_decision_required", "wake_agent": True}
    monkeypatch.setattr(kt, "_reconcile_collect", lambda board=None: fake, raising=False)
    res = json.loads(kt._handle_reconcile({"action": "list"}))
    assert res["packets"][0]["task_id"] == "t1"
    assert "unblock" in res["packets"][0]["suggested_options"]


def test_reconcile_apply_allowed_option(monkeypatch):
    monkeypatch.setattr(kt, "_reconcile_auto_options", lambda: {"unblock", "keep_blocked"})
    called = {}

    def fake_apply(**kw):
        called.update(kw)
        return {"ok": True, "applied": True, "option": kw["option"]}

    monkeypatch.setattr(kt, "_reconcile_apply", fake_apply, raising=False)
    res = json.loads(kt._handle_reconcile({
        "action": "apply", "task_id": "t1", "option": "unblock",
        "packet_signature": "sig1",
    }))
    assert res["applied"] is True
    assert called["confirm_dry_run"] is True          # the gate flag is always set by the tool
    assert called["packet_signature"] == "sig1"       # threaded through for re-validation
    assert called["option"] == "unblock"


def test_reconcile_apply_refuses_human_only_option(monkeypatch):
    monkeypatch.setattr(kt, "_reconcile_auto_options", lambda: {"unblock"})
    # Apply must NOT be called for a non-allowlisted option.
    monkeypatch.setattr(
        kt, "_reconcile_apply",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not apply")),
        raising=False,
    )
    res = json.loads(kt._handle_reconcile({
        "action": "apply", "task_id": "t1",
        "option": "manual_review_with_stale_pr_risk", "packet_signature": "sig1",
    }))
    assert res.get("needs_human") is True
    assert "not in the auto-apply allowlist" in res.get("reason", "")


def test_reconcile_apply_requires_fields(monkeypatch):
    monkeypatch.setattr(kt, "_reconcile_auto_options", lambda: {"unblock"})
    res = json.loads(kt._handle_reconcile({"action": "apply", "option": "unblock"}))
    assert "error" in res  # missing task_id + packet_signature


def test_reconcile_unknown_action():
    res = json.loads(kt._handle_reconcile({"action": "frobnicate"}))
    assert "error" in res
