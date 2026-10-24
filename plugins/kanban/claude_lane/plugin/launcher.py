"""Launch a claude-CLI lane: tmux session + supervisor. Returns the supervisor pid.

Installed from ``plugins/kanban/claude_lane`` in the hermes-agent repo — see
that directory's README for install/rollback ordering. Edit the versioned copy.
"""
import logging
import os
import pwd
import shutil
import subprocess
import uuid

try:
    from . import brief, cfgdir, config, envbuild, modelmap, router, state
except ImportError:  # pragma: no cover
    import brief, cfgdir, config, envbuild, modelmap, router, state

logger = logging.getLogger(__name__)
SUPERVISOR = os.path.expanduser("~/.hermes/scripts/claude_kanban_supervisor.py")
VENV_PY = "/Users/ctao/.hermes/hermes-agent/venv/bin/python"


def supervisor_contract_available():
    """True when this runtime's core carries the supervisor ownership contract.

    The supervisor cannot fence a single lifecycle call without
    ``hermes_cli.kanban_supervisor``, and it correctly refuses to run unfenced
    — so on a core that predates the module (an artifact/core version skew in
    either rollout direction) a lane launch would create a tmux pane only for
    the supervisor to stand down and kill it again, once per dispatch. Checking
    here keeps the whole lane off: no pane, no requeue churn, straight to the
    native worker.
    """
    try:
        import hermes_cli.kanban_supervisor
    except Exception as exc:
        logger.warning(
            "claude-kanban-lane: core ownership contract unavailable (%s: %s); "
            "staying native until it is deployed", exc.__class__.__name__, exc)
        return False
    return True


def _positive_run_id(task):
    """The card's current run as an exact positive int, or None.

    The run is the supervisor's only fence, and it has to come from the card we
    just claimed. Anything else — missing, zero, unparseable — means we cannot
    fence this lane, and an unfenced lane supervisor is what let a leftover
    supervisor renew its successor's claim.
    """
    raw = getattr(task, "current_run_id", None)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def stable_claude_bin(cfg=None):
    """Resolve a STABLE, executable claude path.

    Honors cfg['claude_bin'] when set (the gateway's launchd PATH differs from an
    interactive shell, so an explicit pin is the reliable option); otherwise falls
    back to PATH resolution. Refuses an ephemeral fnm_multishells path.
    """
    candidate = None
    if cfg:
        candidate = (cfg.get("claude_bin") or "").strip() or None
    candidate = os.path.expanduser(candidate) if candidate else shutil.which("claude")
    if not candidate:
        logger.warning("claude-kanban-lane: no claude binary found; staying native")
        return None
    real = os.path.realpath(candidate)
    if (not real) or ("fnm_multishells" in real) or (not os.access(real, os.X_OK)):
        logger.warning("claude-kanban-lane: claude path unstable/unusable: %s", real)
        return None
    return real


def _oauth_token(token_path):
    with open(os.path.expanduser(token_path), encoding="utf-8") as f:
        return f.read().strip()


def _real_home():
    return pwd.getpwuid(os.getuid()).pw_dir  # windows-footgun: ok


def _hermes_bin():
    return os.environ.get("HERMES_BIN") or shutil.which("hermes") or "hermes"


def _pane_env_argv(env):
    """Explicit per-pane environment, immune to tmux server-env inheritance.

    `subprocess.run(..., env=env)` seeds the tmux SERVER env only when this
    launch is the one that starts the server; when the shared socket's server
    is already alive (any overlapping lane session), later panes inherit the
    FIRST session's env — wrong CLAUDE_CONFIG_DIR (claude writes its transcript
    under the other task's cfgdir → guaranteed idle-block) and wrong
    HERMES_KANBAN_* contract (2026-07-06 root cause 1). Prepending
    `/usr/bin/env K=V ...` to the pane argv sets every session-critical var
    explicitly regardless of server state. tmux ≥3.4 execs a multi-argument
    command directly (no shell join), so values with spaces are safe. No `-i`:
    tmux's own pane vars (TERM, TMUX) must survive for the TUI.
    """
    return ["/usr/bin/env"] + [f"{k}={v}" for k, v in sorted(env.items())]


