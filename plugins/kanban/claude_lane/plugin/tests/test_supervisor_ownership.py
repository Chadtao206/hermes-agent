# tests/test_supervisor_ownership.py
"""The lane's half of the core ownership contract.

The supervisor can only fence its lifecycle calls to the run it was launched
for if the launcher tells it which run that is, and can only take over a
readopted session safely if the wrapper refuses to relaunch across runs — or
without a run at all. Both were missing: the supervisor ran with
``expected_run_id=None`` and ``worker_pid=None``, so cards had no recorded owner
and a leftover supervisor could renew its successor's claim.

The invariant these tests pin is narrow and absolute: **no supervisor is ever
launched that cannot be fenced to an exact run.** Every caller-side path that
used to "fall back to unfenced" now declines to launch instead.
"""
import types

import pytest

import launcher
import wrapper


def _cfg(**over):
    cfg = {"tmux_bin": "/tmux", "socket": "s", "heartbeat_seconds": 120,
           "idle_timeout_seconds": 1200, "max_runtime_seconds": 5400,
           "coldstart_grace_seconds": 60}
    cfg.update(over)
    return cfg


def _popen_capture(monkeypatch, module):
    captured = {}
    monkeypatch.setattr(
        module.subprocess, "Popen",
        lambda argv, *a, **kw: captured.update(argv=argv) or types.SimpleNamespace(pid=999))
    return captured


def _launch_stubs(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "stable_claude_bin", lambda cfg=None: "/stable/claude.exe")
    monkeypatch.setattr(launcher, "supervisor_contract_available", lambda: True)
    monkeypatch.setattr(launcher, "_oauth_token", lambda p: "oat-xyz")
    monkeypatch.setattr(launcher, "_real_home", lambda: "/Users/ctao")
    monkeypatch.setattr(launcher, "_hermes_bin", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(launcher.brief, "compile_brief", lambda tid, hb: "BRIEF")
    monkeypatch.setattr(launcher.brief, "persona_for", lambda p: "PERSONA")
    monkeypatch.setattr(launcher.envbuild, "mirror_kanban_env",
                        lambda task, ws, board: {"HERMES_KANBAN_TASK": task.id})
    (tmp_path / "cfg").mkdir(exist_ok=True)
    monkeypatch.setattr(launcher.cfgdir, "prepare_session_config_dir",
                        lambda **kw: str(tmp_path / "cfg"))
    monkeypatch.setattr(launcher.cfgdir, "transcript_path", lambda c, w, s: "/t.jsonl")


# -- launcher: the claimed run/lock reach the supervisor argv ------------------

def test_spawn_supervisor_passes_the_claimed_run_and_lock(monkeypatch):
    captured = _popen_capture(monkeypatch, launcher)
    launcher._spawn_supervisor(
        task=types.SimpleNamespace(id="t_1"), board="default", session="sess",
        transcript="/t.jsonl", cfgdir="/cfg", cfg=_cfg(),
        run_id=1514, claim_lock="host:2463")
    argv = captured["argv"]
    assert argv[argv.index("--run-id") + 1] == "1514"
    assert argv[argv.index("--claim-lock") + 1] == "host:2463"


@pytest.mark.parametrize("bad", [None, 0, -1, "", "nope"])
def test_spawn_supervisor_refuses_an_unfenceable_run(monkeypatch, bad):
    """Launching with no --run-id used to be the "honest fallback": the
    supervisor would resolve the card's current run itself. That is precisely
    how one ended up unfenced when the DB was briefly unreadable, so a caller
    that cannot name the run must not launch a supervisor at all."""
    captured = _popen_capture(monkeypatch, launcher)
    with pytest.raises((ValueError, TypeError)):
        launcher._spawn_supervisor(
            task=types.SimpleNamespace(id="t_1"), board="default", session="sess",
            transcript="/t.jsonl", cfgdir="/cfg", cfg=_cfg(), run_id=bad)
    assert captured == {}  # nothing was spawned


def test_launch_lane_forwards_and_persists_run_and_lock(monkeypatch, tmp_path):
    """The run/lock must come from the just-claimed CARD, and be persisted in
    the sidecar — a readopt relaunch has no other way to fence itself to the
    same run."""
    t = types.SimpleNamespace(id="t_1", assignee="engineer", tenant=None,
                              branch_name=None, current_run_id=1514,
                              claim_lock="host:2463")
    _launch_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher, "_tmux_new_session", lambda **kw: None)
    sup_kw = {}
    monkeypatch.setattr(launcher, "_spawn_supervisor", lambda **kw: (sup_kw.update(kw) or 4242))
    saved = {}
    monkeypatch.setattr(launcher.state, "session_save", lambda tid, rec: saved.update({tid: rec}))

    assert launcher.launch_lane(t, "/ws", "default") == 4242
    assert sup_kw["run_id"] == 1514
    assert sup_kw["claim_lock"] == "host:2463"
    assert saved["t_1"]["run_id"] == 1514
    assert saved["t_1"]["claim_lock"] == "host:2463"


