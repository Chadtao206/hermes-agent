"""Install the guarded monkeypatch and re-adopt orphaned sessions.

Installed from ``plugins/kanban/claude_lane`` in the hermes-agent repo — see
that directory's README for install/rollback ordering. Edit the versioned copy.
"""
import inspect
import logging
import os
import re
import subprocess
import time

import hermes_cli.kanban_db as kdb

# kanban_db was split upstream (Sep 2026; private-name shim removed 2026-09-14): the
# dispatcher calls ``_default_spawn`` as a kanban_db_dispatch global, so the wrap must
# land on that module when it exists, and connect helpers now live in kanban_db_connect.
from hermes_cli import kanban_db_dispatch as _dispatch_mod
from hermes_cli import kanban_db_connect as _connect_mod

try:
    from . import config, launcher, router, state
except ImportError:  # pragma: no cover
    import config, launcher, router, state

logger = logging.getLogger(__name__)
# task id alphabet matches the shim's validator (t_[A-Za-z0-9]+), not just lowercase
# hex, so a future core id-format change can't silently drop a LIVE session from the
# GC-protected `active` set.
_SESSION_RE = re.compile(r"^hermes-kanban-(?P<board>.+)-(?P<task>t_[A-Za-z0-9]+)$")
VENV_PY = "/Users/ctao/.hermes/hermes-agent/venv/bin/python"
SUPERVISOR = os.path.expanduser("~/.hermes/scripts/claude_kanban_supervisor.py")
_TERMINAL_STATUSES = ("done", "blocked", "archived", "gave_up")
_GC_THROTTLE_SECONDS = 3600  # opportunistic GC runs at most this often (timestamp-gated)


def parse_session_name(name):
    m = _SESSION_RE.match(name or "")
    if not m:
        return None
    return (m.group("board"), m.group("task"))


def _signature_ok(fn):
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return ("task" in params and "workspace" in params
            and "board" in params
            and params["board"].kind == inspect.Parameter.KEYWORD_ONLY)


def install_wrap():
    """Wrap _dispatch_mod._default_spawn once, signature-guarded. Returns True if wrapped/already; False if skipped."""
    orig = _dispatch_mod._default_spawn
    if getattr(orig, "__claude_lane_wrapped__", False):
        return True
    if not _signature_ok(orig):
        logger.warning("claude-kanban-lane: _default_spawn signature drifted; staying native")
        return False

    def _wrapped_spawn(task, workspace, *, board=None):
        try:
            _maybe_gc()  # throttled; best-effort
        except Exception:
            pass
        try:
            if router.should_use_lane(task):
                pid = launcher.launch_lane(task, workspace, board)
                if pid:
                    return pid
        except Exception:
            logger.warning("claude-kanban-lane: lane launch failed; native fallback", exc_info=True)
        return orig(task, workspace, board=board)

    _wrapped_spawn.__claude_lane_wrapped__ = True
    _wrapped_spawn.__claude_lane_orig__ = orig
    _dispatch_mod._default_spawn = _wrapped_spawn
    logger.info("claude-kanban-lane: _default_spawn wrapped")
    return True


def _list_sessions(cfg):
    """Return (ok, [session_names]).

    ok=False means the enumeration is UNTRUSTWORTHY (tmux errored for a reason
    other than 'no server') — callers MUST NOT GC then, because a falsely-empty
    list would make every live session's cfgdir GC-eligible. Only a definitive
    'no server' (empty socket) returns (True, []).
    """
    try:
        out = subprocess.run([cfg["tmux_bin"], "-L", cfg["socket"], "list-sessions", "-F", "#{session_name}"],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
    except Exception:
        return (False, [])
    if out.returncode == 0:
        return (True, [ln.strip() for ln in out.stdout.splitlines() if ln.strip()])
    err = (out.stderr or "").lower()
    if "no server running" in err or "error connecting" in err or "no such file" in err:
        return (True, [])  # definitively no sessions
    return (False, [])     # transient/unknown failure -> untrustworthy


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)  # windows-footgun: ok
        return True
    except Exception:
        return False


def _active_task_ids(names):
    active = set()
    for n in names:
        parsed = parse_session_name(n)
        if parsed:
            active.add(parsed[1])
    return active


