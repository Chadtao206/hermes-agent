"""The ownership contract an out-of-process lane supervisor must satisfy.

The live failure this covers: the ``claude-kanban-lane`` supervisor called the
lifecycle shim with neither ``--worker-pid`` nor the run/lock env, so the core
heartbeat ran with ``worker_pid=None`` and ``expected_run_id=None``. The card
therefore had no owner (23 of 44 supervisor-lane runs on the live board had no
``spawned`` event), and a supervisor left over from run N could keep renewing
run N+1's claim.

Two layers are tested here, both checkout-pinned (no host dependency, so the
whole file runs in the standard ``scripts/run_tests.sh`` suite):

* the source-controlled contract in :mod:`hermes_cli.kanban_supervisor`, end to
  end through the real ``hermes kanban heartbeat`` CLI; and
* the source-controlled lane supervisor script under
  ``plugins/kanban/claude_lane`` — driven behaviourally (its ``main`` loop,
  not a regex over its source), so the wiring cannot regress to an unfenced
  heartbeat and the two Boris repros stay covered.

The installed *host* copies (``~/.hermes/scripts``, ``~/.hermes/plugins``) are
verified separately by the installer's runtime probe and the plugin's own
suite; this file deliberately does not read or import them, so a stale/reverted
host tree cannot make it pass or fail.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_guards as kbg
from hermes_cli import kanban_supervisor as ks
from hermes_cli import kanban_db_dispatch as kbd


# The source-controlled lane artifacts. They live in the repo (unlike the
# installed host copies), so these tests always run rather than skip.
_CLAUDE_LANE = Path(__file__).resolve().parents[2] / "plugins/kanban/claude_lane"
SC_SUPERVISOR = _CLAUDE_LANE / "scripts/claude_kanban_supervisor.py"
SC_SHIM = _CLAUDE_LANE / "scripts/hermes-kanban-task"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbc.init_db()
    return home


def _events(conn, task_id, kind):
    return conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id",
        (task_id, kind),
    ).fetchall()


def _card_row(conn, task_id):
    # Raw SQL on purpose: it keeps working when a test has monkeypatched
    # kb.get_task to simulate a read failure.
    return conn.execute(
        "SELECT worker_pid, claim_expires, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def _run_cli(argv):
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return kc.kanban_command(parser.parse_args(argv))


def _load_script(path, name):
    """Import a host-shaped script by path.

    An explicit ``SourceFileLoader`` because the lifecycle shim is an
    extensionless executable, which path-based spec inference declines.
    """
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


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


@pytest.mark.parametrize("bad", [None, 0, -1, "nope"])
def test_shim_env_requires_an_exact_positive_run(kanban_home, bad):
    """There is no unfenced way to address a card. ``run_id`` is required and a
    non-positive/unparseable one raises, so a supervisor that cannot name its
    run fails loudly at the boundary instead of building an env whose
    ``expected_run_id`` silently becomes None."""
    with pytest.raises(ks.SupervisorRunUnresolved):
        ks.supervisor_shim_env({}, task_id="t_1", board="default", run_id=bad)


def test_unknown_run_and_lock_are_overwritten_not_inherited(kanban_home):
    """A supervisor is a gateway child, and the gateway is the parent of every
    lane — so its environment can carry ANOTHER lane's run/lock. Our own run
    must overwrite the inherited one (never trust it), and a lock we did not
    pass must be stripped rather than inherited from a sibling lane."""
    env = ks.supervisor_shim_env(
        {
            "HERMES_KANBAN_RUN_ID": "777",
            "HERMES_KANBAN_CLAIM_LOCK": "other-host:1",
            "HERMES_KANBAN_TASK": "t_someone_else",
        },
        task_id="t_mine", board="default", run_id=1234,
    )
    assert env["HERMES_KANBAN_TASK"] == "t_mine"
    assert env["HERMES_KANBAN_RUN_ID"] == "1234"        # ours wins, 777 discarded
    assert "HERMES_KANBAN_CLAIM_LOCK" not in env         # sibling lock stripped


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
# 3. Core fencing + adoption, end to end through the real CLI / DB
# ---------------------------------------------------------------------------

def test_supervisor_contract_end_to_end_adopts_and_renews(kanban_home, monkeypatch):
    """Everything the supervisor does per heartbeat, wired exactly as it wires
    it: build the env, build the argv, run ``hermes kanban heartbeat``. The
    card ends up owned by the supervisor's long-lived pid with a renewed
    claim — the two facts that were both missing in the incident."""
    now = 9_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    supervisor_pid = 60606
    monkeypatch.setattr(kbd, "_pid_alive", lambda pid: int(pid) == supervisor_pid)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == supervisor_pid)

    with kbc.connect() as conn:
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

    with kbc.connect() as conn:
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
    monkeypatch.setattr(kbd, "_pid_alive", lambda pid: int(pid) == live_pid)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == live_pid)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="superseded", assignee="alice")
        first = kb.claim_task(conn, tid, ttl_seconds=900)
        kb.reclaim_task(conn, tid, signal_fn=lambda *_: None)
        second = kb.claim_task(conn, tid, ttl_seconds=900, claimer=first.claim_lock)
        assert second.claim_lock == first.claim_lock
        # The successor's own supervisor owns the card.
        assert kbg.adopt_worker_pid(
            conn, tid, worker_pid=live_pid,
            expected_run_id=second.current_run_id,
        ) == kbg._OWNERSHIP_RECORDED
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

    with kbc.connect() as conn:
        row = _card_row(conn, tid)
        assert row["worker_pid"] == live_pid          # not adopted
        assert row["claim_expires"] == successor_expiry  # not renewed
        assert _events(conn, tid, "heartbeat") == []

        # Explicit adoption is fenced the same way.
        assert ks.adopt_session_ownership(
            conn, tid, worker_pid=stale_pid,
            expected_run_id=first.current_run_id,
        ) == kbg._OWNERSHIP_FENCED
        assert _card_row(conn, tid)["worker_pid"] == live_pid


def test_adoption_replaces_a_dead_owner_but_never_a_live_one(
    kanban_home, monkeypatch,
):
    """The readopt path's whole purpose is taking over from the dead original
    supervisor; the thing it must never do is run beside a live one."""
    now = 9_200_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    alive = {90909}
    monkeypatch.setattr(kbd, "_pid_alive", lambda pid: int(pid) in alive)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) in alive)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="readopt", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        run_id = task.current_run_id

        # Original supervisor recorded, then died.
        assert kbg.adopt_worker_pid(
            conn, tid, worker_pid=11011, expected_run_id=run_id,
        ) == kbg._OWNERSHIP_RECORDED

        # Replacement adopts over the dead pid.
        assert ks.adopt_session_ownership(
            conn, tid, worker_pid=90909, expected_run_id=run_id,
        ) == kbg._OWNERSHIP_REPLACED_DEAD
        assert _card_row(conn, tid)["worker_pid"] == 90909

        # A third supervisor finds a LIVE owner and is refused.
        assert ks.adopt_session_ownership(
            conn, tid, worker_pid=12012, expected_run_id=run_id,
        ) == kbg._OWNERSHIP_CONFLICT
        assert _card_row(conn, tid)["worker_pid"] == 90909

        # Re-adopting our own pid is a no-op, not a second ownership event.
        assert ks.adopt_session_ownership(
            conn, tid, worker_pid=90909, expected_run_id=run_id,
        ) == kbg._OWNERSHIP_ALREADY_OURS
        assert len(_events(conn, tid, "ownership_adopted")) == 2


def test_resolve_supervised_run_pins_the_card_s_current_run(kanban_home, monkeypatch):
    now = 9_300_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pin", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        assert ks.resolve_supervised_run(conn, tid) == task.current_run_id
        # An explicitly-passed run always wins over the card's current one.
        assert ks.resolve_supervised_run(conn, tid, run_id=5) == 5
        # A card that cannot be found is a HARD (non-transient) failure, not
        # None: returning None here is exactly what let the old fallback run a
        # supervisor unfenced.
        with pytest.raises(ks.SupervisorRunUnresolved) as ei:
            ks.resolve_supervised_run(conn, "t_nope")
        assert ei.value.transient is False


# ---------------------------------------------------------------------------
# 3b. acquire_session_ownership: retry / recover / refuse (Boris repro 2 core)
# ---------------------------------------------------------------------------

def test_acquire_session_ownership_recovers_then_refuses_a_live_foreign_owner(
    kanban_home, monkeypatch,
):
    """Boris repro 2, at the contract level: three DB-read exceptions, then the
    DB recovers and the card names a LIVE foreign owner. The old readopt path
    left ``assume_ours=True`` after three exceptions and supervised anyway; the
    retry now recovers, sees the foreign owner, and REFUSES — it never grants
    ownership it could not verify."""
    now = 9_400_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    foreign_pid = 99999
    monkeypatch.setattr(kbd, "_pid_alive", lambda pid: int(pid) == foreign_pid)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == foreign_pid)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="readopt-flaky", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        run_id = task.current_run_id
        assert kbg.adopt_worker_pid(
            conn, tid, worker_pid=foreign_pid, expected_run_id=run_id,
        ) == kbg._OWNERSHIP_RECORDED
        adopt_events = len(_events(conn, tid, "ownership_adopted"))
        expiry = _card_row(conn, tid)["claim_expires"]

    real_cc = kbc.connect_closing
    calls = {"n": 0}

    def flaky_cc(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise sqlite3.OperationalError("disk I/O error")
        return real_cc(*a, **k)

    monkeypatch.setattr(kbc, "connect_closing", flaky_cc)
    slept: list[float] = []
    verdict, outcome = ks.acquire_session_ownership(
        tid, board="default", worker_pid=11111, expected_run_id=run_id,
        sleep_fn=slept.append,
    )
    assert verdict == ks.OWNERSHIP_REFUSED
    assert outcome == kbg._OWNERSHIP_CONFLICT
    assert calls["n"] == 4        # 3 failures + 1 recovery
    assert len(slept) == 3        # slept between each failure, not after success

    with kbc.connect() as conn:
        row = _card_row(conn, tid)
        assert row["worker_pid"] == foreign_pid                       # unchanged
        assert row["claim_expires"] == expiry
        assert len(_events(conn, tid, "ownership_adopted")) == adopt_events


def test_acquire_session_ownership_is_unverified_when_the_db_never_recovers(
    kanban_home, monkeypatch,
):
    """When the DB never becomes readable the verdict is UNVERIFIED, NOT held:
    unknown is not ours, and the caller stands down without touching the pane."""
    def always_raises(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kbc, "connect_closing", always_raises)
    slept: list[float] = []
    verdict, outcome = ks.acquire_session_ownership(
        "t_x", board="default", worker_pid=4242, expected_run_id=7,
        attempts=3, sleep_fn=slept.append,
    )
    assert verdict == ks.OWNERSHIP_UNVERIFIED
    assert outcome is None
    assert len(slept) == 2  # slept between attempts, none after the last


# ---------------------------------------------------------------------------
# 4. The source-controlled lane supervisor, driven behaviourally
# ---------------------------------------------------------------------------

def _stub_state(monkeypatch):
    """Stub the plugin's per-session ``state`` module, which the supervisor
    imports lazily; the real one writes under the host's live state dir."""
    stub = type("state", (), {
        "cap_reset": staticmethod(lambda _t: None),
        "cap_increment": staticmethod(lambda _t: None),
        "session_delete": staticmethod(lambda _t: None),
    })
    monkeypatch.setitem(sys.modules, "state", stub)


