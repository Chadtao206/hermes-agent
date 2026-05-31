"""Dashboard plugin API on the Postgres backend."""
import json


def test_pg_client_stats_resolves_postgres(pg_client):
    # /stats routes through the backend-aware _store(); a clean PG 'default'
    # board returns zeroed counts (proves the harness resolves Postgres).
    r = pg_client.get("/api/plugins/kanban/stats")
    assert r.status_code == 200
    body = r.json()
    assert "by_status" in body
    assert sum(body["by_status"].values()) == 0
