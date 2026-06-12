import argparse
import json

import pytest

from tools.claude_session import dispatch
from tools.claude_session.cli import build_parser
from tools.claude_session.launcher import HandshakeTimeout, Launcher
from tools.claude_session.models import SessionRecord
from tools.claude_session.registry import Registry


class FakeTmux:
    def __init__(self, *, wait_results=None, pane="", list_panes="0"):
        self.calls = []
        self._wait = list(wait_results or [])
        self.pane = pane
        self.list_panes = list_panes

    def run(self, args, timeout=None):
        self.calls.append(args)
        if args and args[0] == "wait-for" and "-S" not in args and "-U" not in args:
            if self._wait and not self._wait.pop(0):
                raise HandshakeTimeout(args[-1])
            return ""
        if args[:1] == ["capture-pane"]:
            return self.pane
        if args[:1] == ["list-panes"]:
            return self.list_panes
        return ""


def _args(*argv):
    return build_parser().parse_args(list(argv))


def _rec(name):
    return SessionRecord(name=name, uuid="u-" + name, pid=0, workdir="/w",
                         deadline=9e18, turns=0, cost=0.0, started_at=0.0, log_path="/l")


def _deps(tmp_path, **kw):
    fake = FakeTmux(**kw)
    return fake, Registry(tmp_path), Launcher(tmux=fake, projects_dir=tmp_path)


def test_unknown_verb_raises(tmp_path):
    fake, reg, lp = _deps(tmp_path)
    with pytest.raises(SystemExit):
        dispatch.run(argparse.Namespace(verb="bogus"), reg=reg, tmux=fake, lp=lp)


def test_list_emits_sessions(tmp_path, capsys):
    fake, reg, lp = _deps(tmp_path)
    reg.add(_rec("a")); reg.add(_rec("b"))
    dispatch.run(_args("list"), reg=reg, tmux=fake, lp=lp)
    out = json.loads(capsys.readouterr().out)
    assert {s["name"] for s in out["sessions"]} == {"a", "b"}


def test_gc_reaps_dead(tmp_path, capsys):
    fake, reg, lp = _deps(tmp_path, list_panes="")  # empty => pane_dead => reaped
    reg.add(_rec("dead"))
    dispatch.run(_args("gc"), reg=reg, tmux=fake, lp=lp)
    out = json.loads(capsys.readouterr().out)
    assert out["reaped"] == ["dead"]
    assert reg.get("dead") is None


def test_send_unknown_session_errors(tmp_path, capsys):
    fake, reg, lp = _deps(tmp_path)
    rc = dispatch.run(_args("send", "--name", "nope", "--prompt", "hi"),
                      reg=reg, tmux=fake, lp=lp)
    out = json.loads(capsys.readouterr().out)
    assert out["subtype"] == "unknown_session" and rc == 1


def test_send_parses_transcript_and_bumps(tmp_path, capsys):
    fake, reg, lp = _deps(tmp_path, wait_results=[True])
    reg.add(SessionRecord(name="s1", uuid="uuid-xyz", pid=0, workdir="/w",
                          deadline=9e18, turns=0, cost=0.0, started_at=0.0,
                          log_path="/l"))
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / "uuid-xyz.jsonl").write_text(
        '{"type":"assistant","requestId":"r1","message":{"content":'
        '[{"type":"text","text":"hi there"}],"usage":{"input_tokens":1,'
        '"output_tokens":2},"model":"claude-opus-4-5"}}\n')
    dispatch.run(_args("send", "--name", "s1", "--prompt", "hello"),
                 reg=reg, tmux=fake, lp=lp)
    out = json.loads(capsys.readouterr().out)
    assert out["result"] == "hi there"
    assert out["session_name"] == "s1"
    assert reg.get("s1").turns == 1


def test_send_blocks_at_max_turns(tmp_path, capsys):
    fake, reg, lp = _deps(tmp_path)
    rec = _rec("s1"); rec.turns = 2
    reg.add(rec)
    dispatch.run(_args("send", "--name", "s1", "--prompt", "x", "--max-turns", "2"),
                 reg=reg, tmux=fake, lp=lp)
    out = json.loads(capsys.readouterr().out)
    assert out["subtype"] == "error_max_turns"
    # never sent because already at cap
    assert not any(c[:2] == ["send-keys", "-t"] for c in fake.calls)


def test_steer_sends_text(tmp_path, capsys):
    fake, reg, lp = _deps(tmp_path)
    dispatch.run(_args("steer", "--name", "s1", "--text", "stop that"),
                 reg=reg, tmux=fake, lp=lp)
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "sent"
    assert ["send-keys", "-t", "s1", "-l", "--", "stop that"] in fake.calls


def test_slash_sends_command(tmp_path, capsys):
    fake, reg, lp = _deps(tmp_path)
    dispatch.run(_args("slash", "--name", "s1", "--cmd", "/compact"),
                 reg=reg, tmux=fake, lp=lp)
    out = json.loads(capsys.readouterr().out)
    assert out["cmd"] == "/compact"
    assert ["send-keys", "-t", "s1", "-l", "--", "/compact"] in fake.calls


def test_capture_prints_raw_pane(tmp_path, capsys):
    fake, reg, lp = _deps(tmp_path, pane="PANE TEXT")
    dispatch.run(_args("capture", "--name", "s1"), reg=reg, tmux=fake, lp=lp)
    assert capsys.readouterr().out == "PANE TEXT"


def test_status_reports_running(tmp_path, capsys):
    fake, reg, lp = _deps(tmp_path, list_panes="0")
    reg.add(_rec("s1"))
    dispatch.run(_args("status", "--name", "s1"), reg=reg, tmux=fake, lp=lp)
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "running"


def test_stop_removes_and_kills(tmp_path, capsys):
    fake, reg, lp = _deps(tmp_path)
    reg.add(_rec("s1"))
    dispatch.run(_args("stop", "--name", "s1"), reg=reg, tmux=fake, lp=lp)
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "stopped"
    assert reg.get("s1") is None
    assert ["kill-session", "-t", "s1"] in fake.calls


def test_start_reserves_and_launches(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(dispatch.pretrust, "ensure_trusted", lambda *a, **k: True)
    fake, reg, lp = _deps(tmp_path, wait_results=[True])
    dispatch.run(_args("start", "--name", "warm1", "--workdir", str(tmp_path)),
                 reg=reg, tmux=fake, lp=lp)
    out = json.loads(capsys.readouterr().out)
    assert out["subtype"] == "started" and out["session_name"] == "warm1"
    assert reg.get("warm1") is not None


def test_run_oneshot_no_transcript_cleans_up(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(dispatch, "tmux_available", lambda: True)
    monkeypatch.setattr(dispatch, "claude_version_ok", lambda: True)
    monkeypatch.setattr(dispatch.pretrust, "ensure_trusted", lambda *a, **k: True)
    fake, reg, lp = _deps(tmp_path, wait_results=[True, True])  # ready + done
    rc = dispatch.run(
        _args("run", "--task", "hi", "--workdir", str(tmp_path),
              "--oneshot", "--timeout", "5"),
        reg=reg, tmux=fake, lp=lp)
    out = json.loads(capsys.readouterr().out)
    assert out["subtype"] == "error_transcript_unavailable"  # no real transcript
    assert reg.list() == []   # oneshot tore it down
    assert rc == 0
