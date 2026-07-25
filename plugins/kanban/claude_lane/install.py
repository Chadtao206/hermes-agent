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
* install and rollback are ONE TRANSACTION over the whole manifest, never a
  per-file best effort (see below).

Ordering matters in one direction only, and it is worth being explicit about
why. The supervisor script is re-read from disk on every lane spawn; the
launcher/wrapper are imported once at gateway start. So:

* new supervisor + old launcher (the order used here) is SAFE: the launcher
  passes no ``--run-id`` and the new supervisor pins the card's current run
  itself, once, at startup;
* new launcher + old supervisor is NOT: the launcher passes ``--run-id`` /
  ``--claim-lock`` flags the old supervisor's argparse rejects, so the
  supervisor exits 2 and leaves an orphaned claude pane behind.

Transaction semantics
---------------------

A per-file rollback that independently picks each target's newest ``.pre-*``
backup is NOT safe, and both of its failure modes are reachable:

* a FIRST install creates targets that have no backup at all, so a per-file
  rollback leaves every new artifact installed while reporting success;
* after install generation A, an install of generation B that stops part way
  leaves B-stamped backups only on the targets it reached. Restoring each
  target's own newest backup then yields A for those targets and A's older
  *pre-A* predecessor for the untouched ones — a mixed runtime, announced as a
  clean rollback.

So ``--install`` records a durable transaction manifest under
``~/.hermes/.claude-lane-install/`` BEFORE it mutates anything. The manifest
names one generation and, per member, whether the target already existed and
which backup holds its exact pre-install bytes. Rolling back a transaction
therefore means: restore every pre-existing member from THAT transaction's
backup, and delete every member the transaction created. Never a mix.

* every target write is atomic (temp file in the destination dir, fsync,
  ``os.replace``), so a concurrent lane spawn reads either the old file or the
  new one, never a truncated script;
* all sources and destinations are preflighted before the first mutation;
* a mid-install failure immediately restores that exact pre-install set. If the
  automatic restore cannot finish, the manifest is left open so ``--rollback``
  deterministically completes it;
* ``--rollback`` returns nonzero — and does NOT say the core may be rolled back
  — if any manifest member cannot be restored or removed.

Usage::

    python install.py --check
    python install.py --install
    python install.py --rollback
    python install.py --check --home /tmp/fake-home   # tests

Exit codes: 0 success / fully installed, 1 drift or partial install (or a
refused install, or an incomplete rollback), 3 staged but not installed (the
coherent pre-deploy state).
"""
from __future__ import annotations

import argparse
import errno
import filecmp
import hashlib
import json
import os
import re
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

# Transaction journal. Lives beside the artifacts it describes but outside every
# installed tree, so a rollback of the artifacts cannot delete its own receipt.
TXN_DIR_REL = ".hermes/.claude-lane-install"
TXN_VERSION = 1
TXN_OPEN = "pending"          # manifest written, targets may be half-written
TXN_COMMITTED = "committed"   # all targets installed
TXN_ROLLED_BACK = "rolled-back"
_TXN_NAME_RE = re.compile(r"^txn-(\d{4,})-(.+)\.json$")


class TransactionError(Exception):
    """The transaction journal is unusable — never guess a backup set instead."""


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


# --------------------------------------------------------------------------- #
# atomic primitives
# --------------------------------------------------------------------------- #

def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(directory: str) -> None:
    """Make a rename durable. Best effort: not every filesystem allows it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_bytes(target: str, data: bytes, mode: int) -> None:
    """Write ``target`` via temp-file + ``os.replace``, preserving symlinks.

    The lane re-reads the supervisor script on every spawn, so a target must
    never be observable half-written: a reader either opens the old inode or
    the new one.

    Symlinked targets are resolved first, mirroring ``utils.atomic_replace``
    (GitHub #16743): a plain ``os.replace`` would swap the symlink itself for a
    regular file and silently detach a managed deployment that links
    ``~/.hermes`` artifacts into a git-tracked profile package. This installer
    is deliberately stdlib-only — it has to run when the core contract, and so
    the checkout's ``utils``, is unimportable — so the semantics are replicated
    here rather than shared.
    """
    real = os.path.realpath(target) if os.path.islink(target) else target
    directory = os.path.dirname(real) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="." + os.path.basename(real) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        try:
            os.replace(tmp, real)
        except OSError as exc:
            # Cross-device, bind-mount and busy-file deployments cannot rename.
            if exc.errno not in (errno.EXDEV, errno.EBUSY):
                raise
            shutil.copyfile(tmp, real)
            os.chmod(real, mode)
            os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


