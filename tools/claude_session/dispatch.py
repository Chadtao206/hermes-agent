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
from .models import SUBTYPE_NO_TRANSCRIPT, SessionRecord, TurnResult
from .registry import Registry
from .router import Decision, claude_version_ok, decide_path, tmux_available
from .transcript import parse_transcript

STATE_DIR = Path.home() / ".hermes" / "state" / "claude_session"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
POOL_CAP = 3


def _print_fallback(task: str, workdir: str, max_turns, reason: str) -> dict:
    cmd = ["claude", "-p", task, "--output-format", "json",
           "--permission-mode", "bypassPermissions"]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    out = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=600)
    result = json.loads(out.stdout) if out.stdout.strip() else {}
    result["_fallback_reason"] = reason
    return result


def run(args) -> int:
    if args.verb != "run":
        raise SystemExit(f"verb not wired in dispatch: {args.verb}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if decide_path(no_tmux=args.no_tmux, tmux_available=tmux_available(),
                   claude_version_ok=claude_version_ok()) is Decision.PRINT:
        print(json.dumps(_print_fallback(args.task, args.workdir,
                                         args.max_turns, "preflight")))
        return 0

    reg = Registry(STATE_DIR)
    tmux = TmuxRunner()
    lp = Launcher(tmux=tmux, projects_dir=PROJECTS_DIR)
    reg.reap(now=time.time(), pane_dead=lambda n: lp.pane_dead(name=n),
             kill=lambda n: tmux.run(["kill-session", "-t", n]))

    sid = str(uuidlib.uuid4())
    name = f"cs-{sid[:8]}"
    flag = str(STATE_DIR / f"{sid}.input")
    log_path = str(STATE_DIR / f"{name}.log")
    rec = SessionRecord(name=name, uuid=sid, pid=0, workdir=args.workdir,
                        deadline=time.time() + args.timeout, turns=0, cost=0.0,
                        started_at=time.time(), log_path=log_path)

    if not reg.reserve(rec, cap=POOL_CAP):
        print(json.dumps({"type": "result", "subtype": "pool_full", "result": "",
                          "session_id": "", "num_turns": 0,
                          "total_cost_usd": 0.0, "usage": {}}))
        return 0

    pretrust.ensure_trusted(args.workdir)
    settings = json.dumps(build_settings(sid, input_flag=flag))
    try:
        lp.launch(name=name, uuid=sid, workdir=args.workdir, settings_json=settings,
                  model=args.model, ready_timeout=min(60.0, args.timeout),
                  log_path=log_path)
        lp.send(name=name, uuid=sid, prompt=args.task, done_timeout=args.timeout)
    except HandshakeTimeout as exc:
        for chan in (ready_channel(sid), done_channel(sid)):
            try:
                tmux.run(["wait-for", "-S", chan])
            except Exception:
                pass
        lp.kill(name=name)
        reg.remove(name)
        print(json.dumps(_print_fallback(args.task, args.workdir,
                                         args.max_turns, f"handshake:{exc}")))
        return 0

    matches = list(PROJECTS_DIR.rglob(f"{sid}.jsonl"))
    tr = (parse_transcript(matches[0], session_id=sid) if matches
          else TurnResult("", sid, 0, 0.0, {}, SUBTYPE_NO_TRANSCRIPT))

    if args.oneshot:
        lp.kill(name=name)
        reg.remove(name)
    print(json.dumps(cmd_send_result_json(tr, max_budget_usd=args.max_budget_usd)))
    return 0
