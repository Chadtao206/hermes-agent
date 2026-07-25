"""The ownership contract an out-of-process lane supervisor must satisfy.

The live failure this covers: the ``claude-kanban-lane`` supervisor called the
lifecycle shim with neither ``--worker-pid`` nor the run/lock env, so the core
heartbeat ran with ``worker_pid=None`` and ``expected_run_id=None``. The card
therefore had no owner (23 of 44 supervisor-lane runs on the live board had no
``spawned`` event), and a supervisor left over from run N could keep renewing
run N+1's claim.

The second round of that failure is the one this file now pins hardest: the
*fallbacks*. Fencing was wired in, but every path that could not resolve a run
fell back to running unfenced, and a readopt that could not verify its
ownership write fell back to assuming it owned the card forever. Both fallbacks
are gone; the tests below assert that the code refuses instead.

Two layers are tested here, and both are checkout-pinned:

* the source-controlled contract in :mod:`hermes_cli.kanban_supervisor`, end to
  end through the real ``hermes kanban heartbeat`` CLI; and
* the source-controlled lane artifacts under ``plugins/kanban/claude_lane``,
  which are what gets installed to ``~/.hermes``. Testing the versioned copies
  rather than the installed ones is deliberate: the installed files legitimately
  lag until the core they depend on is deployed (see that directory's README),
  and a test that required them to be updated first is what let a half-installed
  integration break the live lane. ``tests/plugins/test_kanban_claude_lane_rollout.py``
  covers the install/drift side of that.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_supervisor as ks


REPO_ROOT = Path(__file__).resolve().parents[2]
LANE_ROOT = REPO_ROOT / "plugins" / "kanban" / "claude_lane"
LANE_SUPERVISOR = LANE_ROOT / "scripts" / "claude_kanban_supervisor.py"
LANE_SHIM = LANE_ROOT / "scripts" / "hermes-kanban-task"
LANE_PLUGIN = LANE_ROOT / "plugin"


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


def _superseded_card(monkeypatch, *, now, live_pid):
    """A card whose run N has been superseded by run N+1, owned by ``live_pid``.

    The dispatcher reuses one ``host:pid`` claim lock, so the successor usually
    holds the very same lock as its predecessor — which is why the RUN is the
    only thing that can tell a stale supervisor apart from a live one.
    """
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == live_pid)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="superseded", assignee="alice")
        first = kb.claim_task(conn, tid, ttl_seconds=900)
        kb.reclaim_task(conn, tid, signal_fn=lambda *_: None)
        second = kb.claim_task(conn, tid, ttl_seconds=900, claimer=first.claim_lock)
        assert second.claim_lock == first.claim_lock
        assert kb.adopt_worker_pid(
            conn, tid, worker_pid=live_pid, expected_run_id=second.current_run_id,
        ) == kb._OWNERSHIP_RECORDED
        row = _card_row(conn, tid)
    return tid, first, second, row


# ---------------------------------------------------------------------------
# 1. The env contract — a run is mandatory, never inherited, never omitted
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


@pytest.mark.parametrize("bad", [None, 0, -1, "", "nope"])
def test_shim_env_refuses_to_build_an_unfenced_call(kanban_home, bad):
    """The exact shape of Boris' first repro. This builder used to *strip*
    ``HERMES_KANBAN_RUN_ID`` when the run was unknown, on the theory that
    unfenced-but-honest beat fencing against the wrong run. It is neither: the
    resulting ``hermes kanban heartbeat`` ran with ``expected_run_id=None``, so
    a supervisor left over from run 1 renewed run 2's claim, added a heartbeat
    event to it and stamped pid 424242 onto it — all with CLI rc 0. There is no
    unfenced mode any more; a supervisor that cannot name its run must exit."""
    with pytest.raises(ks.SupervisorRunUnresolved):
        ks.supervisor_shim_env(
            {"PATH": "/usr/bin"}, task_id="t_abc", board="default", run_id=bad,
        )


def test_inherited_run_and_lock_are_never_trusted(kanban_home):
    """A supervisor is a gateway child, and the gateway is the parent of every
    lane — so its environment can carry ANOTHER lane's run/lock. Ours always
    overwrites; an unknown lock is removed rather than inherited, because
    inheriting it would silently address someone else's claim."""
    env = ks.supervisor_shim_env(
        {
            "HERMES_KANBAN_RUN_ID": "777",
            "HERMES_KANBAN_CLAIM_LOCK": "other-host:1",
            "HERMES_KANBAN_TASK": "t_someone_else",
        },
        task_id="t_mine", board="default", run_id=1514,
    )
    assert env["HERMES_KANBAN_TASK"] == "t_mine"
    assert env["HERMES_KANBAN_RUN_ID"] == "1514"
    assert "HERMES_KANBAN_CLAIM_LOCK" not in env


