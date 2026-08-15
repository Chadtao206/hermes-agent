"""Tests for the Kanban dashboard plugin backend (plugins/kanban/dashboard/plugin_api.py).

The plugin mounts as /api/plugins/kanban/ inside the dashboard's FastAPI app,
but here we attach its router to a bare FastAPI instance so we can test the
REST surface without spinning up the whole dashboard.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _write_recovery_health(*, board: str, escalations: tuple[dict[str, object], ...]):
    """Write a real authenticated public snapshot for dashboard boundary tests."""
    from hermes_cli.kanban_recovery_health import (
        BoardRecoveryHealth,
        PublicEscalation,
        RecoveryHealthSnapshot,
        REQUIRED_COUNTERS,
        write_recovery_health,
    )

    boards = {board, *(str(item["board"]) for item in escalations)}
    snapshot = RecoveryHealthSnapshot(
        schema_version=1,
        revision=0,
        recorded_at=int(time.time()),
        boards=tuple(
            BoardRecoveryHealth(
                board=slug,
                review_mode="shadow",
                transient_mode="shadow",
                census_healthy=True,
                ownerless_blocked_count=0,
                multiply_owned_blocked_count=0,
                validated_config_digest="d" * 64,
                counters={name: 0 for name in REQUIRED_COUNTERS},
            )
            for slug in sorted(boards)
        ),
        escalations=tuple(PublicEscalation(**item) for item in escalations),
        checksum="",
    )
    write_recovery_health(snapshot)


def _escalation(
    *,
    board: str,
    task_id: str,
    signature: str,
    event_signature: str,
    acknowledged: bool = False,
) -> dict[str, object]:
    return {
        "board": board,
        "task_id": task_id,
        "signature": signature,
        "bound_input_signature": "b" * 64,
        "task_event_signature": event_signature,
        "outcome_class": "human_action_required",
        "tier": "initial",
        "acknowledged": acknowledged,
        "reason_code": "ownerless_blocked",
    }


def _run_dashboard_bundle_behavior(scenario: str) -> dict[str, object]:
    """Exercise selected dashboard components through the shipped bundle."""
    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    harness = r'''
const fs = require("fs");
const vm = require("vm");
const scenario = process.argv[1];
let fetchBehavior;
const sameDeps = (left, right) => left && right && left.length === right.length && left.every((item, index) => item === right[index]);
function runner(Component) {
  const states = [];
  const effects = [];
  let cursor = 0;
  return {
    render(props) {
      cursor = 0;
      global.__activeRunner = this;
      const output = Component(props);
      global.__activeRunner = null;
      const pending = effects.filter((effect) => effect.pending);
      pending.forEach((effect) => { effect.pending = false; effect.callback(); });
      return output;
    },
    useState(initial) {
      const index = cursor++;
      if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial;
      return [states[index], (next) => { states[index] = typeof next === "function" ? next(states[index]) : next; }];
    },
    useEffect(callback, deps) {
      const index = cursor++;
      if (!effects[index]) effects[index] = { deps: null, callback: null, pending: false };
      if (!sameDeps(effects[index].deps, deps)) {
        effects[index].deps = deps;
        effects[index].callback = callback;
        effects[index].pending = true;
      }
    },
    useCallback(callback) { cursor++; return callback; },
    useMemo(factory) { cursor++; return factory(); },
    useRef(value) { cursor++; return { current: value }; },
  };
}
const hooks = {
  useState: (...args) => global.__activeRunner.useState(...args),
  useEffect: (...args) => global.__activeRunner.useEffect(...args),
  useCallback: (...args) => global.__activeRunner.useCallback(...args),
  useMemo: (...args) => global.__activeRunner.useMemo(...args),
  useRef: (...args) => global.__activeRunner.useRef(...args),
};
global.window = {
  __HERMES_PLUGIN_SDK__: {
    React: {
      Component: class {},
      createElement: (type, props, ...children) => ({ type, props: props || {}, children }),
    },
    components: {},
    hooks,
    utils: { cn: (...items) => items.filter(Boolean).join(" "), timeAgo: () => "" },
    fetchJSON: (...args) => fetchBehavior(...args),
  },
};
const source = fs.readFileSync(process.env.DASHBOARD_BUNDLE, "utf8");
const marker = "})();";
const end = source.lastIndexOf(marker);
if (end < 0) throw new Error("bundle wrapper missing");
vm.runInThisContext(source.slice(0, end) + "window.__testComponents = { DiagnosticsSection, DiagnosticCard };" + source.slice(end));
const { DiagnosticsSection, DiagnosticCard } = window.__testComponents;
const task = { id: "task-1", assignee: "" };
function find(node, predicate) {
  if (Array.isArray(node)) {
    for (const child of node) {
      const match = find(child, predicate);
      if (match) return match;
    }
    return null;
  }
  if (!node || typeof node !== "object") return null;
  if (predicate(node)) return node;
  for (const child of node.children || []) {
    const match = find(child, predicate);
    if (match) return match;
  }
  return null;
}
function text(node) {
  if (node == null) return "";
  if (Array.isArray(node)) return node.map(text).join(" ");
  if (typeof node === "string") return node;
  return (node.children || []).map(text).join(" ");
}
async function tick() {
  await new Promise(setImmediate);
}
(async () => {
  if (scenario === "health-unavailable") {
    fetchBehavior = () => Promise.reject(new Error("unavailable"));
    const unavailable = runner(DiagnosticsSection);
    unavailable.render({ boardSlug: "default", task, diagnostics: [] });
    await tick();
    unavailable.render({ boardSlug: "default", task, diagnostics: [] });
    const unavailableTree = unavailable.render({ boardSlug: "default", task, diagnostics: [] });
    const diagnostic = find(unavailableTree, (node) => node.type === DiagnosticCard);
    const cardTree = diagnostic
      ? runner(DiagnosticCard).render({ ...diagnostic.props, task, boardSlug: "default", assignees: [] })
      : null;
    fetchBehavior = () => Promise.resolve({ escalations: [] });
    const empty = runner(DiagnosticsSection);
    empty.render({ boardSlug: "default", task, diagnostics: [] });
    await tick();
    const emptyTree = empty.render({ boardSlug: "default", task, diagnostics: [] });
    console.log(JSON.stringify({ unavailableText: text(cardTree), unavailableActions: diagnostic ? diagnostic.props.diag.actions.length : null, emptyIsHidden: emptyTree === null }));
    return;
  }
  if (scenario === "new-escalation") {
    const action = (signature, eventSignature, acknowledged) => ({
      kind: "acknowledge_escalation", label: "Acknowledge escalation",
      payload: { signature, expected_event_signature: eventSignature, acknowledged },
    });
    const card = runner(DiagnosticCard);
    card.render({ diag: { actions: [action("old", "event-old", true)] }, task, boardSlug: "default", assignees: [] });
    card.render({ diag: { actions: [action("new", "event-new", false)] }, task, boardSlug: "default", assignees: [] });
    const tree = card.render({ diag: { actions: [action("new", "event-new", false)] }, task, boardSlug: "default", assignees: [] });
    const actionNode = find(tree, (node) => typeof node.type === "function" && node.props.action && node.props.action.kind === "acknowledge_escalation");
    const actionTree = actionNode ? actionNode.type(actionNode.props) : null;
    console.log(JSON.stringify({ text: text(actionTree), hasAcknowledgeButton: !!find(actionTree, (node) => node.type === "button" && text(node) === "Acknowledge escalation") }));
    return;
  }
  throw new Error("unknown scenario: " + scenario);
})().catch((error) => { console.error(error.stack); process.exit(1); });
'''
    result = subprocess.run(
        ["node", "-e", harness, scenario],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "DASHBOARD_BUNDLE": str(bundle)},
    )
    return json.loads(result.stdout)


def test_dashboard_recovery_health_unavailable_stays_visible_and_non_actionable():
    """A failed health fetch must not render as a clean task with no diagnostics."""
    rendered = _run_dashboard_bundle_behavior("health-unavailable")

    assert "Recovery health unavailable" in rendered["unavailableText"]
    assert "Acknowledgement is disabled" in rendered["unavailableText"]
    assert rendered["unavailableActions"] == 0
    assert rendered["emptyIsHidden"] is True


def test_dashboard_new_escalation_resets_local_acknowledgement_badge():
    """A distinct unacknowledged escalation must offer acknowledgement again."""
    rendered = _run_dashboard_bundle_behavior("new-escalation")

    assert rendered["hasAcknowledgeButton"] is True
    assert "Acknowledged — task remains open" not in rendered["text"]


# ---------------------------------------------------------------------------
# GET /board on an empty DB
# ---------------------------------------------------------------------------


def test_board_empty(client):
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    # All canonical columns present (triage + the rest), each empty.
    names = [c["name"] for c in data["columns"]]
    assert set(names) == kb.VALID_STATUSES - {"archived"}
    for expected in ("triage", "todo", "scheduled", "ready", "running", "blocked", "done"):
        assert expected in names, f"missing column {expected}: {names}"
    assert all(len(c["tasks"]) == 0 for c in data["columns"])
    assert data["tenants"] == []
    assert data["assignees"] == []
    assert data["latest_event_id"] == 0


# ---------------------------------------------------------------------------
# POST /tasks then GET /board sees it
# ---------------------------------------------------------------------------


def test_create_task_appears_on_board(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "Research LLM caching",
            "assignee": "researcher",
            "priority": 3,
            "tenant": "acme",
        },
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["title"] == "Research LLM caching"
    assert task["assignee"] == "researcher"
    assert task["status"] == "ready"  # no parents -> immediately ready
    assert task["priority"] == 3
    assert task["tenant"] == "acme"
    task_id = task["id"]

    # Board now lists it under 'ready'.
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    ready = next(c for c in data["columns"] if c["name"] == "ready")
    assert len(ready["tasks"]) == 1
    assert ready["tasks"][0]["id"] == task_id
    assert "acme" in data["tenants"]
    assert "researcher" in data["assignees"]


def test_patch_board_sets_project_directory(client, tmp_path):
    """Board-level default_workdir must be editable after creation."""
    kb.create_board("late-config")
    project_dir = tmp_path / "late-project"
    project_dir.mkdir()

    response = client.patch(
        "/api/plugins/kanban/boards/late-config",
        json={"default_workdir": str(project_dir)},
    )

    assert response.status_code == 200, response.text
    board = response.json()["board"]
    assert board["default_workdir"] == str(project_dir.resolve())
    # The recommendation flips from scratch to a persistent kind so the
    # create-task dialog's workspace default follows the board setting.
    assert board["default_workspace_kind"] == "dir"
    assert kb.read_board_metadata("late-config")["default_workdir"] == str(
        project_dir.resolve()
    )


def test_scheduled_tasks_have_their_own_column_not_todo(client):
    """Scheduled/time-delay tasks must not be silently bucketed into todo."""

    task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "wait for indexed data", "assignee": "ops"},
    ).json()["task"]

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'scheduled' WHERE id = ?",
                (task["id"],),
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    columns = {c["name"]: c["tasks"] for c in r.json()["columns"]}
    assert any(t["id"] == task["id"] for t in columns["scheduled"])
    assert not any(t["id"] == task["id"] for t in columns["todo"])


def test_tenant_filter(client):
    client.post("/api/plugins/kanban/tasks", json={"title": "A", "tenant": "t1"})
    client.post("/api/plugins/kanban/tasks", json={"title": "B", "tenant": "t2"})

    r = client.get("/api/plugins/kanban/board?tenant=t1")
    counts = {c["name"]: len(c["tasks"]) for c in r.json()["columns"]}
    total = sum(counts.values())
    assert total == 1

    r = client.get("/api/plugins/kanban/board?tenant=t2")
    total = sum(len(c["tasks"]) for c in r.json()["columns"])
    assert total == 1


def test_dashboard_markdown_html_is_sanitized_before_render():
    """Markdown rendering must sanitize HTML before dangerouslySetInnerHTML."""

    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text(encoding="utf-8")

    assert "function sanitizeMarkdownHtml(html)" in js
    assert "MARKDOWN_ALLOWED_TAGS" in js
    assert "sanitizeMarkdownHtml(renderMarkdown(props.source || \"\"))" in js
    assert "dangerouslySetInnerHTML: { __html: renderMarkdown(props.source || \"\") }" not in js


# ---------------------------------------------------------------------------
# GET /tasks/:id returns body + comments + events + links
# ---------------------------------------------------------------------------


def test_task_detail_includes_links_and_events(client):
    parent = client.post(
        "/api/plugins/kanban/tasks", json={"title": "parent"},
    ).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "child", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"  # parent not done yet

    # Detail for the child shows the parent link.
    r = client.get(f"/api/plugins/kanban/tasks/{child['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["task"]["id"] == child["id"]
    assert parent["id"] in data["links"]["parents"]

    # Detail for the parent shows the child.
    r = client.get(f"/api/plugins/kanban/tasks/{parent['id']}")
    assert child["id"] in r.json()["links"]["children"]

    # Events exist from creation.
    assert len(data["events"]) >= 1


# ---------------------------------------------------------------------------
# PATCH /tasks/:id — status transitions
# ---------------------------------------------------------------------------


def test_patch_review_lifecycle_preserves_handoff_and_reopens(client):
    secret = "ghp_" + "D" * 40
    task = client.post(
        "/api/plugins/kanban/tasks", json={"title": "review me", "assignee": "builder"},
    ).json()["task"]

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={
            "status": "review",
            "assignee": "reviewer",
            "summary": f"Implementation ready. {secret}",
            "metadata": {"tests_run": 4, "token": secret},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "review"
    with kb.connect() as conn:
        run = kb.latest_run(conn, task["id"])
        assert run is not None
        assert run.outcome == "review_requested"
        assert run.metadata is not None
        assert run.metadata["tests_run"] == 4
        assert secret not in str(run.summary)
        assert secret not in json.dumps(run.metadata)
        review_event = [
            event for event in kb.list_events(conn, task["id"])
            if event.kind == "review_requested"
        ][-1]
        assert secret not in json.dumps(review_event.payload)
        assert review_event.payload is not None
        assert review_event.payload["implementer"] == "builder"
        assert review_event.payload["reviewer"] == "reviewer"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "ready"
    assert response.json()["task"]["assignee"] == "builder"
    with kb.connect() as conn:
        assert any(
            event.kind == "review_reopened"
            for event in kb.list_events(conn, task["id"])
        )


def test_reopening_parent_demotes_ready_child(client):
    """Reopening a completed parent must invalidate ready children immediately.

    The dispatcher re-checks parent completion on claim, but the dashboard
    should not keep showing a stale child as ready after an operator drags
    its parent back out of done for more work.
    """
    parent = client.post("/api/plugins/kanban/tasks", json={"title": "p"}).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "c", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "done"},
    )
    assert r.status_code == 200

    child_after_done = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_done["status"] == "ready"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "todo"},
    )
    assert r.status_code == 200

    child_after_reopen = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_reopen["status"] == "todo"


def test_reopening_parent_retracts_review_and_blocks_approval(client):
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        child_id = kb.create_task(
            conn,
            title="child in review",
            assignee="reviewer",
            parents=[parent_id],
        )
        grandchild_id = kb.create_task(
            conn,
            title="downstream",
            assignee="writer",
            parents=[child_id],
        )
        implementation = kb.claim_task(conn, child_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            child_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        active_review = kb.claim_review_task(conn, child_id)
        assert active_review is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text

    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "todo"
        reclaimed = kb.latest_run(conn, child_id)
        assert reclaimed is not None
        assert reclaimed.outcome == "reclaimed"
        assert kb.claim_review_task(conn, child_id) is None
        assert not kb.complete_task(conn, child_id, summary="must not approve")
        grandchild = kb.get_task(conn, grandchild_id)
        assert grandchild is not None
        assert grandchild.status == "todo"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "done"},
    )
    assert response.status_code == 200, response.text

    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "review"
        review = kb.claim_review_task(conn, child_id)
        assert review is not None
        assert kb.complete_task(
            conn,
            child_id,
            summary="approved after parent stabilized",
            expected_run_id=review.current_run_id,
        )
        grandchild = kb.get_task(conn, grandchild_id)
        assert grandchild is not None
        assert grandchild.status == "ready"


def test_reopening_parent_recursively_retracts_done_and_running_descendants(client):
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="root", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        child_id = kb.create_task(
            conn,
            title="accepted child",
            assignee="builder",
            parents=[parent_id],
        )
        assert kb.complete_task(conn, child_id)
        grandchild_id = kb.create_task(
            conn,
            title="running grandchild",
            assignee="writer",
            parents=[child_id],
        )
        grandchild_run = kb.claim_task(conn, grandchild_id)
        assert grandchild_run is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text

    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        grandchild = kb.get_task(conn, grandchild_id)
        assert child is not None and child.status == "todo"
        assert grandchild is not None and grandchild.status == "todo"
        assert grandchild.current_run_id is None
        assert kb.claim_task(conn, grandchild_id) is None
        reclaimed = kb.latest_run(conn, grandchild_id)
        assert reclaimed is not None
        assert reclaimed.outcome == "reclaimed"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "done"},
    )
    assert response.status_code == 200, response.text
    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        grandchild = kb.get_task(conn, grandchild_id)
        assert child is not None and child.status == "ready"
        assert grandchild is not None and grandchild.status == "todo"


def test_dashboard_reclaim_of_active_review_preserves_review_phase(client):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="active review", assignee="reviewer")
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id)
        assert review is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "review"
    assert response.json()["task"]["assignee"] == "reviewer"
    with kb.connect() as conn:
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.outcome == "reclaimed"
        next_review = kb.claim_review_task(conn, task_id)
        assert next_review is not None


# ---------------------------------------------------------------------------
# DELETE /tasks/:id
# ---------------------------------------------------------------------------

def test_delete_task(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "to-delete"}).json()["task"]
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["task_id"] == t["id"]

    # Gone from board
    board = client.get("/api/plugins/kanban/board").json()
    all_ids = [tt["id"] for col in board["columns"] for tt in col["tasks"]]
    assert t["id"] not in all_ids

    # Gone from detail
    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Comments + Links
# ---------------------------------------------------------------------------


def test_add_comment(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/comments",
        json={"body": "how's progress?", "author": "teknium"},
    )
    assert r.status_code == 200

    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    comments = r.json()["comments"]
    assert len(comments) == 1
    assert comments[0]["body"] == "how's progress?"
    assert comments[0]["author"] == "teknium"


# ---------------------------------------------------------------------------
# Dispatch nudge
# ---------------------------------------------------------------------------


def test_dispatch_dry_run(client):
    client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "work", "assignee": "researcher"},
    )
    r = client.post("/api/plugins/kanban/dispatch?dry_run=true&max=4")
    assert r.status_code == 200
    body = r.json()
    # DispatchResult is serialized as a dataclass dict.
    assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# Triage column (new v1 status)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Progress rollup (done children / total children)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auto-init on first board read
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket auth (query-param token)
# ---------------------------------------------------------------------------


def test_ws_events_rejects_when_token_required(tmp_path, monkeypatch):
    """Loopback mode: a missing or wrong ?token= must be rejected with
    policy-violation; the correct token is accepted. The kanban WS now
    delegates to web_server._ws_auth_ok, so we stub that with the real
    loopback-token semantics (auth_required False → constant-time token
    compare)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    # Stub web_server with a loopback-mode _ws_auth_ok (auth_required False →
    # accept only the correct ?token=). Mirrors the real gate's loopback path.
    import hermes_cli
    import types

    def _fake_ws_auth_ok(ws):
        return ws.query_params.get("token", "") == "secret-xyz"

    stub = types.SimpleNamespace(
        _SESSION_TOKEN="secret-xyz",
        _ws_auth_ok=_fake_ws_auth_ok,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)
    monkeypatch.setattr(hermes_cli, "web_server", stub, raising=False)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)

    # No token → policy violation close.
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events"):
            pass
    assert exc.value.code == 1008

    # Wrong token → policy violation close.
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events?token=nope"):
            pass
    assert exc.value.code == 1008

    # Correct token → accepted (connect then close cleanly from our side).
    with c.websocket_connect(
        "/api/plugins/kanban/events?token=secret-xyz"
    ) as ws:
        assert ws is not None  # handshake succeeded


    # The bug symptom was a traceback; we don't assert on stderr because
    # capturing asyncio's internal "exception was never retrieved" logging
    # is flaky. The assertion that matters is: no CancelledError escaped.


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


