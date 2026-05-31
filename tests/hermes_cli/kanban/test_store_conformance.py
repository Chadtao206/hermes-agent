import json
import time
from uuid import uuid4

from hermes_cli.kanban_db import HallucinatedCardsError


def test_create_then_get(store):
    tid = store.create_task(title="hello", assignee="engineer")
    t = store.get_task(tid)
    assert t is not None and t.id == tid and t.title == "hello"


def test_get_missing_returns_none(store):
    assert store.get_task("t_does_not_exist") is None


def test_block_unblock_roundtrip(store):
    tid = store.create_task(title="x", assignee="engineer")
    assert store.block_task(tid, reason="need input") is True
    assert store.get_task(tid).status == "blocked"
    assert store.unblock_task(tid) is True
    assert store.get_task(tid).status == "ready"


def test_priority_and_edit_and_status_direct(store):
    tid = store.create_task(title="orig", assignee="engineer")
    assert store.set_task_priority(tid, 7) is True
    assert store.get_task(tid).priority == 7
    assert store.edit_task_fields(tid, title="renamed") is True
    assert store.get_task(tid).title == "renamed"
    assert store.set_status_direct(tid, "todo") is True
    assert store.get_task(tid).status == "todo"


def test_reassign_and_delete(store):
    tid = store.create_task(title="x", assignee="engineer")
    assert store.reassign_task(tid, "reviewer") is True
    assert store.get_task(tid).assignee == "reviewer"
    assert store.delete_task(tid) is True
    assert store.get_task(tid) is None


def test_link_unlink_and_parents_children(store):
    p = store.create_task(title="parent", assignee="engineer")
    c = store.create_task(title="child", assignee="engineer")
    store.link_tasks(p, c)
    assert c in store.child_ids(p)
    assert p in store.parent_ids(c)
    assert store.unlink_tasks(p, c) is True
    assert store.child_ids(p) == []


def test_comment_roundtrip(store):
    tid = store.create_task(title="x", assignee="engineer")
    cid = store.add_comment(tid, author="ops", body="note")
    assert isinstance(cid, int)
    bodies = [c["body"] if isinstance(c, dict) else c.body for c in store.list_comments(tid)]
    assert "note" in bodies