@pytest.mark.parametrize("good,expected", [(1, 1), ("42", 42), (1514, 1514)])
def test_require_run_id_accepts_exact_positive_runs(good, expected):
    assert ks.require_run_id(good) == expected


@pytest.mark.parametrize("bad", [None, 0, -5, "", "x", object()])
def test_require_run_id_rejects_everything_else(bad):
    with pytest.raises(ks.SupervisorRunUnresolved) as exc:
        ks.require_run_id(bad)
    assert exc.value.transient is False  # nothing to retry: it is not a run


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
# 3. Pinning the run: resolve once, or refuse
# ---------------------------------------------------------------------------

def test_pin_supervised_run_prefers_the_launcher_s_run(kanban_home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 9_000_000)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="pin", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        assert ks.resolve_supervised_run(conn, tid) == task.current_run_id
        assert ks.resolve_supervised_run(conn, tid, run_id=5) == 5
    assert ks.pin_supervised_run(tid, board="default") == task.current_run_id
    assert ks.pin_supervised_run(tid, board="default", run_id=7) == 7


def test_pin_supervised_run_classifies_an_unreadable_board_as_transient(
    kanban_home, monkeypatch,
):
    """The production shape: SQLite 'disk I/O error' from WAL contention at
    gateway startup. The old code swallowed it and continued with
    ``run_id=None`` — i.e. unfenced. It must raise, and say the answer may
    exist, so the caller defers instead of guessing."""
    def boom(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kb, "connect_closing", boom)
    with pytest.raises(ks.SupervisorRunUnresolved) as exc:
        ks.pin_supervised_run("t_whatever", board="default")
    assert exc.value.transient is True


def test_pin_supervised_run_refuses_an_absent_card_or_missing_run(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr(kb.time, "time", lambda: 9_010_000)
    with pytest.raises(ks.SupervisorRunUnresolved) as absent:
        ks.pin_supervised_run("t_nope", board="default")
    assert absent.value.transient is False

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="unclaimed", assignee="alice")
        # Never claimed -> no task_runs row -> nothing exact to fence to.
        with pytest.raises(ks.SupervisorRunUnresolved) as norun:
            ks.resolve_supervised_run(conn, tid)
    assert norun.value.transient is False


# ---------------------------------------------------------------------------
# 4. Adoption: held / refused / unverified — and never "assume it is ours"
# ---------------------------------------------------------------------------

def test_acquire_session_ownership_holds_after_replacing_a_dead_owner(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr(kb.time, "time", lambda: 9_100_000)
    replacement = 90909
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == replacement)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="readopt", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        assert kb.adopt_worker_pid(
            conn, tid, worker_pid=11011, expected_run_id=task.current_run_id,
        ) == kb._OWNERSHIP_RECORDED  # the original supervisor, now dead

    verdict, outcome = ks.acquire_session_ownership(
        tid, board="default", worker_pid=replacement,
        expected_run_id=task.current_run_id, sleep_fn=lambda _s: None,
    )
    assert (verdict, outcome) == (ks.OWNERSHIP_HELD, kb._OWNERSHIP_REPLACED_DEAD)
    with kb.connect() as conn:
        assert _card_row(conn, tid)["worker_pid"] == replacement


def test_acquire_session_ownership_refuses_a_live_foreign_owner(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr(kb.time, "time", lambda: 9_110_000)
    live = 51015
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == live)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="occupied", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        kb.adopt_worker_pid(
            conn, tid, worker_pid=live, expected_run_id=task.current_run_id,
        )

    verdict, outcome = ks.acquire_session_ownership(
        tid, board="default", worker_pid=12012,
        expected_run_id=task.current_run_id, sleep_fn=lambda _s: None,
    )
    assert (verdict, outcome) == (ks.OWNERSHIP_REFUSED, kb._OWNERSHIP_CONFLICT)
    with kb.connect() as conn:
        assert _card_row(conn, tid)["worker_pid"] == live
        assert len(_events(conn, tid, "ownership_adopted")) == 1


