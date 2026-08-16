"""The daemon must fail closed on startup and survive a bad tick once running.

Runs under a launchd KeepAlive supervisor: a startup failure must refuse to
build any dependency (never start degraded), and a runtime failure inside one
tick must never propagate past the poll loop (KeepAlive would just restart
into the same exception).

Every test here wires fakes and monkeypatches module-level names -- none of
this touches the real config.yaml, .env, state databases or the network.
"""
from pathlib import Path

from pr_remediation import daemon
from pr_remediation.config import RemediationConfig
from pr_remediation.loop import LoopDeps


def _cfg(mode="live"):
    return RemediationConfig(mode=mode)


def test_main_exits_zero_without_building_deps_when_mode_is_off(monkeypatch):
    built = []
    monkeypatch.setattr(daemon, "load_config", lambda: _cfg(mode="off"))
    monkeypatch.setattr(daemon, "run_selftest", lambda cfg: (_ for _ in ()).throw(
        AssertionError("selftest must not run when mode is off")))
    monkeypatch.setattr(daemon, "build_deps", lambda cfg, token: built.append(1))

    assert daemon.main() == 0
    assert built == []


def test_main_fails_closed_and_builds_nothing_when_selftest_reports_failures(
    monkeypatch, capsys,
):
    built = []
    monkeypatch.setattr(daemon, "load_config", lambda: _cfg(mode="live"))
    monkeypatch.setattr(daemon, "run_selftest",
                        lambda cfg: ["the write token is missing"])
    monkeypatch.setattr(daemon, "build_deps", lambda cfg, token: built.append(1))

    assert daemon.main() == 1
    assert built == []
    assert "the write token is missing" in capsys.readouterr().err


def test_main_fails_closed_when_the_slack_bot_token_is_missing(monkeypatch):
    built = []
    monkeypatch.setattr(daemon, "load_config", lambda: _cfg(mode="live"))
    monkeypatch.setattr(daemon, "run_selftest", lambda cfg: [])
    monkeypatch.setattr(daemon, "parse_env", lambda path: {})
    monkeypatch.setattr(daemon, "build_deps", lambda cfg, token: built.append(1))

    assert daemon.main() == 1
    assert built == []


def test_main_builds_deps_and_enters_the_poll_loop_once_healthy(monkeypatch):
    """A healthy startup must reach build_deps with the loaded config and the
    resolved token, and must hand off to the poll loop -- not run it inline
    forever, and not skip it either."""
    calls = {}
    cfg = _cfg(mode="live")
    sentinel_deps = object()

    monkeypatch.setattr(daemon, "load_config", lambda: cfg)
    monkeypatch.setattr(daemon, "run_selftest", lambda passed_cfg: calls.setdefault(
        "selftest_cfg", passed_cfg) and [])
    monkeypatch.setattr(daemon, "parse_env",
                        lambda path: {"TICKET_HUB_SLACK_BOT_TOKEN": "xoxb-test"})
    monkeypatch.setattr(daemon, "build_deps", lambda passed_cfg, token: (
        calls.__setitem__("build_deps", (passed_cfg, token)), sentinel_deps)[1])
    monkeypatch.setattr(daemon.signal, "signal", lambda *a, **k: None)

    def fake_run(deps, *, should_stop, **kw):
        calls["run_deps"] = deps
        assert should_stop() is False  # signal never fired; loop would proceed
    monkeypatch.setattr(daemon, "_run", fake_run)

    assert daemon.main() == 0
    assert calls["build_deps"] == (cfg, "xoxb-test")
    assert calls["run_deps"] is sentinel_deps


