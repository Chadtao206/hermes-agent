"""The ownership contract an out-of-process lane supervisor must satisfy.

The live failure this covers: the ``claude-kanban-lane`` supervisor called the
lifecycle shim with neither ``--worker-pid`` nor the run/lock env, so the core
heartbeat ran with ``worker_pid=None`` and ``expected_run_id=None``. The card
therefore had no owner (23 of 44 supervisor-lane runs on the live board had no
``spawned`` event), and a supervisor left over from run N could keep renewing
run N+1's claim.

Two layers are tested here:

* the source-controlled contract in :mod:`hermes_cli.kanban_supervisor`, end to
  end through the real ``hermes kanban heartbeat`` CLI; and
* the actual local supervisor/launcher artifacts, when this machine has them —
  they live outside the repo (``~/.hermes/scripts``, ``~/.hermes/plugins``), so
  those tests skip elsewhere rather than pretending to cover them.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_supervisor as ks


LOCAL_SUPERVISOR = Path.home() / ".hermes/scripts/claude_kanban_supervisor.py"
LOCAL_SHIM = Path.home() / ".hermes/scripts/hermes-kanban-task"
LOCAL_LANE_PLUGIN = Path.home() / ".hermes/plugins/claude-kanban-lane"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _events(conn, task_id, kind):
    return conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id",
        (task_id, kind),
    ).fetchall()


def _card_row(conn, task_id):
    return conn.execute(
        "SELECT worker_pid, claim_expires, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def _run_cli(argv):
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return kc.kanban_command(parser.parse_args(argv))


# ---------------------------------------------------------------------------
# 1. The env contract
# ---------------------------------------------------------------------------

def test_shim_env_pins_the_whole_contract(kanban_home):
    env = ks.supervisor_shim_env(
        {"PATH": "/usr/bin"},
        task_id="t_abc", board="default", run_id=1234, claim_lock="host:99",
    )
    assert env["HERMES_KANBAN_TASK"] == "t_abc"
    assert env["HERMES_KANBAN_BOARD"] == "default"
    assert env["HERMES_KANBAN_RUN_ID"] == "1234"
    assert env["HERMES_KANBAN_CLAIM_LOCK"] == "host:99"
    assert env["HERMES_KANBAN_DB"].endswith("kanban.db")
    assert env["PATH"] == "/usr/bin"  # base env is preserved, not replaced
    for key in ks.SUPERVISOR_CONTRACT_ENV_KEYS:
        assert key in env, f"contract key {key} missing"


def test_unknown_run_and_lock_are_stripped_not_inherited(kanban_home):
    """A supervisor is a gateway child, and the gateway is the parent of every
    lane — so its environment can carry ANOTHER lane's run/lock. Leaving those
    in place would fence the call to a foreign run, which is worse than being
    unfenced: it silently addresses somebody else's card."""
    env = ks.supervisor_shim_env(
        {
            "HERMES_KANBAN_RUN_ID": "777",
            "HERMES_KANBAN_CLAIM_LOCK": "other-host:1",
            "HERMES_KANBAN_TASK": "t_someone_else",
        },
        task_id="t_mine", board="default",
    )
    assert env["HERMES_KANBAN_TASK"] == "t_mine"
    assert "HERMES_KANBAN_RUN_ID" not in env
    assert "HERMES_KANBAN_CLAIM_LOCK" not in env


# ---------------------------------------------------------------------------
# 2. The heartbeat argv contract
# ---------------------------------------------------------------------------

def test_heartbeat_argv_carries_the_supervisors_own_pid():
    argv = ks.supervisor_heartbeat_argv(
        task_id="t_abc", worker_pid=4242, note="supervisor alive",
    )
    assert argv[:2] == ["heartbeat", "t_abc"]
    assert argv[argv.index("--worker-pid") + 1] == "4242"
    assert argv[argv.index("--note") + 1] == "supervisor alive"


@pytest.mark.parametrize("bad", [0, -1])
def test_heartbeat_argv_refuses_a_non_pid(bad):
    """A pid that cannot own anything must fail loudly at the boundary rather
    than turn into a heartbeat that silently records no ownership."""
    with pytest.raises(ValueError):
        ks.supervisor_heartbeat_argv(task_id="t_abc", worker_pid=bad)


