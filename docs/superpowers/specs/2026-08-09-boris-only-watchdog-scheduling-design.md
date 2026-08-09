# Boris-Only Scheduled Reaction Watchdog Design

**Date:** 2026-08-09
**Status:** Approved amendment to the just-do-it retirement design
**Parent design:** `docs/superpowers/specs/2026-08-08-just-do-it-cron-retirement-boris-reactions-design.md`
**Scope:** Make the retained watchdog's scheduled/default execution Boris-only,
repair one proven legacy metadata drift, and leave explicit legacy operation
available only for rollback and forensics.

## Problem

The retained job `6daa151da351` was resumed for one scheduled tick after the
Boris reaction migration settled. Its no-argument invocation still selected
all four historically watched channels. The tick made no Slack reaction calls
and did not change Boris state, but it refreshed `alerted_at` on one
pre-existing quarantined record in legacy channel `C07BU8AAPB3`. That changed
the immutable legacy projection hash from the approved checkpoint value
`6b53a2e7c4be9348b8541677f34aaabe16cf6dab78c214c3ee62dacd6a5baa78`.

The current behavior contradicts the retirement contract: a recurring legacy
quarantine alert necessarily mutates legacy state, while retired-channel
reaction records are required to remain immutable. The retained job is paused
until the contradiction is removed.

## Decisions

1. A no-argument invocation, including the scheduler's `no_agent` invocation,
   selects only `#boris-reviews` (`C0BPCKPEFCG`).
2. Repeated explicit `--channel <id>` arguments continue to replace the
   default selection. The three legacy channel IDs remain supported for a
   deliberate operator rollback or forensic pass.
3. Classification, reaction rendering, state precedence, retry, quarantine,
   reconciliation, task fencing, and channel fencing do not otherwise change.
4. The strict legacy projection remains exact. `alerted_at` is not ignored and
   the baseline is not moved forward to accept drift.
5. The single unintended `alerted_at` refresh is restored from the latest
   immutable retained-reaction checkpoint through a one-purpose guarded
   controller operation.
6. The retained job prompt is changed to describe Boris-only scheduled/default
   behavior. It may mention explicit legacy operation only as rollback and
   forensic tooling.
7. The Boris intake change remains independent: after the watchdog is healthy,
   the reviewed intake package is installed so accepted requests initially add
   `:rubbing:` instead of `:thinkies:`.

## Watchdog boundary

The watchdog keeps two channel concepts:

- **scheduled default:** `{C0BPCKPEFCG}`;
- **explicit operator selection:** any channel passed with `--channel`, with
  the existing three legacy IDs retained as supported operational inputs.

Channel selection is resolved before state migration, persisted repair,
reconciliation, hold clearing, or compaction. Therefore a no-argument tick may
read the shared ledger but cannot inspect or mutate a legacy post or legacy
`reacted` entry. Global cursor bookkeeping may continue to advance because it
is not part of the immutable per-channel projection.

The scheduled default continues to use the modern Boris rendering scheme:

```text
blocked > notes > active > approved
face_vomiting > doge_notes > rubbing > blinkapproved
```

Explicit legacy operation continues to render the historical vocabulary and
does not opt into Boris emoji ownership.

## Guarded legacy compensation

The retirement controller adds a narrowly scoped compensation command. It
accepts one exact legacy permalink and explicit expected current and baseline
`alerted_at` integers. A dry run validates every precondition; `--apply` is
required to write.

Before writing, the command must:

- verify the manifest belongs to the selected Hermes home and is finalized;
- verify the exact retained ID is paused under its post-migration name, with
  its one-minute schedule, compatible script, and `no_agent=true`;
- verify both retired IDs remain absent and all four retired entrypoints remain
  absent from active paths and hash-valid in the archive;
- acquire the watchdog's exact reaction-ledger lock without waiting
  indefinitely;
- load the latest immutable retained-reaction checkpoint through its verified
  fingerprint;
