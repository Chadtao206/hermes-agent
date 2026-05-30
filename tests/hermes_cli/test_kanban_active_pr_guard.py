"""WS4 Task 1: a single active_pr predicate shared by the respawn-guard PARK
site and the scheduled-UNPARK promoter, so both sides agree on whether a prior
worker's PR is still 'recent'."""
from hermes_cli import kanban_db as kb

_PR = "https://github.com/acme/widgets/pull/42"


def _conn(tmp_path):
    return kb.connect(db_path=tmp_path / "kanban.db", readonly=False, _bootstrap=True)


def test_guard_holds_when_recent_pr_comment(tmp_path):
    conn = _conn(tmp_path)
    tid = kb.create_task(conn, title="x", assignee="engineer")
    kb.add_comment(conn, task_id=tid, author="engineer", body=f"opened {_PR}")
    assert kb.active_pr_guard_holds(conn, task_id=tid, assignee="engineer") is True


def test_guard_clears_when_no_recent_pr_comment(tmp_path):
    conn = _conn(tmp_path)
    tid = kb.create_task(conn, title="x", assignee="engineer")
    assert kb.active_pr_guard_holds(conn, task_id=tid, assignee="engineer") is False


def test_guard_never_holds_for_reviewer(tmp_path):
    conn = _conn(tmp_path)
    tid = kb.create_task(conn, title="x", assignee="reviewer")
    kb.add_comment(conn, task_id=tid, author="reviewer", body=f"see {_PR}")
    assert kb.active_pr_guard_holds(conn, task_id=tid, assignee="reviewer") is False


def test_check_respawn_guard_uses_shared_predicate(tmp_path):
    conn = _conn(tmp_path)
    tid = kb.create_task(conn, title="x", assignee="engineer")
    kb.add_comment(conn, task_id=tid, author="engineer", body=_PR)
    assert kb.check_respawn_guard(conn, tid) == "active_pr"
