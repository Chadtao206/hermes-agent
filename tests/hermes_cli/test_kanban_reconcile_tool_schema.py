"""WS5 Task 3: kanban_reconcile is registered as an orchestrator-gated kanban
tool with the right schema, and is a member of the kanban toolset declaration."""
import tools.kanban_tools as kt
from tools.registry import registry


def test_reconcile_tool_registered_orchestrator_gated():
    assert "kanban_reconcile" in registry.get_tool_names_for_toolset("kanban")
    entry = registry.get_entry("kanban_reconcile")
    assert entry is not None
    assert entry.handler is kt._handle_reconcile
    assert entry.check_fn is kt._check_kanban_orchestrator_mode


def test_reconcile_tool_schema_action_enum():
    entry = registry.get_entry("kanban_reconcile")
    props = entry.schema["parameters"]["properties"]
    assert props["action"]["enum"] == ["list", "apply"]
    for field in ("task_id", "option", "packet_signature"):
        assert field in props


def test_reconcile_in_kanban_toolset_declaration():
    import toolsets
    tools = toolsets.TOOLSETS["kanban"]["tools"]
    assert "kanban_reconcile" in tools
