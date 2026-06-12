from __future__ import annotations

import json
import subprocess
import time
import uuid as uuidlib
from pathlib import Path

from . import pretrust
from .cli import cmd_send_result_json
from .hooks import build_settings, done_channel, ready_channel
from .launcher import HandshakeTimeout, Launcher, TmuxRunner
from .models import SUBTYPE_MAX_TURNS, SUBTYPE_NO_TRANSCRIPT, SessionRecord, TurnResult
from .registry import Registry
from .router import Decision, claude_version_ok, decide_path, tmux_available
from .transcript import parse_transcript

STATE_DIR = Path.home() / ".hermes" / "state" / "claude_session"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
POOL_CAP = 3


def _emit(payload: dict) -> None:
    print(json.dumps(payload))


def _print_fallback(task: str, workdir: str, max_turns, reason: str) -> dict:
    cmd = ["claude", "-p", task, "--output-format", "json",
           "--permission-mode", "bypassPermissions"]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    out = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=600)
    result = json.loads(out.stdout) if out.stdout.strip() else {}
    result["_fallback_reason"] = reason
    return result


def _drain(tmux, sid: str) -> None:
    # Release any server-side waiter left by a timed-out handshake before kill.
    for chan in (ready_channel(sid), done_channel(sid)):
        try:
            tmux.run(["wait-for", "-S", chan])
        except Exception:
            pass


def _parse_session(lp, sid: str, *, attempts: int = 6, interval: float = 0.05) -> TurnResult:
    """Locate <sid>.jsonl and parse it once its size is stable. The transcript
    may still be flushing when the Stop hook fires, so wait for two equal-size
    reads before trusting it."""
    last_size = -1
    for _ in range(attempts):
        matches = list(lp.projects_dir.rglob(f"{sid}.jsonl"))
        if matches:
            size = matches[0].stat().st_size
            if size == last_size:
                return parse_transcript(matches[0], session_id=sid)
            last_size = size
        time.sleep(interval)
    matches = list(lp.projects_dir.rglob(f"{sid}.jsonl"))
    if matches:
        return parse_transcript(matches[0], session_id=sid)
    return TurnResult("", sid, 0, 0.0, {}, SUBTYPE_NO_TRANSCRIPT)


# ---- verb handlers ----------------------------------------------------------

def _run(args, reg, tmux, lp) -> int:
    if decide_path(no_tmux=args.no_tmux, tmux_available=tmux_available(),
                   claude_version_ok=claude_version_ok()) is Decision.PRINT:
        _emit(_print_fallback(args.task, args.workdir, args.max_turns, "preflight"))
        return 0
    reg.reap(now=time.time(), pane_dead=lambda n: lp.pane_dead(name=n),
             kill=lambda n: tmux.run(["kill-session", "-t", n]))

    sid = str(uuidlib.uuid4())
    name = f"cs-{sid[:8]}"
    flag = str(reg.state_dir / f"{sid}.input")
    log_path = str(reg.state_dir / f"{name}.log")
    rec = SessionRecord(name=name, uuid=sid, pid=0, workdir=args.workdir,
                        deadline=time.time() + args.timeout, turns=0, cost=0.0,
                        started_at=time.time(), log_path=log_path)
    if not reg.reserve(rec, cap=POOL_CAP):
        _emit({"type": "result", "subtype": "pool_full", "result": "",
               "session_id": "", "num_turns": 0, "total_cost_usd": 0.0, "usage": {}})
        return 0

    ok = False
    try:
        pretrust.ensure_trusted(args.workdir)
        lp.launch(name=name, uuid=sid, workdir=args.workdir,
                  settings_json=json.dumps(build_settings(sid, input_flag=flag)),
                  model=args.model, ready_timeout=min(60.0, args.timeout),
                  log_path=log_path)
        lp.send(name=name, uuid=sid, prompt=args.task, done_timeout=args.timeout)
        ok = True
    except Exception as exc:  # any launch/send failure → drain, clean up, fall back
        _drain(tmux, sid)
        reason = "handshake" if isinstance(exc, HandshakeTimeout) \
            else f"launch_error:{type(exc).__name__}"
        _emit(_print_fallback(args.task, args.workdir, args.max_turns, reason))
        return 0
    finally:
        if not ok:
            try:
                lp.kill(name=name)
            except Exception:
                pass
            reg.remove(name)

    tr = _parse_session(lp, sid)
    if args.oneshot:
        try:
            lp.kill(name=name)
        except Exception:
            pass
        reg.remove(name)
    _emit(cmd_send_result_json(tr, max_budget_usd=args.max_budget_usd,
                               max_turns=args.max_turns,
                               session_name=None if args.oneshot else name))
    return 0