def test_bulk_status_ready(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    # Parent-less tasks land in "ready" already; push them to blocked first.
    for tid in (a["id"], b["id"], c2["id"]):
        client.patch(
            f"/api/plugins/kanban/tasks/{tid}",
            json={"status": "blocked", "block_reason": "wait"},
        )

    response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [a["id"], b["id"], c2["id"]], "status": "ready"},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert all(item["ok"] for item in results)
    # All three are now ready.
    board = client.get("/api/plugins/kanban/board").json()
    ready = next(col for col in board["columns"] if col["name"] == "ready")
    ids = {task["id"] for task in ready["tasks"]}
    assert {a["id"], b["id"], c2["id"]}.issubset(ids)


def test_bulk_review_assignment_preserves_implementer_provenance(client):
    tasks = [
        client.post(
            "/api/plugins/kanban/tasks",
            json={"title": title, "assignee": "builder"},
        ).json()["task"]
        for title in ("review a", "review b")
    ]
    response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [task["id"] for task in tasks],
            "status": "review",
            "assignee": "reviewer",
            "summary": "ready",
        },
    )
    assert response.status_code == 200, response.text
    assert all(item["ok"] for item in response.json()["results"])
    with kb.connect() as conn:
        for task in tasks:
            current = kb.get_task(conn, task["id"])
            assert current is not None
            assert current.status == "review"
            assert current.assignee == "reviewer"
            event = [
                item for item in kb.list_events(conn, task["id"])
                if item.kind == "review_requested"
            ][-1]
            assert event.payload is not None
            assert event.payload["implementer"] == "builder"
            assert event.payload["reviewer"] == "reviewer"


