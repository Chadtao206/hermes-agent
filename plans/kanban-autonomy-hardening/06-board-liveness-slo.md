# WS6 — Board-Liveness SLO + Stall Alerts

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` or
> `superpowers:executing-plans`. Independent of other workstreams; can be built in parallel.

**Goal:** Turn the dominant failure mode — a *silent* stall where a task sits and nothing
screams — into a fast alert. Compute a single board-liveness signal and page when any dimension
breaches threshold, so stalls surface in minutes rather than whenever someone happens to look.

**Architecture:** A pure `compute_board_liveness(conn) -> Liveness` over a read-only connection
returns the signals that matter (oldest `ready` age, oldest `blocked`-with-all-parents-done age,
oldest `running` with stale heartbeat, dispatcher/notifier/daemon enabled flags, last successful
tick age, and — if WS1/WS2 present — writer-daemon `health()`). A gateway checker evaluates it
each minute against configured thresholds and emits a deduped alert through the existing notifier
delivery path. Behind `kanban.liveness_alerts` (default `false`).

**Key anchors (verified):**
- Read path: `kb.connect(board=…, readonly=True)` / `snapshot_connect` (`:1763`) for
  non-mutating reads.
- Recompute-ready / claim semantics define "should have been dispatched" (`recompute_ready:3699`,
  ready scan `:7288-7292`).
- Reconciler already classifies blocked/scheduled-with-completed-parents — reuse its predicates
  rather than re-deriving (`kanban_reconciler.py`).
- Gateway tick scaffolding for a periodic checker mirrors `_start_cron_ticker` (`gateway/run.py:20016`).

---

### Task 1: `compute_board_liveness` (pure, read-only)

**Files:**
- Create: `hermes_cli/kanban_liveness.py`
- Test: `tests/hermes_cli/test_kanban_liveness.py`

- [ ] **Step 1: Failing test**

```python
# tests/hermes_cli/test_kanban_liveness.py
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_liveness as liv

def test_oldest_ready_age_flagged(tmp_path):
    db = tmp_path / "kanban.db"
    conn = kb.connect(db_path=db, readonly=False, _bootstrap=True)
    # A ready task created "long ago" (created_at far in the past).
    conn.execute("INSERT INTO tasks (id,title,status,created_at) "
                 "VALUES ('t1','x','ready', 1000)")  # epoch secs; adapt col/units
    conn.commit()
    snap = liv.compute_board_liveness(conn, now=10_000)
    assert snap.oldest_ready_age_seconds == 9000
    breaches = liv.evaluate(snap, thresholds={"oldest_ready_age_seconds": 600})
    assert any(b.dimension == "oldest_ready_age_seconds" for b in breaches)

def test_healthy_board_no_breach(tmp_path):
    db = tmp_path / "kanban.db"
    conn = kb.connect(db_path=db, readonly=False, _bootstrap=True)
    snap = liv.compute_board_liveness(conn, now=10_000)
    assert liv.evaluate(snap, thresholds={"oldest_ready_age_seconds": 600}) == []
```

- [ ] **Step 2: Run red** — FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# hermes_cli/kanban_liveness.py
"""Read-only board liveness signals + threshold evaluation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Liveness:
    oldest_ready_age_seconds: int = 0
    oldest_blocked_done_parents_age_seconds: int = 0
    oldest_stale_running_age_seconds: int = 0
    dispatcher_enabled: bool = True
    notifier_enabled: bool = True
    writer_daemon_disabled: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Breach:
    dimension: str
    value: int
    threshold: int


def _scalar(conn, sql, *params) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def compute_board_liveness(conn, *, now: int) -> Liveness:
    oldest_ready = _scalar(
        conn, "SELECT MAX(? - created_at) FROM tasks WHERE status='ready'", now)
    # blocked tasks whose every dependency parent is done/archived (reuse the
    # reconciler predicate if importing it is cheap; inline SQL otherwise).
    oldest_blocked = _scalar(
        conn,
        "SELECT MAX(? - created_at) FROM tasks t WHERE t.status='blocked' "
        "AND NOT EXISTS (SELECT 1 FROM task_links l JOIN tasks p ON p.id=l.parent_id "
        "WHERE l.child_id=t.id AND l.relation_type='dependency' "
        "AND p.status NOT IN ('done','archived'))", now)
    oldest_stale_running = _scalar(
        conn,
        "SELECT MAX(? - COALESCE(last_heartbeat, created_at)) FROM tasks "
        "WHERE status='running'", now)
    return Liveness(
        oldest_ready_age_seconds=max(0, oldest_ready),
        oldest_blocked_done_parents_age_seconds=max(0, oldest_blocked),
        oldest_stale_running_age_seconds=max(0, oldest_stale_running),
    )


def evaluate(snap: Liveness, *, thresholds: dict[str, int]) -> list[Breach]:
    breaches: list[Breach] = []
    for dim, limit in thresholds.items():
        value = getattr(snap, dim, None)
        if isinstance(value, int) and value > limit:
            breaches.append(Breach(dim, value, limit))
    if snap.writer_daemon_disabled:
        breaches.append(Breach("writer_daemon_disabled", 1, 0))
    if not snap.dispatcher_enabled:
        breaches.append(Breach("dispatcher_disabled", 1, 0))
    if not snap.notifier_enabled:
        breaches.append(Breach("notifier_disabled", 1, 0))
    return breaches
```

