# Kanban Recovery Cron Supervision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make gateway readiness and health prove that cron is armed, advancing, and running the exact declared blocked-lifecycle controllers, with bounded automatic recovery and no restart loop.

**Architecture:** Add exact runtime hashing before background work, extend the cron provider contract with readiness and health snapshots, and move notifier/dispatcher startup behind cron readiness. A gateway-owned supervisor checks provider and controller-job health independently of cron, alerts through live adapters, performs only exact disabled-bit reconciliation, and allows at most one automatic restart per unchanged source/static-config fingerprint.

**Tech Stack:** Python 3.11, asyncio, threads, SHA-256, atomic JSON state, cron provider ABC, jobs.json locked store, systemd `sd_notify`, pytest through `scripts/run_tests.sh`.

## Global Constraints

- Use `scripts/run_tests.sh` for every test command.
- Complete the shared recovery-control configuration task from the router plan before Task 8.
- The runtime fingerprint is written after the gateway owns its lock/PID identity and before cron, notifier, dispatcher, or other mutating background work.
- Module hashes are authoritative; Git commit/dirty state is diagnostic.
- Dynamic `kanban.blocked_recovery` mode, allowlist, and controller-manifest revisions are hot-reloaded and excluded from restart drift.
- Restart-sensitive cron provider/interval and served-profile scope are included in the static config digest.
- Default cron readiness deadline is 30 seconds.
- Provider health is checked every 30 seconds outside the cron ticker.
- Dead ticker, stale heartbeat, or three failed cycles is unhealthy.
- Failure-class changes do not mint new restart budgets.
- Systemd watchdog continues to represent event-loop liveness during cron application failure.
- Exact disabled-only drift may self-heal; missing, duplicate, modified, stale, or intentionally paused jobs remain nonmutating.
- Unknown job-enable outcomes reconcile before any second attempt.
- No live controller job is enabled while implementing this plan.

---

## File Structure

- Create `gateway/runtime_fingerprint.py`: exact source/static-config manifest hashing, atomic startup snapshot, and drift comparison.
- Create `gateway/recovery_control.py`: narrowly hot-reloaded last-known-good recovery control and mutation gate.
- Create `gateway/cron_supervisor.py`: provider health evaluation, direct alerts, restart quarantine, controller-job manifest reconciliation, and durable supervision journal.
- Modify `cron/scheduler_provider.py`: additive readiness and health snapshot contract; built-in implementation.
- Modify `cron/jobs.py`: failed-cycle counters, job role/spec metadata, disabled visibility, and operation-keyed guarded enable.
- Modify `cron/executions.py`: execution heartbeat used by stale-success evaluation.
- Modify `cron/scheduler.py`: update execution heartbeat during real work and mark provider cycle success/failure.
- Modify `gateway/run.py`: startup ordering and supervisor lifecycle.
- Modify `gateway/systemd_notify.py`: separate event-loop feed from application-health status.
- Modify `gateway/status.py`: persist and expose cron application health without treating it as PID death.
- Modify `gateway/code_skew.py`: delegate restart-sensitive detection to the exact runtime fingerprint while preserving compact callers.
- Modify `hermes_cli/cron.py`: effective-health output, disabled required-job visibility, and guarded health acknowledgement.
- Add `tests/gateway/test_runtime_fingerprint.py`, `tests/gateway/test_cron_supervisor.py`, `tests/gateway/test_gateway_cron_readiness.py`, and `tests/cron/test_controller_job_manifest.py`.
- Modify existing provider, scheduler, status, startup, and systemd-watchdog tests.

### Task 1: Persist the exact gateway runtime fingerprint

**Files:**
- Create: `gateway/runtime_fingerprint.py`
- Create: `tests/gateway/test_runtime_fingerprint.py`
- Modify: `gateway/code_skew.py`

**Interfaces:**
- Produces: `build_runtime_fingerprint(static_config: Mapping[str, object]) -> RuntimeFingerprint`
- Produces: `write_runtime_fingerprint(path: Path, value: RuntimeFingerprint) -> None`
- Produces: `compare_runtime_fingerprint(startup: RuntimeFingerprint, static_config) -> RuntimeDrift`

- [ ] **Step 1: Write source-closure and drift tests**

