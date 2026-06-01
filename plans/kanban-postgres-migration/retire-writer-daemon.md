# Retire sqlite writer daemon under Postgres — Implementation Plan (Phase 6 / B3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Under `kanban.backend=postgres`, the gateway does not start the sqlite single-writer daemon or its watchdog (so it stops opening the frozen `kanban.db` + WAL). Under `sqlite`, behavior is byte-identical.

**Architecture:** Add one fork-owned helper `GatewayRunner._writer_daemon_should_run()` = `single_writer_enabled() and resolve_backend() != "postgres"` (defensive: a resolve failure falls back to flag-only), and use it in place of the bare `single_writer_enabled()` guard in `_start_kanban_writer_daemon()` and `_kanban_writer_watchdog()`. No edits to `hermes_cli/kanban_db.py` or `hermes_cli/kanban_writer_daemon.py` (upstream). The daemon has no live callers under PG (dispatcher PG branch + notifier route through the store; `_board_write` is dead code), so this is a pure dead-weight removal.

**Tech Stack:** Python, pytest. Gateway class is `GatewayRunner` in `gateway/run.py` (~line 2231). Test interpreter: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`. No docker/PG needed — monkeypatch `resolve_backend`.

**Hard boundaries:**
- `hermes_cli/kanban_db.py` + `hermes_cli/kanban_writer_daemon.py` **not edited** (upstream); `single_writer_enabled()` reused, not modified.
- sqlite path **byte-identical**: under the default sqlite backend, `_writer_daemon_should_run()` reduces to `single_writer_enabled()`, so existing behavior is unchanged.
- Default backend stays sqlite.

---

### Task 1: Backend-gate the writer daemon + watchdog under Postgres

**Files:**
- Modify: `gateway/run.py` — add `_writer_daemon_should_run()` to `GatewayRunner` (near `_kanban_writer_recovery_cfg`, ~line 5566); change the guard in `_start_kanban_writer_daemon()` (line 5584) and `_kanban_writer_watchdog()` (line 5675).
- Test: `tests/gateway/test_kanban_writer_daemon_postgres.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/gateway/test_kanban_writer_daemon_postgres.py`:

```python
"""Under backend=postgres the gateway must NOT start the sqlite writer daemon
or its watchdog (the daemon would needlessly open the frozen kanban.db + WAL).
Under sqlite, behavior is unchanged."""
import types

import gateway.run as gr


def _stub():
    # _writer_daemon_should_run / _start_kanban_writer_daemon use no GatewayRunner
    # state beyond _kanban_writer_daemons, so a lightweight stub with the methods
    # bound is sufficient (avoids constructing a full GatewayRunner).
    s = types.SimpleNamespace(_kanban_writer_daemons=[])
    s._writer_daemon_should_run = gr.GatewayRunner._writer_daemon_should_run.__get__(s)
    return s


def _patch_backend(monkeypatch, backend):
    import hermes_cli.kanban.store as store_mod
    monkeypatch.setattr(store_mod, "resolve_backend", lambda: backend, raising=True)


def test_should_run_false_under_postgres_even_with_flag_on(monkeypatch):
    import hermes_cli.kanban_db as kb
    monkeypatch.setattr(kb, "single_writer_enabled", lambda *a, **k: True, raising=True)
    _patch_backend(monkeypatch, "postgres")
    assert _stub()._writer_daemon_should_run() is False


def test_should_run_true_under_sqlite_with_flag_on(monkeypatch):
    import hermes_cli.kanban_db as kb
    monkeypatch.setattr(kb, "single_writer_enabled", lambda *a, **k: True, raising=True)
    _patch_backend(monkeypatch, "sqlite")
    assert _stub()._writer_daemon_should_run() is True


def test_should_run_false_under_sqlite_with_flag_off(monkeypatch):
    import hermes_cli.kanban_db as kb
    monkeypatch.setattr(kb, "single_writer_enabled", lambda *a, **k: False, raising=True)
    _patch_backend(monkeypatch, "sqlite")
    assert _stub()._writer_daemon_should_run() is False


