# Kanban workspace storage management

Hermes task workspaces can grow quickly when a worker is interrupted after
materialization or when operating-system errors prevent completion cleanup.
Hermes therefore reports cleanup failures, provides a conservative garbage
collector, and can stop creating workspaces before the disk fills.

## Cleanup lifecycle

Completing a scratch task removes its managed workspace after declared
artifacts have been copied to task attachments. A deletion failure does not
undo the task completion, but it is no longer silent:

- the dispatcher logs a warning with the task id and path;
- the task receives a `workspace_cleanup_failed` event; and
- the remaining directory stays eligible for a later `kanban gc` retry.

Hermes never automatically deletes `dir` workspaces or Git worktrees. Scratch
cleanup also refuses paths outside a Kanban-managed `workspaces/` directory.

## Preview and collect

Preview terminal scratch directories (`done` and `archived`) without changing
workspaces, events, or logs:

```console
hermes kanban gc --dry-run
```

Interrupted workers can leave directories that no task row references. Include
those only after reviewing the preview:

```console
hermes kanban gc --dry-run --include-unreferenced
hermes kanban gc --include-unreferenced
```

The orphan sweep only considers immediate child directories of the active
board's managed scratch root. It preserves symlinks and every directory that
overlaps a task reference, including active scratch tasks, non-scratch
workspaces, and a parent directory containing a nested referenced path.

Without `--dry-run`, GC also applies the configured event and worker-log
retention periods. If a workspace still cannot be removed, the command exits
non-zero and reports the failure in logs and, for task-owned paths, in the task
event history.

## Low-disk watermark

Set `kanban.min_free_disk_gb` in `~/.hermes/config.yaml` to reserve disk space:

```yaml
kanban:
  min_free_disk_gb: 20
```

The shipped default is 5 GiB. Set the value to `0` to disable the check.
Hermes checks the target filesystem before creating a scratch directory,
persistent `dir` workspace, or linked Git worktree. Existing workspaces remain
usable below the watermark so in-flight work is not stranded.

## Pre-update backup retention

Pre-update ZIPs are controlled separately:

```yaml
updates:
  pre_update_backup: true
  backup_keep: 1
```

`backup_keep` is floored to one when backups are enabled, ensuring the newest
recovery point is retained while older `pre-update-*.zip` files are pruned.