def _tmux_new_session(*, tmux_bin, socket, session, workspace, env, argv):
    subprocess.run([tmux_bin, "-L", socket, "new-session", "-d", "-s", session,
                    "-c", workspace, "-x", "220", "-y", "50",
                    *_pane_env_argv(env), *argv],
                   env=env, check=True, stdin=subprocess.DEVNULL)


def _tmux_kill(*, tmux_bin, socket, session):
    """Best-effort kill of a just-created tmux session (no raise)."""
    subprocess.run([tmux_bin, "-L", socket, "kill-session", "-t", session],
                   capture_output=True, stdin=subprocess.DEVNULL, check=False)


def _spawn_supervisor(*, task, board, session, transcript, cfgdir, cfg,
                      run_id, claim_lock=None):
    # DIRECT gateway child (no start_new_session): its exit code must be waitpid-classifiable.
    #
    # --run-id / --claim-lock carry the EXACT run and lock this lane was claimed
    # under. The supervisor fences every lifecycle call to them, which is what
    # keeps a supervisor left over from run N from heartbeating, adopting or
    # renewing the claim of run N+1. Both come from the just-claimed card, not
    # from the gateway's environment.
    #
    # ``run_id`` is required and validated here rather than defaulted: omitting
    # it used to be the "honest fallback" (the supervisor would resolve the
    # card's current run itself), but that path is exactly how a supervisor
    # ended up unfenced when the DB was briefly unreadable. A caller that
    # cannot name the run must not launch a supervisor at all.
    run = int(run_id)
    if run <= 0:
        raise ValueError(f"_spawn_supervisor needs a positive run id, got {run_id!r}")
    argv = [VENV_PY, SUPERVISOR, "--task", task.id, "--board", str(board or "default"),
            "--session", session, "--transcript", transcript, "--cfgdir", cfgdir,
            "--tmux-bin", cfg["tmux_bin"], "--socket", cfg["socket"],
            "--heartbeat", str(cfg["heartbeat_seconds"]),
            "--idle-timeout", str(cfg["idle_timeout_seconds"]),
            "--max-runtime", str(cfg["max_runtime_seconds"]),
            "--coldstart-grace", str(cfg["coldstart_grace_seconds"]),
            "--run-id", str(run)]
    if claim_lock:
        argv += ["--claim-lock", str(claim_lock)]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.pid