def test_should_run_falls_back_to_flag_when_resolve_raises(monkeypatch):
    import hermes_cli.kanban.store as store_mod
    import hermes_cli.kanban_db as kb
    def _boom():
        raise RuntimeError("config blip")
    monkeypatch.setattr(store_mod, "resolve_backend", _boom, raising=True)
    monkeypatch.setattr(kb, "single_writer_enabled", lambda *a, **k: True, raising=True)
    # resolve failure → fall back to flag-only (sqlite default) → True
    assert _stub()._writer_daemon_should_run() is True


def test_start_writer_daemon_is_noop_under_postgres(monkeypatch):
    import hermes_cli.kanban_db as kb
    monkeypatch.setattr(kb, "single_writer_enabled", lambda *a, **k: True, raising=True)
    _patch_backend(monkeypatch, "postgres")
    called = {"spawn": False}
    monkeypatch.setattr(gr, "_spawn_writer_daemons",
                        lambda *a, **k: called.__setitem__("spawn", True) or [],
                        raising=True)
    s = _stub()
    gr.GatewayRunner._start_kanban_writer_daemon(s)
    assert called["spawn"] is False           # never spawned under postgres
    assert s._kanban_writer_daemons == []     # registry untouched


def test_start_writer_daemon_spawns_under_sqlite(monkeypatch):
    import hermes_cli.kanban_db as kb
    monkeypatch.setattr(kb, "single_writer_enabled", lambda *a, **k: True, raising=True)
    monkeypatch.setattr(kb, "list_boards", lambda **k: [{"slug": "default"}], raising=True)
    monkeypatch.setattr(kb, "kanban_db_path", lambda **k: __import__("pathlib").Path("/tmp/x/kanban.db"), raising=True)
    _patch_backend(monkeypatch, "sqlite")
    spawned = {"called": False}
    monkeypatch.setattr(gr, "_spawn_writer_daemons",
                        lambda *a, **k: spawned.__setitem__("called", True) or ["d"],
                        raising=True)
    s = _stub()
    s._kanban_writer_recovery_cfg = gr.GatewayRunner._kanban_writer_recovery_cfg.__get__(s)
    gr.GatewayRunner._start_kanban_writer_daemon(s)
    assert spawned["called"] is True          # sqlite path unchanged → spawns
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/ctao/.hermes/hermes-agent/.worktrees/kanban-pg-retire-writer-daemon && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/gateway/test_kanban_writer_daemon_postgres.py -v`
Expected: FAIL — `AttributeError: ... '_writer_daemon_should_run'` (helper not defined) and the postgres no-op test fails (start still spawns under postgres).

> Implementer note: confirm `gr._spawn_writer_daemons` is a module-level function (it is — `gateway/run.py` ~2175). If `test_start_writer_daemon_spawns_under_sqlite` needs more stub attrs (e.g. `_kanban_writer_recovery_cfg` returns a 3-tuple `(auto_recovery, keep, interval)`), it's bound from the real class above; if that method touches config and raises in the test env, instead monkeypatch it on the stub to `lambda: (False, 5, 60)`. Adjust minimally so the sqlite test proves spawn IS reached; the key assertions are the postgres no-op + the should_run truth table.

- [ ] **Step 3: Add the helper + swap the two guards**

In `gateway/run.py`, add the helper method to `GatewayRunner` (place it just before `_start_kanban_writer_daemon`, ~line 5582):

```python
    def _writer_daemon_should_run(self) -> bool:
        """Whether to run the sqlite single-writer daemon + watchdog.

        The daemon serializes writable sqlite access; under
        ``kanban.backend=postgres`` writes route through the store, so the
        daemon serves no purpose and would needlessly open the frozen
        ``kanban.db`` (+ WAL sidecars). Run it only for the sqlite backend with
        the single-writer flag enabled. A backend-resolution failure falls back
        to flag-only behavior (sqlite default), so a config hiccup never
        mis-gates the gateway."""
        try:
            from hermes_cli.kanban.store import resolve_backend
            if resolve_backend() == "postgres":
                return False
        except Exception:
            pass
        import hermes_cli.kanban_db as _kb
        return _kb.single_writer_enabled()
