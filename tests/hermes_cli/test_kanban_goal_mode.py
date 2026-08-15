"""Tests for kanban goal_mode — per-card Ralph-style goal loop.

Covers three layers:

1. DB: goal_mode / goal_max_turns persist through create_task + from_row,
   and a legacy DB (without the columns) migrates cleanly.
2. Spawn: _default_spawn sets the HERMES_KANBAN_GOAL_MODE env vars only
   when the card opts in.
3. Loop: goals.run_kanban_goal_loop continuation / completion / budget
   behaviour, driven entirely through injected callbacks (no live model).
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import goals
from hermes_cli import kanban as kc
from hermes_cli import strict_goal_judge as strict


def _write_fixture_target_verifier(tmp_path: Path) -> Path:
    """Install the fixed strict-verifier module name in test-local PYTHONPATH."""
    package_root = tmp_path / "strict-target-verifier"
    package = package_root / "review_delegator"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "targets.py").write_text(
        '''import hashlib
import json
import re
import sys
from pathlib import Path


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _verify(target):
    try:
        if type(target) is not dict or set(target) != {"kind", "identity", "revision"}:
            return "mismatched", "invalid_target"
        if target["kind"] != "artifact" or type(target["identity"]) is not str:
            return "mismatched", "invalid_target"
        if type(target["revision"]) is not str or re.fullmatch(r"[0-9a-f]{64}", target["revision"]) is None:
            return "mismatched", "invalid_target"
        identity = json.loads(target["identity"])
        if (
            type(identity) is not dict
            or set(identity) != {"identifier", "path"}
            or any(type(value) is not str for value in identity.values())
            or _canonical_json(identity) != target["identity"]
        ):
            return "mismatched", "invalid_target"
        path = Path(identity["path"])
        if not path.is_absolute() or not path.is_file() or path.resolve() != path:
            return "mismatched", "locator_mismatch"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != target["revision"]:
            return "mismatched", "revision_mismatch"
        return "verified", "verified"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return "mismatched", "invalid_target"


def _main():
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if type(request) is not dict or set(request) != {"schema_version", "operation_id", "target_verification"}:
            return 2
        if request["schema_version"] != 1 or type(request["operation_id"]) is not str:
            return 2
        target = request["target_verification"]
        outcome, reason = _verify(target)
        unsigned = {
            "schema_version": 1,
            "operation_id": request["operation_id"],
            "target_verification": target,
            "outcome": outcome,
            "reason": reason,
        }
        print(_canonical_json({
            **unsigned,
            "verification_signature": hashlib.sha256(
                _canonical_json(unsigned).encode("utf-8")
            ).hexdigest(),
        }))
        return 0
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return 2


if __name__ == "__main__":
    raise SystemExit(_main() if sys.argv[1:] == ["verify-strict-target"] else 2)
''',
        encoding="utf-8",
    )
    return package_root


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PYTHONPATH", str(_write_fixture_target_verifier(tmp_path)))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------





def test_legacy_db_migrates_goal_columns(tmp_path, monkeypatch):
    """A tasks table created without goal columns must gain them on init."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal legacy schema: tasks table missing goal_mode / goal_max_turns.
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    legacy.execute(
        "INSERT INTO tasks (id, title, status, priority, created_at, workspace_kind) "
        "VALUES ('legacy1', 'old', 'ready', 0, 1, 'scratch')"
    )
    legacy.commit()
    legacy.close()

    # init_db runs the additive migration.
    kb.init_db()
    with kb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "goal_mode" in cols
        assert "goal_max_turns" in cols
        task = kb.get_task(conn, "legacy1")
    # Existing row keeps the safe default.
    assert task.goal_mode is False
    assert task.goal_max_turns is None


# ---------------------------------------------------------------------------
# Spawn env
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Goal loop logic (callback-injected, no live model)
# ---------------------------------------------------------------------------

def _patch_judge(monkeypatch, verdicts):
    """Make judge_goal return a scripted sequence of verdicts."""
    seq = list(verdicts)

    def _fake_judge(goal, response, subgoals=None, background_processes=None, **_kw):
        v = seq.pop(0) if seq else "done"
        # 5-tuple contract: verdict, reason, parse failure, wait, transport failure.
        return v, f"scripted:{v}", False, None, False

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)


def test_loop_stops_when_worker_already_completed(monkeypatch):
    # Worker called kanban_complete on its first turn — no judging needed.
    _patch_judge(monkeypatch, ["continue"])  # should never be consulted
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="do the thing",
        run_turn=lambda p: turns.append(p) or "x",
        task_status_fn=lambda: "done",
        block_fn=lambda r: pytest.fail("should not block"),
        first_response="done already",
    )
    assert res["outcome"] == "completed_by_worker"
    assert turns == []  # no extra turns