# ---------------------------------------------------------------------------
# /config endpoint
# ---------------------------------------------------------------------------


def test_config_reads_dashboard_kanban_section(tmp_path, monkeypatch, client):
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        "dashboard:\n"
        "  kanban:\n"
        "    default_tenant: acme\n"
        "    lane_by_profile: false\n"
        "    include_archived_by_default: true\n"
        "    render_markdown: false\n"
    )
    r = client.get("/api/plugins/kanban/config")
    assert r.status_code == 200
    data = r.json()
    assert data["default_tenant"] == "acme"
    assert data["lane_by_profile"] is False
    assert data["include_archived_by_default"] is True
    assert data["render_markdown"] is False


# ---------------------------------------------------------------------------
# Runs surfacing (vulcan-artivus RFC feedback)
# ---------------------------------------------------------------------------


def test_event_dict_includes_run_id(client):
    """GET /tasks/:id returns events with run_id populated."""
    r = client.post("/api/plugins/kanban/tasks", json={"title": "e", "assignee": "worker"})
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
        run_id = kb.latest_run(conn, tid).id
        kb.complete_task(conn, tid, summary="wss")
    finally:
        conn.close()

    r = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert r.status_code == 200
    events = r.json()["events"]
    # Every event in the response must have a run_id key (None or int).
    for e in events:
        assert "run_id" in e, f"missing run_id in event: {e}"
    # completed event must have the actual run_id.
    comp = [e for e in events if e["kind"] == "completed"]
    assert comp[0]["run_id"] == run_id