def test_local_supervisor_heartbeat_owns_the_card_end_to_end(
    kanban_home, monkeypatch, tmp_path,
):
    """Drive the REAL source-controlled supervisor for one loop iteration and
    check the card afterwards.

    The whole chain runs: supervisor loop -> ``_hermes_task_shim`` env ->
    the real lifecycle shim -> ``hermes kanban heartbeat`` -> core. Only the
    two process boundaries are bridged in-process (the shim exec and tmux),
    because that is what makes this runnable at all; every ownership decision
    is the real one. Before this wiring the same iteration left the card with
    ``worker_pid`` NULL and an unfenced heartbeat."""
    import subprocess

    sup = _load_script(SC_SUPERVISOR, "sc_supervisor_e2e")
    shim = _load_script(SC_SHIM, "sc_shim_e2e")

    now = 9_500_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kbc.connect() as conn:
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
    _stub_state(monkeypatch)

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

    with kbc.connect() as conn:
        row = _card_row(conn, tid)
        # The supervisor's own long-lived pid owns the card, and the claim was
        # renewed past the window it was claimed under.
        assert row["worker_pid"] == os.getpid()
        assert row["claim_expires"] > original_expiry
        # One ownership event however many heartbeats the loop issued.
        assert len(_events(conn, tid, "ownership_adopted")) == 1
        assert len(_events(conn, tid, "heartbeat")) == len(heartbeats)