```python
def test_fingerprint_hashes_imported_module_paths(tmp_path, monkeypatch):
    fp = build_runtime_fingerprint(static_config())
    assert fp.module_hashes["gateway.run"] == sha256(Path(gateway.run.__file__))
    assert fp.module_hashes["cron.scheduler_provider"] == sha256(Path(cron.scheduler_provider.__file__))

def test_docs_only_change_does_not_require_restart(fingerprint_fixture):
    touch(fingerprint_fixture.repo / "README.md")
    assert compare_runtime_fingerprint(fingerprint_fixture.startup, static_config()).restart_required is False

def test_loaded_module_change_names_exact_drift(fingerprint_fixture):
    mutate(fingerprint_fixture.module("gateway.run"))
    drift = compare_runtime_fingerprint(fingerprint_fixture.startup, static_config())
    assert drift.changed_modules == ("gateway.run",)
    assert drift.restart_required is True

def test_dynamic_recovery_mode_change_is_not_static_drift(fingerprint_fixture):
    assert compare_runtime_fingerprint(
        fingerprint_fixture.startup,
        static_config(review_mode="live", transient_mode="shadow"),
    ).restart_required is False
```

- [ ] **Step 2: Verify RED**

Run: `scripts/run_tests.sh tests/gateway/test_runtime_fingerprint.py -q`

Expected: current code-skew detection compares only the Git revision and has no module manifest.

- [ ] **Step 3: Implement the authoritative manifest and atomic snapshot**

```python
RESTART_SENSITIVE_MODULES = (
    "gateway.run", "gateway.kanban_watchers", "gateway.status",
    "gateway.code_skew", "gateway.systemd_notify",
    "gateway.runtime_fingerprint", "cron.scheduler", "cron.scheduler_provider",
    "cron.jobs", "cron.executions", "hermes_cli.kanban_db",
    "hermes_cli.config", "hermes_constants", "hermes_time",
)

@dataclass(frozen=True)
class RuntimeFingerprint:
    schema_version: int
    pid: int
    process_started_at: int
    recorded_at: str
    python_executable: str
    git_commit: str | None
    git_dirty: bool | None
    module_hashes: Mapping[str, str]
    static_config_digest: str

    @property
    def source_config_key(self) -> str:
        payload = {"module_hashes": self.module_hashes, "static_config_digest": self.static_config_digest}
        return canonical_sha256(payload)
```

Resolve paths from imported module `__file__`, reject missing/unreadable
required modules, hash file bytes, include PID plus `get_process_start_time`,
Python executable, Git diagnostics, and static cron/provider/profile scope.
Write with a unique same-directory temporary file, file `fsync`, `os.replace`,
and directory `fsync`. Keep `gateway.code_skew.detect_code_skew()` as a compact
adapter over the new comparison. Tasks 6 and 8 extend this same authoritative
manifest in the commits that create `gateway.cron_supervisor` and
`gateway.recovery_control`; Task 1 must not require not-yet-created modules.

- [ ] **Step 4: Run fingerprint and code-skew tests**

Run: `scripts/run_tests.sh tests/gateway/test_runtime_fingerprint.py tests/test_code_skew.py -q`

Expected: all pass without false positives for docs or freshly loaded local cron scripts.

- [ ] **Step 5: Commit runtime fingerprinting**

```bash
git add gateway/runtime_fingerprint.py gateway/code_skew.py tests/gateway/test_runtime_fingerprint.py tests/test_code_skew.py
git commit -m "feat(gateway): fingerprint restart-sensitive runtime"
```

### Task 2: Extend cron providers with readiness and health

**Files:**
- Modify: `cron/scheduler_provider.py`
- Modify: `cron/jobs.py`
- Modify: `tests/cron/test_scheduler_provider.py`
- Modify: `tests/cron/test_ticker_stall_60703.py`

**Interfaces:**
- Produces: `CronProviderHealth`
- Adds: `CronScheduler.wait_ready(timeout: float) -> bool`
- Adds: `CronScheduler.health_snapshot() -> CronProviderHealth`
- Adds: `CronScheduler.mark_ready() -> None`

- [ ] **Step 1: Write built-in and external-provider contract tests**

```python
def test_builtin_ready_only_after_initial_durable_heartbeat(tmp_cron_store):
    provider = InProcessCronScheduler()
    thread = start_provider(provider, interval=0.01)
    assert provider.wait_ready(1.0) is True
    assert ticker_heartbeat_path().exists()
    stop_provider(thread)

def test_external_provider_that_returns_without_signal_is_not_ready():
    provider = ReturningExternalProvider(signals_ready=False)
    start_provider(provider)
    assert provider.wait_ready(0.01) is False

def test_external_armed_provider_is_healthy_after_start_returns():
    provider = ReturningExternalProvider(signals_ready=True)
    start_provider(provider)
    assert provider.health_snapshot().armed is True
    assert provider.health_snapshot().thread_required is False
```

