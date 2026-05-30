import inspect
from hermes_cli import kanban_db as kb


def test_claim_review_task_removed():
    assert not hasattr(kb, "claim_review_task")


def test_dispatch_once_has_no_review_query():
    assert "status = 'review'" not in inspect.getsource(kb.dispatch_once)