def test_local_supervisor_stands_down_when_its_run_cannot_be_resolved(
    kanban_home, monkeypatch, tmp_path,
):
    """Boris repro 1: a supervisor whose run cannot be resolved must NOT fall
    back to an unfenced heartbeat.

    The old host script set ``run_id=None`` after a startup DB exception and
    heartbeated forever through that unfenced env, so a stale run-N supervisor
    renewed run N+1's claim. Here the launch stands down (RUN_UNRESOLVED)
    without a single lifecycle call, and the live successor's PID / expiry /
    events are all left exactly as they were."""
    sup = _load_script(SC_SUPERVISOR, "sc_supervisor_run_unresolved")

    now = 9_600_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    successor_pid = 424242
    monkeypatch.setattr(kbd, "_pid_alive", lambda pid: int(pid) == successor_pid)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == successor_pid)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="run-unresolved", assignee="alice")
        first = kb.claim_task(conn, tid, ttl_seconds=900)
        kb.reclaim_task(conn, tid, signal_fn=lambda *_: None)
        second = kb.claim_task(conn, tid, ttl_seconds=900, claimer=first.claim_lock)
        assert kbg.adopt_worker_pid(
            conn, tid, worker_pid=successor_pid,
            expected_run_id=second.current_run_id,
        ) == kbg._OWNERSHIP_RECORDED
        before = _card_row(conn, tid)
        before_pid = before["worker_pid"]
        before_expiry = before["claim_expires"]
        before_hb = len(_events(conn, tid, "heartbeat"))
        before_adopt = len(_events(conn, tid, "ownership_adopted"))

    # Make run resolution fail: the card lookup pin_supervised_run relies on
    # raises (a transient WAL/disk-I/O error at startup, the incident's shape).
    def flaky_get(conn, task_id):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kb, "get_task", flaky_get)

    shim_calls: list = []
    monkeypatch.setattr(sup, "_hermes_task_shim",
                        lambda *a, **k: shim_calls.append((a, k)))
    killed: list = []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
    _stub_state(monkeypatch)

    # A fresh readopt with NO --run-id, so the run must be resolved from the
    # card — which now fails. The launch must stand down, not run unfenced.
    rc = sup.main([
        "--task", tid, "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"), "--adopt",
    ])
    assert rc == sup.RUN_UNRESOLVED_EXIT_CODE
    assert shim_calls == []   # no heartbeat/adopt/block was issued
    assert killed == []       # --adopt: the (maybe-successor's) pane is untouched

    with kbc.connect() as conn:
        row = _card_row(conn, tid)  # raw SQL, unaffected by the get_task patch
        assert row["worker_pid"] == before_pid == successor_pid
        assert row["claim_expires"] == before_expiry
        assert len(_events(conn, tid, "heartbeat")) == before_hb
        assert len(_events(conn, tid, "ownership_adopted")) == before_adopt