- [ ] **Step 2: Verify RED**

Run: `scripts/run_tests.sh tests/cron/test_scheduler_provider.py tests/cron/test_ticker_stall_60703.py -q`

Expected: the provider ABC has no readiness/health contract.

- [ ] **Step 3: Add default-safe methods and built-in state**

```python
@dataclass(frozen=True)
class CronProviderHealth:
    provider: str
    ready: bool
    armed: bool
    thread_required: bool
    thread_alive: bool | None
    last_heartbeat_at: int | None
    last_success_at: int | None
    consecutive_failed_cycles: int
    failure: str | None

class CronScheduler(ABC):
    def _readiness_event(self) -> threading.Event:
        event = getattr(self, "_cron_readiness_event", None)
        if event is None:
            event = threading.Event()
            self._cron_readiness_event = event
        return event

    def mark_ready(self) -> None:
        self._readiness_event().set()

    def wait_ready(self, timeout: float) -> bool:
        return self._readiness_event().wait(timeout)
```

Keep the additions non-abstract. The built-in calls `mark_ready()` only after
its initial heartbeat is durably recorded. It tracks its running thread and
cycle failure count. External providers must call `mark_ready()` only after
their schedule is armed and return `thread_required=False` from health.

- [ ] **Step 4: Run provider, scheduler, and profile-multiplex tests**

Run: `scripts/run_tests.sh tests/cron/test_scheduler_provider.py tests/cron/test_scheduler.py tests/cron/test_ticker_stall_60703.py tests/cron/test_cron_profile_isolation.py -q`

Expected: all pass for single-profile, multiplex, blocking, and returning providers.

- [ ] **Step 5: Commit provider readiness**

```bash
git add cron/scheduler_provider.py cron/jobs.py tests/cron/test_scheduler_provider.py tests/cron/test_ticker_stall_60703.py
git commit -m "feat(cron): expose provider readiness and health"
```

### Task 3: Track real tick-cycle and execution health

**Files:**
- Modify: `cron/jobs.py`
- Modify: `cron/executions.py`
- Modify: `cron/scheduler.py`
- Create: `tests/cron/test_execution_health.py`
- Modify: `tests/cron/test_scheduler.py`

**Interfaces:**
- Produces: `record_ticker_cycle(success: bool, error: str | None) -> None`
- Produces: `heartbeat_execution(execution_id: str) -> bool`
- Adds: `heartbeat_at` to `executions`

- [ ] **Step 1: Write failed-cycle and in-flight heartbeat tests**

```python
def test_three_failed_cycles_persist_count(tmp_cron_store):
    for _ in range(3):
        record_ticker_cycle(success=False, error="locked")
    health = read_ticker_health()
    assert health.consecutive_failed_cycles == 3
    record_ticker_cycle(success=True, error=None)
    assert read_ticker_health().consecutive_failed_cycles == 0

def test_running_execution_heartbeat_advances(monkeypatch, tmp_cron_store):
    execution = create_execution("job-1", source="builtin")
    monkeypatch.setattr(executions, "_now", lambda: "2026-08-03T00:01:00+00:00")
    assert heartbeat_execution(execution["id"]) is True
    assert latest_execution("job-1")["heartbeat_at"].endswith("00:01:00+00:00")
```

- [ ] **Step 2: Verify RED**

Run: `scripts/run_tests.sh tests/cron/test_execution_health.py tests/cron/test_scheduler.py -q`

Expected: failed cycles are represented only by heartbeat/error files and executions have no live heartbeat field.

- [ ] **Step 3: Implement additive execution migration and cycle state**

Add `heartbeat_at TEXT` with idempotent `PRAGMA table_info` migration. Set it
when execution becomes running and update it from the existing run-claim
heartbeat loop. Store ticker consecutive-failure count and last failure class in
an atomically written health JSON beside existing heartbeat files. A cycle is
successful only when `cron_tick()` returns without exception; dispatching zero
jobs is still a successful cycle.

- [ ] **Step 4: Run execution ledger and scheduler tests**

Run: `scripts/run_tests.sh tests/cron/test_execution_health.py tests/cron/test_execution_ledger.py tests/cron/test_scheduler.py tests/cron/test_script_claim_heartbeat.py -q`

Expected: all pass and a fresh in-flight execution is distinguishable from stale success.

- [ ] **Step 5: Commit durable cron health data**

```bash
git add cron/jobs.py cron/executions.py cron/scheduler.py tests/cron/test_execution_health.py tests/cron/test_scheduler.py
git commit -m "feat(cron): persist cycle and execution health"
```

