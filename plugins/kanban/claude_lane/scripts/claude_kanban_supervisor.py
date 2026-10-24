#!/usr/bin/env /Users/ctao/.hermes/hermes-agent/venv/bin/python
"""Supervisor for one claude-CLI lane session. Direct gateway child = recorded worker_pid.

Contract (spec §7): heartbeat << 3600s; transition the card BEFORE exiting 0
(else detect_crashed_workers blocks it as a protocol violation); exit 75 on
rate-limit/weekly-cap to requeue without a failure; SIGTERM -> kill the pane.

Ownership (``hermes_cli.kanban_supervisor``): every lifecycle call this process
makes is fenced to ONE exact run, pinned once at startup. There is no unfenced
mode — if the run cannot be resolved, or ownership cannot be verified, the
supervisor stands down instead of supervising. See ``_stand_down`` for what
that does to the pane, which differs between a fresh launch (ours to clean up)
and a readopt (possibly somebody else's to leave alone).

Installed from ``plugins/kanban/claude_lane`` in the hermes-agent repo — see
that directory's README for install/rollback ordering. Do not edit in place on
the host; edit the versioned copy and reinstall, or the next install silently
reverts you.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

# Put the checkout root on sys.path OURSELVES before touching the core.
# The venv's editable finder maps only the top-level modules that existed when
# it was generated; after an upstream update added ``hermes_state_common`` (and
# siblings) that ``hermes_state`` imports, ``kanban_db.connect()``'s lazy
# ``from hermes_state import ...`` raised ModuleNotFoundError inside every
# gateway-spawned supervisor (no PYTHONPATH, sys.path[0] = ~/.hermes/scripts).
# ``_read_card`` swallowed it, every tick returned "continue", and ten lanes ran
# for days on interactive dialogs while heartbeating (2026-09-04). The gateway
# itself is immune because hermes_cli/main.py does exactly this insert.
def _bootstrap_checkout_root():
    candidates = [os.path.dirname(sys.prefix),  # <root>/venv -> <root>
                  os.path.expanduser("~/.hermes/hermes-agent")]
    for root in candidates:
        if (os.path.isfile(os.path.join(root, "hermes_state.py"))
                and os.path.isdir(os.path.join(root, "hermes_cli"))):
            if root not in sys.path:
                # Append, never prepend: the editable finder already maps the
                # top-level modules it knows, this only has to make the NEW
                # siblings importable. Prepending would also shadow an explicit
                # PYTHONPATH/cwd, which the rollout tests rely on to stub the core.
                sys.path.append(root)
            return root
    return None


_CHECKOUT_ROOT = _bootstrap_checkout_root()

# The core ownership contract. Imported defensively so this file stays
# importable — and ``--help``-able, and collectable by pytest — against a core
# checkout that predates ``hermes_cli.kanban_supervisor``. That combination is
# reachable in both rollout directions (artifacts installed before the core
# lands, or the core rolled back under installed artifacts), and the honest
# behaviour for it is a clean stand-down into the plugin's native fallback, not
# a ModuleNotFoundError traceback per lane spawn.
try:
    from hermes_cli.kanban_db import (
        KANBAN_RATE_LIMIT_EXIT_CODE,
        worker_log_path,
        get_task,
    )
    # kanban_db split upstream (Sep 2026; shim removed 2026-09-14): connect helpers
    # moved to kanban_db_connect.
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_supervisor import (
        OWNERSHIP_HELD,
        OWNERSHIP_REFUSED,
        SupervisorRunUnresolved,
        acquire_session_ownership,
        pin_supervised_run,
        supervisor_heartbeat_argv,
        supervisor_shim_env,
    )
except Exception as _exc:  # pragma: no cover - exercised via subprocess in tests
    _CONTRACT_IMPORT_ERROR = _exc
    KANBAN_RATE_LIMIT_EXIT_CODE = 75  # documented core constant; only used post-guard
else:
    _CONTRACT_IMPORT_ERROR = None


def _fallback_redact(t):
    """Conservative fail-closed masker used when agent.redact is unimportable.

    Returning text verbatim on an import failure would leak secrets to the
    worker log; instead mask the common high-entropy credential shapes.
    """
    if not t:
        return t
    t = re.sub(r'sk-[A-Za-z0-9_\-]{8,}', '***', t)
    t = re.sub(r'eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-.]+', '***', t)
    t = re.sub(r'gAAAA[A-Za-z0-9_\-=]{8,}', '***', t)
    return t


try:
    from agent.redact import redact_sensitive_text
except Exception:  # pragma: no cover
    def redact_sensitive_text(t, **_):
        return _fallback_redact(t)

# Narrow to specific cap-banner / provider-error shapes so a card whose own work
# mentions "rate limit"/"429"/"overloaded" (e.g. implementing a rate limiter) does
# NOT false-trigger a requeue. These strings are unlikely in normal task prose.
_CAP_PHRASES = ("usage limit reached", "reached your usage limit",
                "out of extra usage", "weekly limit reached")
_RL_PHRASES = ("rate_limit_error", "overloaded_error", "api error: 429", "error: 529")
# TUI-chrome banner shapes (visible in the PANE, never written to the
# transcript). Checked only against a capture-pane snapshot taken when an
# idle/timeout block is about to fire — narrow enough that ordinary task
# conversation in the pane won't false-trigger a requeue.
_PANE_CAP_PHRASES = _CAP_PHRASES + ("hour limit reached", "limit will reset",
                                    "approaching usage limit")
_TERMINAL_STATUSES = ("done", "blocked", "archived", "gave_up")

# Exit code for a cold-start flake (claude exited before writing anything).
# Deliberately NOT 0 — the dispatcher reads a clean exit on a still-``running``
# task as a protocol violation and blocks it — and NOT KANBAN_RATE_LIMIT_EXIT_CODE
# (75), whose quota-deferral semantics would delay the respawn. Any other nonzero
# lands in ``detect_crashed_workers``' ``crashed`` bucket, which releases the task
# ``running`` -> ``ready`` and requeues it, bounded by the failure breaker.
COLDSTART_REQUEUE_EXIT_CODE = 1

# Stand-down codes. All nonzero (a clean exit on a running card reads as a
# protocol violation) and all distinct, because the log line is the only place
# these are ever seen: nothing waits on a readopt relaunch's exit status.
#
#   3  a readable card says we are not the owner (live foreign pid) or our run
#      has been superseded
#   4  ownership could not be VERIFIED (the DB never became readable). Unknown
#      is not ours — the pane is left for a later retry
#   5  no exact run id could be pinned, so nothing may be fenced
#   6  the core ownership contract is missing from this runtime (an artifact /
#      core version skew); the lane degrades to the plugin's native fallback
READOPT_REFUSED_EXIT_CODE = 3
OWNERSHIP_UNVERIFIED_EXIT_CODE = 4
RUN_UNRESOLVED_EXIT_CODE = 5
CONTRACT_UNAVAILABLE_EXIT_CODE = 6


def decide(*, status, worker_pid_matches, transcript_tail, idle_seconds, elapsed, idle_timeout, max_runtime):
    """Pure decision. Returns one of:
       ('terminal',None) ('weekly_cap',None) ('rate_limit',None) ('block',reason) ('continue',None)."""
    if (status != "running") or (not worker_pid_matches):
        return ("terminal", None)
    low = (transcript_tail or "").lower()
    if any(p in low for p in _CAP_PHRASES):
        return ("weekly_cap", None)
    if any(p in low for p in _RL_PHRASES):
        return ("rate_limit", None)
    if elapsed >= max_runtime:
        return ("block", f"claude-lane timeout after {int(elapsed)}s")
    if idle_seconds >= idle_timeout:
        return ("block", f"claude-lane idle for {int(idle_seconds)}s with no progress")
    return ("continue", None)


def caller_decision(read_ok, card, my_pid, transcript_tail, idle_seconds, elapsed,
                    idle_timeout, max_runtime):
    """Pure wrapper over decide() that folds in the read result.

    A transient DB-read failure (read_ok=False) must NOT be treated as the card
    being gone — acting on it would kill the live claude pane. We continue this
    tick instead. Only a genuinely-absent card (read_ok=True, card=None) is
    terminal.

    There is deliberately no "assume we own it" override. One used to exist for
    a readopt whose ownership write could not be verified, and it was permanent:
    it disabled the foreign-pid check for the process lifetime, so a supervisor
    that never confirmed ownership kept running beside the one that had it (a
    readable card naming a live foreign pid still returned 'continue'). Ownership
    is now established before the loop starts (see ``main``), and unverifiable
    ownership stands the supervisor down rather than granting it.

    ``worker_pid is None`` is tolerated, not treated as ownership: it means
    nobody has recorded an owner, which is not evidence against us (a transient
    shim failure can delay our own declaration) — whereas a DIFFERENT pid is
    evidence, and that is what this rejects.
    """
    if not read_ok:
        # Transient read failure: do NOT act on the card's identity this tick.
        # But the time budgets do not depend on the card, and a read that fails
        # for the WHOLE budget (2026-09-04: a ModuleNotFoundError swallowed on
        # every tick for weeks) must still end the lane — otherwise it runs
        # forever while its heartbeat keeps the dispatcher's reclaim at bay.
        if elapsed >= max_runtime:
            return ("block", f"claude-lane timeout after {int(elapsed)}s (card unreadable)")
        if idle_seconds >= idle_timeout:
            return ("block", f"claude-lane idle for {int(idle_seconds)}s with no progress (card unreadable)")
        return ("continue", None)
    status = getattr(card, "status", "running") if card else None
    if card is None:
        return ("terminal", None)  # genuinely absent
    pid_matches = getattr(card, "worker_pid", None) in (my_pid, None)
    return decide(status=status, worker_pid_matches=pid_matches, transcript_tail=transcript_tail,
                  idle_seconds=idle_seconds, elapsed=elapsed, idle_timeout=idle_timeout,
                  max_runtime=max_runtime)


def is_coldstart_flake(*, made_progress, elapsed, coldstart_grace):
    """The tmux pane has died. True iff this looks like a transient cold-start
    failure (claude exited before writing ANYTHING to the transcript, well
    inside the cold-start window) that should be REQUEUED rather than blocked.

    A pane that produced output (``made_progress``), or survived past the
    cold-start ``coldstart_grace`` window before dying, is a genuine failure
    worth surfacing — the caller blocks it (the prior behavior). The window is
    strict (``elapsed < coldstart_grace``) so the boundary blocks.
    """
    return (not made_progress) and (elapsed < coldstart_grace)


# -- side-effecting helpers ---------------------------------------------------
def _tmux_kill(tmux_bin, socket, session):
    subprocess.run([tmux_bin, "-L", socket, "kill-session", "-t", session],
                   capture_output=True, check=False, stdin=subprocess.DEVNULL)


def _tmux_capture(tmux_bin, socket, session, lines=120):
    """Best-effort snapshot of the visible pane text.

    TUI-chrome banners (usage caps, unanswered dialogs) render in the pane but
    are NEVER written to the transcript — before this existed, five lanes
    idle-blocked (2026-07-06) with zero post-mortem trail. Returns '' on any
    failure (dead pane, no server).
    """
    try:
        p = subprocess.run([tmux_bin, "-L", socket, "capture-pane", "-p",
                            "-t", session, "-S", f"-{lines}"],
                           capture_output=True, text=True, timeout=10, check=False,
                           stdin=subprocess.DEVNULL)
        return p.stdout if p.returncode == 0 else ""
    except Exception:
        return ""


def _append_worker_log(log_path, text):
    """Best-effort redacted append to the worker log (diagnostics only)."""
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "ab") as f:
            f.write(redact_sensitive_text(text, force=True).encode("utf-8", "replace"))
    except Exception:
        pass


def _tmux_alive(tmux_bin, socket, session):
    return subprocess.run([tmux_bin, "-L", socket, "has-session", "-t", session],
                          capture_output=True, check=False, stdin=subprocess.DEVNULL).returncode == 0


def _cleanup_cfgdir(cfgdir):
    """Remove the per-session CLAUDE_CONFIG_DIR on terminal exit (the redacted
    transcript is already persisted to the worker log). Best-effort."""
    if not cfgdir:
        return
    try:
        import shutil
        shutil.rmtree(cfgdir, ignore_errors=True)
    except Exception:
        pass


def _stand_down(a, reason, code):
    """Exit without ever supervising. Returns the exit code to use.

    The pane policy is the whole point of this helper:

    * On a **readopt** (``--adopt``) the pane predates us and may belong to the
      supervisor that legitimately owns the card. We have just established that
      we do not own it — or cannot show that we do — so we touch nothing.
      Reaping a genuinely orphaned pane is the wrapper's job; it holds the
      sidecar and can tell an orphan from a live successor.
    * On a **fresh launch** the pane was created for us moments ago and nobody
      else is watching it. Leaving it would mean a live claude session with no
      supervisor while the card is requeued — a double-run and double billing.
      So we kill it and drop its cfgdir before exiting nonzero.
    """
    print(f"claude-lane supervisor standing down: {reason}", file=sys.stderr)
    if not a.adopt:
        _tmux_kill(a.tmux_bin, a.socket, a.session)
        _cleanup_cfgdir(a.cfgdir)
    return code


def _hermes_task_shim(task, board, *args, run_id, claim_lock=None):
    """Invoke the lifecycle shim with an EXPLICIT, run-fenced env.

    The supervisor runs under the gateway env (no HERMES_KANBAN_TASK), so the
    shim's first guard would reject every heartbeat/block. We pin the whole
    HERMES_KANBAN_* contract via the source-controlled builder so supervisor-
    issued calls are authorized, reach the right board's DB, and — crucially —
    are FENCED to the exact run this supervisor was launched for. ``run_id`` is
    a required keyword and the builder rejects a non-positive one: without
    HERMES_KANBAN_RUN_ID the core heartbeat runs with expected_run_id=None, so a
    supervisor left over from run N renews the claim of run N+1.
    """
    shim = os.path.expanduser("~/.hermes/scripts/hermes-kanban-task")
    env = supervisor_shim_env(
        os.environ, task_id=task, board=board,
        run_id=run_id, claim_lock=claim_lock,
    )
    subprocess.run([shim, *args], capture_output=True, env=env, check=False, stdin=subprocess.DEVNULL)


# Last exception swallowed by _read_card (text), or None after a good read. The
# main loop logs it (rate-limited) so a persistently blind watchdog leaves a
# trail in the worker log instead of failing silently for weeks.
LAST_READ_ERROR = None


def _read_card(board, task_id):
    """Read the card. Returns (read_ok, card).

    read_ok is False ONLY on an exception (e.g. SQLite 'disk I/O error' from
    transient WAL contention) — the caller must NOT treat that as 'card gone'
    and kill the live session. read_ok is True when the read completed, in
    which case card may be None only if the card is genuinely absent. The
    exception text is kept in LAST_READ_ERROR for the caller to log.
    """
    global LAST_READ_ERROR
    try:
        with connect_closing(board=board) as conn:
            card = get_task(conn, task_id)
        LAST_READ_ERROR = None
        return True, card
    except Exception as exc:
        LAST_READ_ERROR = f"{exc.__class__.__name__}: {exc}"
        return False, None


def _stream_transcript(transcript, log_path, offset):
    """Append newly-written, redacted transcript lines to the worker log.

    Returns (new_offset, tail, grew). ``grew`` is True when the transcript file
    advanced past the previous offset — even if no parseable assistant/user
    line was emitted yet — so the caller can count raw progress during a long
    single tool call and avoid a false idle-block.
    """
    tail = ""
    if not os.path.exists(transcript):
        return offset, tail, False
    size = os.path.getsize(transcript)
    if size <= offset:
        return offset, tail, False
    with open(transcript, "r", encoding="utf-8") as f:
        f.seek(offset)
        chunk = f.read()
        offset = f.tell()
    lines_out = []
    for line in chunk.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") in ("assistant", "user") and isinstance(d.get("message"), dict):
            content = d["message"].get("content")
            blocks = [content] if isinstance(content, str) else (content or [])
            for b in blocks:
                if isinstance(b, str):
                    lines_out.append(b)
                elif b.get("type") == "text":
                    lines_out.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    lines_out.append(f"[tool {b.get('name')}]")
                elif b.get("type") == "tool_result":
                    lines_out.append(str(b.get("content", "")))
    if lines_out:
        text = redact_sensitive_text("\n".join(lines_out) + "\n", force=True)
        tail = text[-4000:]
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "ab") as f:
                f.write(text.encode("utf-8", "replace"))
        except Exception:
            pass
    return offset, tail, True


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--board", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--tmux-bin", default="/opt/homebrew/bin/tmux")
    ap.add_argument("--socket", default="hermes-kanban")
    ap.add_argument("--heartbeat", type=int, default=120)
    ap.add_argument("--idle-timeout", type=int, default=1200)
    ap.add_argument("--max-runtime", type=int, default=5400)
    ap.add_argument("--adopt", action="store_true",
                    help="Take over an already-live session (readopt relaunch). "
                         "Ownership is a run-fenced compare-and-set: it can "
                         "replace the dead original supervisor's pid, never a "
                         "live one, and never across runs. Unless it succeeds "
                         "this process exits without touching the pane.")
    ap.add_argument("--run-id", type=int, default=None,
                    help="The task_runs row this supervisor was launched for. Every "
                         "lifecycle call it makes is fenced to this run, so a "
                         "supervisor left over from run N cannot heartbeat, adopt, or "
                         "renew the claim of run N+1. Resolved from the card ONCE at "
                         "startup when omitted, and pinned for the process lifetime; "
                         "if it cannot be resolved exactly, the supervisor exits "
                         "rather than running unfenced.")
    ap.add_argument("--claim-lock", default=None,
                    help="The claim_lock held when this lane was claimed. Passed "
                         "through to lifecycle calls as HERMES_KANBAN_CLAIM_LOCK.")
    ap.add_argument("--cfgdir", default="",
                    help="The per-session CLAUDE_CONFIG_DIR; removed on terminal exit.")
    ap.add_argument("--coldstart-grace", type=int, default=60,
                    help="If the tmux pane dies within this many seconds having "
                         "produced NO transcript output, treat it as a transient "
                         "cold-start failure: exit nonzero so the dispatcher "
                         "requeues (running->ready) instead of terminally blocking.")
    a = ap.parse_args(argv)

    # Version skew between this artifact and the installed core. Nothing below
    # can be fenced without the contract, and supervising unfenced is the
    # failure mode this whole file exists to remove, so stand down.
    if _CONTRACT_IMPORT_ERROR is not None:
        return _stand_down(
            a,
            f"hermes_cli.kanban_supervisor unavailable in this runtime "
            f"({_CONTRACT_IMPORT_ERROR.__class__.__name__}: {_CONTRACT_IMPORT_ERROR}); "
            f"install order is core first, then these artifacts",
            CONTRACT_UNAVAILABLE_EXIT_CODE,
        )

    my_pid = os.getpid()
    board = a.board

    # Pin the run ONCE, here, before anything else touches the card. Every
    # lifecycle call below is fenced to it, so a supervisor whose run has been
    # superseded goes quiet instead of acting on its successor's card. Never
    # re-resolve this later: following the card into a new run is exactly the
    # failure the fence exists to prevent. And never fall back to "unfenced but
    # honest" — that fallback is what let a stale run-N supervisor renew run
    # N+1's claim, add heartbeat events to it and stamp its own pid on it.
    try:
        run_id = pin_supervised_run(a.task, board=board, run_id=a.run_id)
    except SupervisorRunUnresolved as exc:
        return _stand_down(
            a, f"cannot fence to an exact run ({exc}"
               f"{'; transient' if exc.transient else ''})",
            RUN_UNRESOLVED_EXIT_CODE,
        )

    if a.adopt:
        # Run-fenced compare-and-set, retried for transient WAL/disk-I/O
        # contention (common at gateway startup). Ownership must be HELD before
        # this process supervises anything: a refusal means somebody else owns
        # the card, and an unverifiable answer means we do not know that we do —
        # neither is a licence to run beside the supervisor that might.
        verdict, outcome = acquire_session_ownership(
            a.task, board=a.board, worker_pid=my_pid, expected_run_id=run_id,
        )
        if verdict != OWNERSHIP_HELD:
            return _stand_down(
                a,
                f"readopt {verdict} (outcome {outcome!r}) for {a.task} run={run_id}",
                READOPT_REFUSED_EXIT_CODE if verdict == OWNERSHIP_REFUSED
                else OWNERSHIP_UNVERIFIED_EXIT_CODE,
            )

    def _sigterm(_signo, _frame):
        _tmux_kill(a.tmux_bin, a.socket, a.session)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm)

    log_path = str(worker_log_path(a.task, board=board))
    start = time.time()
    offset = 0
    last_progress = start
    ever_progressed = False  # has the transcript EVER grown? distinguishes a
    # cold-start flake (claude wrote nothing) from a genuine mid-run pane death.
    last_hb = 0.0
    last_read_err_log = 0.0

    # import state lazily (lives in the plugin dir; supervisor finds it via the path we add)
    sys.path.insert(0, os.path.expanduser("~/.hermes/plugins/claude-kanban-lane"))
    import state

    while True:
        now = time.time()
        if now - last_hb >= a.heartbeat:
            # --worker-pid = OUR pid: this supervisor is the long-lived process
            # that owns the lane, and the dispatcher can't know that (an
            # out-of-process launcher's spawn_fn has no useful child pid to
            # return). Passing the heartbeat invocation's own pid instead
            # would record an owner that is dead before anything reads it.
            _hermes_task_shim(
                a.task, a.board,
                *supervisor_heartbeat_argv(
                    task_id=a.task, worker_pid=my_pid, note="supervisor alive",
                ),
                run_id=run_id, claim_lock=a.claim_lock,
            )
            last_hb = now

        offset, tail, grew = _stream_transcript(a.transcript, log_path, offset)
        # Treat raw file growth as progress too: a long single tool call can run
        # for many minutes without emitting a parsed assistant/user line, and we
        # must not false-idle-block it.
        if tail or grew:
            last_progress = now
            ever_progressed = True

        read_ok, card = _read_card(board, a.task)
        if not read_ok and now - last_read_err_log >= 600:
            _append_worker_log(log_path,
                               f"\n--- supervisor: card read failed ({LAST_READ_ERROR}); "
                               f"watchdog is running on time budgets only ---\n")
            last_read_err_log = now

        action, reason = caller_decision(
            read_ok, card, my_pid, transcript_tail=tail,
            idle_seconds=now - last_progress, elapsed=now - start,
            idle_timeout=a.idle_timeout, max_runtime=a.max_runtime)

        if action == "terminal":
            _tmux_kill(a.tmux_bin, a.socket, a.session)
            state.cap_reset(a.task)
            state.session_delete(a.task)
            _stream_transcript(a.transcript, log_path, offset)  # flush closing lines before rmtree
            _cleanup_cfgdir(a.cfgdir)
            return 0
        if action in ("rate_limit", "weekly_cap"):
            if action == "weekly_cap":
                state.cap_increment(a.task)
            _tmux_kill(a.tmux_bin, a.socket, a.session)
            state.session_delete(a.task)
            _cleanup_cfgdir(a.cfgdir)  # a fresh cfgdir is built on the next dispatch
            return KANBAN_RATE_LIMIT_EXIT_CODE  # 75 -> requeue, no failure
        if action == "block":
            # An idle/timeout block may really be a stall on TUI chrome the
            # transcript never records (cap banner, unanswered dialog).
            # Snapshot the pane BEFORE killing it: persist the evidence to the
            # worker log either way, and reclassify a visible cap/rate-limit
            # banner into the requeue path instead of a terminal block.
            pane = _tmux_capture(a.tmux_bin, a.socket, a.session)
            if pane.strip():
                try:
                    os.makedirs(os.path.dirname(log_path), exist_ok=True)
                    with open(log_path, "ab") as f:
                        f.write(redact_sensitive_text(
                            f"\n--- pane snapshot ({reason}) ---\n{pane}\n--- end pane snapshot ---\n",
                            force=True).encode("utf-8", "replace"))
                except Exception:
                    pass
                plow = pane.lower()
                if any(p in plow for p in _PANE_CAP_PHRASES):
                    state.cap_increment(a.task)
                    _tmux_kill(a.tmux_bin, a.socket, a.session)
                    state.session_delete(a.task)
                    _cleanup_cfgdir(a.cfgdir)
                    return KANBAN_RATE_LIMIT_EXIT_CODE  # 75 -> requeue, no failure
                if any(p in plow for p in _RL_PHRASES):
                    _tmux_kill(a.tmux_bin, a.socket, a.session)
                    state.session_delete(a.task)
                    _cleanup_cfgdir(a.cfgdir)
                    return KANBAN_RATE_LIMIT_EXIT_CODE
            _hermes_task_shim(a.task, a.board, "block", a.task, reason,
                              run_id=run_id, claim_lock=a.claim_lock)
            _tmux_kill(a.tmux_bin, a.socket, a.session)
            state.session_delete(a.task)
            _stream_transcript(a.transcript, log_path, offset)  # flush closing lines before rmtree
            _cleanup_cfgdir(a.cfgdir)
            return 0

        # safety: if the pane died but the card is somehow still ours-running, block (never exit 0 running)
        if not _tmux_alive(a.tmux_bin, a.socket, a.session):
            if is_coldstart_flake(made_progress=ever_progressed, elapsed=now - start,
                                  coldstart_grace=a.coldstart_grace):
                # Transient cold-start: the pane died before claude wrote anything,
                # well inside the cold-start window. Do NOT block — that's terminal
                # and needs a human/cron to clear. Exit nonzero so the dispatcher's
                # crash path requeues (running->ready), bounded by the failure
                # breaker; a genuinely-broken lane still surfaces as gave_up once
                # the breaker trips.
                _tmux_kill(a.tmux_bin, a.socket, a.session)
                state.session_delete(a.task)
                _stream_transcript(a.transcript, log_path, offset)  # flush any closing lines before rmtree
                _cleanup_cfgdir(a.cfgdir)
                return COLDSTART_REQUEUE_EXIT_CODE
            _hermes_task_shim(a.task, a.board, "block", a.task,
                              "claude session exited without completing",
                              run_id=run_id, claim_lock=a.claim_lock)
            state.session_delete(a.task)
            _stream_transcript(a.transcript, log_path, offset)  # flush closing lines before rmtree
            _cleanup_cfgdir(a.cfgdir)
            return 0

        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