# ---------------------------------------------------------------------------
# 3. End to end through the real CLI
# ---------------------------------------------------------------------------

def test_supervisor_contract_end_to_end_adopts_and_renews(kanban_home, monkeypatch):
    """Everything the supervisor does per heartbeat, wired exactly as it wires
    it: build the env, build the argv, run ``hermes kanban heartbeat``. The
    card ends up owned by the supervisor's long-lived pid with a renewed
    claim — the two facts that were both missing in the incident."""
    now = 9_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    supervisor_pid = 60606
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == supervisor_pid)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        original_expiry = _card_row(conn, tid)["claim_expires"]

    env = ks.supervisor_shim_env(
        dict(os.environ), task_id=tid, board="default",
        run_id=task.current_run_id, claim_lock=task.claim_lock,
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    later = now + 800  # past the ORIGINAL window, the incident's shape
    monkeypatch.setattr(kb.time, "time", lambda: later)
    argv = ks.supervisor_heartbeat_argv(
        task_id=tid, worker_pid=supervisor_pid, note="supervisor alive",
    )
    assert _run_cli(["kanban", *argv]) == 0

    with kb.connect() as conn:
        row = _card_row(conn, tid)
        assert row["worker_pid"] == supervisor_pid
        assert row["claim_expires"] > original_expiry
        adopted = _events(conn, tid, "ownership_adopted")
        assert len(adopted) == 1
        assert json.loads(adopted[0]["payload"])["worker_pid"] == supervisor_pid
        # And the card survives a reclaim sweep at the same clock.
        assert kb.release_stale_claims(conn, signal_fn=lambda *_: None) == 0
        assert kb.get_task(conn, tid).status == "running"


def test_stale_supervisor_cannot_heartbeat_or_adopt_the_next_run(
    kanban_home, monkeypatch,
):
    """The core fencing requirement: a supervisor from run N must be inert
    against run N+1. It still holds a valid task id and (because the
    dispatcher reuses one host:pid lock) often the very same claim lock, so
    the RUN is the only thing that can tell it apart."""
    now = 9_100_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    stale_pid, live_pid = 70707, 80808
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == live_pid)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="superseded", assignee="alice")
        first = kb.claim_task(conn, tid, ttl_seconds=900)
        kb.reclaim_task(conn, tid, signal_fn=lambda *_: None)
        second = kb.claim_task(conn, tid, ttl_seconds=900, claimer=first.claim_lock)
        assert second.claim_lock == first.claim_lock
        # The successor's own supervisor owns the card.
        assert kb.adopt_worker_pid(
            conn, tid, worker_pid=live_pid,
            expected_run_id=second.current_run_id,
        ) == kb._OWNERSHIP_RECORDED
        successor_expiry = _card_row(conn, tid)["claim_expires"]

    stale_env = ks.supervisor_shim_env(
        dict(os.environ), task_id=tid, board="default",
        run_id=first.current_run_id, claim_lock=first.claim_lock,
    )
    for key, value in stale_env.items():
        monkeypatch.setenv(key, value)

    later = now + 800
    monkeypatch.setattr(kb.time, "time", lambda: later)
    assert _run_cli([
        "kanban", *ks.supervisor_heartbeat_argv(
            task_id=tid, worker_pid=stale_pid, note="stale supervisor",
        ),
    ]) != 0

    with kb.connect() as conn:
        row = _card_row(conn, tid)
        assert row["worker_pid"] == live_pid          # not adopted
        assert row["claim_expires"] == successor_expiry  # not renewed
        assert _events(conn, tid, "heartbeat") == []

        # Explicit adoption is fenced the same way.
        assert ks.adopt_session_ownership(
            conn, tid, worker_pid=stale_pid,
            expected_run_id=first.current_run_id,
        ) == kb._OWNERSHIP_FENCED
        assert _card_row(conn, tid)["worker_pid"] == live_pid


