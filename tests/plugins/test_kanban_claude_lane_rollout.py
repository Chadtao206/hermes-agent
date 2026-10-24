"""Rollout coherence for the versioned claude-kanban-lane artifacts.

The lane runs from files outside any checkout that call an API inside one. The
incident this covers is the *packaging* half of that: the host artifacts were
hand-edited to import ``hermes_cli.kanban_supervisor`` while the installed core
still predated it, so every lane spawn died with ModuleNotFoundError and the
plugin's own suite stopped collecting — while the repo's tests passed, because
they ran against a checkout the lane never imports.

Three properties are pinned here:

1. **Ordering is enforced, not documented.** ``install.py`` probes the TARGET
   runtime and refuses to install artifacts onto a core that lacks the contract.
2. **Partial installs are an error.** ``--check`` distinguishes fully installed
   / staged / mixed, and mixed is the state that broke the lane.
3. **The artifacts fail closed anyway.** With the contract missing, the
   supervisor still imports (``--help`` works, pytest can collect it) and exits
   with its contract-unavailable code instead of raising — so a core rollback
   under installed artifacts degrades to the native worker.

``install.py``'s own check / install / rollback logic is covered in
``tests/hermes_cli/test_kanban_claude_lane_install.py`` with the runtime probe
stubbed. This file owns the parts that need the real thing: the probe itself,
the artifacts' behaviour against a core that lacks the contract, the versioned
``launcher.py`` / ``wrapper.py`` run against a synthetic plugin package (so "no
unfenced supervisor is ever launched" is checkable with no host installation at
all), and drift between the versioned copies and whatever this host has
installed.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
LANE_ROOT = REPO_ROOT / "plugins" / "kanban" / "claude_lane"
INSTALLER = LANE_ROOT / "install.py"
LANE_SUPERVISOR = LANE_ROOT / "scripts" / "claude_kanban_supervisor.py"
LANE_PLUGIN = LANE_ROOT / "plugin"
LANE_PLUGIN_TESTS = LANE_PLUGIN / "tests"


def _load_installer():
    loader = importlib.machinery.SourceFileLoader("claude_lane_install", str(INSTALLER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def installer():
    return _load_installer()


# ---------------------------------------------------------------------------
# The runtime probe: does the TARGET core have the contract?
# ---------------------------------------------------------------------------

# A synthetic old core has to be hermetic against the *host's own* install, and
# absence of a file is not enough to achieve that. Under a PEP 660 editable
# install the distribution appends an ``_EditableFinder`` to ``sys.meta_path``
# whose child branch is, verbatim:
#
#     parent, _, child = fullname.rpartition(".")
#     if parent and parent in MAPPING:
#         return PathFinder.find_spec(fullname, path=[MAPPING[parent]])
#
# It resolves ``hermes_cli.<child>`` off the *checkout* and never consults this
# package's ``__path__``. So ``import hermes_cli.kanban_supervisor`` against a
# stub that omits it misses in ``PathFinder`` (correct), then falls through to
# that finder and gets served the live module (not correct) — which proceeds to
# import this stub's deliberately minimal ``kanban_db`` and dies on an unrelated
# AttributeError. The stub therefore has to refuse the fall-through itself.
#
# This is a harness concern only: on a real host the editable finder means the
# module genuinely *is* importable, so the installer's probe answering "present"
# is the right answer, not a leak. Only the synthetic core is a fiction that has
# to be maintained against every finder the host happens to have installed.
_HERMETIC_STUB_INIT = '''\
"""A ``hermes_cli`` that resolves its own submodules or none at all."""

import sys
from importlib.machinery import PathFinder

_HERE = __path__[0]


class _OnlyThisStub:
    """Meta-path guard, ahead of every finder the host may have installed.

    Scoped to direct children of this package, which is the shape the leak has
    (``parent in MAPPING``) and the shape this stub has (flat modules). A miss
    raises rather than returning ``None``, so no later finder can answer for a
    module this synthetic core is asserting it does not have.
    """

    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        parent, _, child = fullname.rpartition(".")
        if parent != __name__ or not child:
            return None
        spec = PathFinder.find_spec(fullname, path=[_HERE])
        if spec is None:
            raise ModuleNotFoundError(
                f"No module named {fullname!r}", name=fullname)
        return spec


# Ahead of PathFinder *and* of anything appended after it. ``sys`` / ``PathFinder``
# / ``_HERE`` stay bound: ``find_spec`` reads them on every later import.
if not any(f is _OnlyThisStub for f in sys.meta_path):
    sys.meta_path.insert(0, _OnlyThisStub)
'''


def _stub_core(root: Path, *, attrs) -> Path:
    """A minimal ``hermes_cli`` package exposing ``attrs`` from kanban_supervisor."""
    pkg = root / "hermes_cli"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(_HERMETIC_STUB_INIT, encoding="utf-8")
    (pkg / "kanban_db.py").write_text(
        "KANBAN_RATE_LIMIT_EXIT_CODE = 75\n"
        "def worker_log_path(task, board=None):\n    return '/dev/null'\n"
        "def connect_closing(board=None):\n    raise RuntimeError('stub')\n"
        "def get_task(conn, task_id):\n    return None\n",
        encoding="utf-8",
    )
    if attrs is not None:
        body = "".join(f"{name} = 1\n" for name in attrs)
        (pkg / "kanban_supervisor.py").write_text(body, encoding="utf-8")
    return root


def _fake_python(root: Path, *, stub_root: Path | None) -> Path:
    """An interpreter wrapper that re-adds a PYTHONPATH the probe scrubs.

    The probe deliberately drops PYTHONPATH and runs outside every checkout, so
    the only honest way to point it at a synthetic core is to bake the path into
    the interpreter it is given — which is also how the real thing works (the
    supervisor's shebang pins one venv).
    """
    exe = root / "fake-python"
    prefix = f'PYTHONPATH="{stub_root}" ' if stub_root else ""
    exe.write_text(f'#!/bin/sh\n{prefix}exec "{sys.executable}" "$@"\n', encoding="utf-8")
    exe.chmod(0o755)
    return exe


def test_the_synthetic_old_core_is_hermetic_against_this_host(tmp_path):
    """Guard the guard: the fiction the three probe tests rest on.

    Every "old core" assertion below is only meaningful if the stub really is
    the whole of ``hermes_cli`` for the probed interpreter. It was not: this
    host's PEP 660 editable finder answered for submodules the stub omits, so
    the omission was invisible and the tests failed (or passed) on the live
    module's behaviour instead. Asserted directly, and on the interpreter that
    actually has the editable install, so a regression here reports the cause
    rather than surfacing as a puzzling AttributeError three tests down.
    """
    stub = _stub_core(tmp_path / "hermetic", attrs=None)
    probe = (
        "import hermes_cli\n"
        f"assert hermes_cli.__file__.startswith({str(stub)!r}), hermes_cli.__file__\n"
        "import hermes_cli.kanban_db as kb\n"
        f"assert kb.__file__.startswith({str(stub)!r}), kb.__file__\n"
        "try:\n"
        "    import hermes_cli.kanban_supervisor as leaked\n"
        "except ModuleNotFoundError:\n"
        "    print('HERMETIC')\n"
        "else:\n"
        "    print('LEAKED ' + (leaked.__file__ or '?'))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(stub)
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, cwd=str(stub), env=env, timeout=120, check=False)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip() == "HERMETIC", out.stdout + out.stderr


def test_runtime_probe_detects_a_complete_contract(installer, tmp_path):
    stub = _stub_core(tmp_path / "good", attrs=installer.REQUIRED_CORE_ATTRS)
    ok, detail = installer.runtime_contract_status(str(_fake_python(tmp_path, stub_root=stub)))
    assert ok, detail
    assert "kanban_supervisor" in detail


def test_runtime_probe_rejects_a_partial_contract(installer, tmp_path):
    """A core that has the module but not the API is skew too — this is what a
    half-reverted core looks like."""
    partial = installer.REQUIRED_CORE_ATTRS[:-2]
    stub = _stub_core(tmp_path / "partial", attrs=partial)
    ok, detail = installer.runtime_contract_status(
        str(_fake_python(tmp_path, stub_root=stub)))
    assert not ok
    assert "MISSING_ATTRS" in detail


def test_runtime_probe_rejects_a_core_without_the_module(installer, tmp_path):
    stub = _stub_core(tmp_path / "absent", attrs=None)
    ok, detail = installer.runtime_contract_status(
        str(_fake_python(tmp_path, stub_root=stub)))
    assert not ok
    assert "kanban_supervisor" in detail
    # Name the failure, not just the module: any *other* error mentioning
    # kanban_supervisor would satisfy the line above, and the one this used to
    # get was an AttributeError raised by the host's own live module leaking in
    # past the stub. Rejection has to come from the module being absent.
    assert "ModuleNotFoundError" in detail, detail


# ---------------------------------------------------------------------------
# Fail-closed against a core that lacks the contract
# ---------------------------------------------------------------------------

def _run_supervisor_without_contract(tmp_path, argv):
    """Run the versioned supervisor against a core with no kanban_supervisor.

    PYTHONPATH points at the stub and the cwd is the stub dir, so the import
    resolves there and nowhere else — the same "no PYTHONPATH masking" rule the
    installer's probe follows, applied in reverse.
    """
    stub = _stub_core(tmp_path / "oldcore", attrs=None)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(stub)
    return subprocess.run(
        [sys.executable, str(LANE_SUPERVISOR), *argv],
        capture_output=True, text=True, cwd=str(stub), env=env, timeout=120, check=False,
    )


def test_supervisor_still_imports_and_helps_without_the_contract(tmp_path):
    """The regression that stopped the plugin suite collecting: a module-level
    ``from hermes_cli.kanban_supervisor import ...`` against an older core made
    the file unimportable, so ``--help`` exited 1 and pytest could not even
    collect ``test_supervisor_decide.py``."""
    out = _run_supervisor_without_contract(tmp_path, ["--help"])
    assert out.returncode == 0, out.stderr
    assert "--run-id" in out.stdout


def test_supervisor_stands_down_without_the_contract_and_touches_no_pane(tmp_path):
    """A readopt on a core that lacks the contract: exit with the
    contract-unavailable code and leave the pane to whoever owns it. ``--adopt``
    also proves nothing ran: the tmux binary is deliberately nonexistent, so any
    pane call would raise FileNotFoundError instead of exiting cleanly."""
    out = _run_supervisor_without_contract(tmp_path, [
        "--task", "t_1", "--board", "default", "--session", "sess",
        "--transcript", str(tmp_path / "t.jsonl"), "--adopt",
        "--tmux-bin", "/nonexistent/tmux",
    ])
    assert out.returncode == 6, (out.returncode, out.stdout, out.stderr)
    assert "standing down" in out.stderr
    assert "kanban_supervisor" in out.stderr
    assert "Traceback" not in out.stderr
    # The stand-down has to be for the reason under test. The guard catches
    # broad ``Exception``, so before the stub was made hermetic this passed on
    # an AttributeError from the host's leaked-in live supervisor rather than on
    # the absent module it claims to cover.
    assert "ModuleNotFoundError" in out.stderr, out.stderr


# ---------------------------------------------------------------------------
# The versioned launcher/wrapper, exercised without a host install
# ---------------------------------------------------------------------------

_PLUGIN_STUBS = {
    "brief.py": (
        "def compile_brief(task_id, hermes_bin):\n    return 'BRIEF'\n"
        "def persona_for(profile):\n    return 'PERSONA'\n"
    ),
    "cfgdir.py": (
        "def prepare_session_config_dir(**kw):\n    return '/tmp/cfgdir'\n"
        "def transcript_path(cfgdir, workspace, session_id):\n    return '/tmp/t.jsonl'\n"
    ),
    "config.py": (
        "from pathlib import Path\n"
        "STATE_DIR = Path('/tmp/claude-lane-state')\n"
        "def lane_config():\n"
        "    return {'tmux_bin': '/tmux', 'socket': 's', 'heartbeat_seconds': 120,\n"
        "            'idle_timeout_seconds': 1200, 'max_runtime_seconds': 5400,\n"
        "            'coldstart_grace_seconds': 60, 'deny_read_globs': [],\n"
        "            'oauth_token_path': '/tmp/token'}\n"
    ),
    "envbuild.py": (
        "def mirror_kanban_env(task, workspace, board):\n    return {}\n"
        "def build_session_env(**kw):\n    return {}\n"
        "def _profile_arg(assignee):\n    return assignee\n"
    ),
    "modelmap.py": "def cli_model_alias(model_id):\n    return 'sonnet'\n",
    "router.py": (
        "def profile_model_provider(assignee):\n    return ('m', 'p')\n"
        "def should_use_lane(task):\n    return True\n"
    ),
    "state.py": (
        "from pathlib import Path\n"
        "STATE_DIR = Path('/tmp/claude-lane-state')\n"
        "def session_save(task_id, rec):\n    return None\n"
        "def session_load(task_id):\n    return {}\n"
        "def session_delete(task_id):\n    return None\n"
        "def gc_old_state(active):\n    return 0\n"
        "def cap_reset(task_id):\n    return None\n"
        "def cap_increment(task_id):\n    return None\n"
    ),
}


def test_versioned_launcher_and_wrapper_pass_their_own_ownership_suite(tmp_path):
    """Run the versioned plugin ownership tests against a synthetic plugin
    package built from the versioned ``launcher.py`` / ``wrapper.py`` plus
    stub siblings.

    This is the checkout-pinned half of the plugin's coverage: the same file is
    installed to the host and run there after deployment, but nothing about
    CI's correctness may depend on the host having been updated first.
    """
    plugin = tmp_path / "claude-kanban-lane"
    (plugin / "tests").mkdir(parents=True)
    for name, body in _PLUGIN_STUBS.items():
        (plugin / name).write_text(body, encoding="utf-8")
    for name in ("launcher.py", "wrapper.py"):
        (plugin / name).write_text((LANE_PLUGIN / name).read_text(encoding="utf-8"),
                                   encoding="utf-8")
    for name in ("conftest.py", "test_supervisor_ownership.py"):
        (plugin / "tests" / name).write_text(
            (LANE_PLUGIN_TESTS / name).read_text(encoding="utf-8"), encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(plugin)])
    env.pop("HERMES_KANBAN_TASK", None)  # never let a live lane's env leak in
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(plugin / "tests" / "test_supervisor_ownership.py")],
        capture_output=True, text=True, cwd=str(plugin), env=env, timeout=600, check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    # rc 0 with nothing collected would be a silent pass; pytest exits 5 for
    # "no tests ran", but assert on the summary too so a future rename of the
    # versioned test file cannot quietly empty this out.
    assert " passed" in out.stdout, out.stdout


# ---------------------------------------------------------------------------
# Drift between the versioned copies and whatever this host has installed
# ---------------------------------------------------------------------------

@pytest.mark.allow_real_home_io
def test_installed_host_artifacts_match_the_versioned_copies(installer):
    """Drift check, gated on the install actually being expected.

    Before the core lands, the installed artifacts SHOULD still be the previous
    version — requiring otherwise is what broke the live lane — so this skips
    unless the installed runtime already exposes the contract, at which point
    the artifacts are supposed to have been installed from here.
    """
    home = str(Path.home())
    ok, detail = installer.runtime_contract_status()
    if not ok:
        pytest.skip(f"installed runtime has no supervisor contract yet ({detail}); "
                    "artifacts are correctly still on the previous version")
    missing = [rel_dst for _s, rel_dst, _m in installer.MANIFEST
               if not (Path(home) / rel_dst).exists()]
    if len(missing) == len(installer.MANIFEST):
        pytest.skip("no claude-kanban-lane artifacts installed on this host")
    assert installer.check(home, quiet=True) == 0, (
        "installed lane artifacts differ from the versioned copies; run "
        "plugins/kanban/claude_lane/install.py --check")