# ---------------------------------------------------------------------------
# Per-task force-loaded skills via REST
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dispatcher-presence warning in POST /tasks response
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _task_dict — outer try/except fallback when task_age raises
#
# Background: kanban_db.task_age was hardened in 061a1830 to return None for
# corrupt timestamp values via _safe_int. The companion fix added a belt-and-
# suspenders try/except in plugin_api._task_dict so that *any future* exception
# from task_age (not just ValueError on '%s') still yields a usable dict
# instead of 500'ing GET /board for the entire org.
#
# kanban_db._safe_int / task_age corruption paths are covered in
# tests/hermes_cli/test_kanban_db.py. The OUTER fallback here is not, which
# means a refactor that drops the try/except would not be caught by CI. The
# tests below pin that contract.
# ---------------------------------------------------------------------------


_FALLBACK_AGE = {
    "created_age_seconds": None,
    "started_age_seconds": None,
    "time_to_complete_seconds": None,
}


# ---------------------------------------------------------------------------
# Home-channel subscription endpoints (#19534 follow-up: GUI opt-in)
# ---------------------------------------------------------------------------
#
# Dashboard surface for per-task, per-platform notification toggles. The
# backend endpoints read the live GatewayConfig, so tests set env vars
# (BOT_TOKEN + HOME_CHANNEL) to simulate a user who has run /sethome on
# telegram and discord.