# ---------------------------------------------------------------------------
# CLI judge gate tests (hermes kanban complete bypass fix)
# ---------------------------------------------------------------------------

class TestCLIJudgeGate:
    """hermes kanban complete must apply the same goal_mode judge gate as the
    kanban_complete tool (Issue #38367 sibling gap).

    Uses mocks for kb.get_task and kb.complete_task to avoid depending on the
    full kanban_db schema; the gate logic is the unit under test.
    """

    def _run(self, monkeypatch, *, goal_mode=True, judge_available=True,
             verdict="done", reason="", complete_ok=True, summary="done"):
        import argparse
        import types
        from unittest.mock import MagicMock
        from hermes_cli.kanban import _cmd_complete

        fake_task = types.SimpleNamespace(
            goal_mode=goal_mode,
            title="Finish report",
            body="acceptance: criteria",
        )
        fake_conn = MagicMock()
        complete_calls: list = []

        def fake_connect_closing():
            from contextlib import contextmanager
            @contextmanager
            def _cm():
                yield fake_conn
            return _cm()

        def fake_complete_task(conn, tid, **kw):
            complete_calls.append(tid)
            return complete_ok

        monkeypatch.setattr("hermes_cli.kanban.kb.get_task", lambda conn, tid: fake_task)
        monkeypatch.setattr("hermes_cli.kanban.kb.complete_task", fake_complete_task)
        monkeypatch.setattr("hermes_cli.kanban.kb.connect_closing", fake_connect_closing)
        monkeypatch.setattr("hermes_cli.kanban._worker_run_id_for", lambda _: None)

        _aux_client = (object(), "judge-model") if judge_available else (None, None)
        monkeypatch.setattr(
            "agent.auxiliary_client.get_text_auxiliary_client",
            lambda name: _aux_client,
        )
        # Match the real judge_goal contract:
        # (verdict, reason, parse_failed, wait_directive, transport_failed)
        monkeypatch.setattr(
            "hermes_cli.goals.judge_goal",
            lambda **kw: (verdict, reason, False, None, False),
        )

        args = argparse.Namespace(task_ids=["t1"], summary=summary, result=None, metadata=None)
        return _cmd_complete(args), complete_calls

    def test_judge_rejects_premature_completion(self, monkeypatch):
        rc, complete_calls = self._run(
            monkeypatch, verdict="continue", reason="criteria not met"
        )
        assert rc != 0, "judge rejection must produce non-zero exit code"
        assert complete_calls == [], (
            "complete_task must NOT be invoked when the judge rejects"
        )


    def test_non_goal_mode_task_skips_gate(self, monkeypatch):
        """Plain (non-goal_mode) tasks are never sent to the judge."""
        rc, complete_calls = self._run(monkeypatch, goal_mode=False)
        assert rc == 0
        assert complete_calls == ["t1"]