def test_adoption_replaces_a_dead_owner_but_never_a_live_one(
    kanban_home, monkeypatch,
):
    """The readopt path's whole purpose is taking over from the dead original
    supervisor; the thing it must never do is run beside a live one."""
    now = 9_200_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    alive = {90909}
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) in alive)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="readopt", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        run_id = task.current_run_id

        # Original supervisor recorded, then died.
        assert kb.adopt_worker_pid(
            conn, tid, worker_pid=11011, expected_run_id=run_id,
        ) == kb._OWNERSHIP_RECORDED

        # Replacement adopts over the dead pid.
        assert ks.adopt_session_ownership(
            conn, tid, worker_pid=90909, expected_run_id=run_id,
        ) == kb._OWNERSHIP_REPLACED_DEAD
        assert _card_row(conn, tid)["worker_pid"] == 90909

        # A third supervisor finds a LIVE owner and is refused.
        assert ks.adopt_session_ownership(
            conn, tid, worker_pid=12012, expected_run_id=run_id,
        ) == kb._OWNERSHIP_CONFLICT
        assert _card_row(conn, tid)["worker_pid"] == 90909

        # Re-adopting our own pid is a no-op, not a second ownership event.
        assert ks.adopt_session_ownership(
            conn, tid, worker_pid=90909, expected_run_id=run_id,
        ) == kb._OWNERSHIP_ALREADY_OURS
        assert len(_events(conn, tid, "ownership_adopted")) == 2


def test_resolve_supervised_run_pins_the_card_s_current_run(kanban_home, monkeypatch):
    now = 9_300_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="pin", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        assert ks.resolve_supervised_run(conn, tid) == task.current_run_id
        # An explicitly-passed run always wins over the card's current one.
        assert ks.resolve_supervised_run(conn, tid, run_id=5) == 5
        assert ks.resolve_supervised_run(conn, "t_nope") is None


# ---------------------------------------------------------------------------
# 4. The actual local claude-kanban-lane artifacts
# ---------------------------------------------------------------------------

def _load_local(path, name):
    """Import a host-local script by path.

    An explicit ``SourceFileLoader`` because the lifecycle shim is an
    extensionless executable, which path-based spec inference declines.
    """
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.skipif(
    not LOCAL_SUPERVISOR.exists(),
    reason="claude-kanban-lane supervisor is host-local (not in this repo)",
)
def test_local_supervisor_uses_the_contract():
    """The integration Boris found broken: the supervisor must pass its OWN
    pid and the exact run/lock through the contract builders, not call the
    shim bare. Asserted against the real file so the wiring cannot regress
    back to an unfenced ``worker_pid=None`` heartbeat."""
    source = LOCAL_SUPERVISOR.read_text()
    assert "from hermes_cli.kanban_supervisor import" in source
    assert "supervisor_heartbeat_argv(" in source
    assert "supervisor_shim_env(" in source
    assert "worker_pid=my_pid" in source
    # Run/lock reach the shim env on every lifecycle call.
    assert re.search(r"run_id=run_id,\s*claim_lock=a\.claim_lock", source)
    # Adoption goes through the fenced CAS helper, never the dispatcher's
    # private spawn write.
    assert "adopt_session_ownership(" in source
    assert "_set_worker_pid" not in source

    # The CLI surface the launcher depends on.
    for flag in ("--run-id", "--claim-lock", "--adopt"):
        assert flag in source


