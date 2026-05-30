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
