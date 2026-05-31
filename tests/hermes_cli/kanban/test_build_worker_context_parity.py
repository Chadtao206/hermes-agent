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

The seed is deliberately *rich* so the compared contexts exercise the three
sections that the original review-child-only comparison never reached:

1. ``## Implementation PR evidence`` — renders only when the SHOWN task's own
   assignee lane is "implementation"; covered by also comparing the ENGINEER
   PARENT's context.
2. ``## Prior attempts on this task`` — renders only when the shown task has
   closed (ended) runs; the parent and child each get a prior failed run plus
   (for the parent) its completion run.
3. ``## Recent work by @assignee`` — the cross-task role-history raw SQL (the
   only hand-written PG query, hence the riskiest); renders only when the
   assignee has OTHER completed runs on OTHER tasks, so each compared task gets
   a same-assignee sibling task with exactly one completed run.

Exactly ONE qualifying sibling run per assignee is seeded so the
``ORDER BY ended_at DESC`` in the role-history query has no tie to break
non-deterministically across the two backends.
"""
import re
from uuid import uuid4

from hermes_cli.kanban.store_sqlite import SqliteKanbanStore


def _completed_run(store, *, title, assignee, summary, metadata=None):
    """Create a task, run it once, and complete it.

    Returns the task id. ``create_task`` yields a parent-less ``ready`` task on
    both backends (see conformance ``test_claim_task_atomic``); ``claim_task``
    opens a run row whose ``profile`` is the assignee, and ``complete_task``
    closes it with ``outcome='completed'`` — exactly the shape the cross-task
    ``## Recent work by @assignee`` query selects on.
    """
    tid = store.create_task(title=title, assignee=assignee, body=f"{title} body")
    assert store.claim_task(tid, claimer="w1") is not None
    assert store.complete_task(tid, summary=summary, metadata=metadata) is True
    return tid


def _failed_attempt(store, task_id, *, assignee, error):
    """Drive ``task_id`` through one claimed-then-failed run, leaving it ready.

    ``failure_limit=3`` keeps the single failure under the breaker threshold so
    the task returns to ``ready`` (a retry) with one ENDED, non-completed run —
    i.e. a "prior attempt" that is NOT itself a completion (so it shows under
    ``## Prior attempts`` but is excluded from the completed-only role history).
    """
    assert store.claim_task(task_id, claimer="w1") is not None
    blocked = store.record_task_failure(
        task_id, error, outcome="crashed", failure_limit=3,
        release_claim=True, end_run=True)
    assert blocked is False  # under threshold => retry, run ended, task ready


def _seed(store):
    """Seed a rich task graph and return ``(parent, child)``.

    Layout::

        parent (engineer, implementation lane)
          ├─ one failed attempt  -> ## Prior attempts
          └─ one completion w/ PR -> ## Prior attempts + ## Implementation PR
        child  (reviewer, review lane), depends on parent
          └─ one failed attempt  -> ## Prior attempts + ## Final-review gate
        sibling_eng (engineer)  one completion -> parent's ## Recent work
        sibling_rev (reviewer)  one completion -> child's  ## Recent work
    """
    # --- engineer sibling: one completed run feeds parent's role history ---
    _completed_run(store, title="other engineer task", assignee="engineer",
                   summary="prior engineer work on an unrelated card")

    # --- parent: a prior failed attempt, then a PR-bearing completion ---
    parent = store.create_task(title="parent impl", assignee="engineer",
                               body="parent body")
    _failed_attempt(store, parent, assignee="engineer",
                    error="first parent attempt crashed")
    # SHA must be 7-40 hex chars to satisfy _looks_like_git_sha, so the
    # review child's "## Final-review PR-head gate" branch actually renders
    # (a 6-char value like "abc123" silently fails the gate on both sides and
    # would leave that high-value review-lane section untested).
    assert store.claim_task(parent, claimer="w1") is not None
    store.complete_task(parent, summary="parent done",
                        metadata={"pull_request_head_sha": "abc1234def",
                                  "pr_url": "http://x", "branch_name": "b"})

    # --- reviewer sibling: one completed run feeds child's role history ---
    _completed_run(store, title="other reviewer task", assignee="reviewer",
                   summary="prior reviewer work on an unrelated card")

    # --- child: review lane, depends on the (now done) parent ---
    child = store.create_task(title="review child", assignee="reviewer",
                              body="review body")
    store.link_tasks(parent, child)
    # Parent is done, so the child is dispatchable; give it one prior failed
    # attempt so ## Prior attempts renders for the review-lane context too.
    store.set_status_direct(child, "ready")
    _failed_attempt(store, child, assignee="reviewer",
                    error="first review attempt crashed")
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
    # t_<hex> (the engineer/reviewer SIBLING task ids rendered in the
    # ## Recent work role-history lines — same count and structure on both
    # backends) plus the two run-id renderings (the JSON ``"run_id": N`` in
    # closeout-packet metadata and the literal ``run `N` `` in the
    # PR-head-gate prose) and minute-resolution clocks.
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

        # Review-lane child context (original coverage: review gate, comments).
        child_sqlite = s_sqlite.build_worker_context(c1)
        child_pg = s_pg.build_worker_context(c2)
        assert _normalize(child_sqlite, p1, c1) == _normalize(child_pg, p2, c2)

        # Implementation-lane parent context (new coverage: the three sections
        # the child can never reach — impl-PR-evidence, prior-attempts with a
        # completion + closeout-packet metadata, and engineer role history).
        parent_sqlite = s_sqlite.build_worker_context(p1)
        parent_pg = s_pg.build_worker_context(p2)
        parent_norm = _normalize(parent_sqlite, p1, c1)
        assert parent_norm == _normalize(parent_pg, p2, c2)

        # Eyeball that the three target sections are genuinely present in the
        # richest (parent) context plus the review-lane history on the child;
        # a missing section means the seed failed to trigger it.
        print("\n===== normalized PARENT (implementation lane) context =====")
        print(parent_norm)
        print("===== normalized CHILD (review lane) context =====")
        print(_normalize(child_sqlite, p1, c1))

        assert "## Implementation PR evidence" in parent_norm
        assert "## Prior attempts on this task" in parent_norm
        assert "## Recent work by @engineer" in parent_norm
        # Role history also exercised on the review lane (the reviewer sibling).
        assert "## Recent work by @reviewer" in _normalize(child_sqlite, p1, c1)
        assert "## Prior attempts on this task" in _normalize(child_sqlite, p1, c1)
    finally:
        s_sqlite.close()
        s_pg.close()
        pool.close()
