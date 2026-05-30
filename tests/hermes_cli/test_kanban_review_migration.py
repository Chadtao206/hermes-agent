from hermes_cli import kanban_db as kb


def test_migrate_moves_review_rows_to_ready(tmp_path):
    db = tmp_path / "kanban.db"
    conn = kb.connect(db_path=db, readonly=False, _bootstrap=True)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('r', 'x', 'review', 0)"
    )
    conn.commit()
    moved = kb.migrate_review_status_rows(conn)
    assert moved == 1
    assert conn.execute("SELECT status FROM tasks WHERE id='r'").fetchone()["status"] == "ready"
    # Idempotency: a second call finds no 'review' rows and returns 0.
    assert kb.migrate_review_status_rows(conn) == 0
