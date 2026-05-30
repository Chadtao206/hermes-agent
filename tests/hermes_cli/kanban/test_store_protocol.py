from hermes_cli.kanban.store import KanbanStore


def test_protocol_lists_core_methods():
    # The interface must expose the data operations callers depend on.
    for name in (
        "create_task", "get_task", "list_tasks", "complete_task", "block_task",
        "unblock_task", "schedule_task", "archive_task", "assign_task",
        "reassign_task", "reclaim_task", "set_status_direct", "set_task_priority",
        "edit_task_fields", "delete_task", "link_tasks", "unlink_tasks",
        "add_comment", "add_notify_sub", "remove_notify_sub", "list_notify_subs",
        "claim_unseen_events_for_sub", "add_profile_event_sub",
        "remove_profile_event_sub", "list_profile_event_subs",
        "recompute_ready", "has_spawnable_ready", "close",
    ):
        assert hasattr(KanbanStore, name), name
