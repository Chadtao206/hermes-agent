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
    """Every overwrite is backed up; rollback restores the newest backup. A
    second install over a modified target must be recoverable to the exact
    prior bytes."""
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
