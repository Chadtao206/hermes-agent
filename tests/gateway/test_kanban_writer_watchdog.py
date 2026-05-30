import gateway.run as gr


def test_corruption_error_does_not_permanently_disable_under_recovery(monkeypatch):
    monkeypatch.setattr(gr, "_writer_auto_recovery_enabled", lambda: True, raising=False)
    disabled = {}
    decision = gr._classify_board_write_error(
        "database disk image is malformed", disabled_set=disabled, db_path="/x/kanban.db")
    assert decision == "backoff_retry"
    assert "/x/kanban.db" not in disabled  # NOT permanently disabled


def test_corruption_error_disables_when_recovery_off(monkeypatch):
    monkeypatch.setattr(gr, "_writer_auto_recovery_enabled", lambda: False, raising=False)
    disabled = {}
    decision = gr._classify_board_write_error(
        "database disk image is malformed", disabled_set=disabled, db_path="/x/kanban.db")
    assert decision == "disable"


def test_non_corruption_error_is_not_corruption_classified(monkeypatch):
    monkeypatch.setattr(gr, "_writer_auto_recovery_enabled", lambda: True, raising=False)
    disabled = {}
    decision = gr._classify_board_write_error(
        "some transient connection blip", disabled_set=disabled, db_path="/x/kanban.db")
    # Non-corruption errors are not the daemon's recovery domain; the caller
    # keeps its existing (non-disable) handling — classifier reports "other".
    assert decision == "other"
    assert "/x/kanban.db" not in disabled