def test_local_supervisor_readopt_refuses_after_db_recovers_to_a_live_owner(
    kanban_home, monkeypatch, tmp_path,
):
    """Boris repro 2, at the supervisor level: --adopt survives three transient
    DB-read exceptions, the DB then recovers and names a LIVE foreign owner, and
    the supervisor STANDS DOWN (READOPT_REFUSED) without touching the pane —
    instead of the old ``assume_ours`` path that supervised beside the real
    owner with only a silent heartbeat conflict to show for it."""
    sup = _load_script(SC_SUPERVISOR, "sc_supervisor_flaky_readopt")

    now = 9_700_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    foreign_pid = 99999
    monkeypatch.setattr(kbd, "_pid_alive", lambda pid: int(pid) == foreign_pid)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == foreign_pid)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="flaky-readopt", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        run_id = task.current_run_id
        assert kbg.adopt_worker_pid(
            conn, tid, worker_pid=foreign_pid, expected_run_id=run_id,
        ) == kbg._OWNERSHIP_RECORDED

    real_cc = kbc.connect_closing
    calls = {"n": 0}

    def flaky_cc(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise sqlite3.OperationalError("disk I/O error")
        return real_cc(*a, **k)

    monkeypatch.setattr(kbc, "connect_closing", flaky_cc)
    monkeypatch.setattr(ks.time, "sleep", lambda _s: None)  # acquire's retry sleep
    killed: list = []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
    _stub_state(monkeypatch)

    # --run-id given, so pin_supervised_run returns without a DB read and only
    # the ownership CAS hits the flaky connection.
    rc = sup.main([
        "--task", tid, "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"),
        "--run-id", str(run_id), "--adopt",
    ])
    assert rc == sup.READOPT_REFUSED_EXIT_CODE
    assert calls["n"] == 4     # 3 transient failures + 1 recovery that refused
    assert killed == []        # the live owner's pane is left alone

    with kbc.connect() as conn:
        assert _card_row(conn, tid)["worker_pid"] == foreign_pid
        assert len(_events(conn, tid, "ownership_adopted")) == 1


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
    sup = _load_script(SC_SUPERVISOR, "sc_supervisor_superseded")

    now = 9_800_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    live_pid = 51015
    monkeypatch.setattr(kbd, "_pid_alive", lambda pid: int(pid) == live_pid)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == live_pid)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="readopt-stale", assignee="alice")
        first = kb.claim_task(conn, tid, ttl_seconds=900)
        kb.reclaim_task(conn, tid, signal_fn=lambda *_: None)
        second = kb.claim_task(conn, tid, ttl_seconds=900, claimer=first.claim_lock)
        assert kbg.adopt_worker_pid(
            conn, tid, worker_pid=live_pid,
            expected_run_id=second.current_run_id,
        ) == kbg._OWNERSHIP_RECORDED

    killed: list = []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
    _stub_state(monkeypatch)

    rc = sup.main([
        "--task", tid, "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"),
        "--run-id", str(first.current_run_id), "--adopt",
    ])
    assert rc == sup.READOPT_REFUSED_EXIT_CODE
    assert killed == []  # the live successor's pane is untouched

    with kbc.connect() as conn:
        assert _card_row(conn, tid)["worker_pid"] == live_pid
        assert len(_events(conn, tid, "ownership_adopted")) == 1


