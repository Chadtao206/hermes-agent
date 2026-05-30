"""Fork-owned kanban package: backend-agnostic store interface + the kanban CLI.

Historically the CLI lived at ``hermes_cli/kanban.py``. The store interface
(Phase 1 of the Postgres migration) needed a package, which would have shadowed
that module — so the CLI now lives at ``hermes_cli/kanban/cli.py`` and this
package re-exports its full public surface for backward compatibility. Callers
that did ``from hermes_cli.kanban import run_slash`` / ``build_parser`` /
``kanban_command`` / ``_check_dispatcher_presence`` continue to work unchanged.
"""
import sys as _sys

# Import the CLI submodule and mirror its entire namespace onto this package,
# so every name the old hermes_cli/kanban.py module exposed is still reachable
# as hermes_cli.kanban.<name>.
from . import cli as _cli  # noqa: E402

_self = _sys.modules[__name__]
for _name in dir(_cli):
    if not _name.startswith("__"):
        setattr(_self, _name, getattr(_cli, _name))
del _name

# Canonical store exports win for these names (imported last on purpose).
from .store import KanbanStore, kanban_store, resolve_backend  # noqa: F401,E402