- require that the permalink is a well-formed permalink in one of the three
  legacy channels;
- require the baseline and live records to differ at exactly one leaf: the
  requested record's `alerted_at`;
- require both integer values to match the explicit CLI preconditions; and
- snapshot the complete pre-compensation ledger outside active runtime paths.

The write deep-copies the live ledger, changes only that one `alerted_at`, and
uses an atomic same-directory replacement preserving the original file mode.
While the lock is still held, it proves that the resulting legacy projection
is byte-canonically equal to the immutable checkpoint, the Boris projection is
unchanged, and no other ledger value changed.

The controller writes a compensation evidence record containing the exact
permalink, before/after values, source checkpoint, and full before/after file
fingerprints. It does not append a new retained-reaction checkpoint or refresh
the legacy baseline. Re-running the command is a guarded no-op only when the
baseline value is already present and the full legacy projection matches.

## Retained prompt configuration

A separate exact-ID controller operation updates only the retained job's
prompt while it is paused. It requires the current prompt to equal either the
known all-channel post-migration prompt or the desired Boris-only prompt, so it
cannot overwrite unrelated operator edits. The desired prompt states that
scheduled/default execution is Boris-only and that explicit legacy selection
exists only for rollback and forensics.

The operation does not change the job ID, name, script, schedule, enabled
state, run counters, claims, output, or delivery metadata. Final retired-state
verification requires the Boris-only prompt after a retained-reaction
checkpoint exists.

## Rollout

1. Keep `6daa151da351` paused and confirm the retired IDs and entrypoints remain
   absent.
2. Run the focused watchdog/controller suites with retries disabled.
3. Install the exact reviewed watchdog source and test into the live scripts
   directory.
4. Dry-run and apply the guarded legacy `alerted_at` compensation.
5. Dry-run and apply the exact retained prompt update.
6. Run retired-state verification while the job is paused, allowing only the
   expected paused-state health exception.
7. Resume only `6daa151da351` and observe at least one scheduled tick.
8. Prove the legacy projection still equals the immutable baseline, the tick
   made no Slack writes while Boris was settled, the latest retained execution
   completed, and the retired producers remain absent.
9. Create the Boris intake checkpoint, install the reviewed versioned intake
   package, restart `ai.hermes.boris-intake`, and verify process, heartbeat,
   config, audit, database, deduplication, and watermark continuity.
10. Wait the required 180-second grace in bounded intervals, then run final
    retirement/controller and compatibility verification.

No global rollback is permitted during these steps. No retired producer may
be recreated or re-enabled.

## Tests and acceptance criteria

- A no-argument main invocation with due legacy repair/quarantine state makes
  zero legacy Slack calls and preserves every legacy post and `reacted` entry.
- The same legacy record is processed when its channel is passed explicitly.
- A no-argument invocation still processes Boris discovery, repair, and due
  reconciliation.
- Compensation rejects Boris permalinks, non-checkpoint values, extra legacy
  drift, unpaused retained state, missing retired archives, and a busy ledger
  lock.
- Compensation modifies exactly one field and returns the strict legacy hash
  to the immutable checkpoint value without changing Boris state.
- Prompt configuration is exact-ID, paused-only, idempotent, and preserves all
  non-prompt job fields.
- The full watchdog/controller suite passes through `scripts/run_tests.sh`
  with `--file-retries 0`.
- After one natural scheduled tick, legacy projection equality still passes.
- The live Boris intake package matches the reviewed versioned package and its
  initial reaction contract is `:rubbing:`.

## Out of scope

- Re-enabling or recreating a retired just-do-it producer.
- Rewriting any legacy Slack reaction.
- Clearing or reclassifying the quarantined legacy lane.
- Weakening the legacy projection by excluding mutable-looking fields.
- Moving the immutable checkpoint baseline to the observed drift.
- Changing Boris authorization, command grammar, deduplication, dispatch,
  review policy, or Kanban lifecycle behavior.