```

In `_start_kanban_writer_daemon()` (line 5583-5584), change:
```python
        import hermes_cli.kanban_db as _kb
        if not _kb.single_writer_enabled():
            return
```
to:
```python
        import hermes_cli.kanban_db as _kb
        if not self._writer_daemon_should_run():
            logger.debug("kanban writer daemon: skipped (postgres backend or "
                         "single-writer disabled)")
            return
```
(Keep the `import hermes_cli.kanban_db as _kb` line — the rest of the method still uses `_kb`.)

In `_kanban_writer_watchdog()` (line 5674-5675), change:
```python
        import hermes_cli.kanban_db as _kb
        if not _kb.single_writer_enabled():
            return
```
to:
```python
        import hermes_cli.kanban_db as _kb
        if not self._writer_daemon_should_run():
            return
```
(The `import` line may be unused after this in the watchdog; leave it if other code in the method uses `_kb`, otherwise it's harmless. Do NOT remove anything else.)

> Implementer note: leave `_writer_watchdog_tick` (line 5622) and all other `single_writer_enabled()` call sites (5634, 5744, 6363, 7033, 5962) UNCHANGED — the watchdog loop gate at 5675 means the tick is never reached under PG, and the others are sqlite-only/dead-code/out-of-scope per the design.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/ctao/.hermes/hermes-agent/.worktrees/kanban-pg-retire-writer-daemon && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/gateway/test_kanban_writer_daemon_postgres.py -v`
Expected: PASS (all).

- [ ] **Step 5: Regression — existing writer-daemon/watchdog/notifier tests stay green (sqlite byte-identical)**

Run: `cd /Users/ctao/.hermes/hermes-agent/.worktrees/kanban-pg-retire-writer-daemon && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/gateway/test_kanban_writer_lifecycle.py tests/gateway/test_kanban_writer_watchdog.py tests/gateway/test_kanban_writer_watchdog_restart.py tests/gateway/test_kanban_notifier_single_writer.py -q`
Expected: all PASS (these run under the default sqlite backend, where `_writer_daemon_should_run()` == `single_writer_enabled()`).

- [ ] **Step 6: Confirm boundaries**

Run: `cd /Users/ctao/.hermes/hermes-agent/.worktrees/kanban-pg-retire-writer-daemon && git diff --stat main -- hermes_cli/kanban_db.py hermes_cli/kanban_writer_daemon.py`
Expected: **empty** (upstream untouched).

- [ ] **Step 7: Commit**

```bash
git add gateway/run.py tests/gateway/test_kanban_writer_daemon_postgres.py
git commit -m "feat(kanban-pg): gateway skips the sqlite writer daemon + watchdog under backend=postgres"
```

---

## Self-Review

**Spec coverage:**
- Gate `_start_kanban_writer_daemon` under PG → Task 1 (Step 3 first guard). ✓
- Gate `_kanban_writer_watchdog` under PG → Task 1 (Step 3 second guard). ✓
- One helper holding the rule → `_writer_daemon_should_run` (Step 3). ✓
- Defensive fallback on resolve failure → helper `try/except`, pinned by `test_should_run_falls_back_to_flag_when_resolve_raises`. ✓
- sqlite byte-identical → helper reduces to `single_writer_enabled()` under sqlite; regression suite Step 5. ✓
- kanban_db.py / kanban_writer_daemon.py untouched → Step 6. ✓
- `_board_write` dead code / other call sites unchanged → Step 3 implementer note. ✓

**Placeholder scan:** every step shows complete code; the two implementer notes flag reality-checks (stub attrs, leaving other guards alone) with the tests as the executable spec.

**Type/name consistency:** `_writer_daemon_should_run` defined (Step 3) and used in both guards (Step 3) + the test (Step 1); `_spawn_writer_daemons` is the existing module fn spied in the test; `GatewayRunner` is the class.