### Task 4: Split gateway adapter startup from mutating background loops

**Files:**
- Modify: `gateway/run.py:7994-8810`
- Create: `tests/gateway/test_gateway_cron_readiness.py`
- Modify: `tests/gateway/test_runner_startup_failures.py`
- Modify: `tests/gateway/test_systemd_watchdog_lifecycle.py`

**Interfaces:**
- Changes: `GatewayRunner.start(*, start_background_mutation: bool = True) -> bool`
- Produces: `GatewayRunner.start_background_mutation() -> None`
- Produces: `GatewayRunner.background_mutation_started: bool`

- [ ] **Step 1: Write startup-order tests**

```python
@pytest.mark.asyncio
async def test_deferred_start_connects_adapters_without_dispatcher(runner):
    assert await runner.start(start_background_mutation=False)
    assert runner.adapters
    assert runner.background_mutation_started is False
    assert "kanban_dispatcher_watcher" not in runner.supervised_names

@pytest.mark.asyncio
async def test_background_start_is_idempotent(runner):
    await runner.start(start_background_mutation=False)
    runner.start_background_mutation()
    runner.start_background_mutation()
    assert runner.supervised_names.count("kanban_dispatcher_watcher") == 1
```

- [ ] **Step 2: Verify RED**

Run: `scripts/run_tests.sh tests/gateway/test_gateway_cron_readiness.py tests/gateway/test_runner_startup_failures.py -q`

Expected: `GatewayRunner.start()` immediately starts notifier/dispatcher watchers.

- [ ] **Step 3: Extract the background mutation boundary**

Move session-expiry, Kanban notifier, Kanban dispatcher, recovered-process
watcher, and other mutating background spawns that can create work behind
`start_background_mutation()`. Keep adapter connection, authorization, startup
restore, and direct-delivery capability available before that boundary. Guard
every mutating background entry point with one shared startup event so a future
call site cannot bypass ordering.

- [ ] **Step 4: Run gateway startup and watcher tests**

Run: `scripts/run_tests.sh tests/gateway/test_gateway_cron_readiness.py tests/gateway/test_runner_startup_failures.py tests/gateway/test_kanban_notifier.py tests/gateway/test_systemd_watchdog_lifecycle.py -q`

Expected: all pass and the default `start()` remains backward compatible for direct test callers.

- [ ] **Step 5: Commit the startup boundary**

```bash
git add gateway/run.py tests/gateway/test_gateway_cron_readiness.py tests/gateway/test_runner_startup_failures.py
git commit -m "refactor(gateway): gate mutating loops behind startup boundary"
```

### Task 5: Enforce fingerprint, cron readiness, mutation, and READY ordering

**Files:**
- Modify: `gateway/run.py:24216-24793`
- Modify: `gateway/systemd_notify.py`
- Modify: `tests/gateway/test_gateway_cron_readiness.py`
- Modify: `tests/gateway/test_systemd_notify.py`

**Interfaces:**
- Produces startup order: lock/PID → fingerprint → adapters → cron ready → mutating background → `READY=1`
- Consumes: `cron.startup_ready_timeout_seconds`, default `30`

- [ ] **Step 1: Write readiness timeout and exact-order tests**

```python
@pytest.mark.asyncio
async def test_cron_timeout_aborts_before_background_mutation(start_harness):
    result = await start_harness(provider=never_ready_provider(), timeout=0.01)
    assert result is False
    assert result.runner.background_mutation_started is False
    assert result.systemd_messages == []

@pytest.mark.asyncio
async def test_ready_follows_cron_and_background_boundary(start_harness):
    result = await start_harness(provider=ready_provider())
    assert result.events.index("runtime_fingerprint") < result.events.index("adapters_connected")
    assert result.events.index("adapters_connected") < result.events.index("cron_ready")
    assert result.events.index("cron_ready") < result.events.index("background_mutation")
    assert result.events.index("background_mutation") < result.events.index("READY=1")
```

- [ ] **Step 2: Verify RED**

Run: `scripts/run_tests.sh tests/gateway/test_gateway_cron_readiness.py tests/gateway/test_systemd_notify.py -q`

Expected: current top-level gateway starts cron after `runner.start()` has already spawned the dispatcher.

- [ ] **Step 3: Reorder top-level startup and preserve watchdog meaning**

After runtime lock/PID ownership, write the fingerprint. Call
`runner.start(start_background_mutation=False)`, resolve/start provider, then
await `provider.wait_ready()` from an executor with the configured deadline.
On timeout, set startup-failed runtime status, stop provider/adapters, and return
false. On readiness, call `runner.start_background_mutation()`, start the
supervisor and housekeeping, then start `SystemdWatchdog` and emit `READY=1`.

