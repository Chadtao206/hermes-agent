"""Fork-owned kanban store package: backend-agnostic interface over the board DB."""
from .store import KanbanStore, kanban_store, resolve_backend  # noqa: F401

# ---------------------------------------------------------------------------
# Re-export the CLI surface from hermes_cli/kanban.py so callers that do
#   from hermes_cli.kanban import kanban_command, build_parser, run_slash
# or
#   from hermes_cli import kanban as kc; kc.run_slash(...)
# continue to work even though the hermes_cli/kanban/ package now shadows
# the hermes_cli/kanban.py module.
#
# We load kanban.py under the name ``hermes_cli._kanban_cli`` so Python
# does not confuse it with this package, then pull the public CLI names
# into this package's namespace.
# ---------------------------------------------------------------------------
import importlib.util as _ilu
import os as _os
import sys as _sys

_CLI_PATH = _os.path.normpath(
    _os.path.join(_os.path.dirname(__file__), "..", "kanban.py")
)
_CLI_MOD_NAME = "hermes_cli._kanban_cli"

if _CLI_MOD_NAME not in _sys.modules:
    _spec = _ilu.spec_from_file_location(_CLI_MOD_NAME, _CLI_PATH)
    _cli_mod = _ilu.module_from_spec(_spec)
    _sys.modules[_CLI_MOD_NAME] = _cli_mod
    _spec.loader.exec_module(_cli_mod)
else:
    _cli_mod = _sys.modules[_CLI_MOD_NAME]

# Public CLI API (used by hermes_cli/main.py)
kanban_command = _cli_mod.kanban_command  # noqa: F401
build_parser = _cli_mod.build_parser      # noqa: F401
run_slash = _cli_mod.run_slash            # noqa: F401

# Command handlers (used by tests)
_cmd_create = _cli_mod._cmd_create        # noqa: F401
_cmd_complete = _cli_mod._cmd_complete    # noqa: F401
_cmd_block = _cli_mod._cmd_block          # noqa: F401
_cmd_unblock = _cli_mod._cmd_unblock      # noqa: F401
_cmd_schedule = _cli_mod._cmd_schedule    # noqa: F401
_cmd_assign = _cli_mod._cmd_assign        # noqa: F401
_cmd_reassign = _cli_mod._cmd_reassign    # noqa: F401
_cmd_reclaim = _cli_mod._cmd_reclaim      # noqa: F401
_cmd_comment = _cli_mod._cmd_comment      # noqa: F401
_cmd_link = _cli_mod._cmd_link            # noqa: F401
_cmd_unlink = _cli_mod._cmd_unlink        # noqa: F401
_cmd_archive = _cli_mod._cmd_archive      # noqa: F401
_cmd_edit = _cli_mod._cmd_edit            # noqa: F401
_cmd_show = _cli_mod._cmd_show            # noqa: F401
_cmd_list = _cli_mod._cmd_list            # noqa: F401
_cmd_runs = _cli_mod._cmd_runs            # noqa: F401
_cmd_stats = _cli_mod._cmd_stats          # noqa: F401
_cmd_assignees = _cli_mod._cmd_assignees  # noqa: F401
_cmd_heartbeat = _cli_mod._cmd_heartbeat  # noqa: F401
_cmd_promote = _cli_mod._cmd_promote      # noqa: F401
_cmd_gc = _cli_mod._cmd_gc                # noqa: F401

# Other helpers tests may reference
_parse_workspace_flag = _cli_mod._parse_workspace_flag              # noqa: F401
_parse_branch_flag = _cli_mod._parse_branch_flag                    # noqa: F401
_parse_event_kinds_flag = _cli_mod._parse_event_kinds_flag          # noqa: F401
_parse_profile_sub_id = _cli_mod._parse_profile_sub_id              # noqa: F401
_format_profile_sub_id = _cli_mod._format_profile_sub_id            # noqa: F401
_check_dispatcher_presence = _cli_mod._check_dispatcher_presence    # noqa: F401
_LOG_TASK_CLOSEOUT_SCRIPT = _cli_mod._LOG_TASK_CLOSEOUT_SCRIPT      # noqa: F401
_cmd_closeout = _cli_mod._cmd_closeout                              # noqa: F401
_WAKE_ARM_EVENT_KINDS = _cli_mod._WAKE_ARM_EVENT_KINDS              # noqa: F401
_WAKE_ARM_PROMPT = _cli_mod._WAKE_ARM_PROMPT                        # noqa: F401