def _gc_now(cfg):
    """GC stale per-session state — ONLY when session enumeration is trustworthy
    (else a falsely-empty active set could delete a live session's cfgdir)."""
    ok, names = _list_sessions(cfg)
    if not ok:
        logger.warning("claude-kanban-lane: skipping GC (session enumeration untrustworthy)")
        return
    try:
        n = state.gc_old_state(_active_task_ids(names))
        if n:
            logger.info("claude-kanban-lane: GC removed %s stale state entr(ies)", n)
    except Exception:
        logger.warning("claude-kanban-lane: GC failed", exc_info=True)


def _maybe_gc():
    """Throttled opportunistic GC from the spawn path, so stale per-session state
    is reaped during a long KeepAlive run (readopt only fires at gateway startup)."""
    stamp = state.STATE_DIR / ".last_gc"
    try:
        now = time.time()
        last = stamp.stat().st_mtime if stamp.exists() else 0
        if now - last < _GC_THROTTLE_SECONDS:
            return
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(now), encoding="utf-8")  # touch BEFORE gc so a slow/failing gc doesn't re-trigger every spawn
    except Exception:
        return
    _gc_now(config.lane_config())


def _relaunch_supervisor(rec, cfg):
    # --adopt: take over ownership of the card (a run-fenced compare-and-set in
    # core) BEFORE the loop, so the stale DEAD original pid doesn't read as a
    # pid-mismatch and make the replacement kill the live claude pane it is
    # meant to adopt. --run-id is what keeps that CAS from stamping this pid
    # onto a SUCCESSOR run's card, so it is required here: a replacement we
    # cannot fence is one we must not launch (see ``relaunch_run_id``).
    run_id = int(rec["run_id"])
    if run_id <= 0:
        raise ValueError(f"_relaunch_supervisor needs a positive run id, got {rec['run_id']!r}")
    argv = [VENV_PY, SUPERVISOR, "--task", rec["task"], "--board", rec["board"],
            "--session", rec["session"], "--transcript", rec["transcript"],
            "--cfgdir", rec.get("cfgdir", ""),
            "--tmux-bin", cfg["tmux_bin"], "--socket", cfg["socket"],
            "--heartbeat", str(cfg["heartbeat_seconds"]),
            "--idle-timeout", str(cfg["idle_timeout_seconds"]),
            "--max-runtime", str(cfg["max_runtime_seconds"]),
            "--adopt", "--run-id", str(run_id)]
    if rec.get("claim_lock"):
        argv += ["--claim-lock", str(rec["claim_lock"])]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.pid


def session_run_matches_card(sidecar, card):
    """True when this tmux session still belongs to the card's CURRENT run.

    A session is named per (board, task), not per run, so a pane left behind by
    run N looks identical to run N+1's pane. The sidecar's ``run_id`` is the
    only thing that tells them apart. Unknown (a sidecar written before run_id
    was recorded) reads as a match: the pane is more likely the live one than an
    orphan, and the supervisor's own run fence still stops it from acting on a
    superseded card.
    """
    recorded = sidecar.get("run_id") if sidecar else None
    if recorded is None:
        return True
    current = getattr(card, "current_run_id", None) if card else None
    if current is None:
        return False
    return int(current) == int(recorded)


