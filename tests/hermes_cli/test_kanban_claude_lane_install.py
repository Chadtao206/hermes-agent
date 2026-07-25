"""Behavioural coverage for the claude-kanban-lane host-artifact installer.

Blocker 3 was a rollout that updated the host-local supervisor/plugin files
BEFORE the core module they import was deployed, so every lane spawn died with
ModuleNotFoundError and the plugin suite stopped collecting. ``install.py``
makes that skew unreachable by accident; these tests drive its real check /
install / rollback logic against a temp home, with the runtime contract probe
stubbed so we exercise the ORDERING guarantees rather than this interpreter's
core.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest


_INSTALL = Path(__file__).resolve().parents[2] / "plugins/kanban/claude_lane/install.py"


def _load_installer():
    loader = importlib.machinery.SourceFileLoader("claude_lane_install", str(_INSTALL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def inst():
    return _load_installer()


@pytest.fixture
def contract_present(inst, monkeypatch):
    monkeypatch.setattr(inst, "runtime_contract_status", lambda python=None: (True, "stub: present"))
    return inst


@pytest.fixture
def contract_absent(inst, monkeypatch):
    monkeypatch.setattr(inst, "runtime_contract_status", lambda python=None: (False, "stub: absent"))
    return inst


def _installed_count(inst, home):
    return sum(
        1 for src, dst, _ in inst.entries(str(home))
        if inst.artifact_state(src, dst) == inst.STATE_INSTALLED
    )


def _targets(inst, home):
    return [Path(dst) for _src, dst, _mode in inst.entries(str(home))]


def _seed_generation(targets, tag):
    """Give every target the same coherent generation of content."""
    for index, target in enumerate(targets):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{tag}-{index}\n")


def _generation(targets):
    return [t.read_text() if t.exists() else None for t in targets]


def _expected_generation(tag, count):
    return [f"{tag}-{index}\n" for index in range(count)]


def _manifest_paths(inst, home):
    return sorted(Path(inst._txn_dir(str(home))).glob("txn-*.json"))


def _fail_on_target(inst, monkeypatch, victim, exc):
    """Make the first write of ``victim`` blow up, then behave normally.

    Failing only once lets the same seam serve the install attempt and the
    rollback that follows it, so a test can assert on the restored state rather
    than on a permanently broken filesystem.
    """
    real = inst._atomic_write_bytes
    fired = {"count": 0}

    def flaky(target, data, mode):
        if target == str(victim) and not fired["count"]:
            fired["count"] += 1
            raise exc
        return real(target, data, mode)

    monkeypatch.setattr(inst, "_atomic_write_bytes", flaky)
    return fired


def test_check_reports_staged_when_nothing_installed(contract_present, tmp_path, capsys):
    """The coherent pre-deploy state: the host runs the previous artifacts, the
    versioned copies are staged. Exit 3, not an error."""
    rc = contract_present.check(str(tmp_path))
    out = capsys.readouterr().out
    assert rc == 3
    assert "staged, not installed" in out


def test_install_then_check_is_fully_installed(contract_present, tmp_path, capsys):
    assert contract_present.install(str(tmp_path), stamp="s1") == 0
    # Every artifact now matches its versioned source.
    assert _installed_count(contract_present, tmp_path) == len(contract_present.MANIFEST)
    rc = contract_present.check(str(tmp_path))
    assert rc == 0
    assert "fully installed" in capsys.readouterr().out


def test_install_refuses_without_the_core_contract(contract_absent, tmp_path, capsys):
    """The ordering guarantee: artifacts must not land before the core they
    import. A refused install writes nothing."""
    rc = contract_absent.install(str(tmp_path))
    assert rc == 1
    assert "refusing to install" in capsys.readouterr().err
    assert _installed_count(contract_absent, tmp_path) == 0


def test_force_installs_even_without_the_contract(contract_absent, tmp_path):
    """--force is the deliberate staging escape hatch; the supervisor still
    stands down at runtime until the core lands, but the files are placed."""
    assert contract_absent.install(str(tmp_path), force=True, stamp="s1") == 0
    assert _installed_count(contract_absent, tmp_path) == len(contract_absent.MANIFEST)


def test_check_flags_a_partial_install_as_an_error(contract_present, tmp_path, capsys):
    """The mixed state that actually broke the lane. Install everything, then
    delete ONE target: check must call it out and return 1, not silently pass."""
    contract_present.install(str(tmp_path), stamp="s1")
    first_target = os.path.join(str(tmp_path), contract_present.MANIFEST[0][1])
    os.remove(first_target)
    rc = contract_present.check(str(tmp_path))
    assert rc == 1
    assert "PARTIAL INSTALL" in capsys.readouterr().out


def test_check_flags_incoherent_when_artifacts_present_but_core_absent(
    contract_absent, tmp_path, capsys,
):
    """Artifacts fully installed but the runtime core lacks the contract (a
    core rollback under installed artifacts). Fully-installed count, but the
    verdict is INCOHERENT and the exit code is nonzero."""
    # Place the files with the contract "present", then flip the probe to absent.
    contract_absent.install(str(tmp_path), force=True, stamp="s1")
    rc = contract_absent.check(str(tmp_path))
    assert rc == 1
    assert "INCOHERENT" in capsys.readouterr().out


def test_install_backs_up_then_rollback_restores(contract_present, tmp_path):
    """Every overwrite is backed up, and rolling the transaction back recovers
    the exact prior bytes of a target that pre-existed (while the eight targets
    the same transaction created are removed)."""
    target_rel = contract_present.MANIFEST[0][1]
    target = Path(str(tmp_path)) / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ORIGINAL HOST ARTIFACT\n")

    assert contract_present.install(str(tmp_path), stamp="s1") == 0
    # The versioned copy now overwrites the original.
    assert target.read_text() != "ORIGINAL HOST ARTIFACT\n"
    # A backup of the original was taken.
    backups = list(target.parent.glob(target.name + ".pre-*"))
    assert backups and backups[0].read_text() == "ORIGINAL HOST ARTIFACT\n"

    assert contract_present.rollback(str(tmp_path)) == 0
    assert target.read_text() == "ORIGINAL HOST ARTIFACT\n"


# --------------------------------------------------------------------------- #
# Transaction coherence.
#
# A rollback that independently restores each target's lexicographically newest
# .pre-* backup is not safe, and both failure modes were reachable: a first
# install creates targets that have no backup at all (so they survived a
# "successful" rollback), and an install that stopped part way left newer
# backups only on the targets it reached (so a rollback paired those with other
# targets' older predecessors). These tests pin the transaction that replaced
# it: one generation, all members, restore-or-remove, nonzero on any gap.
# --------------------------------------------------------------------------- #

def test_first_install_rollback_removes_every_created_target(
    contract_present, tmp_path, capsys,
):
    """The 9/9-survivors case. Nothing pre-existed, so a coherent rollback means
    DELETING every artifact the transaction created, and check() must land back
    on the pre-install state."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    assert inst.install(str(tmp_path), stamp="s1") == 0
    assert all(t.exists() for t in targets)

    rc = inst.rollback(str(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    survivors = [t for t in targets if t.exists()]
    assert survivors == [], f"rollback left {len(survivors)} created artifact(s) installed"
    assert f"{len(targets)} removed" in out
    # The pre-install generation was "nothing installed" — check must say so.
    assert inst.check(str(tmp_path)) == 3
    assert "staged, not installed" in capsys.readouterr().out


def test_second_install_rollback_restores_the_whole_previous_generation(
    contract_present, tmp_path, capsys,
):
    """Every target pre-existed as one coherent generation A; installing B and
    rolling back must return ALL of them to A, not just the ones a per-file
    search happened to find a backup for."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    _seed_generation(targets, "GEN-A")

    assert inst.install(str(tmp_path), stamp="s1") == 0
    assert _installed_count(inst, tmp_path) == len(inst.MANIFEST)

    assert inst.rollback(str(tmp_path)) == 0
    assert _generation(targets) == _expected_generation("GEN-A", len(targets))
    assert f"{len(targets)} restored" in capsys.readouterr().out
    # A is not the versioned source, so the host is coherently back to "not installed".
    assert _installed_count(inst, tmp_path) == 0


def test_partial_install_failure_auto_restores_generation_a(
    contract_present, tmp_path, monkeypatch, capsys,
):
    """A write failure part way through install B leaves the host on A exactly —
    automatically, without waiting for anyone to run --rollback."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    _seed_generation(targets, "GEN-A")
    victim = targets[2]
    fired = _fail_on_target(inst, monkeypatch, victim,
                            OSError(28, "No space left on device"))

    rc = inst.install(str(tmp_path), stamp="s1")
    err = capsys.readouterr().err
    assert rc == 1
    assert fired["count"] == 1
    assert "install FAILED" in err
    assert "restored the exact pre-install set" in err
    # EVERY target — the two already overwritten, the victim, and the six never
    # reached — is generation A. No mixing.
    assert _generation(targets) == _expected_generation("GEN-A", len(targets))
    assert _installed_count(inst, tmp_path) == 0
    # The transaction is closed, so a later --rollback does not re-undo it.
    manifest = json.loads(_manifest_paths(inst, tmp_path)[-1].read_text())
    assert manifest["state"] == inst.TXN_ROLLED_BACK


def test_partial_install_failure_on_a_first_install_removes_the_written_targets(
    contract_present, tmp_path, monkeypatch, capsys,
):
    """The same failure with nothing pre-existing: the artifacts written before
    the fault must be removed, not left behind as a partial install."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    _fail_on_target(inst, monkeypatch, targets[2],
                    OSError(13, "Permission denied"))

    assert inst.install(str(tmp_path), stamp="s1") == 1
    capsys.readouterr()
    assert _generation(targets) == [None] * len(targets)
    assert inst.check(str(tmp_path)) == 3
    assert "staged, not installed" in capsys.readouterr().out
    # Nothing half-written was left lying around either.
    assert list(tmp_path.rglob("*.tmp")) == []


def test_interrupted_install_leaves_a_deterministic_rollback(
    contract_present, tmp_path, monkeypatch, capsys,
):
    """A hard interruption (SIGINT) escapes the automatic restore. The durable
    manifest must still let --rollback put every target back on generation A —
    including the targets the interrupted install never touched, which is the
    case a per-file newest-backup search got wrong."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    _seed_generation(targets, "LEGACY")
    # Transaction 1: a completed earlier install, whose backups hold LEGACY.
    assert inst.install(str(tmp_path), stamp="s1") == 0
    # The host then moves to generation A by some other means.
    _seed_generation(targets, "GEN-A")

    _fail_on_target(inst, monkeypatch, targets[2], KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        inst.install(str(tmp_path), stamp="s2")
    capsys.readouterr()
    # Interrupted mid-write: the first two targets are the new generation, the
    # rest are still A, and the transaction is still open.
    assert _generation(targets)[3:] == _expected_generation("GEN-A", len(targets))[3:]
    manifests = _manifest_paths(inst, tmp_path)
    assert json.loads(manifests[-1].read_text())["state"] == inst.TXN_OPEN
    # Older LEGACY backups are still on disk; the rollback must ignore them.
    assert list(targets[-1].parent.glob(targets[-1].name + ".pre-0001-s1"))

    assert inst.rollback(str(tmp_path)) == 0
    assert _generation(targets) == _expected_generation("GEN-A", len(targets))


def test_rollback_refuses_a_mixed_backup_layout_with_no_transaction_journal(
    contract_present, tmp_path, capsys,
):
    """The reported repro's exact on-disk shape: LEGACY backups everywhere plus a
    newer backup on the first two targets only, and no journal. The old code
    restored A for those two and LEGACY for the other seven while printing that
    the core could be rolled back. Refusing is the only coherent answer."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    _seed_generation(targets, "GEN-A")
    for index, target in enumerate(targets):
        Path(f"{target}.pre-20260725-010000").write_text(f"LEGACY-{index}\n")
    for index, target in enumerate(targets[:2]):
        Path(f"{target}.pre-20260725-020000").write_text(f"GEN-A-{index}\n")
        target.write_text(f"GEN-B-{index}\n")
    before = _generation(targets)

    rc = inst.rollback(str(tmp_path))
    captured = capsys.readouterr()
    assert rc == 1
    assert "no open install transaction" in captured.err
    assert "core may now be rolled back safely" not in captured.out
    # Refused means refused: not one byte moved, so no mixed generation exists.
    assert _generation(targets) == before


def test_rollback_without_any_transaction_journal_fails(contract_present, tmp_path, capsys):
    rc = contract_present.rollback(str(tmp_path))
    captured = capsys.readouterr()
    assert rc == 1
    assert "Refusing to guess a backup set" in captured.err
    assert "core may now be rolled back safely" not in captured.out


@pytest.mark.parametrize("corruption", ["not json at all", "{}", '{"version": 99}'])
def test_rollback_with_corrupt_transaction_metadata_fails_and_changes_nothing(
    contract_present, tmp_path, capsys, corruption,
):
    """Corrupt metadata must be an error, never a silent fall-through to the
    previous transaction — that would roll the host back a generation too far."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    _seed_generation(targets, "GEN-A")
    assert inst.install(str(tmp_path), stamp="s1") == 0
    installed = _generation(targets)
    _manifest_paths(inst, tmp_path)[-1].write_text(corruption)

    rc = inst.rollback(str(tmp_path))
    captured = capsys.readouterr()
    assert rc == 1
    assert "cannot roll back" in captured.err
    assert "core may now be rolled back safely" not in captured.out
    assert _generation(targets) == installed


def test_rollback_reports_failure_when_a_recorded_backup_is_missing(
    contract_present, tmp_path, capsys,
):
    """One unrestorable member makes the whole rollback nonzero, and it must not
    claim the core is safe to roll back — the host is mixed."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    _seed_generation(targets, "GEN-A")
    assert inst.install(str(tmp_path), stamp="s1") == 0
    os.remove(str(targets[0]) + ".pre-0001-s1")

    rc = inst.rollback(str(tmp_path))
    captured = capsys.readouterr()
    assert rc == 1
    assert "recorded backup" in captured.err
    assert "rollback INCOMPLETE: 1/" in captured.err
    assert "core may now be rolled back safely" not in captured.out
    # The transaction stays open so the gap can be fixed and the rollback retried.
    assert json.loads(_manifest_paths(inst, tmp_path)[-1].read_text())["state"] != \
        inst.TXN_ROLLED_BACK


def test_rollback_detects_a_corrupted_backup_instead_of_restoring_it(
    contract_present, tmp_path, capsys,
):
    """Restoring truncated bytes over a live supervisor script is worse than
    failing loudly, so the manifest records each backup's digest."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    _seed_generation(targets, "GEN-A")
    assert inst.install(str(tmp_path), stamp="s1") == 0
    Path(str(targets[-1]) + ".pre-0001-s1").write_text("truncated\n")

    rc = inst.rollback(str(tmp_path))
    captured = capsys.readouterr()
    assert rc == 1
    assert "is corrupt (sha256" in captured.err
    assert "core may now be rolled back safely" not in captured.out


def test_repeated_rollback_walks_back_one_generation_at_a_time(
    contract_present, tmp_path,
):
    """Each transaction is marked once rolled back, so a second --rollback takes
    the transaction before it rather than redoing the same one."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    _seed_generation(targets, "LEGACY")
    assert inst.install(str(tmp_path), stamp="s1") == 0
    _seed_generation(targets, "GEN-A")
    assert inst.install(str(tmp_path), stamp="s2") == 0

    assert inst.rollback(str(tmp_path)) == 0
    assert _generation(targets) == _expected_generation("GEN-A", len(targets))
    assert inst.rollback(str(tmp_path)) == 0
    assert _generation(targets) == _expected_generation("LEGACY", len(targets))
    assert inst.rollback(str(tmp_path)) == 1


def test_installs_sharing_a_stamp_get_distinct_transactions_and_backups(
    contract_present, tmp_path,
):
    """Two transactions must never share a manifest or a backup filename: the
    second would overwrite the first's record of the older generation, which is
    how generations got mixed in the first place."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    _seed_generation(targets, "LEGACY")
    assert inst.install(str(tmp_path), stamp="same") == 0
    _seed_generation(targets, "GEN-A")
    assert inst.install(str(tmp_path), stamp="same") == 0

    assert len(_manifest_paths(inst, tmp_path)) == 2
    backups = sorted(p.name for p in targets[0].parent.glob(targets[0].name + ".pre-*"))
    assert backups == [f"{targets[0].name}.pre-0001-same",
                       f"{targets[0].name}.pre-0002-same"]
    # And the newest transaction is the one that gets undone.
    assert inst.rollback(str(tmp_path)) == 0
    assert _generation(targets) == _expected_generation("GEN-A", len(targets))


def test_install_preflights_destinations_before_mutating_anything(
    contract_present, tmp_path, capsys,
):
    """A destination that cannot be written is caught before the first write, so
    the earlier manifest entries are never installed."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    # The LAST target's path is occupied by a directory: unwritable as a file.
    targets[-1].parent.mkdir(parents=True, exist_ok=True)
    targets[-1].mkdir()

    rc = inst.install(str(tmp_path), stamp="s1")
    assert rc == 1
    assert "refusing to install" in capsys.readouterr().err
    # Not even the first artifact landed, and no transaction was opened.
    assert _installed_count(inst, tmp_path) == 0
    assert _manifest_paths(inst, tmp_path) == []


def test_install_refuses_a_missing_versioned_source_before_mutating(
    contract_present, tmp_path, monkeypatch, capsys,
):
    inst = contract_present
    monkeypatch.setattr(inst, "MANIFEST", inst.MANIFEST + (
        ("plugin/does-not-exist.py", ".hermes/plugins/claude-kanban-lane/nope.py", 0o644),
    ))
    rc = inst.install(str(tmp_path), stamp="s1")
    assert rc == 1
    assert "missing versioned artifact" in capsys.readouterr().err
    assert _installed_count(inst, tmp_path) == 0
    assert _manifest_paths(inst, tmp_path) == []


def test_install_writes_targets_atomically(contract_present, tmp_path, monkeypatch):
    """The supervisor script is re-read on every lane spawn, so a target must be
    replaced by rename — never opened for truncating writes in place."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    _seed_generation(targets, "GEN-A")
    original_inodes = {t: t.stat().st_ino for t in targets}

    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace",
                        lambda src, dst, *a, **k: (replaced.append(dst),
                                                   real_replace(src, dst, *a, **k))[1])
    assert inst.install(str(tmp_path), stamp="s1") == 0

    for target in targets:
        assert str(target) in replaced, f"{target} was not installed via os.replace"
        assert target.stat().st_ino != original_inodes[target]
    assert list(tmp_path.rglob("*.tmp")) == []


def test_install_and_rollback_preserve_a_symlinked_target(contract_present, tmp_path):
    """GitHub #16743's invariant, applied to the host artifacts: a managed
    deployment may symlink a `~/.hermes` artifact into a git-tracked profile
    package. Installing must write THROUGH the link, and so must the rollback —
    a plain os.replace would swap the link for a regular file and detach it."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    _seed_generation(targets, "GEN-A")
    # Redirect the supervisor script at a real file living elsewhere.
    profile = tmp_path / "profile-package"
    profile.mkdir()
    real = profile / "claude_kanban_supervisor.py"
    real.write_text("GEN-A-0\n")
    targets[0].unlink()
    targets[0].symlink_to(real)

    assert inst.install(str(tmp_path), stamp="s1") == 0
    assert targets[0].is_symlink(), "install detached the symlink"
    assert real.read_text() == targets[0].read_text() != "GEN-A-0\n"

    assert inst.rollback(str(tmp_path)) == 0
    assert targets[0].is_symlink(), "rollback detached the symlink"
    assert real.read_text() == "GEN-A-0\n"
    assert _generation(targets) == _expected_generation("GEN-A", len(targets))


def test_install_refuses_a_dangling_symlinked_target(contract_present, tmp_path, capsys):
    """A link that resolves to nothing cannot be backed up, so it is a preflight
    refusal rather than a half-installed host."""
    inst = contract_present
    targets = _targets(inst, tmp_path)
    targets[0].parent.mkdir(parents=True, exist_ok=True)
    targets[0].symlink_to(tmp_path / "nowhere")

    assert inst.install(str(tmp_path), stamp="s1") == 1
    assert "not a regular file" in capsys.readouterr().err
    assert _installed_count(inst, tmp_path) == 0
    assert _manifest_paths(inst, tmp_path) == []


def test_manifest_orders_scripts_before_plugin_modules(inst):
    """Install order (list order) matters: the supervisor script and shim,
    re-read per spawn, must land BEFORE the launcher/wrapper that pass them new
    flags — new-launcher + old-supervisor is the unsafe direction."""
    targets = [dst for _src, dst, _mode in inst.MANIFEST]
    sup_idx = next(i for i, t in enumerate(targets) if t.endswith("claude_kanban_supervisor.py"))
    launcher_idx = next(i for i, t in enumerate(targets) if t.endswith("launcher.py"))
    wrapper_idx = next(i for i, t in enumerate(targets) if t.endswith("wrapper.py"))
    assert sup_idx < launcher_idx
    assert sup_idx < wrapper_idx


def test_every_manifest_source_exists(inst):
    """A missing versioned artifact would make ``install`` fail mid-way; catch
    a dropped file at test time instead."""
    for src, _dst, _mode in inst.entries(os.path.expanduser("~")):
        assert os.path.exists(src), f"missing versioned artifact: {src}"
