"""Import wiring for the source-controlled claude-kanban-lane plugin tests.

These tests exercise the VERSIONED ``launcher`` / ``wrapper`` / supervisor
under ``plugins/kanban/claude_lane`` (the files this change actually touches).
Those two modules import stable sibling infrastructure — ``brief``, ``cfgdir``,
``config``, ``envbuild``, ``modelmap``, ``router``, ``state`` — that is NOT part
of the versioned artifact set: it ships with, and only exists in, the installed
host plugin. So:

* the versioned ``plugin/`` dir goes on ``sys.path`` FIRST, so ``import
  launcher`` / ``import wrapper`` resolve to the copies under test (never the
  host's, which may be an older/reverted install); and
* the installed host plugin dir goes on next, purely to satisfy the unchanged
  siblings.

When the host plugin isn't installed (CI, a fresh checkout) the siblings can't
be resolved, so the suite is skipped rather than failing collection — the same
posture the old host-local tests took. The changed files' behaviour is still
covered host-free by ``tests/hermes_cli/test_kanban_supervisor_contract.py``,
which drives the versioned supervisor + shim end to end.
"""
import os
import sys

_VERSIONED_PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LANE_ROOT = os.path.dirname(_VERSIONED_PLUGIN)
_HOST_PLUGIN = os.path.expanduser("~/.hermes/plugins/claude-kanban-lane")
_SIBLINGS = ("brief", "cfgdir", "config", "envbuild", "modelmap", "router", "state")


def _host_has_siblings():
    if not os.path.isdir(_HOST_PLUGIN):
        return False
    return all(os.path.exists(os.path.join(_HOST_PLUGIN, f"{m}.py")) for m in _SIBLINGS)


if _host_has_siblings():
    # Versioned launcher/wrapper/supervisor_decide win; host supplies siblings.
    sys.path.insert(0, _HOST_PLUGIN)
    sys.path.insert(0, _VERSIONED_PLUGIN)
    # test_supervisor_decide imports the supervisor by path from this env var;
    # pin it at the VERSIONED script so the checkout copy is what's asserted.
    os.environ.setdefault(
        "CLAUDE_LANE_SUPERVISOR",
        os.path.join(_LANE_ROOT, "scripts", "claude_kanban_supervisor.py"),
    )
    os.environ.setdefault(
        "CLAUDE_LANE_SHIM",
        os.path.join(_LANE_ROOT, "scripts", "hermes-kanban-task"),
    )
else:
    # No installed host plugin -> the unchanged siblings can't be imported.
    # Skip the whole directory instead of erroring at collection.
    collect_ignore_glob = ["*"]
