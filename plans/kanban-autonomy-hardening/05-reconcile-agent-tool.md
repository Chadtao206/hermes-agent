# WS5 — `kanban_reconcile` Agent Tool

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` or
> `superpowers:executing-plans`. Soft-depends on WS1 (writes go through the daemon under the
> flag; the tool's apply path uses the same write surface).

**Goal:** Let Jensen resolve `jensen_decision_required` reconcile packets through a
schema-validated agent tool instead of hand-typed `hermes kanban reconcile --apply-option …`
shell. This removes the fragility of free-form shell (mis-copied `--packet-signature`, wrong
flags) and lets you gate exactly which options Jensen may auto-apply vs. which still require a
human.

**Architecture:** A new `kanban_reconcile` tool with two actions — `list` (returns the current
decision packets from `collect_reconcile_actions`) and `apply` (calls the existing
`apply_reconcile_decision`, which already re-validates the packet signature against a fresh
reconcile pass before mutating). An allowlist in config (`kanban.reconcile_tool_auto_options`)
decides which options the tool will apply autonomously; anything else returns a structured
"needs human" result. The tool is orchestrator-only (same gating as `kanban_list`/
`kanban_unblock`).

**Key anchors (verified):**
- `kanban_reconciler.py`: `collect_reconcile_actions(...)`, decision packets with
  `suggested_options` + `packet_signature`, `apply_reconcile_decision(..., confirm_dry_run=True)`
  (`:1777`, gate `:1830`, signature re-validation `:1852`).
- Tool registration + orchestrator gating: `tools/kanban_tools.py` (`_check_kanban_orchestrator_mode`
  `:49-90`, tool list `:1338-1417`); toolset membership `toolsets.py:303-317`.
- CLI reference impl: `hermes_cli/kanban.py:_cmd_reconcile` (`:1420-1435`).

---

### Task 1: Tool handler — `list` action

**Files:**
- Modify: `tools/kanban_tools.py` (add `_handle_reconcile` + register `kanban_reconcile`)
- Test: `tests/hermes_cli/test_kanban_reconcile_tool.py`

- [ ] **Step 1: Failing test**

```python
# tests/hermes_cli/test_kanban_reconcile_tool.py
import tools.kanban_tools as kt

def test_reconcile_list_returns_packets(monkeypatch):
    fake = {"packets": [{"task_id": "t1", "packet_signature": "sig1",
                         "suggested_options": ["unblock", "keep_blocked", "close"]}]}
    monkeypatch.setattr(kt, "_reconcile_collect", lambda board=None: fake, raising=False)
    res = kt._handle_reconcile({"action": "list"})
    assert res["packets"][0]["task_id"] == "t1"
    assert "unblock" in res["packets"][0]["suggested_options"]
```

- [ ] **Step 2: Run red** — FAIL (no `_handle_reconcile`).

- [ ] **Step 3: Implement** — add `_handle_reconcile` dispatching on `action`; for `list`, call
  a thin `_reconcile_collect(board)` wrapper around `kanban_reconciler.collect_reconcile_actions`
  / the same JSON the CLI `--json` path returns. Register `kanban_reconcile` in the tool list and
  gate it behind `_check_kanban_orchestrator_mode()` (orchestrator-only, like `kanban_unblock`).

- [ ] **Step 4: Run green** — PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/kanban_tools.py tests/hermes_cli/test_kanban_reconcile_tool.py
git commit -m "feat(kanban): add kanban_reconcile tool (list action)"
```

---

### Task 2: `apply` action with option allowlist + signature re-validation

**Files:**
- Modify: `tools/kanban_tools.py` (`_handle_reconcile` apply branch)
- Test: extend `tests/hermes_cli/test_kanban_reconcile_tool.py`

- [ ] **Step 1: Failing test**