def test_main_fails_closed_when_selftest_itself_raises(monkeypatch, capsys):
    """run_selftest's call to load_boris_intake_config() is not itself a
    checked failure mode everywhere: boris_intake.config.load_config only
    catches OSError and yaml.YAMLError, so something exotic -- e.g. a
    UnicodeDecodeError from a non-UTF-8 config file -- would otherwise
    propagate out of run_selftest and crash main() with a raw traceback.
    Under a KeepAlive supervisor that just loops the same crash forever;
    main() must report it the same clean way as any other precondition
    failure instead."""
    built = []
    monkeypatch.setattr(daemon, "load_config", lambda: _cfg(mode="live"))

    def boom(cfg):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    monkeypatch.setattr(daemon, "run_selftest", boom)
    monkeypatch.setattr(daemon, "build_deps", lambda cfg, token: built.append(1))

    assert daemon.main() == 1
    assert built == []
    assert "selftest" in capsys.readouterr().err.lower()


def test_sigterm_handler_installed_by_main_actually_stops_the_poll_loop(monkeypatch):
    """Covers the real signal wiring end to end: capture the handler main()
    actually registers with signal.signal (rather than neutering signal.signal
    to a no-op, and rather than injecting should_stop directly), invoke it the
    way a real SIGTERM delivery would, and assert the should_stop() closure
    `_run` receives reflects the flip. If `stop()` were neutered to never set
    the flag, should_stop() would stay False after the handler fires and this
    test would fail."""
    calls = {}
    handlers = {}
    cfg = _cfg(mode="live")
    sentinel_deps = object()

    monkeypatch.setattr(daemon, "load_config", lambda: cfg)
    monkeypatch.setattr(daemon, "run_selftest", lambda passed_cfg: [])
    monkeypatch.setattr(daemon, "parse_env",
                        lambda path: {"TICKET_HUB_SLACK_BOT_TOKEN": "xoxb-test"})
    monkeypatch.setattr(daemon, "build_deps",
                        lambda passed_cfg, token: sentinel_deps)

    def fake_signal(signum, handler):
        handlers[signum] = handler
    monkeypatch.setattr(daemon.signal, "signal", fake_signal)

    def fake_run(deps, *, should_stop, **kw):
        calls["should_stop_before"] = should_stop()
        # Invoke the real handler main() installed, exactly as the signal
        # module would call it on delivery: (signum, frame).
        handlers[daemon.signal.SIGTERM](daemon.signal.SIGTERM, None)
        calls["should_stop_after"] = should_stop()
    monkeypatch.setattr(daemon, "_run", fake_run)

    assert daemon.main() == 0
    assert calls["should_stop_before"] is False
    assert calls["should_stop_after"] is True


def test_run_survives_a_bad_tick_and_keeps_polling(tmp_path, monkeypatch):
    """The one thing neither startup path guards: a tick that raises once
    the daemon is already up. `_run` must log it and go on to the next tick
    rather than letting the exception escape and kill the process."""
    heartbeat = tmp_path / "heartbeat"
    ticks = []

    def flaky_tick(deps):
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr(daemon, "tick", flaky_tick)
    stops = iter([False, False, True])
    sleeps = []
    deps = LoopDeps(source=None, github=None, slack=None, lanes=None,
                    store=None, cfg=RemediationConfig(), now=lambda: 0.0)

    daemon._run(deps, should_stop=lambda: next(stops),
               sleep=sleeps.append, heartbeat=heartbeat)

    # Both ticks ran (the first raised, the loop did not stop there); the
    # process is still alive to report it, which is the whole point.
    assert len(ticks) == 2
    assert len(sleeps) == 2
    assert heartbeat.is_file()