Add `SystemdWatchdog.application_status(message, healthy)` that sends only
`STATUS=...`; it never changes `_unhealthy` and never stops `WATCHDOG=1` feeds.

- [ ] **Step 4: Run gateway lifecycle and systemd tests**

Run: `scripts/run_tests.sh tests/gateway/test_gateway_cron_readiness.py tests/gateway/test_systemd_notify.py tests/gateway/test_systemd_watchdog_lifecycle.py tests/gateway/test_runner_startup_failures.py -q`

Expected: all pass and cron application failure cannot cause a systemd watchdog restart loop.

- [ ] **Step 5: Commit readiness-gated startup**

```bash
git add gateway/run.py gateway/systemd_notify.py tests/gateway/test_gateway_cron_readiness.py tests/gateway/test_systemd_notify.py tests/gateway/test_systemd_watchdog_lifecycle.py
git commit -m "feat(gateway): require cron readiness before mutation"
```

### Task 6: Add gateway-owned cron health supervision and direct alerts

**Files:**
- Create: `gateway/cron_supervisor.py`
- Create: `tests/gateway/test_cron_supervisor.py`
- Modify: `gateway/runtime_fingerprint.py`
- Modify: `tests/gateway/test_runtime_fingerprint.py`
- Modify: `gateway/run.py`
- Modify: `gateway/status.py`

**Interfaces:**
- Produces: `CronSupervisor.evaluate(provider_health, now) -> SupervisionDecision`
- Produces: `CronSupervisor.run(interval_seconds: float = 30.0) -> Awaitable[None]`
- Consumes: direct async `alert(message: str)` callback using live adapters

- [ ] **Step 1: Write dead/stale/failed-cycle and direct-alert tests**

```python
@pytest.mark.parametrize("health, failure", [
    (provider_health(thread_required=True, thread_alive=False), "dead_ticker"),
    (provider_health(last_heartbeat_at=100), "stale_heartbeat"),
    (provider_health(consecutive_failed_cycles=3), "failed_cycles"),
])
def test_unhealthy_classes(health, failure):
    decision = supervisor(now=400).evaluate(health)
    assert decision.healthy is False
    assert failure in decision.failure_classes

@pytest.mark.asyncio
async def test_alert_does_not_depend_on_cron_tick(supervisor, adapter_spy):
    await supervisor.check_once(unhealthy_provider())
    assert adapter_spy.messages == [pytest.helpers.contains("CRON_UNHEALTHY")]

def test_supervisor_module_is_in_authoritative_runtime_manifest():
    assert "gateway.cron_supervisor" in RESTART_SENSITIVE_MODULES
```

- [ ] **Step 2: Verify RED**

Run: `scripts/run_tests.sh tests/gateway/test_cron_supervisor.py -q`

Expected: no gateway task independently evaluates ticker/provider health.

- [ ] **Step 3: Implement health classification and application status**

```python
@dataclass(frozen=True)
class SupervisionDecision:
    healthy: bool
    failure_classes: tuple[str, ...]
    signature: str
    restart_allowed: bool
    quarantine: bool
```

For the built-in provider, unhealthy means dead required thread, heartbeat age
greater than `max(3 * configured_interval, 180)`, or at least three consecutive
failed cycles. External providers use armed/liveness fields without requiring a
live startup thread. Persist compact health in gateway runtime status, mark
blocked-lifecycle mutation disabled when unhealthy/quarantined, send a
change-gated direct alert, and continue the systemd event-loop feed.
Add `gateway.cron_supervisor` to the authoritative runtime manifest in the same
commit that creates the module, then assert that its imported `__file__` hash is
present in the startup snapshot.

- [ ] **Step 4: Run supervisor, status, and provider tests**

Run: `scripts/run_tests.sh tests/gateway/test_cron_supervisor.py tests/gateway/test_runtime_fingerprint.py tests/gateway/test_status.py tests/gateway/test_systemd_watchdog_lifecycle.py tests/cron/test_scheduler_provider.py -q`

Expected: all pass; health detection runs even when no cron tick can execute.

- [ ] **Step 5: Commit gateway-owned cron supervision**

```bash
git add gateway/cron_supervisor.py gateway/runtime_fingerprint.py gateway/run.py \
  gateway/status.py tests/gateway/test_cron_supervisor.py \
  tests/gateway/test_runtime_fingerprint.py tests/gateway/test_status.py
git commit -m "feat(gateway): supervise cron health independently"
```

### Task 7: Add bounded automatic restart and quarantine