@pytest.mark.parametrize("run_id", [None, 0, "nope"])
def test_launch_lane_stays_native_without_a_run_and_leaves_no_pane(
    monkeypatch, tmp_path, run_id,
):
    """A card we cannot fence a supervisor to must fall back to the native
    worker BEFORE a pane exists. Discovering it afterwards would leave a live
    claude session with no supervisor while the native fallback runs the same
    card — a double-run and double API billing."""
    t = types.SimpleNamespace(id="t_1", assignee="engineer", tenant=None,
                              branch_name=None, current_run_id=run_id,
                              claim_lock="host:1")
    _launch_stubs(monkeypatch, tmp_path)
    created, spawned = [], []
    monkeypatch.setattr(launcher, "_tmux_new_session", lambda **kw: created.append(kw))
    monkeypatch.setattr(launcher, "_spawn_supervisor", lambda **kw: spawned.append(kw) or 1)
    monkeypatch.setattr(launcher.state, "session_save", lambda tid, rec: None)

    assert launcher.launch_lane(t, "/ws", "default") is None  # -> native fallback
    assert created == []  # no pane was ever created
    assert spawned == []


def test_launch_lane_stays_native_when_the_core_contract_is_missing(monkeypatch, tmp_path):
    """Artifact/core version skew (artifacts installed before the core landed,
    or the core rolled back under them). The supervisor would correctly stand
    down, but only after a pane existed; checking here keeps the whole lane
    off instead of churning a pane per dispatch."""
    t = types.SimpleNamespace(id="t_1", assignee="engineer", tenant=None,
                              branch_name=None, current_run_id=1514, claim_lock="host:1")
    _launch_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher, "supervisor_contract_available", lambda: False)
    created = []
    monkeypatch.setattr(launcher, "_tmux_new_session", lambda **kw: created.append(kw))

    assert launcher.launch_lane(t, "/ws", "default") is None
    assert created == []


# -- wrapper: readopt must not cross runs, or run unfenced --------------------

def test_relaunch_supervisor_passes_the_sidecar_run(monkeypatch):
    captured = _popen_capture(monkeypatch, wrapper)
    wrapper._relaunch_supervisor(
        {"task": "t_1", "board": "default", "session": "sess",
         "transcript": "/t.jsonl", "run_id": 1514, "claim_lock": "host:1"},
        _cfg())
    argv = captured["argv"]
    assert "--adopt" in argv
    assert argv[argv.index("--run-id") + 1] == "1514"
    assert argv[argv.index("--claim-lock") + 1] == "host:1"


@pytest.mark.parametrize("rec_extra", [{}, {"run_id": None}, {"run_id": 0}])
def test_relaunch_supervisor_refuses_without_a_run(monkeypatch, rec_extra):
    captured = _popen_capture(monkeypatch, wrapper)
    rec = {"task": "t_1", "board": "default", "session": "sess",
           "transcript": "/t.jsonl", **rec_extra}
    with pytest.raises((ValueError, TypeError, KeyError)):
        wrapper._relaunch_supervisor(rec, _cfg())
    assert captured == {}


def test_session_run_matches_card_identifies_an_orphan_pane():
    """A tmux session is named per (board, task), NOT per run, so run N's
    leftover pane is indistinguishable from run N+1's by name alone. The
    sidecar's run_id is the only discriminator."""
    card_1514 = types.SimpleNamespace(current_run_id=1514, status="running")
    card_1515 = types.SimpleNamespace(current_run_id=1515, status="running")
    assert wrapper.session_run_matches_card({"run_id": 1514}, card_1514) is True
    assert wrapper.session_run_matches_card({"run_id": 1514}, card_1515) is False
    # No recorded run (a sidecar written before this existed): treat the pane as
    # live rather than reaping it; the run a replacement gets fenced to is then
    # resolved from the card (see relaunch_run_id).
    assert wrapper.session_run_matches_card({}, card_1515) is True
    # A running card with no run pointer at all cannot be matched.
    assert wrapper.session_run_matches_card(
        {"run_id": 1514}, types.SimpleNamespace(current_run_id=None)) is False


def test_relaunch_run_id_prefers_the_sidecar_then_resolves_once_from_the_card():
    card = types.SimpleNamespace(current_run_id=1515, status="running")
    assert wrapper.relaunch_run_id({"run_id": 1514}, card) == 1514
    # Legacy sidecar: resolve ONCE, from the card this readopt pass already read.
    assert wrapper.relaunch_run_id({}, card) == 1515
    # Nothing exact to fence to.
    assert wrapper.relaunch_run_id({}, types.SimpleNamespace(current_run_id=None)) is None
    assert wrapper.relaunch_run_id({"run_id": 0}, None) is None