def test_run_stops_immediately_when_should_stop_is_already_true(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    deps = LoopDeps(source=None, github=None, slack=None, lanes=None,
                    store=None, cfg=RemediationConfig(), now=lambda: 0.0)
    calls = []

    daemon._run(deps, should_stop=lambda: True, sleep=calls.append,
               heartbeat=heartbeat)

    assert calls == []
    assert not heartbeat.exists()


# --- C4: importing this module must have no global effect -------------------

def _import_in(*roots: str, script: str | None = None) -> dict:
    """Load daemon.py in a fresh interpreter with exactly `roots` on sys.path,
    and report what that did to the path.

    `script` loads the file the way launchd does (as a script, by path);
    otherwise it is imported as `pr_remediation.daemon`, the way pytest does.

    A subprocess because sys.path damage is global and one-way: by the time a
    test runs, this process has long since imported the module, so the only
    place the question can still be asked honestly is a new one.
    """
    import json
    import subprocess
    import sys

    load = (
        f"import runpy; runpy.run_path({script!r}, run_name='not_main')"
        if script else "import pr_remediation.daemon"
    )
    probe = (
        "import json, sys\n"
        # `roots` and the interpreter's own stdlib/site-packages, and nothing
        # else -- in particular not the cwd, which `-c` would otherwise add.
        f"sys.path[:] = {list(roots)!r} + [p for p in sys.path if p]\n"
        f"{load}\n"
        "import boris_intake\n"
        "print(json.dumps({\n"
        "    'path': sys.path,\n"
        "    'boris_intake': boris_intake.__file__,\n"
        "    'slack_read_imported': 'slack_read' in sys.modules,\n"
        "}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_importing_the_daemon_does_not_put_the_deployed_tree_on_sys_path():
    """THE regression test, and it can only be written across two services.

    daemon.py used to `sys.path.insert(0, "/Users/ctao/.hermes/scripts")` at
    module scope. That tree holds a DEPLOYED copy of `boris_intake`; this repo
    holds its own under `cron/scripts`. Prepending it therefore rebound the
    entire `boris_intake` package for the rest of the process the moment pytest
    merely *collected* this file -- so in the combined run
    (`pytest tests/cron/pr_remediation tests/cron/boris_intake`) the sibling's
    tests were exercising the deployed copy, and a defect introduced into the
    repo's copy could not fail them. Proven at the time by deleting the marker
    check from cron/scripts/boris_intake/gate.py: the boris_intake suite alone
    failed 1, the combined run passed 686.

    Two things are asserted, and the second is the one that bites: the deployed
    root is absent from sys.path, and `boris_intake` resolves out of the repo.
    """
    from pr_remediation import daemon

    package_root = str(Path(daemon.__file__).resolve().parent.parent)
    repo_root = str(Path(package_root).parent.parent)
    result = _import_in(package_root, repo_root)

    assert daemon.DEPLOYED_SCRIPTS not in result["path"], (
        "importing the daemon put the deployed script tree on sys.path; every "
        "package that exists in both trees is now bound to the deployed copy "
        "for the rest of this process")
    assert result["boris_intake"].startswith(package_root), (
        f"boris_intake resolved to {result['boris_intake']}, not the repo copy "
        f"under {package_root}")
    assert result["slack_read_imported"] is False, (
        "slack_read lives only in the deployed tree; importing it at module "
        "scope is what forced the sys.path mutation in the first place")


def test_the_daemon_still_imports_the_way_launchd_actually_starts_it():
    """The other direction, so the fix above cannot be 'fixed' into a broken
    daemon. launchd runs `python .../scripts/pr_remediation/daemon.py`, which
    puts ONLY the package's own directory on sys.path -- not its parent -- so
    `import pr_remediation.…` needs a root added before those imports run.
    That root is derived from __file__ and appended, which is what makes it
    harmless: it can only ever supply what nothing earlier already provides."""
    from pr_remediation import daemon

    script = str(Path(daemon.__file__).resolve())
    package_dir = str(Path(script).parent)
    # Exactly the sys.path `python .../pr_remediation/daemon.py` produces.
    result = _import_in(package_dir, script=script)

    assert str(Path(package_dir).parent) in result["path"], (
        "the daemon no longer makes its own package importable, so launchd "
        "would fail to start it")
    assert result["boris_intake"].startswith(str(Path(package_dir).parent)), (
        "boris_intake must come from the same tree as the daemon that is "
        "running")
