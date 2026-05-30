from hermes_cli import kanban_db as kb


def test_review_not_a_valid_status():
    assert "review" not in kb.VALID_STATUSES


def test_reviewer_pr_head_closeout_gate_still_present():
    import inspect
    src = inspect.getsource(kb.complete_task)
    assert "pr head" in src.lower() or "pr_head" in src.lower()