def test_local_supervisor_stands_down_when_a_foreign_owner_appears_mid_run(
    kanban_home, monkeypatch, tmp_path,
):
    """``--adopt`` used to make the supervisor assume it owned the card for its
    whole life, so it kept running after a reclaim handed the card to somebody
    else. Now that adoption is a verified CAS, the real pid check applies: a
    foreign owner appearing mid-run is a stand-down, not something to ignore."""
    sup = _load_script(SC_SUPERVISOR, "sc_supervisor_standdown")

    now = 9_900_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(kbd, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="standdown", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)

    killed: list = []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(sup, "_tmux_alive", lambda *a, **k: True)
    monkeypatch.setattr(sup, "_stream_transcript", lambda *a, **k: (0, "", False))
    monkeypatch.setattr(sup, "_hermes_task_shim", lambda *a, **k: None)
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
    _stub_state(monkeypatch)
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

    with kbc.connect() as conn:
        # The adoption itself did land first — this is a stand-down, not a
        # refusal to adopt.
        assert len(_events(conn, tid, "ownership_adopted")) == 1


# ---------------------------------------------------------------------------
# 5. The source-controlled lifecycle shim, driven behaviourally
# ---------------------------------------------------------------------------

def test_local_shim_forwards_worker_pid(kanban_home, monkeypatch, tmp_path):
    """The shim is the only thing between the supervisor and the CLI; if it
    drops ``--worker-pid`` the whole chain is back to unowned cards. Driven,
    not regex'd: a fake ``hermes`` records the argv it receives."""
    shim = _load_script(SC_SHIM, "sc_shim_worker_pid")
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "fake-hermes"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    fake.chmod(0o755)

    monkeypatch.setenv("HERMES_BIN", str(fake))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_MINE")
    rc = shim.main(["heartbeat", "--note", "supervisor alive",
                    "--worker-pid", "4242"])
    assert rc == 0
    out = calls.read_text()
    assert "kanban heartbeat t_MINE" in out
    assert "--worker-pid 4242" in out


