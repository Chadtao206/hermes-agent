import time
from uuid import uuid4


def test_create_then_get(store):
    tid = store.create_task(title="hello", assignee="engineer")
    t = store.get_task(tid)
    assert t is not None and t.id == tid and t.title == "hello"


def test_get_missing_returns_none(store):
    assert store.get_task("t_does_not_exist") is None


def test_block_unblock_roundtrip(store):
    tid = store.create_task(title="x", assignee="engineer")
    assert store.block_task(tid, reason="need input") is True
    assert store.get_task(tid).status == "blocked"
    assert store.unblock_task(tid) is True
    assert store.get_task(tid).status == "ready"


def test_priority_and_edit_and_status_direct(store):
    tid = store.create_task(title="orig", assignee="engineer")
    assert store.set_task_priority(tid, 7) is True
    assert store.get_task(tid).priority == 7
    assert store.edit_task_fields(tid, title="renamed") is True
    assert store.get_task(tid).title == "renamed"
    assert store.set_status_direct(tid, "todo") is True
    assert store.get_task(tid).status == "todo"


def test_reassign_and_delete(store):
    tid = store.create_task(title="x", assignee="engineer")
    assert store.reassign_task(tid, "reviewer") is True
    assert store.get_task(tid).assignee == "reviewer"
    assert store.delete_task(tid) is True
    assert store.get_task(tid) is None


def test_link_unlink_and_parents_children(store):
    p = store.create_task(title="parent", assignee="engineer")
    c = store.create_task(title="child", assignee="engineer")
    store.link_tasks(p, c)
    assert c in store.child_ids(p)
    assert p in store.parent_ids(c)
    assert store.unlink_tasks(p, c) is True
    assert store.child_ids(p) == []


def test_comment_roundtrip(store):
    tid = store.create_task(title="x", assignee="engineer")
    cid = store.add_comment(tid, author="ops", body="note")
    assert isinstance(cid, int)
    bodies = [c["body"] if isinstance(c, dict) else c.body for c in store.list_comments(tid)]
    assert "note" in bodies


def test_notify_sub_add_list_remove(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.add_notify_sub(task_id=tid, platform="telegram", chat_id="c1")
    subs = store.list_notify_subs()
    assert any((s["task_id"] if isinstance(s, dict) else s.task_id) == tid for s in subs)
    scoped = store.list_notify_subs(task_id=tid)
    assert scoped and all((s["task_id"] if isinstance(s, dict) else s.task_id) == tid for s in scoped)
    assert store.remove_notify_sub(task_id=tid, platform="telegram", chat_id="c1") is True


def test_profile_sub_and_recompute(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.add_profile_event_sub(task_id=tid, profile="engineer")
    subs = store.list_profile_event_subs(task_id=tid, profile="engineer", enabled_only=False)
    assert subs
    assert isinstance(store.recompute_ready(), int)
    assert isinstance(store.has_spawnable_ready(), bool)
    assert store.remove_profile_event_sub(task_id=tid, profile="engineer", name="") is True


def test_gc_events_returns_int(store):
    store.create_task(title="x", assignee="engineer")
    assert isinstance(store.gc_events(), int)


def test_set_workspace_path(store):
    tid = store.create_task(title="x", assignee="engineer")
    # set_workspace_path returns None (kb function has no return value)
    store.set_workspace_path(tid, "/tmp/ws")
    assert store.get_task(tid).workspace_path == "/tmp/ws"


def test_promote_task_is_callable(store):
    # promote_task requires a specific task state (todo/blocked) and a mandatory
    # `actor` kwarg; rather than wiring up full state, just confirm the method
    # exists, is callable, and returns a (bool, str|None) tuple on a missing task.
    tid = store.create_task(title="x", assignee="engineer")
    result = store.promote_task(tid, actor="ops")
    assert isinstance(result, tuple) and len(result) == 2
    ok, err = result
    assert isinstance(ok, bool)


def test_complete_basic_and_runs_and_summary(store):
    tid = store.create_task(title="x", assignee="engineer")
    assert store.complete_task(tid, result="done-res", summary="sum") is True
    assert store.get_task(tid).status == "done"
    s = store.latest_summary(tid)
    assert s is None or isinstance(s, str)
    assert isinstance(store.list_runs(tid), list)


def test_events_recorded_and_listed(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.set_task_priority(tid, 3)
    kinds = [e.kind for e in store.list_events(tid)]
    assert "created" in kinds and "reprioritized" in kinds


def test_notify_event_claiming_cursor(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.add_notify_sub(task_id=tid, platform="telegram", chat_id="c1")
    store.add_comment(tid, author="ops", body="n1")
    old, new, evs = store.claim_unseen_events_for_sub(
        task_id=tid, platform="telegram", chat_id="c1")
    assert new >= old and isinstance(evs, list)
    old2, new2, evs2 = store.claim_unseen_events_for_sub(
        task_id=tid, platform="telegram", chat_id="c1")
    assert new2 == new and evs2 == []


def test_claim_task_atomic(store):
    tid = store.create_task(title="x", assignee="engineer")
    assert store.get_task(tid).status == "ready"
    t1 = store.claim_task(tid, claimer="w1")
    assert t1 is not None and t1.status == "running"
    # second claim of the same (now-running) task returns None
    assert store.claim_task(tid, claimer="w2") is None


def test_heartbeat_worker(store):
    tid = store.create_task(title="x", assignee="engineer")
    claimed = store.claim_task(tid, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    assert store.heartbeat_worker(task_id=tid) is True
    # a task left in ready (never claimed) cannot heartbeat
    tid2 = store.create_task(title="y", assignee="engineer")
    assert store.heartbeat_worker(task_id=tid2) is False


def test_notifier_heartbeat_roundtrip(store):
    # The notifier-heartbeat sidecar is a shared SQLite DB keyed by
    # board_slug/db_path; use a unique key per run so the two backend
    # parametrizations don't collide.
    unique = uuid4().hex
    notifier_id = "n_" + unique
    board_slug = "bs_" + unique
    db_path = "/tmp/hb_" + unique + ".sqlite3"
    store.record_notifier_heartbeat(
        notifier_id=notifier_id,
        board_slug=board_slug,
        db_path=db_path,
        notifier_profile="engineer",
        host="testhost",
        pid=4242,
        started_at=int(time.time()),
    )
    rows = store.list_notifier_heartbeats(board_slug=board_slug, db_path=db_path)
    ids = [r["notifier_id"] if isinstance(r, dict) else r.notifier_id for r in rows]
    assert notifier_id in ids


def test_list_profile_wake_events_empty(store):
    rows = store.list_profile_wake_events()
    assert isinstance(rows, list)