> Adapt column names/units (`created_at`, `last_heartbeat`) to the real schema — read a couple of
> existing queries in `kanban_db.py` to confirm whether timestamps are epoch seconds or ms.

- [ ] **Step 4: Run green** — `python -m pytest tests/hermes_cli/test_kanban_liveness.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_liveness.py tests/hermes_cli/test_kanban_liveness.py
git commit -m "feat(kanban): read-only board-liveness signals + threshold evaluation"
```

---

### Task 2: `hermes kanban liveness` CLI (observability + manual check)

**Files:**
- Modify: `hermes_cli/kanban.py` (add `liveness` subcommand → JSON)
- Test: `tests/hermes_cli/test_kanban_liveness_cli.py`

- [ ] **Step 1: Failing test**

```python
# tests/hermes_cli/test_kanban_liveness_cli.py
import json, subprocess, sys

def test_liveness_cli_emits_json(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    out = subprocess.run([sys.executable, "-m", "hermes_cli.kanban", "liveness", "--json"],
                         capture_output=True, text=True)
    assert out.returncode == 0
    data = json.loads(out.stdout)
    assert "oldest_ready_age_seconds" in data
```

- [ ] **Step 2: Run red** — FAIL (no subcommand).

- [ ] **Step 3: Implement** — add `_cmd_liveness` that opens a read-only/snapshot connection,
  calls `compute_board_liveness(conn, now=<wall clock>)`, and prints `dataclasses.asdict(snap)`
  as JSON. Register the `liveness` subparser with `--board`/`--json`. Use the module's existing
  time accessor for `now` (do not call `Date`/`time` directly if there's a project wrapper).

- [ ] **Step 4: Run green** — PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban.py tests/hermes_cli/test_kanban_liveness_cli.py
git commit -m "feat(kanban): 'hermes kanban liveness --json' command"
```

---

### Task 3: Gateway checker + deduped alert delivery

**Files:**
- Modify: `gateway/run.py` (add `_start_kanban_liveness_checker()` + one start call)
- Test: `tests/gateway/test_kanban_liveness_alert.py`

- [ ] **Step 1: Failing test**

```python
# tests/gateway/test_kanban_liveness_alert.py
import gateway.run as gr
from hermes_cli import kanban_liveness as liv

def test_alert_fires_once_per_breach_window():
    sent = []
    state: dict = {}
    breaches = [liv.Breach("oldest_ready_age_seconds", 9000, 600)]
    gr._maybe_emit_liveness_alert(breaches, board="default", state=state, emit=sent.append)
    gr._maybe_emit_liveness_alert(breaches, board="default", state=state, emit=sent.append)
    assert len(sent) == 1  # deduped: same breach signature doesn't re-page

def test_alert_refires_after_clear():
    sent = []; state: dict = {}
    b = [liv.Breach("oldest_ready_age_seconds", 9000, 600)]
    gr._maybe_emit_liveness_alert(b, board="default", state=state, emit=sent.append)
    gr._maybe_emit_liveness_alert([], board="default", state=state, emit=sent.append)  # cleared
    gr._maybe_emit_liveness_alert(b, board="default", state=state, emit=sent.append)  # re-breach
    assert len(sent) == 2
```

- [ ] **Step 2: Run red** — FAIL (no `_maybe_emit_liveness_alert`).

- [ ] **Step 3: Implement** — add the pure dedup helper `_maybe_emit_liveness_alert(breaches,
  board, state, emit)` (signature = sorted breach dimensions; only emit on transition into a new
  breach signature; clear when breaches empty). Add `_start_kanban_liveness_checker()` mirroring
  `_start_cron_ticker`: each minute, for each board, open a read-only/snapshot conn, compute +
  evaluate against `config.kanban.liveness_thresholds`, and route `emit` through the existing
  notifier chat-delivery path (home channel / ops). Gate the whole checker on
  `config.kanban.liveness_alerts`. Populate `Liveness.writer_daemon_disabled`/`*_enabled` from
  the gateway's known subsystem state (WS1/WS2 daemons, the existing disabled sets) when present.

- [ ] **Step 4: Run green** — `python -m pytest tests/gateway/test_kanban_liveness_alert.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/run.py tests/gateway/test_kanban_liveness_alert.py
git commit -m "feat(gateway): periodic board-liveness checker with deduped stall alerts"
```

---

### Task 4: Config + thresholds

**Files:**
- Modify: `config.yaml`, `cli-config.yaml.example`

- [ ] **Step 1: Document**

```yaml
kanban:
  liveness_alerts: false
  liveness_thresholds:
    oldest_ready_age_seconds: 900            # ready task not dispatched in 15m → alert
    oldest_blocked_done_parents_age_seconds: 1800  # blocked but unblockable for 30m
    oldest_stale_running_age_seconds: 5400   # running, no heartbeat 90m
```

- [ ] **Step 2: Commit**

```bash
git add config.yaml cli-config.yaml.example
git commit -m "docs(kanban): document liveness alert thresholds"
```

---

## WS6 acceptance criteria

- `hermes kanban liveness --json` reports the signals for any board.
- With `liveness_alerts: true`, a task stuck in `ready`/`blocked`/`running` past threshold raises
  exactly one alert per breach window (re-arms after the breach clears).
- A disabled dispatcher/notifier or an exhausted writer daemon (WS2) raises an alert.
- Flag off → no checker runs.