**Files:**
- Modify: `gateway/cron_supervisor.py`
- Modify: `gateway/run.py`
- Modify: `hermes_cli/cron.py`
- Modify: `tests/gateway/test_cron_supervisor.py`
- Modify: `tests/hermes_cli/test_cron.py`

**Interfaces:**
- Persists: `<default-root>/state/cron-supervision.json`
- CLI: `hermes cron acknowledge-health --signature <signature> --json`

- [ ] **Step 1: Write unchanged-fingerprint restart-budget tests**

```python
def test_alternating_failure_classes_share_one_restart_budget(supervisor):
    first = supervisor.evaluate(unhealthy("dead_ticker"))
    second = supervisor.evaluate(unhealthy("stale_heartbeat"))
    assert first.restart_allowed is True
    supervisor.record_restart_requested(first)
    assert supervisor.evaluate(unhealthy("failed_cycles")).restart_allowed is False

def test_restarted_unhealthy_process_enters_quarantine(supervisor):
    supervisor.record_restart_requested(supervisor.evaluate(unhealthy("dead_ticker")))
    decision = supervisor.evaluate(unhealthy("dead_ticker"), process_started_at=next_start())
    assert decision.quarantine is True

def test_exact_acknowledgement_or_fingerprint_change_grants_one_new_budget(supervisor):
    quarantined = supervisor.quarantined_decision()
    assert supervisor.acknowledge("wrong").accepted is False
    assert supervisor.acknowledge(quarantined.signature).accepted is True
    assert supervisor.evaluate(unhealthy("dead_ticker")).restart_allowed is True
```

- [ ] **Step 2: Verify RED**

Run: `scripts/run_tests.sh tests/gateway/test_cron_supervisor.py tests/hermes_cli/test_cron.py -q`

Expected: no durable per-fingerprint restart budget or health acknowledgement exists.

- [ ] **Step 3: Persist and enforce one restart per source/static-config key**

The supervision record contains schema version, source/static-config key,
process-start fingerprint, observed failure classes, restart-requested time,
restarted process start, quarantine signature, and acknowledgement. Write it
atomically with checksum and `fsync`. The supervisor invokes the existing
gateway restart request once. If the new process is unhealthy under the same
key, keep it running, withhold healthy application status, and quarantine.
Changing failure class updates diagnostics only. Exact health acknowledgement
or a new source/static-config key clears quarantine and grants one budget.

- [ ] **Step 4: Run restart-loop and CLI tests**

Run: `scripts/run_tests.sh tests/gateway/test_cron_supervisor.py tests/hermes_cli/test_cron.py tests/hermes_cli/test_gateway_restart_loop.py tests/gateway/test_startup_restart_race.py -q`

Expected: all pass with at most one restart request per unchanged key.

- [ ] **Step 5: Commit restart quarantine**

```bash
git add gateway/cron_supervisor.py gateway/run.py hermes_cli/cron.py tests/gateway/test_cron_supervisor.py tests/hermes_cli/test_cron.py
git commit -m "feat(cron): quarantine repeated unhealthy restarts"
```

### Task 8: Hot-reload recovery controls with a last-known-good observation path

**Files:**
- Create: `gateway/recovery_control.py`
- Create: `tests/gateway/test_recovery_control.py`
- Modify: `gateway/cron_supervisor.py`
- Modify: `gateway/runtime_fingerprint.py`
- Modify: `tests/gateway/test_runtime_fingerprint.py`

**Interfaces:**
- Produces: `RecoveryControlWatcher.load_initial() -> ValidatedControl`
- Produces: `RecoveryControlWatcher.poll() -> ControlReloadResult`
- Produces: `RecoveryControlWatcher.current_observation -> RecoveryControl`
- Produces: `RecoveryControlWatcher.mutation_allowed -> bool`

- [ ] **Step 1: Write narrow-reload and malformed-revision tests**

```python
def test_mode_reload_does_not_require_gateway_restart(watcher):
    first = watcher.load_initial()
    write_control(review_mode="live", transient_mode="shadow")
    result = watcher.poll()
    assert result.applied is True
    assert result.restart_required is False
    assert result.value.digest() != first.value.digest()

def test_malformed_reload_retains_observation_and_disables_mutation(watcher):
    initial = watcher.load_initial()
    write_malformed_yaml()
    result = watcher.poll()
    assert result.applied is False
    assert watcher.current_observation == initial.value
    assert watcher.mutation_allowed is False

def test_recovery_control_module_is_in_authoritative_runtime_manifest():
    assert "gateway.recovery_control" in RESTART_SENSITIVE_MODULES
```