@pytest.mark.skipif(
    not (LOCAL_SUPERVISOR.exists() and LOCAL_SHIM.exists()),
    reason="claude-kanban-lane supervisor/shim are host-local (not in this repo)",
)
def test_local_supervisor_heartbeat_owns_the_card_end_to_end(
    kanban_home, monkeypatch, tmp_path,
):
    """Drive the REAL local supervisor for one loop iteration and check the
    card afterwards.

    The whole chain runs: supervisor loop -> ``_hermes_task_shim`` env ->
    the real lifecycle shim -> ``hermes kanban heartbeat`` -> core. Only the
    two process boundaries are bridged in-process (the shim exec and tmux),
    because that is what makes this runnable at all; every ownership decision
    is the real one. Before this wiring the same iteration left the card with
    ``worker_pid`` NULL and an unfenced heartbeat."""
    import subprocess

    sup = _load_local(LOCAL_SUPERVISOR, "local_claude_kanban_supervisor_e2e")
    shim = _load_local(LOCAL_SHIM, "local_hermes_kanban_task_shim")

    now = 9_400_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane-e2e", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        original_expiry = _card_row(conn, tid)["claim_expires"]

    # The shim's own subprocess boundary -> the real CLI, in-process.
    monkeypatch.setattr(
        shim, "subprocess",
        type("S", (), {"run": staticmethod(
            lambda argv, **kw: subprocess.CompletedProcess(
                argv, _run_cli(argv[1:]),
            )
        )}),
    )

    captured: list[dict] = []

    def fake_run(argv, **kw):
        """Stand in for exec'ing the shim: same argv, same env, same code."""
        env = dict(kw.get("env") or {})
        captured.append({"argv": list(argv), "env": env})
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            rc = shim.main(list(argv[1:]))
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return subprocess.CompletedProcess(argv, rc)

    monkeypatch.setattr(sup.subprocess, "run", fake_run)
    monkeypatch.setattr(sup, "_tmux_alive", lambda *a, **k: True)
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: None)
    monkeypatch.setattr(sup, "_stream_transcript", lambda *a, **k: (0, "", False))
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)

    # A stub for the plugin's per-session state module, which ``main`` imports
    # lazily; the real one writes under the host's live state dir.
    stub_state = type("state", (), {
        "cap_reset": staticmethod(lambda _t: None),
        "cap_increment": staticmethod(lambda _t: None),
        "session_delete": staticmethod(lambda _t: None),
    })
    monkeypatch.setitem(__import__("sys").modules, "state", stub_state)

    # Second read reports the card done, so the loop exits after one iteration.
    reads = {"n": 0}
    real_read = sup._read_card

    def read_card(board, task_id):
        reads["n"] += 1
        if reads["n"] > 1:
            return True, type("card", (), {"status": "done", "worker_pid": None})
        return real_read(board, task_id)

    monkeypatch.setattr(sup, "_read_card", read_card)

    later = now + 800  # past the ORIGINAL claim window
    monkeypatch.setattr(kb.time, "time", lambda: later)
    assert sup.main([
        "--task", tid, "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"),
        "--run-id", str(task.current_run_id),
        "--claim-lock", task.claim_lock,
        "--heartbeat", "0",
    ]) == 0

    heartbeats = [c for c in captured if "heartbeat" in c["argv"]]
    assert heartbeats, f"supervisor issued no heartbeat: {captured}"
    for call in heartbeats:
        argv = call["argv"]
        assert argv[argv.index("--worker-pid") + 1] == str(os.getpid())
        assert call["env"]["HERMES_KANBAN_RUN_ID"] == str(task.current_run_id)
        assert call["env"]["HERMES_KANBAN_CLAIM_LOCK"] == task.claim_lock
        assert call["env"]["HERMES_KANBAN_TASK"] == tid

    with kb.connect() as conn:
        row = _card_row(conn, tid)
        # The supervisor's own long-lived pid owns the card, and the claim was
        # renewed past the window it was claimed under.
        assert row["worker_pid"] == os.getpid()
        assert row["claim_expires"] > original_expiry
        # One ownership event however many heartbeats the loop issued.
        assert len(_events(conn, tid, "ownership_adopted")) == 1
        assert len(_events(conn, tid, "heartbeat")) == len(heartbeats)