@pytest.fixture
def with_home_channels(monkeypatch):
    """Simulate a user with home channels set on telegram and discord."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:fake")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "1234567")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "42")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_NAME", "Main TG")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "disc_fake")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "9999999")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL_NAME", "Main Discord")
    # Slack has a token but NO home — should be excluded from the list.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack_fake")


def test_home_channels_lists_only_platforms_with_home(client, with_home_channels):
    """GET /home-channels returns entries only for platforms where the
    user has set a home; untoggled-subscribed bool is false by default."""
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    platforms = {h["platform"] for h in r.json()["home_channels"]}
    assert platforms == {"telegram", "discord"}, (
        f"slack has a token but no home — must not appear. got {platforms}"
    )
    for h in r.json()["home_channels"]:
        assert h["subscribed"] is False


# ---------------------------------------------------------------------------
# Recovery endpoints (reclaim + reassign) and warnings field
# ---------------------------------------------------------------------------


def test_recovery_health_lists_only_current_board_escalations(client):
    """The dashboard may only display authenticated escalation evidence for its board."""
    _write_recovery_health(
        board="default",
        escalations=(
            _escalation(
                board="default",
                task_id="t_default1234",
                signature="a" * 64,
                event_signature="c" * 64,
            ),
            _escalation(
                board="other",
                task_id="t_other1234",
                signature="e" * 64,
                event_signature="f" * 64,
            ),
        ),
    )

    response = client.get("/api/plugins/kanban/recovery-health?board=default")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["board"] == "default"
    assert body["escalations"] == [
        {
            "board": "default",
            "task_id": "t_default1234",
            "signature": "a" * 64,
            "bound_input_signature": "b" * 64,
            "task_event_signature": "c" * 64,
            "outcome_class": "human_action_required",
            "tier": "initial",
            "acknowledged": False,
            "reason_code": "ownerless_blocked",
        }
    ]


def test_recovery_health_unavailable_is_not_an_authoritative_empty_view(client):
    """A missing signed snapshot is unavailable, never a trustworthy empty list."""
    response = client.get("/api/plugins/kanban/recovery-health?board=default")

    assert response.status_code == 503
    assert "escalations" not in response.json()


def test_dashboard_acknowledgement_is_lifecycle_neutral_and_replay_safe(client):
    """Exact public evidence may add one acknowledgement without changing task state."""
    conn = kb.connect(board="default")
    try:
        task_id = kb.create_task(
            conn,
            title="needs an operator decision",
            assignee="owner",
            tenant="team-a",
        )
        assert kb.block_task(conn, task_id, reason="operator decision", kind="needs_input")
        before = kb.get_task(conn, task_id)
        assert before is not None
        event_signature = kb.canonical_event_signature(kb.list_events(conn, task_id)[-1])
    finally:
        conn.close()

    _write_recovery_health(
        board="default",
        escalations=(
            _escalation(
                board="default",
                task_id=task_id,
                signature="a" * 64,
                event_signature=event_signature,
            ),
        ),
    )
    payload = {
        "signature": "a" * 64,
        "expected_event_signature": event_signature,
        "operation_id": "dashboard-ack-1",
    }

    response = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/acknowledge-escalation?board=default",
        json=payload,
    )
    replay = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/acknowledge-escalation?board=default",
        json=payload,
    )

    assert response.status_code == 200, response.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["event_id"] == response.json()["event_id"]
    conn = kb.connect(board="default")
    try:
        after = kb.get_task(conn, task_id)
        assert after is not None
        assert (
            after.status,
            after.assignee,
            after.current_step_key,
            after.max_retries,
            after.block_kind,
            after.block_recurrences,
            after.consecutive_failures,
        ) == (
            before.status,
            before.assignee,
            before.current_step_key,
            before.max_retries,
            before.block_kind,
            before.block_recurrences,
            before.consecutive_failures,
        )
        acknowledgements = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "escalation_acknowledged"
        ]
        assert len(acknowledgements) == 1
        assert acknowledgements[0].payload["actor"] == "dashboard"
    finally:
        conn.close()


def test_dashboard_acknowledgement_replays_after_health_snapshot_marks_it_acknowledged(client):
    """A refreshed acknowledged snapshot may replay its exact existing operation."""
    conn = kb.connect(board="default")
    try:
        task_id = kb.create_task(conn, title="replay after health refresh", tenant="team-a")
        assert kb.block_task(conn, task_id, reason="needs input", kind="needs_input")
        event_signature = kb.canonical_event_signature(kb.list_events(conn, task_id)[-1])
    finally:
        conn.close()
    payload = {
        "signature": "a" * 64,
        "expected_event_signature": event_signature,
        "operation_id": "dashboard-ack-after-health-refresh",
    }
    _write_recovery_health(
        board="default",
        escalations=(
            _escalation(
                board="default",
                task_id=task_id,
                signature=payload["signature"],
                event_signature=event_signature,
            ),
        ),
    )
    committed = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/acknowledge-escalation?board=default",
        json=payload,
    )
    _write_recovery_health(
        board="default",
        escalations=(
            _escalation(
                board="default",
                task_id=task_id,
                signature=payload["signature"],
                event_signature=event_signature,
                acknowledged=True,
            ),
        ),
    )

    replay = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/acknowledge-escalation?board=default",
        json=payload,
    )

    assert committed.status_code == 200, committed.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["event_id"] == committed.json()["event_id"]
    conn = kb.connect(board="default")
    try:
        assert len([
            event for event in kb.list_events(conn, task_id)
            if event.kind == "escalation_acknowledged"
        ]) == 1
    finally:
        conn.close()


def test_dashboard_rejects_unproven_acknowledged_health_snapshot(client):
    """An acknowledged snapshot alone must not authorize a new mutation."""
    conn = kb.connect(board="default")
    try:
        task_id = kb.create_task(conn, title="unproven health acknowledgement", tenant="team-a")
        assert kb.block_task(conn, task_id, reason="needs input", kind="needs_input")
        event_signature = kb.canonical_event_signature(kb.list_events(conn, task_id)[-1])
    finally:
        conn.close()
    _write_recovery_health(
        board="default",
        escalations=(
            _escalation(
                board="default",
                task_id=task_id,
                signature="a" * 64,
                event_signature=event_signature,
                acknowledged=True,
            ),
        ),
    )

    response = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/acknowledge-escalation?board=default",
        json={
            "signature": "a" * 64,
            "expected_event_signature": event_signature,
            "operation_id": "dashboard-ack-unproven-health",
        },
    )

    assert response.status_code == 409
    conn = kb.connect(board="default")
    try:
        assert not any(
            event.kind == "escalation_acknowledged"
            for event in kb.list_events(conn, task_id)
        )
    finally:
        conn.close()


def test_dashboard_acknowledgement_rejects_a_tenant_changed_after_context_read(client, monkeypatch):
    """The route passes its exact task tenant to the core's atomic precondition."""
    conn = kb.connect(board="default")
    try:
        task_id = kb.create_task(conn, title="tenant race", tenant="team-a")
        assert kb.block_task(conn, task_id, reason="needs input", kind="needs_input")
        event_signature = kb.canonical_event_signature(kb.list_events(conn, task_id)[-1])
    finally:
        conn.close()
    _write_recovery_health(
        board="default",
        escalations=(
            _escalation(
                board="default",
                task_id=task_id,
                signature="a" * 64,
                event_signature=event_signature,
            ),
        ),
    )
    original_get_task = kb.get_task

    def change_tenant_after_dashboard_read(connection, requested_task_id):
        task = original_get_task(connection, requested_task_id)
        if requested_task_id == task_id:
            with kb.write_txn(connection):
                connection.execute("UPDATE tasks SET tenant = 'team-b' WHERE id = ?", (task_id,))
        return task

    monkeypatch.setattr(kb, "get_task", change_tenant_after_dashboard_read)

    response = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/acknowledge-escalation?board=default",
        json={
            "signature": "a" * 64,
            "expected_event_signature": event_signature,
            "operation_id": "dashboard-ack-tenant-race",
        },
    )

    assert response.status_code == 409
    conn = kb.connect(board="default")
    try:
        assert original_get_task(conn, task_id).tenant == "team-b"
        assert not any(
            event.kind == "escalation_acknowledged"
            for event in kb.list_events(conn, task_id)
        )
    finally:
        conn.close()


