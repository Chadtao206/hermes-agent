"""build_worker_context must be byte-identical across sqlite and postgres.

Seed an identical logical task graph into a sqlite store and a postgres store
and assert ``build_worker_context`` returns the same string. The only legitimate
cross-backend differences are non-deterministic identifiers and clocks:

* task ids (``t_<hex>``) — independently generated per backend,
* run-id integers embedded in the closeout-packet metadata JSON — separate
  autoincrement/sequence spaces,
* minute-resolution timestamps — both backends stamp ``int(time.time())`` and
  could straddle a minute boundary between the two ``_seed`` calls.

All three are normalized the SAME way on both sides (see ``_normalize``); the
remaining content must be identical. The parity assertion itself is NOT
weakened — it is an exact string equality after deterministic substitution.
"""
import re
from uuid import uuid4

from hermes_cli.kanban.store_sqlite import SqliteKanbanStore


def _seed(store):
    parent = store.create_task(title="parent impl", assignee="engineer",
                               body="parent body")
    store.set_status_direct(parent, "running")
    # SHA must be 7-40 hex chars to satisfy _looks_like_git_sha, so the
    # review child's "## Final-review PR-head gate" branch actually renders
    # (a 6-char value like "abc123" silently fails the gate on both sides and
    # would leave that high-value review-lane section untested).
    store.complete_task(parent, summary="parent done",
                        metadata={"pull_request_head_sha": "abc1234def",
                                  "pr_url": "http://x", "branch_name": "b"})
    child = store.create_task(title="review child", assignee="reviewer",
                              body="review body")
    store.link_tasks(parent, child)
    store.add_comment(child, author="ops", body="please review carefully")
    return parent, child


def _pg_store(dsn):
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    pool = pg_pool.make_pool(dsn)
    pg_pool.ensure_schema(pool)
    return PostgresKanbanStore(board=f"test_{uuid4().hex[:8]}", pool=pool), pool


_TASK_ID_RE = re.compile(r"t_[0-9a-f]{8}")
_RUN_ID_JSON_RE = re.compile(r'"run_id": \d+')
_RUN_ID_GATE_RE = re.compile(r"run `\d+`")
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")


def _normalize(text: str, parent: str, child: str) -> str:
    # Anchor the two real ids first so they map to stable, distinct tokens
    # regardless of the order they appear; then blanket-normalize any other
    # t_<hex> (defensive — there should be none) plus the two run-id renderings
    # (the JSON ``"run_id": N`` in closeout-packet metadata and the literal
    # ``run `N` `` in the PR-head-gate prose) and minute-resolution clocks.
    text = text.replace(parent, "<PARENT>").replace(child, "<CHILD>")
    text = _TASK_ID_RE.sub("<TID>", text)
    text = _RUN_ID_JSON_RE.sub('"run_id": <RID>', text)
    text = _RUN_ID_GATE_RE.sub("run `<RID>`", text)
    text = _TS_RE.sub("<TS>", text)
    return text


def test_build_worker_context_parity(tmp_path, monkeypatch, _pg_dsn):
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    from hermes_cli import kanban_db as kb
    kb.connect(db_path=db, readonly=False, _bootstrap=True).close()
    s_sqlite = SqliteKanbanStore(board=None)
    s_pg, pool = _pg_store(_pg_dsn)
    try:
        p1, c1 = _seed(s_sqlite)
        p2, c2 = _seed(s_pg)
        t1 = s_sqlite.build_worker_context(c1)
        t2 = s_pg.build_worker_context(c2)
        assert _normalize(t1, p1, c1) == _normalize(t2, p2, c2)
    finally:
        s_sqlite.close()
        s_pg.close()
        pool.close()
