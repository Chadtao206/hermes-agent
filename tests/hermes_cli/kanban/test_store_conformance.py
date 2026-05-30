def test_create_then_get(store):
    tid = store.create_task(title="hello", assignee="engineer")
    t = store.get_task(tid)
    assert t is not None and t.id == tid and t.title == "hello"


def test_get_missing_returns_none(store):
    assert store.get_task("t_does_not_exist") is None