def _start(args, reg, tmux, lp) -> int:
    reg.reap(now=time.time(), pane_dead=lambda n: lp.pane_dead(name=n),
             kill=lambda n: tmux.run(["kill-session", "-t", n]))
    sid = str(uuidlib.uuid4())
    name = args.name
    flag = str(reg.state_dir / f"{sid}.input")
    log_path = str(reg.state_dir / f"{name}.log")
    rec = SessionRecord(name=name, uuid=sid, pid=0, workdir=args.workdir,
                        deadline=time.time() + args.timeout, turns=0, cost=0.0,
                        started_at=time.time(), log_path=log_path)
    if not reg.reserve(rec, cap=POOL_CAP):
        _emit({"type": "start", "subtype": "pool_full", "name": name})
        return 0
    ok = False
    try:
        pretrust.ensure_trusted(args.workdir)
        lp.launch(name=name, uuid=sid, workdir=args.workdir,
                  settings_json=json.dumps(build_settings(sid, input_flag=flag)),
                  model=args.model, ready_timeout=min(60.0, args.timeout),
                  log_path=log_path)
        ok = True
    except Exception as exc:
        _drain(tmux, sid)
        _emit({"type": "start", "subtype": "error", "name": name,
               "error": f"{type(exc).__name__}:{exc}"})
        return 1
    finally:
        if not ok:
            try:
                lp.kill(name=name)
            except Exception:
                pass
            reg.remove(name)
    _emit({"type": "start", "subtype": "started",
           "session_name": name, "session_id": sid})
    return 0


def _send(args, reg, tmux, lp) -> int:
    rec = reg.get(args.name)
    if rec is None:
        _emit({"type": "result", "subtype": "unknown_session", "result": "",
               "session_id": "", "num_turns": 0, "total_cost_usd": 0.0,
               "usage": {}, "name": args.name})
        return 1
    if args.max_turns is not None and rec.turns >= args.max_turns:
        _emit({"type": "result", "subtype": SUBTYPE_MAX_TURNS, "result": "",
               "session_id": rec.uuid, "num_turns": rec.turns,
               "total_cost_usd": rec.cost, "usage": {}, "session_name": rec.name})
        return 0
    lp.send(name=rec.name, uuid=rec.uuid, prompt=args.prompt, done_timeout=args.timeout)
    tr = _parse_session(lp, rec.uuid)  # transcript num_turns/cost are cumulative
    reg.bump(rec.name, turns=tr.num_turns, cost=tr.total_cost_usd)
    _emit(cmd_send_result_json(tr, max_budget_usd=args.max_budget_usd,
                               max_turns=args.max_turns, session_name=rec.name))
    return 0


def _capture(args, reg, tmux, lp) -> int:
    print(lp.capture(name=args.name, lines=args.lines), end="")  # raw pane (watch)
    return 0


def _steer(args, reg, tmux, lp) -> int:
    lp.send_text(name=args.name, text=args.text)
    _emit({"type": "steer", "name": args.name, "status": "sent"})
    return 0


def _slash(args, reg, tmux, lp) -> int:
    lp.send_text(name=args.name, text=args.cmd)
    _emit({"type": "slash", "name": args.name, "cmd": args.cmd, "status": "sent"})
    return 0


def _status(args, reg, tmux, lp) -> int:
    rec = reg.get(args.name)
    if rec is None:
        _emit({"type": "status", "name": args.name, "state": "unknown"})
        return 1
    dead = lp.pane_dead(name=args.name)
    _emit({"type": "status", "name": rec.name, "session_id": rec.uuid,
           "state": "dead" if dead else "running", "turns": rec.turns,
           "cost": rec.cost, "workdir": rec.workdir, "deadline": rec.deadline})
    return 0


def _stop(args, reg, tmux, lp) -> int:
    for action in (lambda: lp.send_text(name=args.name, text="/exit"),
                   lambda: lp.kill(name=args.name)):
        try:
            action()
        except Exception:
            pass
    reg.remove(args.name)
    _emit({"type": "stop", "name": args.name, "status": "stopped"})
    return 0


def _list(args, reg, tmux, lp) -> int:
    _emit({"type": "list", "sessions": [r.to_dict() for r in reg.list()]})
    return 0


def _gc(args, reg, tmux, lp) -> int:
    reaped = reg.reap(now=time.time(), pane_dead=lambda n: lp.pane_dead(name=n),
                      kill=lambda n: tmux.run(["kill-session", "-t", n]))
    _emit({"type": "gc", "reaped": reaped})
    return 0


_HANDLERS = {
    "run": _run, "start": _start, "send": _send, "capture": _capture,
    "steer": _steer, "slash": _slash, "status": _status, "stop": _stop,
    "list": _list, "gc": _gc,
}


def run(args, *, reg=None, tmux=None, lp=None) -> int:
    handler = _HANDLERS.get(args.verb)
    if handler is None:
        raise SystemExit(f"unknown verb: {args.verb}")
    if reg is None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        reg = Registry(STATE_DIR)
    if tmux is None:
        tmux = TmuxRunner()
    if lp is None:
        lp = Launcher(tmux=tmux, projects_dir=PROJECTS_DIR)
    return handler(args, reg, tmux, lp)
