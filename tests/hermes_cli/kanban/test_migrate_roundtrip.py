import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import migrate_sqlite_to_pg as m
from hermes_cli.kanban.store_sqlite import SqliteKanbanStore


@pytest.fixture
def realistic_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    p = tmp_path / "kanban.db"
    kb.connect(db_path=p, readonly=False, _bootstrap=True).close()
    s = SqliteKanbanStore(board=None)
    # tasks + links + run + events + comment (via lifecycle)
    parent = s.create_task(title="parent", assignee="engineer")
    child = s.create_task(title="child", assignee="engineer")
    s.link_tasks(parent, child)
    s.claim_task(parent, claimer="w1")          # -> task_runs + claimed event
    s.add_comment(parent, author="ops", body="note")
    s.complete_task(parent, summary="done")     # -> completed event, run ended
    # a blocked task to vary status
    other = s.create_task(title="other", assignee="engineer")
    s.block_task(other, reason="need input")
    # notify sub + cursor advance (last_event_id -> real event id range)
    s.add_notify_sub(task_id=child, platform="telegram", chat_id="c1")
    # new_cursor=1 is the first event id on this fresh SQLite source board, so the
    # seeded last_event_id cursor is deterministic (it references a real event).
    s.advance_notify_cursor(task_id=child, platform="telegram", chat_id="c1",
                            new_cursor=1)
    # profile event sub + claim (-> kanban_profile_event_claims) + wake event
    s.add_profile_event_sub(task_id=other, profile="engineer")
    old, new, _ = s.claim_unseen_events_for_profile_sub(task_id=other,
                                                        profile="engineer")
    s.record_profile_wake_success(task_id=other, profile="engineer",
                                  new_cursor=new, last_wake_at=1700000000)
    s.close()
    return tmp_path, p


def test_full_board_round_trip(realistic_home, _pg_dsn):
    home, p = realistic_home
    report, _schema = m.dry_run(_pg_dsn, "default", sqlite_path=p)
    assert report.ok, report.render()
    # every migrated table's count matches exactly
    for table, (src, tgt) in report.counts.items():
        assert src == tgt, f"{table}: {src} != {tgt}"
    # the tables that must be non-empty actually got data
    for table in ("tasks", "task_links", "task_runs", "task_events",
                  "task_comments", "kanban_notify_subs",
                  "kanban_profile_event_subs", "kanban_profile_event_claims",
                  "kanban_profile_wake_events"):
        assert report.counts[table][0] > 0, f"{table} unexpectedly empty in source"