```python
# append to tests/hermes_cli/test_kanban_reconcile_tool.py
def test_reconcile_apply_allowed_option(monkeypatch):
    monkeypatch.setattr(kt, "_reconcile_auto_options", lambda: {"unblock", "keep_blocked"})
    called = {}
    def fake_apply(**kw): called.update(kw); return {"applied": True, "option": kw["option"]}
    monkeypatch.setattr(kt, "_reconcile_apply", fake_apply, raising=False)
    res = kt._handle_reconcile({"action": "apply", "task_id": "t1",
                                "option": "unblock", "packet_signature": "sig1"})
    assert res["applied"] is True
    assert called["confirm_dry_run"] is True           # the gate flag is always set by the tool
    assert called["packet_signature"] == "sig1"        # signature threaded through for re-validation

def test_reconcile_apply_refuses_human_only_option(monkeypatch):
    monkeypatch.setattr(kt, "_reconcile_auto_options", lambda: {"unblock"})
    res = kt._handle_reconcile({"action": "apply", "task_id": "t1",
                                "option": "manual_review_with_stale_pr_risk",
                                "packet_signature": "sig1"})
    assert res.get("needs_human") is True
    assert "not in the auto-apply allowlist" in res.get("reason", "")
```

- [ ] **Step 2: Run red** — FAIL.

- [ ] **Step 3: Implement** — in the apply branch: reject if `option` not in
  `_reconcile_auto_options()` (config `kanban.reconcile_tool_auto_options`, default e.g.
  `["unblock", "keep_blocked", "keep_parked"]`) → return `{"needs_human": True, "reason": …}`.
  Otherwise call `_reconcile_apply(task_id=…, option=…, packet_signature=…, confirm_dry_run=True,
  author="jensen")` wrapping `kanban_reconciler.apply_reconcile_decision` (which re-validates the
  signature against a fresh pass before writing). Return the structured result.

- [ ] **Step 4: Run green** — PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/kanban_tools.py tests/hermes_cli/test_kanban_reconcile_tool.py
git commit -m "feat(kanban): kanban_reconcile apply with option allowlist + sig revalidation"
```

---

### Task 3: Schema, toolset membership, wake-prompt update

**Files:**
- Modify: `tools/kanban_tools.py` (JSON schema for `kanban_reconcile`)
- Modify: `toolsets.py` (add `kanban_reconcile` to the kanban orchestrator toolset)
- Modify: `scripts/kanban_reconcile_wake_triage.py` (point Jensen's wake prompt at the tool
  instead of "re-run `hermes kanban reconcile`")
- Modify: `config.yaml` (`kanban.reconcile_tool_auto_options`)
- Test: `tests/hermes_cli/test_kanban_reconcile_tool_schema.py`

- [ ] **Step 1: Failing test**

```python
# tests/hermes_cli/test_kanban_reconcile_tool_schema.py
import tools.kanban_tools as kt

def test_reconcile_tool_schema_registered():
    names = {t["name"] if isinstance(t, dict) else getattr(t, "name", None)
             for t in kt.get_kanban_tool_schemas()}   # adapt to real accessor
    assert "kanban_reconcile" in names
```

- [ ] **Step 2: Run red** — FAIL.

- [ ] **Step 3: Implement** — add the tool schema (`action` enum `["list","apply"]`, optional
  `task_id`/`option`/`packet_signature`/`board`); add to the orchestrator toolset in
  `toolsets.py`; update the wake-triage prompt (`_jensen_prompt_text`) to instruct Jensen to call
  `kanban_reconcile{action:"list"}` then `apply`; document `reconcile_tool_auto_options` in
  `config.yaml`.

- [ ] **Step 4: Run green** — `python -m pytest tests/hermes_cli/ -k reconcile -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/kanban_tools.py toolsets.py scripts/kanban_reconcile_wake_triage.py \
        config.yaml tests/hermes_cli/test_kanban_reconcile_tool_schema.py
git commit -m "feat(kanban): register kanban_reconcile tool, toolset, and wake-prompt routing"
```

---

## WS5 acceptance criteria

- Jensen, woken on a `jensen_decision_required` packet, can `kanban_reconcile{action:"list"}` and
  `kanban_reconcile{action:"apply", …}` an allowlisted option end-to-end with no shell.
- Non-allowlisted options return `needs_human` (still escalate to a person).
- The apply path keeps the existing fresh-pass signature re-validation (no blind application of
  stale wake output).
- The tool is orchestrator-only (hidden from task workers).