def test_local_shim_refuses_a_foreign_task_id(kanban_home, monkeypatch, tmp_path):
    """The shim forces the caller's own task id; a foreign id-shaped positional
    is refused before ``hermes`` is ever invoked."""
    shim = _load_script(SC_SHIM, "sc_shim_foreign")
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "fake-hermes"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    fake.chmod(0o755)

    monkeypatch.setenv("HERMES_BIN", str(fake))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_MINE")
    rc = shim.main(["complete", "t_OTHER", "--summary", "x"])
    assert rc != 0
    assert not calls.exists()  # hermes never called


# ---------------------------------------------------------------------------
# 6. Boundaries and the remaining stand-down paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("good,expected", [(1, 1), ("42", 42), (1514, 1514)])
def test_require_run_id_accepts_exact_positive_runs(good, expected):
    assert ks.require_run_id(good) == expected


@pytest.mark.parametrize("bad", [None, 0, -5, "", "x", object()])
def test_require_run_id_rejects_everything_else(bad):
    """The single boundary that makes "fenced" unfalsifiable: everything
    downstream may assume an int it can compare with ``current_run_id``."""
    with pytest.raises(ks.SupervisorRunUnresolved) as ei:
        ks.require_run_id(bad)
    assert ei.value.transient is False  # not a run at all: nothing to retry


def test_pin_supervised_run_distinguishes_transient_from_hard_failure(
    kanban_home, monkeypatch,
):
    """The classification the caller acts on. An unreadable BOARD may become
    readable (retry later, leave the pane); a card with no run never will. The
    old code collapsed both into ``run_id = None`` and carried on unfenced."""
    monkeypatch.setattr(kb.time, "time", lambda: 9_950_000)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pin", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)

    # An explicitly-passed run needs no DB at all.
    assert ks.pin_supervised_run(tid, board="default", run_id=task.current_run_id) \
        == task.current_run_id
    assert ks.pin_supervised_run(tid, board="default") == task.current_run_id

    def unopenable(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kbc, "connect_closing", unopenable)
    with pytest.raises(ks.SupervisorRunUnresolved) as ei:
        ks.pin_supervised_run(tid, board="default")
    assert ei.value.transient is True


def test_acquire_session_ownership_holds_after_replacing_a_dead_owner(
    kanban_home, monkeypatch,
):
    """The positive verdict. Without this, an implementation that refused
    everything would satisfy every other assertion in this file — and the
    readopt path exists precisely to take over from the dead original."""
    monkeypatch.setattr(kb.time, "time", lambda: 10_000_000)
    replacement = 90909
    monkeypatch.setattr(kbd, "_pid_alive", lambda pid: int(pid) == replacement)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == replacement)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="dead-owner", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        assert kbg.adopt_worker_pid(
            conn, tid, worker_pid=11011, expected_run_id=task.current_run_id,
        ) == kbg._OWNERSHIP_RECORDED  # the original supervisor, since died

    verdict, outcome = ks.acquire_session_ownership(
        tid, board="default", worker_pid=replacement,
        expected_run_id=task.current_run_id, sleep_fn=lambda _s: None,
    )
    assert (verdict, outcome) == (ks.OWNERSHIP_HELD, kbg._OWNERSHIP_REPLACED_DEAD)
    with kbc.connect() as conn:
        assert _card_row(conn, tid)["worker_pid"] == replacement