def launch_lane(task, workspace, board):
    """Build env + cfgdir + brief, start the tmux claude session + supervisor. Return supervisor pid."""
    cfg = config.lane_config()
    claude = stable_claude_bin(cfg)
    if not claude:
        return None  # wrapper falls through to native

    # Everything a fenced supervisor needs is checked BEFORE a pane exists.
    # Both of these are "this lane cannot be supervised safely", and the safe
    # answer is the native worker — but only if we bail out here: discovering
    # it after `tmux new-session` would leave a live claude pane behind for the
    # native fallback to double-run.
    run_id = _positive_run_id(task)
    if run_id is None:
        logger.warning(
            "claude-kanban-lane: task %s has no current_run_id; staying native "
            "(a supervisor with no run to fence to must not be launched)",
            getattr(task, "id", "?"))
        return None
    if not supervisor_contract_available():
        return None

    if getattr(task, "max_runtime_seconds", None) is not None:
        logger.warning(
            "claude-kanban-lane: task %s has max_runtime_seconds=%s; core "
            "enforce_max_runtime may SIGTERM the supervisor early (its SIGTERM "
            "handler still kills the pane cleanly)",
            getattr(task, "id", "?"), task.max_runtime_seconds)
    session_id = str(uuid.uuid4())
    session = f"hermes-kanban-{board or 'default'}-{task.id}"
    real_home = _real_home()
    cdir = cfgdir.prepare_session_config_dir(
        task_id=task.id, workspace=workspace, deny_read_globs=cfg["deny_read_globs"],
        real_home=real_home, base_state_dir=os.path.join(str(config.STATE_DIR), "cfgdirs"))
    transcript = cfgdir.transcript_path(cdir, workspace, session_id)
    # Empty MCP config + --strict-mcp-config => use ONLY these (zero) servers, so the
    # unattended session never discovers a project .mcp.json (e.g. ~/.hermes/.mcp.json,
    # found by walking up from a workspace under ~/.hermes) and blocks on its approval
    # prompt. --strict-mcp-config alone is a no-op without a --mcp-config argument.
    mcp_cfg = os.path.join(cdir, "empty-mcp.json")
    with open(mcp_cfg, "w", encoding="utf-8") as f:
        f.write('{"mcpServers": {}}')

    kanban_env = envbuild.mirror_kanban_env(task, workspace, board)
    env = envbuild.build_session_env(
        kanban_env=kanban_env, oauth_token=_oauth_token(cfg["oauth_token_path"]),
        real_home=real_home, cfgdir=cdir, hermes_bin=_hermes_bin())

    model_id, _provider = router.profile_model_provider(task.assignee)
    alias = cfg.get("model") or modelmap.cli_model_alias(model_id)
    persona = brief.persona_for(envbuild._profile_arg(task.assignee))
    body = brief.compile_brief(task.id, _hermes_bin())

    # --strict-mcp-config: ignore all discovered MCP servers (we pass none), so an
    # unattended session never blocks on an MCP-approval prompt; also shrinks the
    # locked-down blast radius (no third-party MCP tools).
    # --disallowedTools AskUserQuestion: an unattended pane has nobody to answer
    # a multiple-choice dialog; four lanes sat on one for days (2026-09-04).
    # Bypass mode does not suppress the tool, so remove it from the session.
    argv = [claude, "--model", alias, "--permission-mode", "bypassPermissions",
            "--mcp-config", mcp_cfg, "--strict-mcp-config",
            "--disallowedTools", "AskUserQuestion",
            "--session-id", session_id, "--append-system-prompt", persona, body]

    _tmux_new_session(tmux_bin=cfg["tmux_bin"], socket=cfg["socket"], session=session,
                      workspace=workspace, env=env, argv=argv)

    # The pane is now live. If spawning the supervisor or persisting the sidecar
    # fails, the wrapper catches and falls through to a NATIVE worker — which
    # would double-run the card (claude pane + native API worker => double API
    # billing). Kill the just-created pane first, then re-raise so the native
    # fallback runs WITHOUT an orphan pane and WITHOUT a double-run.
    try:
        claim_lock = getattr(task, "claim_lock", None)
        pid = _spawn_supervisor(task=task, board=board, session=session,
                                transcript=transcript, cfgdir=cdir, cfg=cfg,
                                run_id=run_id, claim_lock=claim_lock)
        # run_id / claim_lock are persisted so a readopt relaunch can fence the
        # replacement supervisor to the SAME run this session was launched for,
        # and can tell "the card is still on our run" from "our run was
        # superseded and this pane is an orphan".
        state.session_save(task.id, {
            "session": session, "board": str(board or "default"), "session_id": session_id,
            "cfgdir": cdir, "workspace": workspace, "transcript": transcript, "supervisor_pid": pid,
            "run_id": run_id,
            "claim_lock": claim_lock,
        })
    except Exception:
        _tmux_kill(tmux_bin=cfg["tmux_bin"], socket=cfg["socket"], session=session)
        logger.warning("claude-kanban-lane: post-pane setup failed; killed pane %s, "
                       "re-raising for native fallback", session, exc_info=True)
        raise
    logger.info("claude-kanban-lane: launched %s (supervisor pid %s)", session, pid)
    return pid
