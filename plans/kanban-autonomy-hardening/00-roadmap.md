# Kanban Autonomy Hardening — Master Roadmap

> **For agentic workers:** This is the program-level roadmap. Each workstream has its own
> plan file in this directory and is independently shippable. Execute them in the sequence
> below. Each plan uses checkbox (`- [ ]`) steps — use `superpowers:subagent-driven-development`
> or `superpowers:executing-plans` to implement task-by-task.

**Goal:** Make the Hermes kanban orchestrator (Jensen) reliably take complex multi-profile,
review/revise workflows to completion unattended — by removing the corruption-driven "freeze
and wait for a human" failure mode and closing the remaining silent-stall paths.

**Core thesis:** The orchestration *logic* is sound and largely autonomous. What blocks
unattended completion is (a) a shared-SQLite write architecture that corrupts under its own
concurrency and then refuses to self-recover, and (b) a few "freeze-and-wait" reflexes. Fix
the DB write story first — everything else rides on that database.

---

## Locked decisions (from design discussion 2026-05-29)

1. **DB architecture (#1):** **Single-writer daemon, keep SQLite.** All writes funnel through
   one owner process; workers/dispatcher/notifier/CLI become clients over a local Unix socket.
   Reads stay direct `mode=ro`. This *structurally* eliminates concurrent writers and the
   SIGKILL-mid-write torn-page hazard (workers no longer hold a writable file handle at all),
   while preserving the schema, `.bak`/`recover`/`doctor` tooling, and Control Center read paths.
2. **Review lane (#3):** **Delete the dormant `review` status lane.** Trust Jensen's judgment;
   keep the `kanban_block(reason="review-required: …")` + `assignee=reviewer` + PR-head-SHA
   convention. Remove the unreachable `review`-status infrastructure to cut merge-tax and stop
   implying a capability that doesn't exist.

---

## Workstreams & sequencing

```
WS1 ── Single-writer daemon ───────────┐  (foundation; everything safer once it lands)
        │                              │
        ▼                              ▼
WS2 ── Corruption auto-recovery     WS6 ── Board-liveness SLO + stall alerts
        (daemon = single recovery point)   (independent; can start anytime)
WS3 ── Delete dead review lane ── (independent; smallest; do early to reduce merge-tax)
WS4 ── Scheduled-park silent-stall fix ── (independent)
WS5 ── kanban_reconcile agent tool ── (independent; nicer after WS1's write proxy exists)
```

**Recommended order:** WS3 (quick win, reduces merge surface) → **WS1 (priority foundation)** →
WS2 → WS4 → WS5 → WS6. WS6 can be parallelized at any time; WS3/WS4 are independent of WS1.

| WS | Title | Size | Depends on | Plan file |
|----|-------|------|-----------|-----------|
| 1 | Single-writer daemon | L | — | `01-single-writer-daemon.md` |
| 2 | Corruption auto-recovery | M | WS1 | `02-corruption-auto-recovery.md` |
| 3 | Delete dead review lane | S | — | `03-delete-review-lane.md` |
| 4 | Scheduled-park stall fix | S–M | — | `04-scheduled-stall-fix.md` |
| 5 | `kanban_reconcile` agent tool | M | WS1 (soft) | `05-reconcile-agent-tool.md` |
| 6 | Board-liveness SLO + alerts | M | — | `06-board-liveness-slo.md` |

---

## Cross-cutting conventions (apply to every workstream)

- **Feature flags in `config.yaml` under `kanban:`** — every behavioral change ships behind a
  flag defaulting to today's behavior, so each workstream is safe to merge before it's enabled.
  Flags introduced: `kanban.single_writer_daemon` (WS1), `kanban.writer_auto_recovery` (WS2),
  `kanban.promote_scheduled_on_guard_clear` (WS4), `kanban.liveness_alerts` (WS6).
- **TDD:** failing test → run-red → minimal impl → run-green → commit. One behavior per commit.
- **Test runner:** `cd /Users/ctao/.hermes/hermes-agent && python -m pytest <path> -v`.
  Existing kanban tests live in `tests/hermes_cli/` and `tests/gateway/`.
- **Existing-file edits:** plan steps cite the real function + anchor line, but the *executor
  must read the current function before editing* (the file is large and merges with upstream).
  Match surrounding style; do not restructure unrelated code.
- **No commits/pushes without explicit user request.** Work on a branch off the current
  `integrate/hermes-origin-*` integration branch.
- **Merge-tax awareness (memory item #6):** `gateway/run.py` and `agent/prompt_builder.py` are
  the worst upstream-conflict hotspots. Where a workstream touches them, prefer adding a
  *new* function/hook and a single call-site line over rewriting existing upstream regions.

---

## Definition of done (program level)

- A worker process being `SIGKILL`'d (the dispatcher's `detect_crashed_workers` reclaim path)
  can no longer torn-write the board DB, because workers do not open the DB for write.
- A corruption event on the hot board triggers automatic, bounded recovery and resumes
  dispatch/notify without a human or a gateway restart (WS2), and pages a human only if
  recovery fails.
- No reachable code references the `review` status; review/revise runs on the documented
  block-reason convention (WS3).
- A task parked to `scheduled` by the respawn guard returns to `ready` automatically once its
  guard condition clears (WS4).
- Jensen resolves `jensen_decision_required` reconcile packets via a schema-validated
  `kanban_reconcile` tool rather than free-form shell (WS5).
- A stalled board (oldest non-terminal task age, disabled subsystem, dead daemon) raises an
  alert within minutes instead of being discovered incidentally (WS6).

---

## Risk register

| Risk | Mitigation |
|------|-----------|
| Daemon becomes a new single point of failure | WS2 watchdog auto-restarts it; clients retry with backoff; flag-off falls back to direct writes |
| Write-proxy migration misses a write call-site → silent direct write under the flag | WS1 adds an assertion in `connect(readonly=False)` that refuses direct writable opens in client processes when the flag is on; test enumerates all writers |
| Socket protocol perf regression vs in-process SQLite | Reads stay direct (unaffected); writes are already serialized by SQLite's writer lock today, so daemon serialization is not a new bottleneck; benchmark in WS1 acceptance |
| Recovery auto-restore loses in-flight writes | Recovery is bounded, logs exactly what was restored, keeps the corrupt copy; WS2 prefers WAL-checkpoint/`.recover` before backup-restore |
| Upstream merge conflict in `gateway/run.py` | New subsystem starter function + one call line; no edits to dispatcher/notifier internals beyond swapping the writer handle |

---

## Pre-work for the implementing agent

1. Read `forensics/kanban-rca-*` and `forensics/kanban-corruption-investigation-*` in
   `/Users/ctao/.hermes/` — confirm the RCA pins corruption to concurrent writes / killed
   workers. If it points elsewhere (NFS, disk, a specific code path), re-open the WS1 design
   decision before building.
2. Confirm the board is **not** on a network filesystem in production (the daemon design
   assumes a local socket + local file; WAL already falls back to DELETE on NFS).
3. Branch: `git checkout -b feat/kanban-autonomy-hardening` off the current integration branch.