def test_acquire_session_ownership_reports_unverified_when_the_db_never_answers(
    kanban_home, monkeypatch,
):
    """Unknown is not ours. The old code retried three times and then continued
    with ``assume_ours=True`` for the rest of the process' life."""
    monkeypatch.setattr(kb.time, "time", lambda: 9_120_000)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="unreadable", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)

    attempts = {"n": 0}

    def boom(*a, **kw):
        attempts["n"] += 1
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kb, "connect_closing", boom)
    slept = []
    verdict, outcome = ks.acquire_session_ownership(
        tid, board="default", worker_pid=4242,
        expected_run_id=task.current_run_id, attempts=3,
        sleep_fn=slept.append,
    )
    assert (verdict, outcome) == (ks.OWNERSHIP_UNVERIFIED, None)
    assert attempts["n"] == 3
    assert slept == [ks.ADOPTION_RETRY_SECONDS] * 2  # no sleep after the last try


def test_three_db_errors_then_a_live_foreign_owner_is_refused(
    kanban_home, monkeypatch,
):
    """Boris' second repro, at the layer that decides it.

    Three adoption attempts raise (WAL contention at gateway startup), then the
    DB comes back — and the card names a LIVE foreign supervisor. The old code
    had already given up on verifying and passed ``assume_ours=True`` forever,
    so ``caller_decision`` returned 'continue' for a readable card naming pid
    99999 while we were pid 11111: two supervisors on one run, with only a
    silent heartbeat conflict to show for it. Now the retry sees the recovered
    DB, gets a conflict, and the verdict is a stand-down.
    """
    monkeypatch.setattr(kb.time, "time", lambda: 9_130_000)
    live_foreign = 99999
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == live_foreign)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="foreign-owner", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        kb.adopt_worker_pid(
            conn, tid, worker_pid=live_foreign,
            expected_run_id=task.current_run_id,
        )
        before = _card_row(conn, tid)

    real_connect_closing = kb.connect_closing
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise sqlite3.OperationalError("disk I/O error")
        return real_connect_closing(*a, **kw)

    monkeypatch.setattr(kb, "connect_closing", flaky)
    verdict, outcome = ks.acquire_session_ownership(
        tid, board="default", worker_pid=11111,
        expected_run_id=task.current_run_id, attempts=5,
        sleep_fn=lambda _s: None,
    )
    assert (verdict, outcome) == (ks.OWNERSHIP_REFUSED, kb._OWNERSHIP_CONFLICT)
    assert calls["n"] == 4  # it really did retry into the recovered DB
    monkeypatch.setattr(kb, "connect_closing", real_connect_closing)
    with kb.connect() as conn:
        after = _card_row(conn, tid)
        assert after["worker_pid"] == before["worker_pid"] == live_foreign
        assert after["claim_expires"] == before["claim_expires"]
        assert len(_events(conn, tid, "ownership_adopted")) == 1


def test_adoption_is_run_fenced_and_never_takes_a_live_owner(kanban_home, monkeypatch):
    """The readopt path's whole purpose is taking over from the dead original
    supervisor; the thing it must never do is run beside a live one, or across
    runs."""
    now = 9_200_000
    tid, first, second, row = _superseded_card(monkeypatch, now=now, live_pid=90909)
    with kb.connect() as conn:
        # Fenced to the superseded run -> refused, card untouched.
        assert ks.adopt_session_ownership(
            conn, tid, worker_pid=70707, expected_run_id=first.current_run_id,
        ) == kb._OWNERSHIP_FENCED
        # Right run, but a live owner -> still refused.
        assert ks.adopt_session_ownership(
            conn, tid, worker_pid=70707, expected_run_id=second.current_run_id,
        ) == kb._OWNERSHIP_CONFLICT
        # Our own pid again is a no-op, not a second ownership event.
        assert ks.adopt_session_ownership(
            conn, tid, worker_pid=90909, expected_run_id=second.current_run_id,
        ) == kb._OWNERSHIP_ALREADY_OURS
        assert _card_row(conn, tid)["worker_pid"] == 90909
        assert len(_events(conn, tid, "ownership_adopted")) == 1


