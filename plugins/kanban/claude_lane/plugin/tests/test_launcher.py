# tests/test_launcher.py
import types
import pytest
import launcher


def test_stable_claude_bin_refuses_fnm(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda _x: "/x/fnm_multishells/9_9/bin/claude")
    monkeypatch.setattr(launcher.os.path, "realpath", lambda _x: "/y/fnm_multishells/9_9/bin/claude")
    assert launcher.stable_claude_bin() is None


def test_stable_claude_bin_ok(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda _x: "/shim/claude")
    monkeypatch.setattr(launcher.os.path, "realpath", lambda _x: "/stable/claude.exe")
    monkeypatch.setattr(launcher.os, "access", lambda _p, _m: True)
    assert launcher.stable_claude_bin() == "/stable/claude.exe"


def test_launch_lane_returns_supervisor_pid(monkeypatch, tmp_path):
    t = types.SimpleNamespace(id="t_1", assignee="engineer", tenant=None, branch_name=None,
                              current_run_id=1, claim_lock="host:1")
    monkeypatch.setattr(launcher, "stable_claude_bin", lambda cfg=None: "/stable/claude.exe")
    monkeypatch.setattr(launcher, "supervisor_contract_available", lambda: True)
    monkeypatch.setattr(launcher, "_oauth_token", lambda p: "oat-xyz")
    monkeypatch.setattr(launcher, "_real_home", lambda: "/Users/ctao")
    monkeypatch.setattr(launcher, "_hermes_bin", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(launcher.brief, "compile_brief", lambda tid, hb: "BRIEF")
    monkeypatch.setattr(launcher.brief, "persona_for", lambda p: "PERSONA")
    monkeypatch.setattr(launcher.envbuild, "mirror_kanban_env",
                        lambda task, ws, board: {"HERMES_KANBAN_TASK": task.id, "HERMES_PROFILE": "engineer",
                                                 "HERMES_KANBAN_BOARD": "default"})
    (tmp_path / "cfg").mkdir(exist_ok=True)  # real prepare_* makes the dir; the stub must too (empty-mcp.json write)
    monkeypatch.setattr(launcher.cfgdir, "prepare_session_config_dir", lambda **kw: str(tmp_path / "cfg"))
    monkeypatch.setattr(launcher.cfgdir, "transcript_path", lambda c, w, s: "/t.jsonl")
    recorded = {}
    monkeypatch.setattr(launcher, "_tmux_new_session", lambda **kw: recorded.update(kw))
    sup_kw = {}
    monkeypatch.setattr(launcher, "_spawn_supervisor", lambda **kw: (sup_kw.update(kw) or 4242))
    saved = {}
    monkeypatch.setattr(launcher.state, "session_save", lambda tid, rec: saved.update({tid: rec}))
    pid = launcher.launch_lane(t, "/ws", "default")
    assert pid == 4242
    assert recorded["session"].startswith("hermes-kanban-default-t_1")
    assert saved["t_1"]["board"] == "default"
    # unattended sessions must not block on MCP-approval prompts: empty --mcp-config
    # + --strict-mcp-config => zero servers, no project .mcp.json discovery
    assert "--strict-mcp-config" in recorded["argv"]
    assert "--mcp-config" in recorded["argv"]
    assert "--permission-mode" in recorded["argv"] and "bypassPermissions" in recorded["argv"]
    # supervisor gets the cfgdir so it can clean it up on terminal exit
    assert sup_kw.get("cfgdir") == str(tmp_path / "cfg")


def test_spawn_supervisor_passes_coldstart_grace(monkeypatch):
    # The cold-start requeue threshold is config-driven; assert the cfg value
    # actually reaches the supervisor argv (a dropped flag would silently fall
    # back to the supervisor default and make the config key dead).
    captured = {}
    monkeypatch.setattr(launcher.subprocess, "Popen",
                        lambda argv, *a, **kw: captured.update(argv=argv) or types.SimpleNamespace(pid=999))
    cfg = {"tmux_bin": "/tmux", "socket": "s", "heartbeat_seconds": 120,
           "idle_timeout_seconds": 1200, "max_runtime_seconds": 5400,
           "coldstart_grace_seconds": 77}
    pid = launcher._spawn_supervisor(task=types.SimpleNamespace(id="t_1"), board="default",
                                     session="sess", transcript="/t.jsonl", cfgdir="/cfg", cfg=cfg,
                                     run_id=1514)
    assert pid == 999
    argv = captured["argv"]
    assert "--coldstart-grace" in argv
    assert argv[argv.index("--coldstart-grace") + 1] == "77"


# -- MAJOR 4: partial-launch race -> kill the pane + re-raise (no double-run) --
def test_launch_lane_kills_pane_and_reraises_on_spawn_failure(monkeypatch, tmp_path):
    t = types.SimpleNamespace(id="t_1", assignee="engineer", tenant=None, branch_name=None,
                              current_run_id=1, claim_lock="host:1")
    monkeypatch.setattr(launcher, "stable_claude_bin", lambda cfg=None: "/stable/claude.exe")
    monkeypatch.setattr(launcher, "supervisor_contract_available", lambda: True)
    monkeypatch.setattr(launcher, "_oauth_token", lambda p: "oat-xyz")
    monkeypatch.setattr(launcher, "_real_home", lambda: "/Users/ctao")
    monkeypatch.setattr(launcher, "_hermes_bin", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(launcher.brief, "compile_brief", lambda tid, hb: "BRIEF")
    monkeypatch.setattr(launcher.brief, "persona_for", lambda p: "PERSONA")
    monkeypatch.setattr(launcher.envbuild, "mirror_kanban_env",
                        lambda task, ws, board: {"HERMES_KANBAN_TASK": task.id})
    (tmp_path / "cfg").mkdir(exist_ok=True)  # real prepare_* makes the dir; the stub must too (empty-mcp.json write)
    monkeypatch.setattr(launcher.cfgdir, "prepare_session_config_dir", lambda **kw: str(tmp_path / "cfg"))
    monkeypatch.setattr(launcher.cfgdir, "transcript_path", lambda c, w, s: "/t.jsonl")
    # tmux new-session succeeds (no-op); supervisor spawn then blows up.
    created = {}
    monkeypatch.setattr(launcher, "_tmux_new_session",
                        lambda **kw: created.update({"session": kw["session"]}))

    def boom(**kw):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(launcher, "_spawn_supervisor", boom)
    killed = {}
    monkeypatch.setattr(launcher, "_tmux_kill",
                        lambda **kw: killed.update({"session": kw["session"]}))
    # session_save should never be reached, but guard it.
    monkeypatch.setattr(launcher.state, "session_save", lambda tid, rec: None)

    with pytest.raises(RuntimeError):
        launcher.launch_lane(t, "/ws", "default")
    # the just-created pane was killed (so native fallback doesn't double-run)
    assert killed.get("session") == created.get("session")
    assert created.get("session", "").startswith("hermes-kanban-default-t_1")


def test_launch_lane_disallows_ask_user_question(monkeypatch, tmp_path):
    # 2026-09-04: four of ten wedged lanes were parked on an AskUserQuestion
    # dialog nobody can answer in an unattended tmux pane. Bypass mode does not
    # suppress that tool; removing it from the session does.
    import launcher, types
    recorded = {}
    t = types.SimpleNamespace(id="t_q", assignee="engineer", tenant=None, branch_name=None,
                              current_run_id=7, claim_lock=None, max_runtime_seconds=None)
    monkeypatch.setattr(launcher, "stable_claude_bin", lambda cfg=None: "/stable/claude.exe")
    monkeypatch.setattr(launcher, "supervisor_contract_available", lambda: True)
    monkeypatch.setattr(launcher, "_oauth_token", lambda p: "oat-xyz")
    monkeypatch.setattr(launcher, "_real_home", lambda: "/Users/ctao")
    monkeypatch.setattr(launcher, "_hermes_bin", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(launcher.brief, "compile_brief", lambda tid, hb: "BRIEF")
    monkeypatch.setattr(launcher.brief, "persona_for", lambda p: "PERSONA")
    monkeypatch.setattr(launcher.envbuild, "mirror_kanban_env", lambda task, ws, board: {})
    monkeypatch.setattr(launcher.envbuild, "build_session_env", lambda **kw: {"HOME": "/Users/ctao"})
    monkeypatch.setattr(launcher.cfgdir, "prepare_session_config_dir", lambda **kw: str(tmp_path / "cfg"))
    monkeypatch.setattr(launcher.cfgdir, "transcript_path", lambda c, w, s: "/t.jsonl")
    monkeypatch.setattr(launcher.router, "profile_model_provider", lambda p: ("claude-opus-4-8", "anthropic"))
    monkeypatch.setattr(launcher, "_tmux_new_session", lambda **kw: recorded.update(kw))
    monkeypatch.setattr(launcher, "_spawn_supervisor", lambda **kw: 4242)
    monkeypatch.setattr(launcher.state, "session_save", lambda tid, rec: None)
    (tmp_path / "cfg").mkdir()
    launcher.launch_lane(t, str(tmp_path), "default")
    argv = recorded["argv"]
    assert "--disallowedTools" in argv
    assert argv[argv.index("--disallowedTools") + 1] == "AskUserQuestion"