def _strict_completion_cli_fixture(
    tmp_path: Path,
    target: dict[str, str] | None = None,
) -> tuple[str, str]:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="ship reviewed change", body="tests pass")
        assert kb.block_task(conn, parent, reason="review", kind="review")
        parent_event = kb.list_events(conn, parent)[-1]
        reviewer = kb.create_review_generation_task(
            conn,
            kb.ReviewGenerationCreate(
                board="default",
                tenant=None,
                key=f"review-lane:default::{parent}:g1",
                title="review",
                body="Review the immutable target.",
                assignee="reviewer",
                created_by="blocked-recovery-router",
                parent_preconditions=kb.ReviewParentPreconditions(
                    task_id=parent,
                    expected_status="blocked",
                    expected_block_kind="review",
                    expected_event_signature=kb.canonical_event_signature(parent_event),
                    expected_review_input_digest=kb.review_input_digest(conn, parent),
                ),
                operation_id="goal-mode-review-generation",
            ),
        ).task_id
        if target is None:
            artifact = tmp_path / "review-target.txt"
            artifact.write_text("immutable target\n", encoding="utf-8")
            target = {
                "kind": "artifact",
                "identity": json.dumps(
                    {"identifier": "review-target", "path": str(artifact)},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "revision": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        metadata = {
            "review": {
                "schema_version": 1,
                "verdict": "APPROVED",
                "target": target,
                "findings": [],
            }
        }
        assert kb.complete_task(conn, reviewer, summary="approved", metadata=metadata)
        reviewer_event = [
            event for event in kb.list_events(conn, reviewer) if event.kind == "completed"
        ][-1]
        target = metadata["review"]["target"]
        preconditions = kb.ReviewedCompletionPreconditions(
            tenant="",
            parent_block_event_id=parent_event.id,
            parent_block_event_signature=kb.canonical_event_signature(parent_event),
            review_input_digest=kb.review_input_digest(conn, parent),
            target_signature=kb.canonical_payload_signature(target),
            reviewer_task_id=reviewer,
            reviewer_completion_event_id=reviewer_event.id,
            reviewer_completion_event_signature=kb.canonical_event_signature(
                reviewer_event
            ),
            reviewer_approval_signature=kb.canonical_payload_signature(
                reviewer_event.payload["review"]
            ),
            operation_id="strict-cli-completion",
        )
    envelope = asdict(preconditions)
    envelope["target_verification"] = target
    command = (
        f"complete {parent} --result shipped --summary verified "
        "--require-goal-judge --automation-preconditions "
        f"'{json.dumps(envelope, separators=(',', ':'))}' --tenant '' --json"
    )
    return parent, command


@pytest.mark.parametrize("outcome", ["rejected", "unavailable", "parse_failed"])
def test_strict_completion_nonacceptance_never_mutates(
    kanban_home,
    monkeypatch,
    tmp_path,
    outcome,
):
    parent, command = _strict_completion_cli_fixture(tmp_path)
    monkeypatch.setattr(
        strict,
        "judge_goal_strict",
        lambda value: strict.StrictJudgeResult(outcome=outcome, reason="not accepted"),
    )

    payload = json.loads(kc.run_slash(command))

    assert payload["outcome"] == outcome
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, parent).status == "blocked"


def test_strict_completion_rechecks_after_judge_acceptance(
    kanban_home,
    monkeypatch,
    tmp_path,
):
    parent, command = _strict_completion_cli_fixture(tmp_path)

    def mutate_then_accept(value):
        with kb.connect_closing() as conn:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET body = ? WHERE id = ?",
                    ("changed during judge", parent),
                )
        return strict.StrictJudgeResult(outcome="accepted", reason="accepted")

    monkeypatch.setattr(strict, "judge_goal_strict", mutate_then_accept)

    payload = json.loads(kc.run_slash(command))

    assert payload["outcome"] == "precondition_changed"
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, parent).status == "blocked"


def test_strict_completion_rechecks_target_after_judge_acceptance(
    kanban_home,
    monkeypatch,
    tmp_path,
):
    artifact = tmp_path / "target.txt"
    artifact.write_text("immutable target\n", encoding="utf-8")
    target = {
        "kind": "artifact",
        "identity": json.dumps(
            {"identifier": "release", "path": str(artifact)},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "revision": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }
    parent, command = _strict_completion_cli_fixture(tmp_path, target)

    def mutate_target_then_accept(value):
        artifact.write_text("drifted target\n", encoding="utf-8")
        return strict.StrictJudgeResult(outcome="accepted", reason="accepted")

    monkeypatch.setattr(strict, "judge_goal_strict", mutate_target_then_accept)

    payload = json.loads(kc.run_slash(command))

    assert payload["outcome"] == "precondition_changed"
    assert payload["reason"].startswith("target_mismatched:")
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, parent).status == "blocked"


def test_strict_completion_exact_replay_bypasses_a_changed_target(
    kanban_home,
    monkeypatch,
    tmp_path,
):
    parent, command = _strict_completion_cli_fixture(tmp_path)
    monkeypatch.setattr(
        strict,
        "judge_goal_strict",
        lambda _value: strict.StrictJudgeResult(outcome="accepted", reason="accepted"),
    )
    first = json.loads(kc.run_slash(command))
    assert first["outcome"] == "accepted"

    (tmp_path / "review-target.txt").write_text("drifted target\n", encoding="utf-8")
    def verifier_should_not_run(*_args):
        raise AssertionError("replay invoked verifier")

    monkeypatch.setattr(
        kc,
        "_post_judge_target_verifier",
        verifier_should_not_run,
    )

    replay = json.loads(kc.run_slash(command))

    assert replay["outcome"] == "accepted"
    assert replay["reason"] == "replayed"
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, parent).status == "done"


def test_strict_completion_exact_acceptance_completes_plain_parent(
    kanban_home,
    monkeypatch,
    tmp_path,
):
    parent, command = _strict_completion_cli_fixture(tmp_path)
    monkeypatch.setattr(
        strict,
        "judge_goal_strict",
        lambda value: strict.StrictJudgeResult(outcome="accepted", reason="accepted"),
    )

    payload = json.loads(kc.run_slash(command))

    assert payload["outcome"] == "accepted"
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, parent).status == "done"