def test_notify_sub_add_list_remove(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.add_notify_sub(task_id=tid, platform="telegram", chat_id="c1")
    subs = store.list_notify_subs()
    assert any((s["task_id"] if isinstance(s, dict) else s.task_id) == tid for s in subs)
    scoped = store.list_notify_subs(task_id=tid)
    assert scoped and all((s["task_id"] if isinstance(s, dict) else s.task_id) == tid for s in scoped)
    assert store.remove_notify_sub(task_id=tid, platform="telegram", chat_id="c1") is True


def test_profile_sub_and_recompute(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.add_profile_event_sub(task_id=tid, profile="engineer")
    subs = store.list_profile_event_subs(task_id=tid, profile="engineer", enabled_only=False)
    assert subs
    assert isinstance(store.recompute_ready(), int)
    assert isinstance(store.has_spawnable_ready(), bool)
    assert store.remove_profile_event_sub(task_id=tid, profile="engineer", name="") is True


def test_gc_events_returns_int(store):
    store.create_task(title="x", assignee="engineer")
    assert isinstance(store.gc_events(), int)


def test_set_workspace_path(store):
    tid = store.create_task(title="x", assignee="engineer")
    # set_workspace_path returns None (kb function has no return value)
    store.set_workspace_path(tid, "/tmp/ws")
    assert store.get_task(tid).workspace_path == "/tmp/ws"


def test_promote_task_is_callable(store):
    # promote_task requires a specific task state (todo/blocked) and a mandatory
    # `actor` kwarg; rather than wiring up full state, just confirm the method
    # exists, is callable, and returns a (bool, str|None) tuple on a missing task.
    tid = store.create_task(title="x", assignee="engineer")
    result = store.promote_task(tid, actor="ops")
    assert isinstance(result, tuple) and len(result) == 2
    ok, err = result
    assert isinstance(ok, bool)


def test_complete_basic_and_runs_and_summary(store):
    tid = store.create_task(title="x", assignee="engineer")
    assert store.complete_task(tid, result="done-res", summary="sum") is True
    assert store.get_task(tid).status == "done"
    s = store.latest_summary(tid)
    assert s is None or isinstance(s, str)
    assert isinstance(store.list_runs(tid), list)


def test_events_recorded_and_listed(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.set_task_priority(tid, 3)
    kinds = [e.kind for e in store.list_events(tid)]
    assert "created" in kinds and "reprioritized" in kinds


def test_notify_event_claiming_cursor(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.add_notify_sub(task_id=tid, platform="telegram", chat_id="c1")
    store.add_comment(tid, author="ops", body="n1")
    old, new, evs = store.claim_unseen_events_for_sub(
        task_id=tid, platform="telegram", chat_id="c1")
    assert new >= old and isinstance(evs, list)
    old2, new2, evs2 = store.claim_unseen_events_for_sub(
        task_id=tid, platform="telegram", chat_id="c1")
    assert new2 == new and evs2 == []


def test_claim_task_atomic(store):
    tid = store.create_task(title="x", assignee="engineer")
    assert store.get_task(tid).status == "ready"
    t1 = store.claim_task(tid, claimer="w1")
    assert t1 is not None and t1.status == "running"
    # second claim of the same (now-running) task returns None
    assert store.claim_task(tid, claimer="w2") is None


def test_record_failure_breaker_trips(store):
    tid = store.create_task(title="x", assignee="engineer")
    claimed = store.claim_task(tid, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    # failure_limit=1: the very first failure trips the breaker.
    blocked = store.record_task_failure(
        tid, "boom", outcome="spawn_failed", failure_limit=1)
    assert blocked is True
    assert store.get_task(tid).status == "blocked"
    kinds = [e.kind for e in store.list_events(tid)]
    assert "gave_up" in kinds


def test_record_failure_retry(store):
    tid = store.create_task(title="x", assignee="engineer")
    claimed = store.claim_task(tid, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    # failure_limit=3: a single failure stays under threshold -> retry (ready).
    blocked = store.record_task_failure(
        tid, "boom", outcome="spawn_failed", failure_limit=3)
    assert blocked is False
    assert store.get_task(tid).status == "ready"
    # end_run=True emits the outcome-named event on the retry path.
    kinds = [e.kind for e in store.list_events(tid)]
    assert "spawn_failed" in kinds


def test_record_failure_timeout_crash_trip(store):
    # Timeout/crash path: release_claim=False, end_run=False. The breaker
    # tripping here uses the SECOND UPDATE variant (status IN ('ready','running'))
    # and closes NO run; the caller is responsible for its own outcome event.
    tid = store.create_task(title="x", assignee="engineer")
    claimed = store.claim_task(tid, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    blocked = store.record_task_failure(
        tid, "stuck", outcome="timed_out", failure_limit=1,
        release_claim=False, end_run=False)
    assert blocked is True
    assert store.get_task(tid).status == "blocked"
    kinds = [e.kind for e in store.list_events(tid)]
    assert "gave_up" in kinds
    # end_run=False: this call must NOT emit the outcome-named event;
    # the real flow's caller emits its own timed_out event.
    assert "timed_out" not in kinds


def test_record_failure_event_payload_extra(store):
    # event_payload_extra merges into the gave_up event payload when the
    # breaker trips (spawn-path defaults release_claim/end_run=True).
    tid = store.create_task(title="x", assignee="engineer")
    claimed = store.claim_task(tid, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    blocked = store.record_task_failure(
        tid, "boom", outcome="crashed", failure_limit=1,
        event_payload_extra={"pid": 4242})
    assert blocked is True
    assert store.get_task(tid).status == "blocked"
    gave_up = [e for e in store.list_events(tid) if e.kind == "gave_up"]
    assert gave_up, "expected a gave_up event"
    payload = gave_up[-1].payload
    # Tolerate either backend exposing payload as a dict or a JSON string.
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert isinstance(payload, dict)
    assert payload.get("pid") == 4242


def test_heartbeat_worker(store):
    tid = store.create_task(title="x", assignee="engineer")
    claimed = store.claim_task(tid, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    assert store.heartbeat_worker(task_id=tid) is True
    # a task left in ready (never claimed) cannot heartbeat
    tid2 = store.create_task(title="y", assignee="engineer")
    assert store.heartbeat_worker(task_id=tid2) is False


def test_notifier_heartbeat_roundtrip(store):
    # The notifier-heartbeat sidecar is a shared SQLite DB keyed by
    # board_slug/db_path; use a unique key per run so the two backend
    # parametrizations don't collide.
    unique = uuid4().hex
    notifier_id = "n_" + unique
    board_slug = "bs_" + unique
    db_path = "/tmp/hb_" + unique + ".sqlite3"
    store.record_notifier_heartbeat(
        notifier_id=notifier_id,
        board_slug=board_slug,
        db_path=db_path,
        notifier_profile="engineer",
        host="testhost",
        pid=4242,
        started_at=int(time.time()),
    )
    rows = store.list_notifier_heartbeats(board_slug=board_slug, db_path=db_path)
    ids = [r["notifier_id"] if isinstance(r, dict) else r.notifier_id for r in rows]
    assert notifier_id in ids


def test_list_profile_wake_events_empty(store):
    rows = store.list_profile_wake_events()
    assert isinstance(rows, list)


def _field(row, key):
    return row[key] if isinstance(row, dict) else getattr(row, key)


def test_profile_wake_success_advances_and_clears(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.add_profile_event_sub(task_id=tid, profile="engineer")
    # Generate a terminal-kind event the default profile sub watches.
    store.block_task(tid, reason="need input")
    old, new, evs = store.claim_unseen_events_for_profile_sub(
        task_id=tid, profile="engineer")
    assert new >= old and isinstance(evs, list)
    when = int(time.time())
    wid = store.record_profile_wake_success(
        task_id=tid, profile="engineer", new_cursor=new, last_wake_at=when)
    assert isinstance(wid, int) and wid > 0
    wake_rows = store.list_profile_wake_events(task_id=tid)
    assert any(_field(r, "status") == "success" for r in wake_rows)
    subs = store.list_profile_event_subs(
        task_id=tid, profile="engineer", enabled_only=False)
    assert subs
    assert int(_field(subs[0], "last_event_id")) >= new


def test_profile_wake_failure_rewinds(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.add_profile_event_sub(task_id=tid, profile="engineer")
    store.block_task(tid, reason="need input")
    old, new, evs = store.claim_unseen_events_for_profile_sub(
        task_id=tid, profile="engineer")
    assert new > old and evs
    wid = store.record_profile_wake_failure(
        task_id=tid, profile="engineer",
        claimed_cursor=new, old_cursor=old, error="boom")
    assert isinstance(wid, int) and wid > 0
    subs = store.list_profile_event_subs(
        task_id=tid, profile="engineer", enabled_only=False)
    assert subs
    assert int(_field(subs[0], "last_event_id")) == old
    assert int(_field(subs[0], "wake_failure_count")) == 1
    wake_rows = store.list_profile_wake_events(task_id=tid)
    assert any(_field(r, "status") == "failed" for r in wake_rows)


def test_notify_cursor_advance_rewind(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.add_notify_sub(task_id=tid, platform="telegram", chat_id="c1")
    store.advance_notify_cursor(
        task_id=tid, platform="telegram", chat_id="c1", new_cursor=5)
    subs = store.list_notify_subs(task_id=tid)
    assert subs and int(_field(subs[0], "last_event_id")) == 5
    assert store.rewind_notify_cursor(
        task_id=tid, platform="telegram", chat_id="c1",
        claimed_cursor=5, old_cursor=2) is True
    subs = store.list_notify_subs(task_id=tid)
    assert int(_field(subs[0], "last_event_id")) == 2
    # Stale CAS: claimed_cursor no longer matches the row -> no rewind.
    assert store.rewind_notify_cursor(
        task_id=tid, platform="telegram", chat_id="c1",
        claimed_cursor=5, old_cursor=1) is False


def test_complete_hallucinated_cards_rejected(store):
    tid = store.create_task(title="x", assignee="engineer")
    claimed = store.claim_task(tid, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    try:
        store.complete_task(
            tid,
            summary="made t_deadbeefcafe",
            created_cards=["t_deadbeefcafe"],
        )
        raised = False
    except HallucinatedCardsError:
        raised = True
    assert raised, "expected HallucinatedCardsError for a phantom created_card"
    # Task state must NOT be mutated by a phantom-card rejection.
    assert store.get_task(tid).status == "running"
    kinds = [e.kind for e in store.list_events(tid)]
    assert "completion_blocked_hallucination" in kinds
    assert "completed" not in kinds


def test_complete_with_verified_cards(store):
    # Parent P (assignee "engineer") and a child card C whose created_by
    # matches P's assignee profile — satisfies the created_by==assignee
    # trust condition of _verify_created_cards on both backends.
    parent = store.create_task(title="parent", assignee="engineer")
    claimed = store.claim_task(parent, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    child = store.create_task(
        title="spawned card", assignee="engineer", created_by="engineer")
    assert store.complete_task(
        parent, summary="done", created_cards=[child]) is True
    assert store.get_task(parent).status == "done"
    completed = [e for e in store.list_events(parent) if e.kind == "completed"]
    assert completed, "expected a completed event"
    payload = completed[-1].payload
    assert isinstance(payload, dict)
    assert child in (payload.get("verified_cards") or [])


def test_reviewer_completion_uses_newest_parent_pr_head_across_parents(store):
    parent_a = store.create_task(title="implementation A", assignee="engineer")
    assert store.complete_task(
        parent_a,
        summary="Opened PR A.",
        metadata={"pull_request_head_sha": "aaaaaaaa11111111"},
    ) is True
    parent_b = store.create_task(title="implementation B", assignee="engineer")
    assert store.complete_task(
        parent_b,
        summary="Opened PR B.",
        metadata={"pull_request_head_sha": "bbbbbbbb22222222"},
    ) is True

    sha_by_parent = {
        parent_a: "aaaaaaaa11111111",
        parent_b: "bbbbbbbb22222222",
    }
    first_parent, last_parent = sorted([parent_a, parent_b])

    backend = type(store).__name__
    if backend == "PostgresKanbanStore":
        with store._pool.connection() as c, c.cursor() as cc:
            cc.execute(
                "UPDATE task_runs SET ended_at=%s WHERE board=%s AND task_id=%s",
                (1000, store.board, first_parent),
            )
            cc.execute(
                "UPDATE task_runs SET ended_at=%s WHERE board=%s AND task_id=%s",
                (2000, store.board, last_parent),
            )
    elif backend == "SqliteKanbanStore":
        from hermes_cli import kanban_db as kb
        with kb.connect(readonly=False) as c:
            c.execute(
                "UPDATE task_runs SET ended_at=? WHERE task_id=?",
                (1000, first_parent),
            )
            c.execute(
                "UPDATE task_runs SET ended_at=? WHERE task_id=?",
                (2000, last_parent),
            )
            c.commit()
    else:
        raise AssertionError(f"unexpected backend {backend}")

    review = store.create_task(
        title="final review",
        assignee="reviewer",
        parents=[parent_a, parent_b],
    )
    claimed = store.claim_task(review, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    run_id = store.get_task(review).current_run_id
    assert run_id is not None

    newest_sha = sha_by_parent[last_parent]
    stale_sha = sha_by_parent[first_parent]

    blocked = False
    try:
        store.complete_task(
            review,
            summary="Approved stale parent head.",
            metadata={"reviewed_pr_head_sha": stale_sha},
            expected_run_id=run_id,
        )
    except ValueError as exc:
        blocked = "current parent PR head" in str(exc)
    assert blocked, "expected stale reviewed_pr_head_sha to be rejected"

    assert store.complete_task(
        review,
        summary="Approved newest parent head.",
        metadata={"reviewed_pr_head_sha": newest_sha},
        expected_run_id=run_id,
    ) is True

    task_after = store.get_task(review)
    events = store.list_events(review)
    gate_events = [e for e in events if e.kind == "completion_blocked_pr_head_gate"]
    assert task_after is not None
    assert task_after.status == "done"
    assert len(gate_events) == 1

    payload = gate_events[0].payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert isinstance(payload, dict)
    assert payload["expected_pr_head_sha"] == newest_sha
    assert payload["parent_task_id"] == last_parent
    assert payload["reviewed_pr_head_sha"] == stale_sha


def test_complete_closeout_packet_present(store):
    tid = store.create_task(title="x", assignee="engineer")
    claimed = store.claim_task(tid, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    assert store.complete_task(
        tid,
        summary="all done",
        metadata={"pr_url": "https://github.com/x/y/pull/1"},
    ) is True
    completed = [e for e in store.list_events(tid) if e.kind == "completed"]
    assert completed, "expected a completed event"
    payload = completed[-1].payload
    assert isinstance(payload, dict)
    packet = payload.get("closeout_packet")
    assert isinstance(packet, dict)
    assert packet.get("schema_version") == 1
    assert packet.get("outcome") == "completed"
    assert packet.get("pr_url") == "https://github.com/x/y/pull/1"


def test_complete_on_cleanup_hook(store):
    tid = store.create_task(title="x", assignee="engineer")
    claimed = store.claim_task(tid, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    calls: list[str] = []
    assert store.complete_task(
        tid, summary="done", on_cleanup=lambda t: calls.append(t)) is True
    assert store.get_task(tid).status == "done"
    backend = type(store).__name__
    if backend == "PostgresKanbanStore":
        # The store owns no filesystem/tmux work, so it must invoke the hook.
        assert calls == [tid]
    elif backend == "SqliteKanbanStore":
        # kanban_db.complete_task does its own _cleanup_workspace internally;
        # the store drops the hook so cleanup is not double-driven.
        assert calls == []
    else:
        raise AssertionError(f"unexpected backend {backend}")


def test_complete_dangling_ended_run(store):
    """complete_task on a task whose current_run_id points at an
    ALREADY-ENDED run must NOT synthesize a spurious run nor leave the
    pointer dangling: it must close the run defensively (double_close_attempt
    event), clear current_run_id, and return True — mirroring _end_run.

    The public API maintains the invariant, so we force the corrupt
    precondition white-box (branch on backend for SETUP only), then assert
    identically on both backends.
    """
    tid = store.create_task(title="x", assignee="engineer")
    claimed = store.claim_task(tid, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    # Active run R for the task.
    runs = store.list_runs(tid)
    assert len(runs) == 1
    rid = _field(runs[0], "id")
    assert store.get_task(tid).current_run_id == rid

    # Force R ended WITHOUT clearing tasks.current_run_id (the corruption).
    backend = type(store).__name__
    if backend == "PostgresKanbanStore":
        with store._pool.connection() as c, c.cursor() as cc:
            cc.execute(
                "UPDATE task_runs SET ended_at=%s WHERE board=%s AND id=%s",
                (int(time.time()), store.board, rid))
    elif backend == "SqliteKanbanStore":
        # The conformance fixture set HERMES_KANBAN_DB, so kb.connect()
        # targets the test DB.
        from hermes_cli import kanban_db as kb
        with kb.connect(readonly=False) as c:
            c.execute(
                "UPDATE task_runs SET ended_at=? WHERE id=?",
                (int(time.time()), rid))
            c.commit()
    else:
        raise AssertionError(f"unexpected backend {backend}")

    # Sanity: pointer still dangles at the now-ended run.
    assert store.get_task(tid).current_run_id == rid

    assert store.complete_task(tid, summary="done") is True
    assert store.get_task(tid).status == "done"

    # No spurious synthetic run — still exactly the one run R.
    after = store.list_runs(tid)
    assert len(after) == 1, [(_field(r, "id"), _field(r, "ended_at")) for r in after]
    assert _field(after[0], "id") == rid
    # Pointer cleared (no dangle).
    assert store.get_task(tid).current_run_id is None
    # Defensive double-close telemetry emitted.
    kinds = [e.kind for e in store.list_events(tid)]
    assert "double_close_attempt" in kinds


def test_complete_never_claimed_synthesizes_run(store):
    """complete_task on a never-claimed task (no active run) synthesizes one
    zero-duration run and re-resolves the closeout packet's run_id to that
    synthesized run (not None)."""
    tid = store.create_task(title="x", assignee="engineer")
    assert store.get_task(tid).status == "ready"
    assert store.complete_task(
        tid, summary="manual close", metadata={"k": "v"}) is True
    assert store.get_task(tid).status == "done"

    runs = store.list_runs(tid)
    assert len(runs) == 1, "expected exactly one synthesized run"
    synth_id = _field(runs[0], "id")
    assert _field(runs[0], "outcome") == "completed"

    completed = [e for e in store.list_events(tid) if e.kind == "completed"]
    assert completed, "expected a completed event"
    packet = completed[-1].payload.get("closeout_packet")
    assert isinstance(packet, dict)
    assert packet.get("run_id") == synth_id


def test_profile_event_cursor_advance_rewind(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.add_profile_event_sub(task_id=tid, profile="engineer")
    when = int(time.time())
    store.advance_profile_event_cursor(
        task_id=tid, profile="engineer", new_cursor=5, last_wake_at=when)
    subs = store.list_profile_event_subs(
        task_id=tid, profile="engineer", enabled_only=False)
    assert subs and int(_field(subs[0], "last_event_id")) == 5
    assert _field(subs[0], "last_wake_at") is not None
    assert store.rewind_profile_event_cursor(
        task_id=tid, profile="engineer",
        claimed_cursor=5, old_cursor=2)
    subs = store.list_profile_event_subs(
        task_id=tid, profile="engineer", enabled_only=False)
    assert int(_field(subs[0], "last_event_id")) == 2
    # Stale CAS: claimed_cursor no longer matches the row -> no rewind.
    assert not store.rewind_profile_event_cursor(
        task_id=tid, profile="engineer",
        claimed_cursor=5, old_cursor=0)
    subs = store.list_profile_event_subs(
        task_id=tid, profile="engineer", enabled_only=False)
    assert int(_field(subs[0], "last_event_id")) == 2


# --- dispatch core (A5) ----------------------------------------------------

def test_dispatch_plan_claims_ready(store, monkeypatch, tmp_path):
    import pathlib

    # Backend-agnostic injected callbacks so the DB ready-scan path runs.
    profile_exists = lambda a: True  # noqa: E731
    resolve_workspace = lambda task, board=None: str(tmp_path)  # noqa: E731

    # For the SQLITE path, dispatch_once uses the module-level functions, not
    # the injected callbacks; monkeypatch those so the spawnable check + the
    # workspace resolution + the respawn guard all pass for a fresh ready task.
    # Harmless for PG (which uses the injected callbacks above).
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda a: True,
                        raising=False)
    monkeypatch.setattr(
        "hermes_cli.kanban_db.resolve_workspace",
        lambda task, board=None: pathlib.Path(str(tmp_path)))
    monkeypatch.setattr("hermes_cli.kanban_db.check_respawn_guard",
                        lambda conn, task_id: None)

    tid = store.create_task(title="dispatch me", assignee="engineer")
    assert store.get_task(tid).status == "ready"

    plan = store.dispatch_plan(
        resolve_workspace=resolve_workspace, profile_exists=profile_exists,
        max_spawn=5)
    # The task was claimed + captured (not yet spawned).
    claimed_ids = [t.id for t, ws in plan.to_spawn]
    assert tid in claimed_ids
    # The claim flipped it to running.
    assert store.get_task(tid).status == "running"
    # The workspace resolved to tmp_path on both backends.
    by_id = {t.id: ws for t, ws in plan.to_spawn}
    assert by_id[tid] == str(tmp_path)

    # Second pass: the now-running task is no longer ready, so it must NOT
    # reappear in the new plan.
    plan2 = store.dispatch_plan(
        resolve_workspace=resolve_workspace, profile_exists=profile_exists,
        max_spawn=5)
    assert tid not in [t.id for t, ws in plan2.to_spawn]


def test_record_spawn_failure_breaker(store):
    tid = store.create_task(title="x", assignee="engineer")
    claimed = store.claim_task(tid, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    # failure_limit=1: the first spawn failure trips the breaker -> blocked.
    blocked = store.record_spawn_failure(tid, "spawn boom", failure_limit=1)
    assert blocked is True
    assert store.get_task(tid).status == "blocked"
    kinds = [e.kind for e in store.list_events(tid)]
    assert "gave_up" in kinds


def test_record_spawn_success_sets_pid(store):
    tid = store.create_task(title="x", assignee="engineer")
    claimed = store.claim_task(tid, claimer="w1")
    assert claimed is not None and claimed.status == "running"
    store.record_spawn_success(tid, 4242)
    assert store.get_task(tid).worker_pid == 4242
    spawned = [e for e in store.list_events(tid) if e.kind == "spawned"]
    assert spawned, "expected a spawned event"
    payload = spawned[-1].payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload.get("pid") == 4242


def test_pre_spawn_validation_auto_blocks(store, monkeypatch):
    # A ready task whose forced skill cannot be resolved fails pre-spawn
    # validation; both backends must auto-block it (not silently defer).
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda a: True,
                        raising=False)
    tid = store.create_task(title="bad skill", assignee="engineer",
                            skills=["__phase45_missing_skill__"])
    assert store.get_task(tid).status == "ready"
    store.dispatch_plan(profile_exists=lambda a: True, max_spawn=5)
    assert store.get_task(tid).status == "blocked"
    kinds = [e.kind for e in store.list_events(tid)]
    assert "pre_spawn_validation_failed" in kinds
    assert "gave_up" in kinds
    assert "blocked" in kinds
    task = store.get_task(tid)
    assert task.consecutive_failures == 1
    runs = store.list_runs(tid)
    assert len(runs) == 1
    assert _field(runs[0], "outcome") == "spawn_failed"


def test_block_systemic_spawn_failure_signature(store):
    a = store.create_task(title="a", assignee="engineer")
    b = store.create_task(title="b", assignee="engineer")
    c = store.create_task(title="c", assignee="engineer")
    assert all(store.get_task(t).status == "ready" for t in (a, b, c))
    blocked = store.block_systemic_spawn_failure_signature(
        [a, b, c], failure_signature="boom", error="spawn boom",
        signature_count=3)
    assert set(blocked) == {a, b, c}
    for t in (a, b, c):
        assert store.get_task(t).status == "blocked"
        # sibling block must NOT bump the per-task failure counter
        assert store.get_task(t).consecutive_failures == 0
        kinds = [e.kind for e in store.list_events(t)]
        assert "systemic_failure_signature" in kinds and "gave_up" in kinds \
            and "blocked" in kinds


def test_build_worker_context_basic(store):
    tid = store.create_task(title="ctx task", assignee="engineer",
                            body="do the thing")
    text = store.build_worker_context(tid)
    assert f"# Kanban task {tid}: ctx task" in text
    assert "## Closeout requirement (do not skip)" in text
    assert "## Body" in text and "do the thing" in text
    assert text.endswith("\n")


def test_build_worker_context_unknown_raises(store):
    import pytest
    with pytest.raises(ValueError):
        store.build_worker_context("t_nope")


def test_parent_ids_rollup_relation(store):
    from hermes_cli.kanban_db import LINK_RELATION_ROLLUP
    p = store.create_task(title="p", assignee="engineer")
    c = store.create_task(title="c", assignee="engineer")
    store.link_tasks(p, c, relation_type=LINK_RELATION_ROLLUP)
    assert store.parent_ids(c, relation_type=LINK_RELATION_ROLLUP) == [p]
    assert store.parent_ids(c) == []  # default dependency relation only


# --- create_task input-validation parity (sqlite vs postgres) -------------
import pytest


def test_create_task_rejects_blank_title(store):
    with pytest.raises(ValueError):
        store.create_task(title="   ", assignee="engineer")


def test_create_task_strips_title(store):
    tid = store.create_task(title="  padded  ", assignee="engineer")
    assert store.get_task(tid).title == "padded"


def test_create_task_rejects_unknown_parent(store):
    with pytest.raises(ValueError):
        store.create_task(title="child", assignee="engineer",
                          parents=["does-not-exist"])


def test_create_task_rejects_bad_workspace_kind(store):
    with pytest.raises(ValueError):
        store.create_task(title="x", assignee="engineer",
                          workspace_kind="bogus")


def test_create_task_rejects_toolset_named_skill(store):
    # 'web' is a toolset, not a skill -> ValueError on both backends
    with pytest.raises(ValueError):
        store.create_task(title="x", assignee="engineer", skills=["web"])


def test_create_task_rejects_comma_skill(store):
    with pytest.raises(ValueError):
        store.create_task(title="x", assignee="engineer", skills=["a,b"])


def test_create_task_branch_name_requires_worktree(store):
    with pytest.raises(ValueError):
        store.create_task(title="x", assignee="engineer", branch_name="b",
                          workspace_kind="scratch")
