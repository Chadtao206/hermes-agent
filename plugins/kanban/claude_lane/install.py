#!/usr/bin/env python3
"""Install, verify or roll back the claude-kanban-lane host artifacts.

The lane runs from files that live OUTSIDE any checkout (``~/.hermes/scripts``,
``~/.hermes/plugins/claude-kanban-lane``) but depend on core APIs that live
inside one. Editing the host copies by hand is how the two got out of step: the
artifacts were updated to import ``hermes_cli.kanban_supervisor`` while the
installed core still predated it, so every lane spawn died with
ModuleNotFoundError and the plugin's own test suite stopped collecting.

This script makes that skew impossible to reach by accident:

* the versioned copies next to it are the source of truth (``--check`` reports
  any drift, and reports a PARTIAL install as an error — that mixed state is
  the one that broke the lane);
* ``--install`` refuses to run until the TARGET RUNTIME actually exposes the
  core contract, which encodes the ordering: **core first, artifacts second**;
* every overwrite is backed up, and ``--rollback`` restores the newest backup
  set in the reverse order, so an artifact rollback can precede a core
  rollback.

Ordering matters in one direction only, and it is worth being explicit about
why. The supervisor script is re-read from disk on every lane spawn; the
launcher/wrapper are imported once at gateway start. So:

* new supervisor + old launcher (the order used here) is SAFE: the launcher
  passes no ``--run-id`` and the new supervisor pins the card's current run
  itself, once, at startup;
* new launcher + old supervisor is NOT: the launcher passes ``--run-id`` /
  ``--claim-lock`` flags the old supervisor's argparse rejects, so the
  supervisor exits 2 and leaves an orphaned claude pane behind.

Usage::

    python install.py --check
    python install.py --install
    python install.py --rollback
    python install.py --check --home /tmp/fake-home   # tests

Exit codes: 0 success / fully installed, 1 drift or partial install (or a
refused install), 3 staged but not installed (the coherent pre-deploy state).
"""
from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
import time

# (versioned path relative to this file, host path relative to $HOME, mode).
# Install order is the list order: the supervisor script and the shim BEFORE the
# plugin modules that pass them new flags. Tests are last; nothing imports them.
MANIFEST = (
    ("scripts/claude_kanban_supervisor.py", ".hermes/scripts/claude_kanban_supervisor.py", 0o755),
    ("scripts/hermes-kanban-task", ".hermes/scripts/hermes-kanban-task", 0o755),
    ("plugin/launcher.py", ".hermes/plugins/claude-kanban-lane/launcher.py", 0o644),
    ("plugin/wrapper.py", ".hermes/plugins/claude-kanban-lane/wrapper.py", 0o644),
    ("plugin/tests/test_supervisor_decide.py",
     ".hermes/plugins/claude-kanban-lane/tests/test_supervisor_decide.py", 0o644),
    ("plugin/tests/test_supervisor_ownership.py",
     ".hermes/plugins/claude-kanban-lane/tests/test_supervisor_ownership.py", 0o644),
    ("plugin/tests/test_launcher.py",
     ".hermes/plugins/claude-kanban-lane/tests/test_launcher.py", 0o644),
    ("plugin/tests/test_wrapper.py",
     ".hermes/plugins/claude-kanban-lane/tests/test_wrapper.py", 0o644),
    ("plugin/tests/test_shim.py",
     ".hermes/plugins/claude-kanban-lane/tests/test_shim.py", 0o644),
)

# The core surface the installed artifacts call. Probed in the TARGET runtime,
# not this process: the whole point is to catch "the checkout I am reading is
# not the checkout the lane will import".
REQUIRED_CORE_ATTRS = (
    "require_run_id",
    "supervisor_shim_env",
    "supervisor_heartbeat_argv",
    "pin_supervised_run",
    "adopt_session_ownership",
    "acquire_session_ownership",
    "OWNERSHIP_HELD",
    "SupervisorRunUnresolved",
)

DEFAULT_RUNTIME_PYTHON = "/Users/ctao/.hermes/hermes-agent/venv/bin/python"

STATE_INSTALLED = "installed"
STATE_DRIFTED = "drifted"
STATE_MISSING = "missing"


def versioned_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def entries(home: str):
    """Yield ``(source_path, target_path, mode)`` for the whole manifest."""
    root = versioned_root()
    for rel_src, rel_dst, mode in MANIFEST:
        yield (os.path.join(root, rel_src), os.path.join(home, rel_dst), mode)


def artifact_state(source: str, target: str) -> str:
    if not os.path.exists(target):
        return STATE_MISSING
    return STATE_INSTALLED if filecmp.cmp(source, target, shallow=False) else STATE_DRIFTED