# --------------------------------------------------------------------------- #
# transaction journal
# --------------------------------------------------------------------------- #

def _txn_dir(home: str) -> str:
    return os.path.join(home, TXN_DIR_REL)


def _backup_path(target: str, txn_id: str) -> str:
    return f"{target}.pre-{txn_id}"


def _allocate_txn(home: str, stamp: str) -> tuple[str, str]:
    """Reserve the next transaction id. Returns ``(txn_id, manifest_path)``.

    The id carries a monotonic sequence, not just the caller's stamp: two
    installs sharing a stamp must not share a manifest OR a backup filename, or
    the second would overwrite the first's record of the older generation —
    reintroducing exactly the generation-mixing this guards against.
    """
    directory = _txn_dir(home)
    os.makedirs(directory, exist_ok=True)
    seq = 0
    for name in os.listdir(directory):
        match = _TXN_NAME_RE.match(name)
        if match:
            seq = max(seq, int(match.group(1)))
    txn_id = f"{seq + 1:04d}-{stamp}"
    return txn_id, os.path.join(directory, f"txn-{txn_id}.json")


def _write_manifest(path: str, manifest: dict) -> None:
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode()
    _atomic_write_bytes(path, payload, 0o644)


def _load_manifest(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            manifest = json.loads(fh.read().decode())
    except OSError as exc:
        raise TransactionError(f"{path} is unreadable ({exc.strerror or exc})") from exc
    except ValueError as exc:
        raise TransactionError(f"{path} is not valid JSON ({exc})") from exc
    if not isinstance(manifest, dict):
        raise TransactionError(f"{path} is not a transaction manifest object")
    version = manifest.get("version")
    if version != TXN_VERSION:
        raise TransactionError(f"{path} has unsupported manifest version {version!r} "
                               f"(this installer writes {TXN_VERSION})")
    members = manifest.get("members")
    if not isinstance(members, list) or not members:
        raise TransactionError(f"{path} records no transaction members")
    for member in members:
        if not isinstance(member, dict):
            raise TransactionError(f"{path} has a malformed member entry")
        missing = [k for k in ("target", "mode", "existed") if k not in member]
        if missing:
            raise TransactionError(f"{path} member is missing {', '.join(missing)}")
    manifest["path"] = path
    return manifest


def _latest_open_transaction(home: str) -> dict | None:
    """The newest transaction that has not already been rolled back.

    A corrupt or unreadable newest manifest raises rather than silently falling
    through to an older one: that fallback would roll the host back to the wrong
    generation while reporting success.
    """
    directory = _txn_dir(home)
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    candidates = []
    for name in names:
        match = _TXN_NAME_RE.match(name)
        if match:
            candidates.append((int(match.group(1)), name))
    for _seq, name in sorted(candidates, reverse=True):
        manifest = _load_manifest(os.path.join(directory, name))
        if manifest.get("state") != TXN_ROLLED_BACK:
            return manifest
    return None


def _restore_transaction(manifest: dict, *, quiet: bool = False) -> list[str]:
    """Restore one transaction's exact pre-install set. Returns failure strings.

    Reverse manifest order for the same reason install order matters: the
    plugin modules that pass the newer flags go back first, so no window exists
    where an old supervisor is handed flags it cannot parse.

    Idempotent with respect to how far the install got: a pre-existing member is
    rewritten from its recorded backup whether or not it was reached, and a
    created member is removed whether or not it was written.
    """
    failures: list[str] = []
    for member in reversed(manifest["members"]):
        target = member["target"]
        if member["existed"]:
            backup = member.get("backup")
            if not backup or not os.path.isfile(backup):
                failures.append(f"{target}: recorded backup "
                                f"{backup or '(none)'} is missing")
                continue
            recorded = member.get("backup_sha256")
            if recorded and _sha256(backup) != recorded:
                failures.append(f"{target}: backup {backup} is corrupt "
                                f"(sha256 does not match the manifest)")
                continue
            try:
                with open(backup, "rb") as fh:
                    data = fh.read()
                os.makedirs(os.path.dirname(target), exist_ok=True)
                _atomic_write_bytes(target, data, int(member.get("restore_mode")
                                                      or member["mode"]))
            except OSError as exc:
                failures.append(f"{target}: restore failed ({exc.strerror or exc})")
                continue
            if not quiet:
                print(f"  restored        {target} <- {backup}")
        else:
            if os.path.lexists(target):
                try:
                    os.remove(target)
                except OSError as exc:
                    failures.append(f"{target}: could not remove the artifact this "
                                    f"transaction created ({exc.strerror or exc})")
                    continue
                if not quiet:
                    print(f"  removed         {target} (created by this transaction)")
            elif not quiet:
                print(f"  absent          {target} (created by this transaction)")
    # Directories this transaction created, deepest first, and only while empty.
    for directory in sorted(manifest.get("created_dirs") or [], key=len, reverse=True):
        try:
            os.rmdir(directory)
        except OSError:
            pass
    return failures


def _plan_destination(target: str) -> tuple[list[str], str | None]:
    """Preflight one destination. Returns ``(dirs_to_create, error)``."""
    directory = os.path.dirname(target)
    missing: list[str] = []
    probe = directory
    while probe and not os.path.exists(probe):
        missing.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if not os.path.isdir(probe):
        return ([], f"{probe} exists but is not a directory")
    if not os.access(probe, os.W_OK | os.X_OK):
        return ([], f"{probe} is not writable")
    if os.path.lexists(target):
        # isfile() follows symlinks, so a link to a real file is fine — the
        # write resolves it and the link survives. A dangling link, a link to a
        # directory, or a directory in the way is not.
        if not os.path.isfile(target):
            return ([], f"{target} exists but is not a regular file "
                        f"(or is a symlink that does not resolve to one)")
        if not os.access(target, os.R_OK):
            return ([], f"{target} is not readable, so it cannot be backed up")
        if not os.access(target, os.W_OK):
            return ([], f"{target} is not writable")
    return (list(reversed(missing)), None)


# --------------------------------------------------------------------------- #
# install / rollback
# --------------------------------------------------------------------------- #

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

    # ---- preflight: read every source and vet every destination BEFORE any
    # mutation, so the common failures never produce a half-installed host.
    plan: list[tuple[str, int, bytes, bool]] = []
    created_dirs: list[str] = []
    for source, target, mode in entries(home):
        if not os.path.isfile(source):
            print(f"missing versioned artifact: {source}", file=sys.stderr)
            return 1
        try:
            with open(source, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            print(f"unreadable versioned artifact {source}: {exc.strerror or exc}",
                  file=sys.stderr)
            return 1
        needed, error = _plan_destination(target)
        if error:
            print(f"refusing to install: {error}", file=sys.stderr)
            return 1
        for directory in needed:
            if directory not in created_dirs:
                created_dirs.append(directory)
        plan.append((target, mode, data, os.path.isfile(target)))

    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    try:
        txn_id, manifest_path = _allocate_txn(home, stamp)
    except OSError as exc:
        print(f"refusing to install: cannot write the transaction journal in "
              f"{_txn_dir(home)} ({exc.strerror or exc})", file=sys.stderr)
        return 1

    # ---- back up every pre-existing target up front. Backups are additive, so
    # a failure here still leaves the host on its current generation.
    members: list[dict] = []
    for target, mode, _data, existed in plan:
        member = {"target": target, "mode": mode, "existed": existed,
                  "backup": None, "backup_sha256": None, "restore_mode": None}
        if existed:
            backup = _backup_path(target, txn_id)
            try:
                shutil.copy2(target, backup)
                member["backup"] = backup
                member["backup_sha256"] = _sha256(backup)
                member["restore_mode"] = os.stat(target).st_mode & 0o7777
            except OSError as exc:
                print(f"refusing to install: cannot back up {target} "
                      f"({exc.strerror or exc}); nothing was changed", file=sys.stderr)
                return 1
            print(f"  backed up {target} -> {backup}")
        members.append(member)

    manifest = {"version": TXN_VERSION, "id": txn_id, "stamp": stamp, "home": home,
                "state": TXN_OPEN, "created_dirs": created_dirs, "members": members}
    try:
        _write_manifest(manifest_path, manifest)
    except OSError as exc:
        print(f"refusing to install: cannot record transaction {txn_id} "
              f"({exc.strerror or exc}); nothing was changed", file=sys.stderr)
        return 1

    # ---- mutate. Past this point every failure path restores the exact set the
    # manifest describes, or leaves the manifest open for --rollback to do it.
    try:
        for (target, mode, data, _existed) in plan:
            directory = os.path.dirname(target)
            if directory:
                os.makedirs(directory, exist_ok=True)
            _atomic_write_bytes(target, data, mode)
            print(f"  installed  {target}")
    except Exception as exc:
        print(f"install FAILED on transaction {txn_id}: "
              f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        failures = _restore_transaction(manifest)
        if failures:
            for failure in failures:
                print(f"  FAILED  {failure}", file=sys.stderr)
            print(f"automatic rollback of transaction {txn_id} is INCOMPLETE — the host "
                  f"is MIXED. Transaction {txn_id} is still open; fix the cause and run "
                  f"`python install.py --rollback` to finish it. Do NOT roll back the core "
                  f"until that succeeds.", file=sys.stderr)
            return 1
        try:
            _write_manifest(manifest_path, {**manifest, "state": TXN_ROLLED_BACK})
        except OSError:
            pass
        print(f"restored the exact pre-install set of transaction {txn_id}; "
              f"the host is unchanged", file=sys.stderr)
        return 1

    _write_manifest(manifest_path, {**manifest, "state": TXN_COMMITTED})
    created = sum(1 for m in members if not m["existed"])
    print(f"installed {len(members)} artifact(s) as transaction {txn_id} "
          f"({created} created, {len(members) - created} overwritten)")
    print("rollback with: python install.py --rollback")
    return 0


def rollback(home: str) -> int:
    """Undo ONE install transaction in full: restore what it overwrote, remove
    what it created.

    Never a per-target "newest backup" search. That search cannot see targets
    the transaction created (they have no backup) and, when a later install
    stopped part way, it pairs freshly-backed-up targets with other targets'
    older predecessors — a mixed runtime reported as a clean rollback.

    Repeated calls walk back one generation at a time: each rolled-back
    transaction is marked, so the next call takes the one before it.
    """
    try:
        manifest = _latest_open_transaction(home)
    except TransactionError as exc:
        print(f"cannot roll back: {exc}.\n"
              f"Refusing to guess a backup set — restore or remove the manifest in "
              f"{_txn_dir(home)} and re-run.", file=sys.stderr)
        return 1
    if manifest is None:
        print(f"cannot roll back: no open install transaction in {_txn_dir(home)}.\n"
              f"Either nothing was installed by this script, or its journal was removed. "
              f"Refusing to guess a backup set: independently restoring each target's "
              f"newest backup can mix generations.", file=sys.stderr)
        return 1

    members = manifest["members"]
    print(f"rolling back transaction {manifest.get('id')} "
          f"({len(members)} member(s), state {manifest.get('state')})")
    failures = _restore_transaction(manifest)
    if failures:
        for failure in failures:
            print(f"  FAILED  {failure}", file=sys.stderr)
        print(f"rollback INCOMPLETE: {len(failures)}/{len(members)} member(s) of "
              f"transaction {manifest.get('id')} could not be restored or removed. The "
              f"host is in a MIXED state and the core must NOT be rolled back.",
              file=sys.stderr)
        return 1

    try:
        _write_manifest(manifest["path"], {**{k: v for k, v in manifest.items()
                                              if k != "path"},
                                          "state": TXN_ROLLED_BACK})
    except OSError as exc:
        print(f"rolled back the artifacts, but could not mark transaction "
              f"{manifest.get('id')} rolled back ({exc.strerror or exc}); a repeat "
              f"--rollback would redo the same restore", file=sys.stderr)
        return 1
    restored = sum(1 for m in members if m["existed"])
    print(f"rolled back {len(members)} artifact(s) of transaction {manifest.get('id')}: "
          f"{restored} restored, {len(members) - restored} removed")
    print("core may now be rolled back safely (artifacts no longer require it)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report install state (default)")
    mode.add_argument("--install", action="store_true", help="install the versioned artifacts")
    mode.add_argument("--rollback", action="store_true",
                      help="undo the newest install transaction in full")
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