def relaunch_run_id(sidecar, card):
    """The exact run a replacement supervisor may be fenced to, or None.

    The sidecar's recorded run is authoritative. A legacy sidecar (written
    before run_id was persisted) is allowed to resolve its run ONCE, from the
    card this readopt pass already read — that is a real fence, unlike letting
    the supervisor discover the run later and follow the card into whichever run
    happens to be current by then. ``None`` means there is nothing exact to
    fence to, and the caller must leave the pane alone rather than start an
    unfenced supervisor on it.
    """
    for raw in (sidecar.get("run_id") if sidecar else None,
                getattr(card, "current_run_id", None) if card else None):
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def readopt():
    """On startup: adopt live supervisors; relaunch for dead-supervisor sessions; reap terminals.

    Ends by GC-ing stale per-session state for tasks with NO live session.
    """
    cfg = config.lane_config()
    enum_ok, names = _list_sessions(cfg)
    active = set()  # task_ids with a LIVE tmux session — never GC'd
    for name in names:
        parsed = parse_session_name(name)
        if not parsed:
            continue
        board, task_id = parsed
        active.add(task_id)
        sidecar = state.session_load(task_id) or {}
        try:
            with _connect_mod.connect_closing(board=board) as conn:
                card = kdb.get_task(conn, task_id)
            read_ok = True  # the query completed (card may be None if absent)
        except Exception:
            # Transient DB-read failure (e.g. SQLite 'disk I/O error' from WAL
            # contention). Do NOT treat as 'card gone' — leave the live session.
            card = None
            read_ok = False
        if not read_ok:
            logger.warning("claude-kanban-lane: readopt skipping %s (card read failed)", name)
            continue
        status = getattr(card, "status", None) if card else None
        if status == "running" and not session_run_matches_card(sidecar, card):
            # The card is running, but on a DIFFERENT run than the one this pane
            # was launched for: our run was reclaimed and re-dispatched while the
            # gateway was down. Relaunching an adopting supervisor here is the
            # run-N-adopts-run-N+1 bug — it would fight the successor's own
            # supervisor over the card. Reap the orphan pane instead.
            subprocess.run([cfg["tmux_bin"], "-L", cfg["socket"], "kill-session", "-t", name],
                           capture_output=True, stdin=subprocess.DEVNULL, check=False)
            state.session_delete(task_id)
            active.discard(task_id)
            logger.warning(
                "claude-kanban-lane: readopt reaped %s — pane belongs to run %s "
                "but the card is on run %s", name, sidecar.get("run_id"),
                getattr(card, "current_run_id", None))
        elif status == "running":
            sup_pid = sidecar.get("supervisor_pid")
            if sup_pid and _pid_alive(sup_pid):
                continue  # adopt live supervisor; do nothing
            run_id = relaunch_run_id(sidecar, card)
            if run_id is None:
                # Nothing exact to fence a replacement to. Leave the pane and
                # the card untouched and retry on a later readopt: an unfenced
                # supervisor could renew a successor run's claim, and killing a
                # pane we cannot identify could take out a live session.
                logger.warning(
                    "claude-kanban-lane: readopt leaving %s alone — no exact run "
                    "to fence a replacement supervisor to (sidecar run %r, card "
                    "run %r)", name, sidecar.get("run_id"),
                    getattr(card, "current_run_id", None))
                continue
            if not launcher.supervisor_contract_available():
                # Version skew: the replacement would stand down immediately
                # (it cannot fence anything). Leave the pane for a later retry
                # rather than churning a supervisor per readopt.
                logger.warning(
                    "claude-kanban-lane: readopt leaving %s alone — core "
                    "ownership contract unavailable in this runtime", name)
                continue
            rec = {"task": task_id, "board": board, "session": name,
                   "transcript": sidecar.get("transcript", ""), "cfgdir": sidecar.get("cfgdir", ""),
                   "run_id": run_id, "claim_lock": sidecar.get("claim_lock")}
            new_pid = _relaunch_supervisor(rec, cfg)
            # Persist the NEW supervisor pid (and the run we fenced it to, which
            # a legacy sidecar did not record) so a subsequent readopt sees it
            # alive and does not relaunch a duplicate supervisor.
            state.session_save(task_id, {**sidecar, "task": task_id, "board": board,
                                         "session": name,
                                         "transcript": sidecar.get("transcript", ""),
                                         "run_id": run_id,
                                         "supervisor_pid": new_pid})
            logger.info("claude-kanban-lane: readopt relaunched supervisor for %s (pid %s)", name, new_pid)
        elif card is None or status in _TERMINAL_STATUSES:
            subprocess.run([cfg["tmux_bin"], "-L", cfg["socket"], "kill-session", "-t", name],
                           capture_output=True, stdin=subprocess.DEVNULL, check=False)
            state.session_delete(task_id)
            logger.info("claude-kanban-lane: readopt reaped orphan session %s", name)

    # GC stale per-session state for tasks with NO live session (backstop for the
    # supervisor's own on-exit cfgdir cleanup, e.g. after a crash). Never touches
    # an active task; age-gated so a just-launched cfgdir is safe. SKIP entirely if
    # session enumeration was untrustworthy (active may be falsely empty).
    if not enum_ok:
        logger.warning("claude-kanban-lane: readopt skipping GC (session enumeration untrustworthy)")
        return
    try:
        n = state.gc_old_state(active)
        if n:
            logger.info("claude-kanban-lane: readopt GC removed %s stale state entr(ies)", n)
    except Exception:
        logger.warning("claude-kanban-lane: readopt GC failed", exc_info=True)
