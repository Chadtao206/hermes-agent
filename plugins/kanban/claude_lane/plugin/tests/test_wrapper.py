# tests/test_wrapper.py
import inspect
import types
import wrapper

# kanban_db split upstream (Sep 2026; no shim for the dispatcher's private names,
# removed 2026-09-14): _default_spawn is read/patched on the module the dispatcher uses.
from hermes_cli import kanban_db_dispatch as kdb


def _fake_default_spawn(task, workspace, *, board=None):
    return 111  # native pid


def test_install_wrap_idempotent_and_signature_preserved(monkeypatch):
    monkeypatch.setattr(kdb, "_default_spawn", _fake_default_spawn, raising=True)
    assert wrapper.install_wrap() is True
    assert getattr(kdb._default_spawn, "__claude_lane_wrapped__", False) is True
    assert "board" in inspect.signature(kdb._default_spawn).parameters
    first = kdb._default_spawn
    assert wrapper.install_wrap() is True   # second call no-ops
    assert kdb._default_spawn is first      # not double-wrapped


def test_wrapper_routes_native_when_not_lane(monkeypatch):
    monkeypatch.setattr(kdb, "_default_spawn", _fake_default_spawn, raising=True)
    monkeypatch.setattr(wrapper.router, "should_use_lane", lambda task: False)
    wrapper.install_wrap()
    t = types.SimpleNamespace(id="t_1", assignee="engineer", skills=None)
    assert kdb._default_spawn(t, "/ws", board="default") == 111  # native


def test_wrapper_routes_lane_when_eligible(monkeypatch):
    monkeypatch.setattr(kdb, "_default_spawn", _fake_default_spawn, raising=True)
    monkeypatch.setattr(wrapper.router, "should_use_lane", lambda task: True)
    monkeypatch.setattr(wrapper.launcher, "launch_lane", lambda task, ws, board: 999)
    wrapper.install_wrap()
    t = types.SimpleNamespace(id="t_1", assignee="engineer", skills=None)
    assert kdb._default_spawn(t, "/ws", board="default") == 999  # lane


def test_install_wrap_bails_on_signature_drift(monkeypatch):
    def drifted(task, workspace):  # no keyword-only board
        return 1
    monkeypatch.setattr(kdb, "_default_spawn", drifted, raising=True)
    assert wrapper.install_wrap() is False
    assert kdb._default_spawn is drifted  # untouched -> native everywhere


def test_parse_session_name_handles_hyphenated_board():
    assert wrapper.parse_session_name("hermes-kanban-pr-review-t_abc123") == ("pr-review", "t_abc123")
    assert wrapper.parse_session_name("not-a-session") is None


# -- BLOCKER 2(b): relaunch must pass --adopt so it claims the live session ---
def test_relaunch_supervisor_passes_adopt(monkeypatch):
    captured = {}

    def fake_popen(argv, *a, **kw):
        captured["argv"] = argv
        return types.SimpleNamespace(pid=1234)

    monkeypatch.setattr(wrapper.subprocess, "Popen", fake_popen)
    cfg = {"tmux_bin": "tmux", "socket": "sock", "heartbeat_seconds": 120,
           "idle_timeout_seconds": 1200, "max_runtime_seconds": 5400}
    rec = {"task": "t_1", "board": "default", "session": "hermes-kanban-default-t_1",
           "transcript": "/t.jsonl", "run_id": 1514}
    wrapper._relaunch_supervisor(rec, cfg)
    assert "--adopt" in captured["argv"]
    # ... and always fenced: an adopting supervisor with no run could stamp its
    # pid onto a successor run's card.
    assert captured["argv"][captured["argv"].index("--run-id") + 1] == "1514"


# -- MAJOR 6(b): a transient card-read failure must NOT kill the session ------
def test_readopt_skips_session_on_read_failure(monkeypatch):
    monkeypatch.setattr(wrapper.config, "lane_config",
                        lambda: {"tmux_bin": "tmux", "socket": "sock",
                                 "heartbeat_seconds": 120, "idle_timeout_seconds": 1200,
                                 "max_runtime_seconds": 5400})
    monkeypatch.setattr(wrapper, "_list_sessions",
                        lambda cfg: (True, ["hermes-kanban-default-t_abc123"]))
    monkeypatch.setattr(wrapper.state, "session_load", lambda tid: {})

    def boom(*a, **kw):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(wrapper._connect_mod, "connect_closing", boom)

    killed = {"n": 0}
    deleted = {"n": 0}
    relaunched = {"n": 0}

    def fake_run(cmd, *a, **kw):
        if "kill-session" in cmd:
            killed["n"] += 1
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(wrapper.subprocess, "run", fake_run)
    monkeypatch.setattr(wrapper.state, "session_delete",
                        lambda tid: deleted.__setitem__("n", deleted["n"] + 1))
    monkeypatch.setattr(wrapper, "_relaunch_supervisor",
                        lambda rec, cfg: relaunched.__setitem__("n", relaunched["n"] + 1))
    gc = {}
    monkeypatch.setattr(wrapper.state, "gc_old_state",
                        lambda active, **k: gc.update({"active": set(active)}) or 0)

    wrapper.readopt()
    assert killed["n"] == 0       # live session left alone
    assert deleted["n"] == 0      # sidecar not removed
    assert relaunched["n"] == 0   # not relaunched either
    # GC runs at the end and the live session's task is in the protected active set
    assert gc["active"] == {"t_abc123"}


def test_list_sessions_distinguishes_failure_from_empty(monkeypatch):
    import types as _t
    cfg = {"tmux_bin": "tmux", "socket": "sock"}
    # rc==0, empty -> trustworthy empty
    monkeypatch.setattr(wrapper.subprocess, "run",
                        lambda *a, **k: _t.SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert wrapper._list_sessions(cfg) == (True, [])
    # rc!=0 'no server' -> trustworthy empty
    monkeypatch.setattr(wrapper.subprocess, "run",
                        lambda *a, **k: _t.SimpleNamespace(returncode=1, stdout="", stderr="no server running on /tmp/x"))
    assert wrapper._list_sessions(cfg) == (True, [])
    # rc!=0 other failure -> UNTRUSTWORTHY (must not GC)
    monkeypatch.setattr(wrapper.subprocess, "run",
                        lambda *a, **k: _t.SimpleNamespace(returncode=1, stdout="", stderr="server busy: resource temporarily unavailable"))
    assert wrapper._list_sessions(cfg) == (False, [])


def test_gc_now_skips_when_enumeration_untrustworthy(monkeypatch):
    monkeypatch.setattr(wrapper, "_list_sessions", lambda cfg: (False, []))
    called = {"n": 0}
    monkeypatch.setattr(wrapper.state, "gc_old_state", lambda active, **k: called.__setitem__("n", called["n"] + 1))
    wrapper._gc_now({"tmux_bin": "tmux", "socket": "sock"})
    assert called["n"] == 0  # footgun guard: never GC on an untrustworthy (maybe-falsely-empty) active set


def test_parse_session_name_matches_mixedcase_id():
    # regex broadened to the shim's alphabet so an id-format change can't drop a live session
    assert wrapper.parse_session_name("hermes-kanban-default-t_AbC123") == ("default", "t_AbC123")