def _readopt_stubs(monkeypatch, *, card, sidecar):
    monkeypatch.setattr(wrapper.config, "lane_config", lambda: _cfg())
    monkeypatch.setattr(wrapper, "_list_sessions",
                        lambda cfg: (True, ["hermes-kanban-default-t_1"]))
    monkeypatch.setattr(wrapper.state, "session_load", lambda tid: dict(sidecar))
    monkeypatch.setattr(wrapper.launcher, "supervisor_contract_available", lambda: True)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(wrapper._connect_mod, "connect_closing", lambda board=None: _Conn())
    monkeypatch.setattr(wrapper.kdb, "get_task", lambda conn, tid: card)
    killed, deleted, relaunched, saved = [], [], [], []
    monkeypatch.setattr(wrapper.subprocess, "run",
                        lambda argv, **kw: killed.append(argv) or types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(wrapper.state, "session_delete", lambda tid: deleted.append(tid))
    monkeypatch.setattr(wrapper, "_relaunch_supervisor",
                        lambda rec, cfg: relaunched.append(rec) or 555)
    monkeypatch.setattr(wrapper.state, "session_save", lambda tid, rec: saved.append(rec))
    monkeypatch.setattr(wrapper.state, "gc_old_state", lambda active: 0)
    return {"killed": killed, "deleted": deleted, "relaunched": relaunched, "saved": saved}


def test_readopt_reaps_a_pane_whose_run_was_superseded(monkeypatch):
    """The run-N-adopts-run-N+1 bug, from the wrapper's side: relaunching an
    adopting supervisor here would put it in a fight with the successor's own
    supervisor over the card. Reap the orphan pane instead."""
    seen = _readopt_stubs(
        monkeypatch,
        card=types.SimpleNamespace(status="running", current_run_id=1515),
        sidecar={"run_id": 1514, "supervisor_pid": None, "transcript": "/t.jsonl"})
    gc_active = {}
    monkeypatch.setattr(wrapper.state, "gc_old_state",
                        lambda active: gc_active.update(active=set(active)) or 0)

    wrapper.readopt()

    assert seen["relaunched"] == []
    assert any("kill-session" in argv for argv in seen["killed"])
    assert seen["deleted"] == ["t_1"]
    # The reaped task is no longer protected from state GC.
    assert "t_1" not in gc_active["active"]


def test_readopt_relaunches_when_the_run_still_matches(monkeypatch):
    seen = _readopt_stubs(
        monkeypatch,
        card=types.SimpleNamespace(status="running", current_run_id=1514),
        sidecar={"run_id": 1514, "supervisor_pid": 1, "claim_lock": "host:1",
                 "transcript": "/t.jsonl"})
    monkeypatch.setattr(wrapper, "_pid_alive", lambda pid: False)  # original died

    wrapper.readopt()

    assert len(seen["relaunched"]) == 1
    assert seen["relaunched"][0]["run_id"] == 1514
    assert seen["relaunched"][0]["claim_lock"] == "host:1"


def test_readopt_fences_a_legacy_sidecar_to_the_card_s_run(monkeypatch):
    """A sidecar written before run_id was persisted still gets a FENCED
    replacement: the run is resolved once, here, from the card this pass
    already read — not later by the supervisor, which by then could follow the
    card into whichever run happens to be current."""
    seen = _readopt_stubs(
        monkeypatch,
        card=types.SimpleNamespace(status="running", current_run_id=1600),
        sidecar={"supervisor_pid": 1, "transcript": "/t.jsonl"})
    monkeypatch.setattr(wrapper, "_pid_alive", lambda pid: False)

    wrapper.readopt()

    assert [r["run_id"] for r in seen["relaunched"]] == [1600]
    # The resolved run is persisted so the next readopt does not re-resolve it.
    assert seen["saved"] and seen["saved"][-1]["run_id"] == 1600


def test_readopt_leaves_the_pane_alone_when_no_run_can_be_resolved(monkeypatch):
    """A running card with no run pointer and a legacy sidecar: there is nothing
    exact to fence to. Leave both pane and card untouched and retry later —
    starting an unfenced supervisor could renew a successor's claim, and killing
    a pane we cannot identify could take out a live session."""
    seen = _readopt_stubs(
        monkeypatch,
        card=types.SimpleNamespace(status="running", current_run_id=None),
        sidecar={"supervisor_pid": 1, "transcript": "/t.jsonl"})
    monkeypatch.setattr(wrapper, "_pid_alive", lambda pid: False)

    wrapper.readopt()

    assert seen["relaunched"] == []
    assert not any("kill-session" in argv for argv in seen["killed"])
    assert seen["deleted"] == []


def test_readopt_leaves_the_pane_alone_without_the_core_contract(monkeypatch):
    """Version skew: a replacement would stand down immediately, so relaunching
    one per readopt is pure churn."""
    seen = _readopt_stubs(
        monkeypatch,
        card=types.SimpleNamespace(status="running", current_run_id=1514),
        sidecar={"run_id": 1514, "supervisor_pid": 1, "transcript": "/t.jsonl"})
    monkeypatch.setattr(wrapper, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(wrapper.launcher, "supervisor_contract_available", lambda: False)

    wrapper.readopt()

    assert seen["relaunched"] == []
    assert not any("kill-session" in argv for argv in seen["killed"])
    assert seen["deleted"] == []