@pytest.mark.parametrize("bad", [None, 0, -1])
def test_adoption_refuses_an_unfenced_run(kanban_home, monkeypatch, bad):
    monkeypatch.setattr(kb.time, "time", lambda: 9_210_000)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="unfenced-adopt", assignee="alice")
        kb.claim_task(conn, tid, ttl_seconds=900)
        with pytest.raises(ks.SupervisorRunUnresolved):
            ks.adopt_session_ownership(conn, tid, worker_pid=4242, expected_run_id=bad)
        with pytest.raises(ks.SupervisorRunUnresolved):
            ks.acquire_session_ownership(
                tid, board="default", worker_pid=4242, expected_run_id=bad,
            )
        assert _card_row(conn, tid)["worker_pid"] is None


# ---------------------------------------------------------------------------
# 5. End to end through the real CLI
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
    tid, first, second, before = _superseded_card(
        monkeypatch, now=now, live_pid=80808,
    )
    stale_pid = 70707

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
        assert row["worker_pid"] == 80808                     # not adopted
        assert row["claim_expires"] == before["claim_expires"]  # not renewed
        assert _events(conn, tid, "heartbeat") == []


# ---------------------------------------------------------------------------
# 6. The source-controlled lane artifacts
# ---------------------------------------------------------------------------

def _load_lane(path, name):
    """Import a versioned lane artifact by path.

    An explicit ``SourceFileLoader`` because the lifecycle shim is an
    extensionless executable, which path-based spec inference declines.
    """
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def lane_supervisor(monkeypatch):
    """The real versioned supervisor, with its process-level effects stubbed."""
    sup = _load_lane(LANE_SUPERVISOR, "lane_claude_kanban_supervisor")
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
    # The retry sleep lives in the contract module, and its default is 2s x 5.
    monkeypatch.setattr(ks.time, "sleep", lambda _s: None)
    return sup


def test_lane_supervisor_uses_the_contract():
    """The integration Boris found broken: the supervisor must pass its OWN
    pid and the exact run/lock through the contract builders, not call the
    shim bare. Asserted against the real file so the wiring cannot regress
    back to an unfenced ``worker_pid=None`` heartbeat — or to an unfenced
    fallback when the run cannot be resolved."""
    source = LANE_SUPERVISOR.read_text(encoding="utf-8")
    assert "from hermes_cli.kanban_supervisor import" in source
    assert "supervisor_heartbeat_argv(" in source
    assert "supervisor_shim_env(" in source
    assert "worker_pid=my_pid" in source
    # Run/lock reach the shim env on every lifecycle call.
    assert re.search(r"run_id=run_id,\s*claim_lock=a\.claim_lock", source)
    # Adoption goes through the fenced CAS helper, never the dispatcher's
    # private spawn write.
    assert "acquire_session_ownership(" in source
    assert "_set_worker_pid" not in source
    # No fail-open fallbacks: the two that Boris found are gone by name.
    assert "run_id = None" not in source
    assert "assume_ours" not in source
    # The run is pinned exactly once, and refusing is a real code path.
    assert source.count("pin_supervised_run(") == 1
    assert "SupervisorRunUnresolved" in source

    for flag in ("--run-id", "--claim-lock", "--adopt"):
        assert flag in source


def test_lane_supervisor_heartbeat_owns_the_card_end_to_end(
    kanban_home, monkeypatch, tmp_path, lane_supervisor,
):
    """Drive the REAL supervisor for one loop iteration and check the card
    afterwards.

    The whole chain runs: supervisor loop -> ``_hermes_task_shim`` env ->
    the real lifecycle shim -> ``hermes kanban heartbeat`` -> core. Only the
    two process boundaries are bridged in-process (the shim exec and tmux),
    because that is what makes this runnable at all; every ownership decision
    is the real one. Before this wiring the same iteration left the card with
    ``worker_pid`` NULL and an unfenced heartbeat."""
    import subprocess

    sup = lane_supervisor
    shim = _load_lane(LANE_SHIM, "lane_hermes_kanban_task_shim")

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