@pytest.mark.skipif(
    not LOCAL_SUPERVISOR.exists(),
    reason="claude-kanban-lane supervisor is host-local (not in this repo)",
)
def test_local_supervisor_readopt_refuses_a_superseded_run(
    kanban_home, monkeypatch, tmp_path,
):
    """A supervisor relaunched for run N, finding the card on run N+1, must
    not adopt it.

    ``--adopt`` exists so a replacement can take over the dead original's pid;
    unfenced, it also let a leftover supervisor stamp itself onto the
    SUCCESSOR run and then act (kill panes, block the card) on work it never
    launched. It refuses and leaves both the card and the pane alone — a
    process that just learned it is not the owner is the last one that should
    be killing sessions."""
    sup = _load_local(LOCAL_SUPERVISOR, "local_claude_kanban_supervisor_adopt")

    now = 9_500_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    live_pid = 51015
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == live_pid)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="readopt-stale", assignee="alice")
        first = kb.claim_task(conn, tid, ttl_seconds=900)
        kb.reclaim_task(conn, tid, signal_fn=lambda *_: None)
        second = kb.claim_task(conn, tid, ttl_seconds=900, claimer=first.claim_lock)
        assert kb.adopt_worker_pid(
            conn, tid, worker_pid=live_pid,
            expected_run_id=second.current_run_id,
        ) == kb._OWNERSHIP_RECORDED

    killed: list = []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)

    rc = sup.main([
        "--task", tid, "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"),
        "--run-id", str(first.current_run_id), "--adopt",
    ])
    assert rc == sup.READOPT_REFUSED_EXIT_CODE
    assert killed == []  # the live successor's pane is untouched

    with kb.connect() as conn:
        assert _card_row(conn, tid)["worker_pid"] == live_pid
        assert len(_events(conn, tid, "ownership_adopted")) == 1


@pytest.mark.skipif(
    not LOCAL_SUPERVISOR.exists(),
    reason="claude-kanban-lane supervisor is host-local (not in this repo)",
)
def test_verified_adoption_stops_assuming_ownership(kanban_home, monkeypatch, tmp_path):
    """``--adopt`` used to make the supervisor assume it owned the card for its
    whole life, so it kept running after a reclaim handed the card to somebody
    else. Now that adoption is a verified CAS, the real pid check applies: a
    foreign owner appearing mid-run is a stand-down, not something to ignore."""
    sup = _load_local(LOCAL_SUPERVISOR, "local_claude_kanban_supervisor_standdown")

    now = 9_600_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="standdown", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)

    killed: list = []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(sup, "_tmux_alive", lambda *a, **k: True)
    monkeypatch.setattr(sup, "_stream_transcript", lambda *a, **k: (0, "", False))
    monkeypatch.setattr(sup, "_hermes_task_shim", lambda *a, **k: None)
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
    monkeypatch.setitem(__import__("sys").modules, "state", type("state", (), {
        "cap_reset": staticmethod(lambda _t: None),
        "cap_increment": staticmethod(lambda _t: None),
        "session_delete": staticmethod(lambda _t: None),
    }))
    # A different supervisor takes the card over between ticks.
    monkeypatch.setattr(sup, "_read_card", lambda board, task_id: (
        True, type("card", (), {"status": "running", "worker_pid": 424242}),
    ))

    assert sup.main([
        "--task", tid, "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"),
        "--run-id", str(task.current_run_id), "--adopt",
    ]) == 0
    assert killed, "supervisor should have stood down and killed its own pane"

    with kb.connect() as conn:
        # The adoption itself did land first — this is a stand-down, not a
        # refusal to adopt.
        assert len(_events(conn, tid, "ownership_adopted")) == 1


@pytest.mark.skipif(
    not LOCAL_SHIM.exists(),
    reason="claude-kanban-lane lifecycle shim is host-local (not in this repo)",
)
def test_local_shim_forwards_worker_pid():
    """The shim is the only thing between the supervisor and the CLI; if it
    drops ``--worker-pid`` the whole chain is back to unowned cards."""
    source = LOCAL_SHIM.read_text()
    assert "--worker-pid" in source
    assert 'cmd += ["--worker-pid"' in source


@pytest.mark.skipif(
    not (LOCAL_LANE_PLUGIN / "launcher.py").exists(),
    reason="claude-kanban-lane plugin is host-local (not in this repo)",
)
def test_local_launcher_passes_the_claimed_run_and_lock():
    """The run/lock must come from the just-claimed card, and be persisted so
    a readopt relaunch can fence the replacement to the SAME run."""
    launcher = (LOCAL_LANE_PLUGIN / "launcher.py").read_text()
    assert '"--run-id"' in launcher
    assert '"--claim-lock"' in launcher
    assert 'getattr(task, "current_run_id", None)' in launcher
    assert '"run_id"' in launcher and '"claim_lock"' in launcher

    wrapper = (LOCAL_LANE_PLUGIN / "wrapper.py").read_text()
    assert '"--run-id"' in wrapper
    assert "session_run_matches_card" in wrapper