def runtime_contract_status(python: str | None = None) -> tuple[bool, str]:
    """Does the TARGET runtime expose the core supervisor contract?

    Probed in a subprocess with PYTHONPATH scrubbed and a cwd outside every
    checkout, because a probe that inherits either would answer for the
    developer's tree instead of the runtime the lane actually imports — the
    exact masking that let the broken install look fine locally.
    """
    exe = python or (DEFAULT_RUNTIME_PYTHON
                     if os.path.exists(DEFAULT_RUNTIME_PYTHON) else sys.executable)
    probe = (
        "import hermes_cli.kanban_supervisor as ks\n"
        f"missing = [n for n in {REQUIRED_CORE_ATTRS!r} if not hasattr(ks, n)]\n"
        "print('MISSING_ATTRS ' + ','.join(missing) if missing else 'OK ' + (ks.__file__ or '?'))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    try:
        out = subprocess.run([exe, "-c", probe], capture_output=True, text=True,
                             cwd=tempfile.gettempdir(), env=env, timeout=120)
    except Exception as exc:  # pragma: no cover - unusable interpreter
        return (False, f"{exe}: probe failed ({exc.__class__.__name__}: {exc})")
    text = (out.stdout or "").strip()
    if out.returncode == 0 and text.startswith("OK "):
        return (True, f"{exe}: {text[3:]}")
    detail = text or (out.stderr or "").strip().splitlines()[-1:] or ["no output"]
    if isinstance(detail, list):
        detail = detail[0]
    return (False, f"{exe}: {detail}")


def check(home: str, python: str | None = None, *, quiet: bool = False) -> int:
    states = []
    for source, target, _mode in entries(home):
        state = artifact_state(source, target)
        states.append(state)
        if not quiet:
            print(f"  {state:9s} {target}")
    ok, detail = runtime_contract_status(python)
    if not quiet:
        print(f"  core contract: {'present' if ok else 'ABSENT'} — {detail}")
    installed = sum(1 for s in states if s == STATE_INSTALLED)
    if installed == len(states):
        if not quiet:
            print("fully installed" if ok
                  else "INCOHERENT: artifacts installed but the runtime core lacks the contract")
        return 0 if ok else 1
    if installed == 0:
        if not quiet:
            print("staged, not installed (coherent: the host runs the previous artifacts)")
        return 3
    if not quiet:
        print(f"PARTIAL INSTALL: {installed}/{len(states)} artifacts installed — "
              "this is the mixed state that breaks the lane; run --install or --rollback")
    return 1


def _backup_path(target: str, stamp: str) -> str:
    return f"{target}.pre-{stamp}"


def install(home: str, python: str | None = None, *, force: bool = False,
            stamp: str | None = None) -> int:
    ok, detail = runtime_contract_status(python)
    if not ok and not force:
        print(f"refusing to install: the target runtime has no core supervisor "
              f"contract ({detail}).\n"
              f"Install order is core FIRST (merge/deploy hermes_cli/kanban_supervisor.py), "
              f"then these artifacts. Re-run with --force only if you are deliberately "
              f"staging artifacts that will stand down until the core lands.",
              file=sys.stderr)
        return 1
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    installed = []
    for source, target, mode in entries(home):
        if not os.path.exists(source):
            print(f"missing versioned artifact: {source}", file=sys.stderr)
            return 1
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            backup = _backup_path(target, stamp)
            shutil.copy2(target, backup)
            print(f"  backed up {target} -> {backup}")
        shutil.copyfile(source, target)
        os.chmod(target, mode)
        installed.append(target)
        print(f"  installed  {target}")
    print(f"installed {len(installed)} artifact(s); backup stamp {stamp}")
    print("rollback with: python install.py --rollback")
    return 0


def _newest_backup(target: str) -> str | None:
    directory = os.path.dirname(target)
    prefix = os.path.basename(target) + ".pre-"
    try:
        names = [n for n in os.listdir(directory) if n.startswith(prefix)]
    except OSError:
        return None
    if not names:
        return None
    return os.path.join(directory, sorted(names)[-1])


def rollback(home: str) -> int:
    """Restore the newest backup of each artifact, in REVERSE install order.

    Reverse order for the same reason install order matters: the plugin modules
    that pass the newer flags go back first, so no window exists where an old
    supervisor is handed flags it cannot parse.
    """
    restored = 0
    for source, target, mode in reversed(list(entries(home))):
        backup = _newest_backup(target)
        if backup is None:
            print(f"  no backup       {target}")
            continue
        shutil.copyfile(backup, target)
        os.chmod(target, mode)
        restored += 1
        print(f"  restored        {target} <- {backup}")
    print(f"restored {restored} artifact(s)")
    if restored:
        print("core may now be rolled back safely (artifacts no longer require it)")
    return 0 if restored else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report install state (default)")
    mode.add_argument("--install", action="store_true", help="install the versioned artifacts")
    mode.add_argument("--rollback", action="store_true", help="restore the newest backups")
    ap.add_argument("--home", default=os.path.expanduser("~"),
                    help="target home directory (tests point this at a tmp tree)")
    ap.add_argument("--python", default=None,
                    help="interpreter whose hermes_cli is probed for the core contract")
    ap.add_argument("--force", action="store_true",
                    help="install even when the target runtime lacks the core contract")
    ap.add_argument("--stamp", default=None, help="backup suffix (default: local timestamp)")
    a = ap.parse_args(argv)
    if a.install:
        return install(a.home, a.python, force=a.force, stamp=a.stamp)
    if a.rollback:
        return rollback(a.home)
    return check(a.home, a.python)


if __name__ == "__main__":
    sys.exit(main())