def test_lane_supervisor_stands_down_when_the_run_cannot_be_resolved(
    kanban_home, monkeypatch, tmp_path, lane_supervisor,
):
    """Boris' first repro, driven through the real supervisor.

    A supervisor launched without ``--run-id`` whose startup card read fails
    used to continue with ``run_id=None`` and heartbeat forever through an
    unfenced env — successfully renewing its SUCCESSOR's claim (CLI rc 0,
    worker_pid None -> 424242, expiry extended, heartbeat event added). It must
    now exit before it can mutate anything, and leave a readopted pane alone.
    """
    sup = lane_supervisor
    now = 9_500_000
    live_pid = 80808
    tid, first, second, before = _superseded_card(
        monkeypatch, now=now, live_pid=live_pid,
    )

    killed, shimmed = [], []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(sup, "_hermes_task_shim", lambda *a, **k: shimmed.append(a))

    def unreadable(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    real_connect_closing = kb.connect_closing
    monkeypatch.setattr(kb, "connect_closing", unreadable)

    rc = sup.main([
        "--task", tid, "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"), "--adopt",
    ])
    assert rc == sup.RUN_UNRESOLVED_EXIT_CODE
    assert killed == []    # a readopt never touches a pane it may not own
    assert shimmed == []   # and issues no lifecycle call at all

    # Restore only the connection factory. ``monkeypatch.undo()`` would also
    # revert the kanban_home fixture's HERMES_HOME / Path.home patches and
    # point these assertions at the LIVE board.
    monkeypatch.setattr(kb, "connect_closing", real_connect_closing)
    with kb.connect() as conn:
        after = _card_row(conn, tid)
        assert after["worker_pid"] == live_pid              # successor untouched
        assert after["claim_expires"] == before["claim_expires"]
        assert after["current_run_id"] == second.current_run_id
        assert _events(conn, tid, "heartbeat") == []
        assert len(_events(conn, tid, "ownership_adopted")) == 1


def test_lane_supervisor_kills_its_own_pane_when_it_cannot_fence_a_fresh_launch(
    kanban_home, monkeypatch, tmp_path, lane_supervisor,
):
    """The same refusal, on the launch path rather than the readopt path.

    Here the pane was created for us seconds ago and nobody else is watching
    it, so leaving it would mean a live claude session with no supervisor while
    the card is requeued — a double-run. The launcher is supposed to have
    stopped this before a pane existed (it checks ``current_run_id`` first);
    this is the backstop for a hand-launched or stale-launcher spawn."""
    sup = lane_supervisor
    monkeypatch.setattr(kb.time, "time", lambda: 9_600_000)

    killed = []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))

    rc = sup.main([
        "--task", "t_missing", "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"),
    ])
    assert rc == sup.RUN_UNRESOLVED_EXIT_CODE
    assert killed, "a fresh launch must not leave an unsupervised pane behind"


def test_lane_supervisor_readopt_refuses_a_superseded_run(
    kanban_home, monkeypatch, tmp_path, lane_supervisor,
):
    """A supervisor relaunched for run N, finding the card on run N+1, must
    not adopt it.

    ``--adopt`` exists so a replacement can take over the dead original's pid;
    unfenced, it also let a leftover supervisor stamp itself onto the
    SUCCESSOR run and then act (kill panes, block the card) on work it never
    launched. It refuses and leaves both the card and the pane alone."""
    sup = lane_supervisor
    tid, first, second, before = _superseded_card(
        monkeypatch, now=9_700_000, live_pid=51015,
    )

    killed = []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))

    rc = sup.main([
        "--task", tid, "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"),
        "--run-id", str(first.current_run_id), "--adopt",
    ])
    assert rc == sup.READOPT_REFUSED_EXIT_CODE
    assert killed == []  # the live successor's pane is untouched

    with kb.connect() as conn:
        assert _card_row(conn, tid)["worker_pid"] == 51015
        assert len(_events(conn, tid, "ownership_adopted")) == 1


def test_lane_supervisor_readopt_stands_down_on_a_live_foreign_owner_after_db_errors(
    kanban_home, monkeypatch, tmp_path, lane_supervisor,
):
    """Boris' second repro, driven through the real supervisor.

    Three adoption attempts raise, the DB recovers, and the card names a live
    foreign supervisor. The old code had stopped verifying by then and ran the
    whole loop with ``assume_ours=True``, so it supervised a card it did not
    own. It must stand down — and, being a readopt, without touching the pane
    the real owner is watching."""
    sup = lane_supervisor
    now = 9_800_000
    live_foreign = 99999
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == live_foreign)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="foreign-live", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        kb.adopt_worker_pid(
            conn, tid, worker_pid=live_foreign, expected_run_id=task.current_run_id,
        )
        before = _card_row(conn, tid)

    real_connect_closing = kb.connect_closing
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise sqlite3.OperationalError("disk I/O error")
        return real_connect_closing(*a, **kw)

    monkeypatch.setattr(kb, "connect_closing", flaky)
    killed, shimmed = [], []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(sup, "_hermes_task_shim", lambda *a, **k: shimmed.append(a))

    rc = sup.main([
        "--task", tid, "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"),
        "--run-id", str(task.current_run_id), "--adopt",
    ])
    assert rc == sup.READOPT_REFUSED_EXIT_CODE
    assert killed == []
    assert shimmed == []

    monkeypatch.setattr(kb, "connect_closing", real_connect_closing)
    with kb.connect() as conn:
        after = _card_row(conn, tid)
        assert after["worker_pid"] == before["worker_pid"] == live_foreign
        assert after["claim_expires"] == before["claim_expires"]
        assert _events(conn, tid, "heartbeat") == []
        assert len(_events(conn, tid, "ownership_adopted")) == 1


