from tools.claude_session.models import SessionRecord
from tools.claude_session.registry import Registry


def _rec(name, deadline=9e18):
    return SessionRecord(name=name, uuid="u-" + name, pid=1, workdir="/w",
                         deadline=deadline, turns=0, cost=0.0, started_at=0.0,
                         log_path="/l")


def test_add_get_list_remove(tmp_path):
    reg = Registry(tmp_path)
    reg.add(_rec("a"))
    assert reg.get("a").uuid == "u-a"
    assert [r.name for r in reg.list()] == ["a"]
    reg.remove("a")
    assert reg.get("a") is None


def test_reserve_is_atomic_and_respects_cap(tmp_path):
    reg = Registry(tmp_path)
    assert reg.reserve(_rec("a"), cap=2) is True
    assert reg.reserve(_rec("b"), cap=2) is True
    assert reg.reserve(_rec("c"), cap=2) is False
    assert reg.active_count() == 2
    assert reg.get("c") is None


def test_reap_kills_dead_and_expired(tmp_path):
    reg = Registry(tmp_path)
    reg.add(_rec("dead"))
    reg.add(_rec("expired", deadline=100.0))
    reg.add(_rec("alive"))
    killed = []
    reaped = reg.reap(now=200.0, pane_dead=lambda n: n == "dead", kill=killed.append)
    assert set(reaped) == {"dead", "expired"}
    assert set(killed) == {"dead", "expired"}
    assert [r.name for r in reg.list()] == ["alive"]