- [ ] **Step 2: Verify RED**

Run: `scripts/run_tests.sh tests/gateway/test_recovery_control.py -q`

Expected: no gateway-owned narrow recovery-control watcher exists.

- [ ] **Step 3: Implement revision/digest validation without rebuilding runtime state**

Poll config mtime/size, load through `hermes_cli.kanban_recovery_config`, and
replace only the validated recovery-control snapshot. Do not rebuild prompts,
toolsets, sessions, adapters, or system prompt state. Persist validated revision,
digest, error, and per-board clean-census eligibility in health. On malformed
revision keep last-known-good observation, disable new recovery mutation, and
alert through the supervisor. Add `gateway.recovery_control` to the authoritative
runtime manifest in the same commit that creates the module.

- [ ] **Step 4: Run recovery control, config, and prompt-cache tests**

Run: `scripts/run_tests.sh tests/gateway/test_recovery_control.py tests/gateway/test_runtime_fingerprint.py tests/hermes_cli/test_kanban_recovery_config.py tests/gateway/test_prompt_tail_freeze.py tests/gateway/test_runtime_env_reload_config_authority.py -q`

Expected: all pass and recovery reload never invalidates a conversation prompt cache.

- [ ] **Step 5: Commit narrow hot reload**

```bash
git add gateway/recovery_control.py gateway/cron_supervisor.py \
  gateway/runtime_fingerprint.py tests/gateway/test_recovery_control.py \
  tests/gateway/test_runtime_fingerprint.py
git commit -m "feat(gateway): hot-reload blocked recovery controls"
```

### Task 9: Reconcile exact controller-job manifests

**Files:**
- Modify: `cron/jobs.py`
- Modify: `cron/executions.py`
- Modify: `gateway/cron_supervisor.py`
- Create: `tests/cron/test_controller_job_manifest.py`
- Modify: `tests/gateway/test_cron_supervisor.py`

**Interfaces:**
- Produces: `guarded_set_job_enabled(job_id, expected_signature, enabled, operation_id) -> JobMutationResult`
- Produces: `evaluate_controller_jobs(specs, jobs, executions, now) -> tuple[ControllerJobHealth, ...]`

- [ ] **Step 1: Write exact self-heal and refusal matrix**

```python
def test_exact_disabled_only_drift_can_enable_once(controller_fixture):
    health = evaluate_controller_jobs(specs(mode="shadow"), jobs(enabled=False), fresh_execution(), now=100)
    assert health[0].reconciliation == "enable_allowed"

@pytest.mark.parametrize("drift", [
    "desired_paused", "missing", "duplicate", "role_tag", "source_manifest",
    "schedule", "delivery", "stale_success",
])
def test_nonexact_drift_never_enables(controller_fixture, drift):
    assert evaluate_controller_jobs(*controller_fixture.with_drift(drift)).enable_operations == ()

def test_fresh_inflight_execution_defers_stale_success(controller_fixture):
    health = evaluate_controller_jobs(specs(), jobs(), running_execution(heartbeat_age=10), now=100)
    assert health[0].reconciliation == "inflight_deferred"
```

- [ ] **Step 2: Verify RED**

Run: `scripts/run_tests.sh tests/cron/test_controller_job_manifest.py tests/gateway/test_cron_supervisor.py -q`

Expected: jobs have no immutable controller role/spec contract or operation-keyed enable.

- [ ] **Step 3: Add job metadata and guarded locked mutation**

Store `controller_role`, `controller_role_tag`, `source_manifest_digest`,
`schedule_digest`, `delivery_scope`, and `last_enable_operation_id` on declared
controller jobs. Canonical job-state signature hashes those fields plus job id
and enabled state. Under the existing cross-process job lock, compare the exact
signature, replay the operation id, change only `enabled`, save atomically, and
return a structured result.

The supervisor computes local source-closure digests from declared absolute
paths. It may enable once only if exactly one matching record exists, every
immutable field agrees, desired mode is shadow/live, last success is fresh or a
fresh bounded execution is in flight, and the sole drift is disabled. Journal
intent before update; reconcile unknown writes from enabled state plus operation
id. Never create or guess a job.

- [ ] **Step 4: Run job-store concurrency and manifest tests**

Run: `scripts/run_tests.sh tests/cron/test_controller_job_manifest.py tests/cron/test_jobs.py tests/cron/test_jobs_crossprocess_lock.py tests/cron/test_execution_health.py tests/gateway/test_cron_supervisor.py -q`

Expected: all pass, including unknown enable outcome and concurrent store writers.