def test_lane_supervisor_readopt_defers_when_ownership_cannot_be_verified(
    kanban_home, monkeypatch, tmp_path, lane_supervisor,
):
    """The DB never becomes readable during adoption. Unknown ownership is a
    deferral: exit with the unverified code, leave the pane for whoever owns
    it, and let a later readopt try again. The old code took this branch as a
    licence to supervise."""
    sup = lane_supervisor
    now = 9_900_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="unverifiable", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        before = _card_row(conn, tid)

    def unreadable(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    real_connect_closing = kb.connect_closing
    monkeypatch.setattr(kb, "connect_closing", unreadable)
    killed, shimmed = [], []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(sup, "_hermes_task_shim", lambda *a, **k: shimmed.append(a))

    rc = sup.main([
        "--task", tid, "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"),
        "--run-id", str(task.current_run_id), "--adopt",
    ])
    assert rc == sup.OWNERSHIP_UNVERIFIED_EXIT_CODE
    assert killed == []
    assert shimmed == []

    monkeypatch.setattr(kb, "connect_closing", real_connect_closing)
    with kb.connect() as conn:
        after = _card_row(conn, tid)
        assert after["worker_pid"] == before["worker_pid"]  # still unowned
        assert after["claim_expires"] == before["claim_expires"]
        assert _events(conn, tid, "ownership_adopted") == []


def test_verified_adoption_stands_down_on_a_mid_run_takeover(
    kanban_home, monkeypatch, tmp_path, lane_supervisor,
):
    """``--adopt`` used to make the supervisor assume it owned the card for its
    whole life, so it kept running after a reclaim handed the card to somebody
    else. Now that adoption is a verified CAS, the real pid check applies: a
    foreign owner appearing mid-run is a stand-down, not something to ignore."""
    sup = lane_supervisor
    monkeypatch.setattr(kb.time, "time", lambda: 10_000_000)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="standdown", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)

    killed: list = []
    monkeypatch.setattr(sup, "_tmux_kill", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(sup, "_tmux_alive", lambda *a, **k: True)
    monkeypatch.setattr(sup, "_stream_transcript", lambda *a, **k: (0, "", False))
    monkeypatch.setattr(sup, "_hermes_task_shim", lambda *a, **k: None)
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


def test_lane_shim_forwards_worker_pid():
    """The shim is the only thing between the supervisor and the CLI; if it
    drops ``--worker-pid`` the whole chain is back to unowned cards."""
    source = LANE_SHIM.read_text(encoding="utf-8")
    assert "--worker-pid" in source
    assert 'cmd += ["--worker-pid"' in source


def test_lane_launcher_and_wrapper_never_launch_an_unfenced_supervisor():
    """The caller side of the same invariant, asserted on the versioned files:
    the run comes from the just-claimed card, is persisted for readopt, and
    every path that cannot produce one declines to launch."""
    launcher = (LANE_PLUGIN / "launcher.py").read_text(encoding="utf-8")
    assert '"--run-id"' in launcher
    assert '"--claim-lock"' in launcher
    assert 'getattr(task, "current_run_id", None)' in launcher
    assert '"run_id"' in launcher and '"claim_lock"' in launcher
    # No pane may exist before the run is known.
    assert launcher.index("_positive_run_id(task)") < launcher.index("_tmux_new_session(tmux_bin")
    assert "supervisor_contract_available" in launcher

    wrapper = (LANE_PLUGIN / "wrapper.py").read_text(encoding="utf-8")
    assert '"--run-id"' in wrapper
    assert "session_run_matches_card" in wrapper
    assert "relaunch_run_id" in wrapper