def test_stale_or_unknown_dashboard_signature_is_rejected(client):
    """Evidence absent from the current signed health snapshot cannot mutate a task."""
    conn = kb.connect(board="default")
    try:
        task_id = kb.create_task(conn, title="blocked", tenant="team-a")
        assert kb.block_task(conn, task_id, reason="needs input", kind="needs_input")
        event_signature = kb.canonical_event_signature(kb.list_events(conn, task_id)[-1])
    finally:
        conn.close()
    _write_recovery_health(
        board="default",
        escalations=(
            _escalation(
                board="default",
                task_id=task_id,
                signature="a" * 64,
                event_signature=event_signature,
            ),
        ),
    )

    response = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/acknowledge-escalation?board=default",
        json={
            "signature": "0" * 64,
            "expected_event_signature": "1" * 64,
            "operation_id": "dashboard-ack-stale",
        },
    )

    assert response.status_code == 409
    conn = kb.connect(board="default")
    try:
        assert not any(
            event.kind == "escalation_acknowledged"
            for event in kb.list_events(conn, task_id)
        )
    finally:
        conn.close()


def test_reclaim_endpoint_releases_running_claim(client):
    """POST /tasks/<id>/reclaim drops the claim, returns ok, and emits
    a manual reclaimed event."""
    import secrets
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="running", assignee="x")
        lock = secrets.token_hex(8)
        future = int(time.time()) + 3600
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, future, 99999, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (t, lock, future, 99999, int(time.time())),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, t))
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reclaim",
        json={"reason": "browser recovery"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t

    # Confirm the task is back to ready.
    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT status, claim_lock FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
    finally:
        conn2.close()


def test_reassign_endpoint_switches_profile(client):
    """POST /tasks/<id>/reassign changes the assignee field."""
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="task", assignee="orig")
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reassign",
        json={"profile": "newbie", "reclaim_first": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == "newbie"

    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT assignee FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["assignee"] == "newbie"
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Diagnostics endpoint (/api/plugins/kanban/diagnostics)
# ---------------------------------------------------------------------------


def test_diagnostics_endpoint_surfaces_blocked_hallucination(client):
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        real = kb.create_task(conn, title="real", assignee="x", created_by="alice")
        import pytest as _pytest
        with _pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent, summary="phantom",
                created_cards=[real, "t_ffff00001234"],
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["diagnostics"][0]
    assert row["task_id"] == parent
    assert row["diagnostics"][0]["kind"] == "hallucinated_cards"
    assert row["diagnostics"][0]["severity"] == "error"
    assert "t_ffff00001234" in row["diagnostics"][0]["data"]["phantom_ids"]


# ---------------------------------------------------------------------------
# POST /tasks/:id/specify — triage specifier endpoint
# ---------------------------------------------------------------------------


def _patch_specifier_response(monkeypatch, *, content, model="test-model"):
    """Helper: install a fake auxiliary client so the specifier endpoint
    can run without hitting any real provider."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    # specify_task routes through call_llm now (#35566) — mock it directly.
    fake_call = MagicMock(return_value=resp)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call)
    return fake_call


def test_specify_happy_path(client, monkeypatch):
    import json as jsonlib

    # Create a triage task.
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "one-liner", "triage": True},
    ).json()["task"]
    assert t["status"] == "triage"

    _patch_specifier_response(
        monkeypatch,
        content=jsonlib.dumps(
            {"title": "Polished", "body": "**Goal**\nDo the thing."}
        ),
    )

    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/specify",
        json={"author": "ui-tester"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t["id"]
    assert body["new_title"] == "Polished"

    # Task should have moved off the triage column.
    detail = client.get(f"/api/plugins/kanban/tasks/{t['id']}").json()["task"]
    assert detail["status"] in {"todo", "ready"}
    assert detail["title"] == "Polished"
    assert "**Goal**" in (detail["body"] or "")


# ---------------------------------------------------------------------------
# Final result visibility for Done cards
# ---------------------------------------------------------------------------