- [ ] **Step 5: Commit controller-job reconciliation**

```bash
git add cron/jobs.py cron/executions.py gateway/cron_supervisor.py tests/cron/test_controller_job_manifest.py tests/gateway/test_cron_supervisor.py
git commit -m "feat(cron): reconcile declared controller jobs"
```

### Task 10: Expose effective cron health and disabled required jobs

**Files:**
- Modify: `hermes_cli/cron.py`
- Modify: `gateway/status.py`
- Modify: `tests/hermes_cli/test_cron.py`
- Modify: `tests/gateway/test_status.py`

**Interfaces:**
- CLI: `hermes cron status --json`
- CLI: `hermes cron list --required --json`

- [ ] **Step 1: Write disabled visibility and health-shape tests**

```python
def test_required_list_includes_disabled_controller(cli, controller_jobs):
    payload = cli.json("cron", "list", "--required", "--json")
    assert payload[0]["enabled"] is False
    assert payload[0]["controller_role"] == "blocked_router"

def test_status_distinguishes_process_and_application_health(cli):
    payload = cli.json("cron", "status", "--json")
    assert payload["gateway_process_healthy"] is True
    assert payload["cron_application_healthy"] is False
    assert payload["restart_quarantine"] is True
```

- [ ] **Step 2: Verify RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_cron.py tests/gateway/test_status.py -q`

Expected: default list/status hides the disabled required controller relationship.

- [ ] **Step 3: Add effective-health fields without changing ordinary list defaults**

Expose provider readiness/armed/thread state, heartbeat/success age, failed
cycles, restart count/quarantine, runtime drift, recovery-control revision, and
required controller desired/observed mode, spec agreement, last-success age,
next due, and reconciliation. `--required` includes disabled manifest roles;
ordinary `hermes cron list` keeps its current output compatibility.

- [ ] **Step 4: Run cron CLI and gateway status suites**

Run: `scripts/run_tests.sh tests/hermes_cli/test_cron.py tests/hermes_cli/test_cron_parser_builder.py tests/gateway/test_status.py tests/gateway/test_cron_supervisor.py -q`

Expected: all pass with stable human and JSON views.

- [ ] **Step 5: Commit effective-health surfaces**

```bash
git add hermes_cli/cron.py gateway/status.py tests/hermes_cli/test_cron.py tests/gateway/test_status.py
git commit -m "feat(cron): expose controller effectiveness health"
```

### Task 11: Run the cron supervision integration gate

**Files:**
- Verify: all files changed by Tasks 1-10.

**Interfaces:**
- Produces: gateway/cron code ready for paused/shadow rollout.

- [ ] **Step 1: Run focused new suites**

Run:

```bash
scripts/run_tests.sh \
  tests/gateway/test_runtime_fingerprint.py \
  tests/gateway/test_recovery_control.py \
  tests/gateway/test_cron_supervisor.py \
  tests/gateway/test_gateway_cron_readiness.py \
  tests/cron/test_controller_job_manifest.py \
  tests/cron/test_execution_health.py -q
```

Expected: all pass with no flaky-file retry summary.

- [ ] **Step 2: Run complete cron/provider/gateway lifecycle regressions**

Run:

```bash
scripts/run_tests.sh \
  tests/cron \
  tests/hermes_cli/test_cron.py \
  tests/hermes_cli/test_cron_parser_builder.py \
  tests/hermes_cli/test_gateway_restart_loop.py \
  tests/gateway/test_status.py \
  tests/gateway/test_systemd_notify.py \
  tests/gateway/test_systemd_watchdog_lifecycle.py \
  tests/gateway/test_runner_startup_failures.py \
  tests/gateway/test_startup_restart_race.py -q
```

Expected: all pass without changing ordinary cron execution/delivery semantics.

- [ ] **Step 3: Run fault-injection integration cases**

In a temporary Hermes root, inject dead ticker, stale heartbeat, three failed
cycles, alternating failure classes, returning external provider, exact disabled
controller, duplicate controller, modified schedule, stale success, fresh
in-flight execution, and enable-after-write timeout. Assert direct alerts and no
second mutation/restart under unchanged signatures.

- [ ] **Step 4: Inspect and compile**

Run:

```bash
python -m compileall -q gateway cron hermes_cli
git diff --check
git status --short
```

Expected: both commands exit 0 and only plan-scoped files are modified.

- [ ] **Step 5: Request code review before deployment**

Use `superpowers:requesting-code-review`. Require explicit review of startup
ordering, systemd watchdog separation, restart-budget persistence, and the
disabled-only job reconciliation predicate.