@pytest.mark.parametrize("bad", [None, 0, -1])
def test_adoption_itself_refuses_an_unfenced_run(kanban_home, monkeypatch, bad):
    """Adoption is the one write that can take a card away from another pid, so
    it must never run without a run fence: unfenced, a leftover supervisor
    stamps itself onto whatever run happens to be current."""
    monkeypatch.setattr(kb.time, "time", lambda: 10_050_000)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unfenced-adopt", assignee="alice")
        kb.claim_task(conn, tid, ttl_seconds=900)
        with pytest.raises(ks.SupervisorRunUnresolved):
            ks.adopt_session_ownership(conn, tid, worker_pid=4242, expected_run_id=bad)
        with pytest.raises(ks.SupervisorRunUnresolved):
            ks.acquire_session_ownership(
                tid, board="default", worker_pid=4242, expected_run_id=bad,
            )
        assert _card_row(conn, tid)["worker_pid"] is None


def test_local_supervisor_kills_its_own_pane_when_a_fresh_launch_cannot_be_fenced(
    kanban_home, monkeypatch, tmp_path,
):
    """The launch-path half of the run fence, where the pane policy inverts.

    On a readopt the pane predates us and may belong to the real owner, so we
    touch nothing. On a FRESH launch it was created for us seconds ago and
    nobody else is watching it: leaving it would mean a live claude session with
    no supervisor while the card is requeued — a double-run and double API
    billing. The launcher is meant to catch this before a pane exists; this is
    the backstop for a hand-launched or stale-launcher spawn."""
    sup = _load_script(SC_SUPERVISOR, "sc_supervisor_fresh_unfenced")
    monkeypatch.setattr(kb.time, "time", lambda: 10_100_000)

    killed: list = []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
    _stub_state(monkeypatch)

    rc = sup.main([
        "--task", "t_no_such_card", "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"),
    ])
    assert rc == sup.RUN_UNRESOLVED_EXIT_CODE
    assert killed, "a fresh launch must not leave an unsupervised pane behind"


def test_local_supervisor_readopt_defers_when_ownership_is_unverifiable(
    kanban_home, monkeypatch, tmp_path,
):
    """The DB never recovers during adoption, so ownership is UNKNOWN — which is
    not ours. Exit with the unverified code, leave the pane for whoever owns it,
    and let a later readopt try again. The old code treated this exact branch as
    a licence to supervise for the rest of the process' life."""
    sup = _load_script(SC_SUPERVISOR, "sc_supervisor_unverified")
    monkeypatch.setattr(kb.time, "time", lambda: 10_150_000)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unverifiable", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        before = _card_row(conn, tid)
        before_pid, before_expiry = before["worker_pid"], before["claim_expires"]

    real_cc = kbc.connect_closing

    def never_opens(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kbc, "connect_closing", never_opens)
    monkeypatch.setattr(ks.time, "sleep", lambda _s: None)  # acquire's retry sleep
    killed, shim_calls = [], []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(sup, "_hermes_task_shim", lambda *a, **k: shim_calls.append(a))
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
    _stub_state(monkeypatch)

    rc = sup.main([
        "--task", tid, "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"),
        "--run-id", str(task.current_run_id), "--adopt",
    ])
    assert rc == sup.OWNERSHIP_UNVERIFIED_EXIT_CODE
    assert killed == []
    assert shim_calls == []

    # Restore only the connection factory: monkeypatch.undo() would also revert
    # the kanban_home fixture and point these assertions at the LIVE board.
    monkeypatch.setattr(kbc, "connect_closing", real_cc)
    with kbc.connect() as conn:
        row = _card_row(conn, tid)
        assert row["worker_pid"] == before_pid          # still unowned
        assert row["claim_expires"] == before_expiry
        assert _events(conn, tid, "ownership_adopted") == []
