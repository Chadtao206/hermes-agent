#!/usr/bin/env python3
"""Guarded, exact-ID retirement for the two deprecated just-do-it producers.

The module is intentionally a one-shot operator control plane rather than a
runtime scheduler feature.  Every mutation is scoped to explicit paths and
exact job IDs, and every recoverable input is fingerprinted before use.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone, UTC
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from cron import jobs as cron_jobs
from gateway.status import get_process_start_time
from hermes_constants import get_default_hermes_root, get_hermes_home


RETIRE = {
    "ee4582d0598f": {
        "name": "just-do-it-channel-watch",
        "script": "slack_channel_watch.py",
        "schedule": {"kind": "interval", "minutes": 2},
    },
    "ff865c33c5ea": {
        "name": "just-do-it-blocked-review-requeue-watchdog",
        "script": "just_do_it_blocked_review_requeue_watchdog.py",
        "schedule": {"kind": "cron", "expr": "*/2 * * * *"},
    },
}
RETAIN_ID = "6daa151da351"
RETAIN_SCRIPT = "just_do_it_review_reaction_watchdog.py"
RETAIN_PRE_MIGRATION_NAME = "just-do-it-review-reaction-state-watchdog"
RETAIN_POST_MIGRATION_NAME = "boris-review-reaction-state-watchdog"
RETAIN_ALL_CHANNEL_PROMPT = (
    "No-agent authoritative state machine for source-linked Boris reviewer lanes "
    "and historical legacy reviewer lanes. In #boris-reviews it maintains exactly "
    "one owned reaction with precedence face_vomiting > doge_notes > rubbing > "
    "blinkapproved. Legacy watched-channel reactions retain their prior vocabulary. "
    "Quiet when settled; emits compact transition, hold, reconciliation, or failure "
    "output. It never creates or mutates Kanban tasks. Do not schedule additional "
    "cron jobs."
)
RETAIN_BORIS_ONLY_PROMPT = (
    "No-agent authoritative state machine for source-linked Boris reviewer lanes. "
    "In #boris-reviews it maintains exactly one owned reaction with precedence "
    "face_vomiting > doge_notes > rubbing > blinkapproved. Scheduled/default "
    "execution is Boris-only; explicit legacy-channel operation remains available "
    "only for rollback and forensics. Quiet when settled; emits compact transition, "
    "hold, reconciliation, or failure output. It never creates or mutates Kanban "
    "tasks. Do not schedule additional cron jobs."
)
DEPRECATED_CREATOR = "just-do-it-blocked-review-requeue-watchdog"
DEPRECATED_IDEMPOTENCY_PREFIX = "just-do-it-rereview:"

BORIS_CHANNEL = "C0BPCKPEFCG"
BORIS_LAUNCHD_LABEL = "ai.hermes.boris-intake"
BORIS_ROLLING_COUNT_TABLES = frozenset({"requests"})
LEGACY_CHANNELS = ("C08K8FQKX9S", "C07BU8AAPB3", "C09339EGZ9U")
ENTRYPOINTS = {
    "retired-channel-script": "slack_channel_watch.py",
    "retired-channel-test": "test_slack_channel_watch.py",
    "retired-requeue-script": "just_do_it_blocked_review_requeue_watchdog.py",
    "retired-requeue-test": "test_just_do_it_blocked_review_requeue_watchdog.py",
}
DURABLE_RECOVERY_OBJECTS = {
    "cron/scripts/just_do_it_blocked_review_requeue_watchdog.py": "blob",
    "tests/cron/test_just_do_it_blocked_review_requeue_watchdog.py": "blob",
    "cron/scripts/just_do_it_review_reaction_watchdog.py": "blob",
    "tests/cron/test_just_do_it_review_reaction_watchdog.py": "blob",
    "cron/scripts/boris_intake": "tree",
    "tests/cron/boris_intake": "tree",
}
RETAINED_DURABLE_PATHS = (
    "cron/scripts/just_do_it_review_reaction_watchdog.py",
    "tests/cron/test_just_do_it_review_reaction_watchdog.py",
    "cron/scripts/boris_intake",
    "tests/cron/boris_intake",
)
_FULL_COMMIT_ID = re.compile(r"[0-9a-fA-F]{40}")
_REACTION_PERMALINK = re.compile(
    r"https://ylopo\.slack\.com/archives/(?P<channel>[A-Z0-9]+)/p\d{16}(?!\d)"
    r"(?:\?[^\s)>\'\"]*)?",
    re.IGNORECASE,
)


class RetirementError(RuntimeError):
    """A fail-closed retirement guard rejected the requested operation."""


def _resolved(value: Path | str) -> Path:
    return Path(value).expanduser().resolve()


def _matches_script_identity(
    value: Any, live_scripts: Path, expected_basename: str
) -> bool:
    """Match a stored script to one exact file in the selected scripts root."""
    if not isinstance(value, str) or not value:
        return False
    try:
        scripts_root = _resolved(live_scripts)
        expected_relative = Path(expected_basename)
        if (
            expected_relative.is_absolute()
            or expected_relative != Path(expected_relative.name)
        ):
            return False
        expected = _resolved(scripts_root / expected_relative)
        if expected.parent != scripts_root:
            return False

        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return _resolved(candidate) == expected
        if candidate != expected_relative:
            return False
        return _resolved(scripts_root / candidate) == expected
    except (OSError, RuntimeError, ValueError):
        return False


def _matches_schedule_identity(value: Any, expected: Mapping[str, Any]) -> bool:
    """Compare exact scheduler behavior while allowing derived display text."""
    if not isinstance(value, Mapping):
        return False
    operational = dict(value)
    operational.pop("display", None)
    return operational == dict(expected)


def _is_live_home(path: Path | str) -> bool:
    """Return whether a home is the current/default home or a real profile."""
    home = _resolved(path)
    current = _resolved(get_hermes_home())
    default = _resolved(get_default_hermes_root())
    if home in {current, default}:
        return True
    try:
        relative = home.relative_to(default / "profiles")
    except ValueError:
        return False
    return bool(relative.parts)


@dataclass(frozen=True)
class Paths:
    hermes_home: Path
    source_root: Path
    archive_root: Path
    jobs_path: Optional[Path] = None
    executions_path: Optional[Path] = None
    board_path: Optional[Path] = None
    live_scripts: Optional[Path] = None
    state_dir: Optional[Path] = None
    cron_output: Optional[Path] = None
    boris_launchd_plist: Optional[Path] = None
    # Hermetic tests and isolated rehearsals provide a JSON process census.
    # Production may omit it and use the OS process table.
    process_listing_path: Optional[Path] = None
    boris_service_status_path: Optional[Path] = None

    def __post_init__(self) -> None:
        home = _resolved(self.hermes_home)
        source = _resolved(self.source_root)
        archive = _resolved(self.archive_root)
        if self.boris_launchd_plist is None:
            plist_root = Path.home() if _is_live_home(home) else home
            plist_default = (
                plist_root
                / "Library"
                / "LaunchAgents"
                / f"{BORIS_LAUNCHD_LABEL}.plist"
            )
        else:
            plist_default = self.boris_launchd_plist
        defaults = {
            "hermes_home": home,
            "source_root": source,
            "archive_root": archive,
            "jobs_path": home / "cron" / "jobs.json",
            "executions_path": home / "cron" / "executions.db",
            "board_path": home / "kanban.db",
            "live_scripts": home / "scripts",
            "state_dir": home / "state",
            "cron_output": home / "cron" / "output",
            "boris_launchd_plist": plist_default,
        }
        for name, default in defaults.items():
            raw = getattr(self, name)
            object.__setattr__(self, name, _resolved(raw if raw is not None else default))
        if self.process_listing_path is not None:
            object.__setattr__(
                self, "process_listing_path", _resolved(self.process_listing_path)
            )
        if self.boris_service_status_path is not None:
            object.__setattr__(
                self,
                "boris_service_status_path",
                _resolved(self.boris_service_status_path),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            name: str(value) if isinstance(value, Path) else value
            for name, value in asdict(self).items()
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Paths:
        return cls(**{key: Path(value) if value is not None else None for key, value in raw.items()})


@dataclass(frozen=True)
class FileFingerprint:
    sha256: str
    mode: int
    size: int
    active_path: Path
    archive_path: Path
    kind: str = "file"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "mode": self.mode,
            "size": self.size,
            "active_path": str(self.active_path),
            "archive_path": str(self.archive_path),
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FileFingerprint:
        return cls(
            sha256=str(raw["sha256"]),
            mode=int(raw["mode"]),
            size=int(raw["size"]),
            active_path=Path(str(raw["active_path"])),
            archive_path=Path(str(raw["archive_path"])),
            kind=str(raw.get("kind", "file")),
        )


@dataclass
class Manifest:
    manifest_path: Path
    paths: Paths
    created_epoch: int
    created_iso: str
    archive_dir: Path
    jobs: dict[str, dict[str, Any]]
    files: dict[str, FileFingerprint]
    latest_executions: dict[str, dict[str, Any] | None]
    task_census: dict[str, list[dict[str, Any]]]
    repository_commit: str
    repository_tree_hashes: dict[str, str]
    legacy_reaction_projection_sha256: str
    phase: str = "preflight"
    cutoff_epoch: Optional[int] = None
    cutoff_iso: Optional[str] = None
    quiescent_files: dict[str, FileFingerprint] = field(default_factory=dict)
    component_checkpoints: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    changed_components: list[str] = field(default_factory=list)
    protected_output_paths: dict[str, Path] = field(default_factory=dict)
    finalized_epoch: Optional[int] = None
    finalized_iso: Optional[str] = None
    restored_epoch: Optional[int] = None
    restored_iso: Optional[str] = None
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "manifest_path": str(self.manifest_path),
            "paths": self.paths.to_dict(),
            "created_epoch": self.created_epoch,
            "created_iso": self.created_iso,
            "archive_dir": str(self.archive_dir),
            "jobs": copy.deepcopy(self.jobs),
            "files": {key: value.to_dict() for key, value in self.files.items()},
            "latest_executions": copy.deepcopy(self.latest_executions),
            "task_census": copy.deepcopy(self.task_census),
            "repository_commit": self.repository_commit,
            "repository_tree_hashes": dict(self.repository_tree_hashes),
            "legacy_reaction_projection_sha256": self.legacy_reaction_projection_sha256,
            "phase": self.phase,
            "cutoff_epoch": self.cutoff_epoch,
            "cutoff_iso": self.cutoff_iso,
            "quiescent_files": {
                key: value.to_dict() for key, value in self.quiescent_files.items()
            },
            "component_checkpoints": copy.deepcopy(self.component_checkpoints),
            "changed_components": list(self.changed_components),
            "protected_output_paths": {
                key: str(value) for key, value in self.protected_output_paths.items()
            },
            "finalized_epoch": self.finalized_epoch,
            "finalized_iso": self.finalized_iso,
            "restored_epoch": self.restored_epoch,
            "restored_iso": self.restored_iso,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def save(self) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=self.manifest_path.parent,
            prefix=f".{self.manifest_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self.to_json())
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.manifest_path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: Path | str) -> Manifest:
        manifest_path = _resolved(path)
        if not manifest_path.is_absolute():  # defensive; _resolved is absolute
            raise RetirementError("manifest path must be absolute")
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        recorded = _resolved(raw["manifest_path"])
        if recorded != manifest_path:
            raise RetirementError("manifest path does not match its recorded identity")
        return cls(
            version=int(raw.get("version", 1)),
            manifest_path=manifest_path,
            paths=Paths.from_dict(raw["paths"]),
            created_epoch=int(raw["created_epoch"]),
            created_iso=str(raw["created_iso"]),
            archive_dir=Path(raw["archive_dir"]),
            jobs=copy.deepcopy(raw["jobs"]),
            files={
                key: FileFingerprint.from_dict(value)
                for key, value in raw["files"].items()
            },
            latest_executions=copy.deepcopy(raw["latest_executions"]),
            task_census=copy.deepcopy(raw["task_census"]),
            repository_commit=str(raw["repository_commit"]),
            repository_tree_hashes=dict(raw["repository_tree_hashes"]),
            legacy_reaction_projection_sha256=str(
                raw["legacy_reaction_projection_sha256"]
            ),
            phase=str(raw.get("phase", "preflight")),
            cutoff_epoch=(
                int(raw["cutoff_epoch"]) if raw.get("cutoff_epoch") is not None else None
            ),
            cutoff_iso=raw.get("cutoff_iso"),
            quiescent_files={
                key: FileFingerprint.from_dict(value)
                for key, value in raw.get("quiescent_files", {}).items()
            },
            component_checkpoints=copy.deepcopy(raw.get("component_checkpoints", {})),
            changed_components=list(raw.get("changed_components", [])),
            protected_output_paths={
                key: Path(value)
                for key, value in raw.get("protected_output_paths", {}).items()
            },
            finalized_epoch=raw.get("finalized_epoch"),
            finalized_iso=raw.get("finalized_iso"),
            restored_epoch=raw.get("restored_epoch"),
            restored_iso=raw.get("restored_iso"),
        )


def _epoch(value: Any) -> int:
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return int(current.timestamp())
    return int(value)


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return current.astimezone(UTC)
    return datetime.fromtimestamp(int(value), tz=UTC)


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=UTC).isoformat()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _tree_details(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        relative = child.relative_to(path).as_posix()
        child_stat = child.lstat()
        mode = stat.S_IMODE(child_stat.st_mode)
        if child.is_symlink():
            raise RetirementError(f"refusing symlink in protected tree: {child}")
        if child.is_dir():
            digest.update(f"D\0{relative}\0{mode:o}\0".encode())
            continue
        if not child.is_file():
            raise RetirementError(f"refusing non-file in protected tree: {child}")
        item_hash = _file_hash(child)
        total += child_stat.st_size
        digest.update(
            f"F\0{relative}\0{mode:o}\0{child_stat.st_size}\0{item_hash}\0".encode()
        )
    return digest.hexdigest(), total


def _measure(path: Path) -> tuple[str, int, int, str]:
    if not path.exists():
        raise RetirementError(f"required path is missing: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if path.is_file():
        return _file_hash(path), mode, path.stat().st_size, "file"
    if path.is_dir():
        digest, size = _tree_details(path)
        return digest, mode, size, "tree"
    raise RetirementError(f"required path is not a file or directory: {path}")


def _fingerprint(path: Path, archive_path: Path) -> FileFingerprint:
    digest, mode, size, kind = _measure(path)
    return FileFingerprint(digest, mode, size, path, archive_path, kind)


def _copy_preserving(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        if destination.exists():
            raise RetirementError(f"snapshot destination already exists: {destination}")
        shutil.copytree(source, destination, copy_function=shutil.copy2)
    else:
        shutil.copy2(source, destination)


def _atomic_write_bytes(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _snapshot(source: Path, destination: Path) -> FileFingerprint:
    before = _fingerprint(source, destination)
    _copy_preserving(source, destination)
    after = _fingerprint(destination, destination)
    if (before.sha256, before.mode, before.size, before.kind) != (
        after.sha256,
        after.mode,
        after.size,
        after.kind,
    ):
        raise RetirementError(f"snapshot fingerprint mismatch for {source}")
    return before


def _sqlite_snapshot(source: Path, destination: Path) -> FileFingerprint:
    if not source.is_file():
        raise RetirementError(f"required SQLite database is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RetirementError(f"snapshot destination already exists: {destination}")
    source_conn = sqlite3.connect(str(source), timeout=10)
    destination_conn = sqlite3.connect(str(destination), timeout=10)
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    finally:
        destination_conn.close()
        source_conn.close()
    os.chmod(destination, stat.S_IMODE(source.stat().st_mode))
    logical_digest, logical_size = _sqlite_digest(destination)
    return FileFingerprint(
        sha256=logical_digest,
        mode=stat.S_IMODE(destination.stat().st_mode),
        size=logical_size,
        active_path=source,
        archive_path=destination,
        kind="sqlite",
    )


def _sqlite_digest(path: Path) -> tuple[str, int]:
    """Hash logical SQLite contents, independent of WAL/header page churn."""
    if not path.is_file():
        raise RetirementError(f"required SQLite database is missing: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        payload = "\n".join(connection.iterdump()).encode("utf-8")
    finally:
        connection.close()
    return hashlib.sha256(payload).hexdigest(), len(payload)


def _matches(fingerprint: FileFingerprint, path: Optional[Path] = None) -> bool:
    target = path or fingerprint.active_path
    if fingerprint.kind == "sqlite":
        try:
            digest, size = _sqlite_digest(target)
            mode = stat.S_IMODE(target.stat().st_mode)
        except (OSError, RetirementError, sqlite3.Error):
            return False
        return (
            digest == fingerprint.sha256
            and size == fingerprint.size
            and mode == fingerprint.mode
        )
    try:
        digest, mode, size, kind = _measure(target)
    except RetirementError:
        return False
    return (
        digest == fingerprint.sha256
        and mode == fingerprint.mode
        and size == fingerprint.size
        and kind == fingerprint.kind
    )


def _require_match(fingerprint: FileFingerprint, path: Optional[Path] = None) -> None:
    target = path or fingerprint.active_path
    if not _matches(fingerprint, target):
        raise RetirementError(f"fingerprint mismatch for {target}")


def _assert_store_paths(paths: Paths) -> None:
    expected_jobs = paths.hermes_home / "cron" / "jobs.json"
    expected_output = paths.hermes_home / "cron" / "output"
    if paths.jobs_path != expected_jobs or paths.cron_output != expected_output:
        raise RetirementError(
            "jobs_path and cron_output must resolve inside the selected Hermes cron store"
        )


def _select_exact_job_records(
    jobs: Iterable[Mapping[str, Any]], job_ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    job_records = list(jobs)
    selected: dict[str, dict[str, Any]] = {}
    for wanted in job_ids:
        matches = [job for job in job_records if str(job.get("id")) == wanted]
        if len(matches) != 1:
            subject = "retained job" if wanted == RETAIN_ID else f"job {wanted}"
            raise RetirementError(
                f"{subject} fingerprint mismatch: expected exactly one record, "
                f"found {len(matches)}"
            )
        selected[wanted] = copy.deepcopy(dict(matches[0]))
    return selected


def _job_map_locked(paths: Paths) -> dict[str, dict[str, Any]]:
    _assert_store_paths(paths)
    with cron_jobs.use_cron_store(paths.hermes_home):
        with cron_jobs._jobs_lock():
            return _select_exact_job_records(
                cron_jobs.load_jobs(), (*sorted(RETIRE), RETAIN_ID)
            )


def _validate_job_fingerprints(paths: Paths, selected: Mapping[str, dict[str, Any]]) -> None:
    for job_id, expected in RETIRE.items():
        job = selected[job_id]
        conditions = {
            "name": job.get("name") == expected["name"],
            "schedule": _matches_schedule_identity(
                job.get("schedule"), expected["schedule"]
            ),
            "script": _matches_script_identity(
                job.get("script"), paths.live_scripts, expected["script"]
            ),
            "enabled": job.get("enabled") is True,
            "state": job.get("state") == "scheduled",
        }
        failed = [name for name, valid in conditions.items() if not valid]
        if failed:
            raise RetirementError(
                f"job {job_id} fingerprint mismatch: {', '.join(failed)}"
            )
    retained = selected[RETAIN_ID]
    retained_conditions = {
        "script": _matches_script_identity(
            retained.get("script"), paths.live_scripts, RETAIN_SCRIPT
        ),
        "schedule": _matches_schedule_identity(
            retained.get("schedule"), {"kind": "cron", "expr": "* * * * *"}
        ),
        "no_agent": retained.get("no_agent") is True,
        "enabled": retained.get("enabled") is True,
        "state": retained.get("state") == "scheduled",
    }
    failed = [name for name, valid in retained_conditions.items() if not valid]
    if failed:
        raise RetirementError(
            f"job {RETAIN_ID} fingerprint mismatch: {', '.join(failed)}"
        )


def _validate_preflight_job_selection(
    paths: Paths,
    manifest: Manifest,
    selected: Mapping[str, dict[str, Any]],
) -> None:
    for job_id, expected in manifest.jobs.items():
        if selected.get(job_id) != expected:
            raise RetirementError(f"job {job_id} drifted after preflight")
    _validate_job_fingerprints(paths, selected)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise RetirementError(f"required SQLite database is missing: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _latest_executions(path: Path) -> dict[str, dict[str, Any] | None]:
    result: dict[str, dict[str, Any] | None] = {}
    with _connect_readonly(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(executions)").fetchall()
        }
        required = {
            "id",
            "job_id",
            "status",
            "claimed_at",
            "started_at",
            "finished_at",
            "pid",
            "process_started_at",
        }
        if not required <= columns:
            raise RetirementError("executions schema does not contain required identity fields")
        for job_id in (*sorted(RETIRE), RETAIN_ID):
            row = connection.execute(
                """SELECT id, job_id, status, claimed_at, started_at, finished_at,
                          pid, process_started_at
                   FROM executions WHERE job_id=?
                   ORDER BY unixepoch(claimed_at) DESC, id DESC LIMIT 1""",
                (job_id,),
            ).fetchone()
            result[job_id] = dict(row) if row is not None else None
    return result


_SOURCE_PERMALINK = re.compile(
    r"Source Slack message:\s*https://[^\s/]+/archives/([A-Z0-9]+)/p\d+",
    re.IGNORECASE,
)


def _task_census(path: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "deprecated_creator": [],
        "deprecated_idempotency": [],
        "legacy_source_permalink": [],
    }
    with _connect_readonly(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        required = {"id", "created_by", "idempotency_key", "body", "created_at"}
        if not required <= columns:
            raise RetirementError("board tasks schema does not contain provenance fields")
        rows = connection.execute(
            """SELECT id, created_by, idempotency_key, body, created_at
               FROM tasks
               WHERE created_by=? OR idempotency_key LIKE ?
                  OR body LIKE '%Source Slack message:%'
               ORDER BY created_at, id""",
            (DEPRECATED_CREATOR, f"{DEPRECATED_IDEMPOTENCY_PREFIX}%"),
        ).fetchall()
    for raw in rows:
        row = dict(raw)
        body = str(row.get("body") or "")
        summary = {
            "id": str(row["id"]),
            "created_at": row.get("created_at"),
            "created_by": row.get("created_by"),
            "idempotency_key": row.get("idempotency_key"),
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        }
        if row.get("created_by") == DEPRECATED_CREATOR:
            result["deprecated_creator"].append(summary)
        if str(row.get("idempotency_key") or "").startswith(
            DEPRECATED_IDEMPOTENCY_PREFIX
        ):
            result["deprecated_idempotency"].append(summary)
        channels = {match.group(1).upper() for match in _SOURCE_PERMALINK.finditer(body)}
        if any(channel != BORIS_CHANNEL for channel in channels):
            result["legacy_source_permalink"].append(summary)
    return result


def _task_census_drift(
    baseline: Mapping[str, list[dict[str, Any]]],
    current: Mapping[str, list[dict[str, Any]]],
) -> list[str]:
    errors: list[str] = []
    for category in baseline:
        old = {json.dumps(row, sort_keys=True) for row in baseline.get(category, [])}
        new = {json.dumps(row, sort_keys=True) for row in current.get(category, [])}
        if additions := sorted(new - old):
            ids = sorted(json.loads(value)["id"] for value in additions)
            errors.append(
                f"new deprecated-provenance task in {category}: {', '.join(ids)}"
            )
    return errors


def _legacy_projection(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"invalid": True}
    posts = raw.get("posts") if isinstance(raw.get("posts"), dict) else {}
    reacted = raw.get("reacted") if isinstance(raw.get("reacted"), dict) else {}
    legacy_posts = {
        key: value for key, value in posts.items() if f"/{BORIS_CHANNEL}/" not in key
    }
    legacy_reacted: dict[str, Any] = {}
    for key, value in reacted.items():
        permalink = key.split(":", 1)[1] if isinstance(key, str) and ":" in key else ""
        if f"/{BORIS_CHANNEL}/" not in permalink:
            legacy_reacted[key] = value
    return {"posts": legacy_posts, "reacted": legacy_reacted}


def _boris_projection(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"invalid": True}
    posts = raw.get("posts") if isinstance(raw.get("posts"), dict) else {}
    reacted = raw.get("reacted") if isinstance(raw.get("reacted"), dict) else {}
    boris_posts = {
        key: value for key, value in posts.items() if f"/{BORIS_CHANNEL}/" in key
    }
    boris_reacted: dict[str, Any] = {}
    for key, value in reacted.items():
        permalink = key.split(":", 1)[1] if isinstance(key, str) and ":" in key else ""
        if f"/{BORIS_CHANNEL}/" in permalink:
            boris_reacted[key] = value
    return {"posts": boris_posts, "reacted": boris_reacted}


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _json_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _repository_identity(
    source_root: Path, recovery_ref: str
) -> tuple[str, dict[str, str]]:
    if not isinstance(recovery_ref, str) or not _FULL_COMMIT_ID.fullmatch(
        recovery_ref
    ):
        raise RetirementError(
            "--recovery-ref must be an exact full 40-hex commit ID"
        )

    def run_git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(source_root), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def git(*args: str) -> str:
        result = run_git(*args)
        if result.returncode != 0:
            raise RetirementError(
                f"unable to resolve committed recovery object for source root: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    try:
        recovery_commit = git(
            "rev-parse", "--verify", f"{recovery_ref}^{{commit}}"
        ).lower()
    except RetirementError as exc:
        raise RetirementError(
            "--recovery-ref does not identify an exact commit object"
        ) from exc
    if recovery_commit != recovery_ref.lower():
        raise RetirementError(
            "--recovery-ref must identify the exact commit object, not a tag object"
        )

    head_commit = git("rev-parse", "--verify", "HEAD^{commit}").lower()
    ancestor = run_git(
        "merge-base", "--is-ancestor", recovery_commit, head_commit
    )
    if ancestor.returncode == 1:
        raise RetirementError(
            "--recovery-ref is not an ancestor of current HEAD"
        )
    if ancestor.returncode != 0:
        raise RetirementError(
            "unable to verify recovery commit ancestry: "
            + ancestor.stderr.strip()
        )

    durable_paths = tuple(DURABLE_RECOVERY_OBJECTS)
    if git(
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        *durable_paths,
    ):
        raise RetirementError("durable recovery source has uncommitted changes")

    hashes: dict[str, str] = {}
    for relative, expected_kind in DURABLE_RECOVERY_OBJECTS.items():
        result = run_git(
            "rev-parse", "--verify", f"{recovery_commit}:{relative}"
        )
        if result.returncode != 0:
            raise RetirementError(
                f"recovery commit is missing durable recovery path: {relative}"
            )
        object_id = result.stdout.strip().lower()
        if git("cat-file", "-t", object_id) != expected_kind:
            raise RetirementError(
                f"durable recovery path has unexpected Git object type: {relative}"
            )
        hashes[relative] = object_id

    for relative in RETAINED_DURABLE_PATHS:
        result = run_git(
            "rev-parse", "--verify", f"{head_commit}:{relative}"
        )
        if result.returncode != 0:
            raise RetirementError(
                f"retained durable source is missing from current HEAD: {relative}"
            )
        if result.stdout.strip().lower() != hashes[relative]:
            raise RetirementError(
                f"retained durable source diverged from recovery commit: {relative}"
            )
    return recovery_commit, hashes


def _unique_archive_dir(paths: Paths, epoch: int) -> Path:
    stamp = datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    base = paths.archive_root / f"just-do-it-retirement-{stamp}"
    candidate = base
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = Path(f"{base}-{suffix}")
    return candidate


def _preflight_asset_sources(paths: Paths) -> dict[str, Path]:
    assets = {
        **{key: paths.live_scripts / name for key, name in ENTRYPOINTS.items()},
        "retained-output": paths.cron_output / RETAIN_ID,
        "retired-output-ee4582d0598f": paths.cron_output / "ee4582d0598f",
        "retired-output-ff865c33c5ea": paths.cron_output / "ff865c33c5ea",
        "requeue-ledger": paths.state_dir / "just_do_it_blocked_review_requeue.json",
        "reaction-ledger": paths.state_dir / "just_do_it_review_reactions.json",
        "reaction-live-script": paths.live_scripts / RETAIN_SCRIPT,
        "reaction-live-test": paths.live_scripts
        / "test_just_do_it_review_reaction_watchdog.py",
        "boris-live-source": paths.live_scripts / "boris_intake",
        "boris-versioned-source": paths.source_root / "cron" / "scripts" / "boris_intake",
        "boris-versioned-tests": paths.source_root / "tests" / "cron" / "boris_intake",
        "boris-audit": paths.state_dir / "boris_intake_audit.jsonl",
        "boris-heartbeat": paths.state_dir / "boris_intake_heartbeat",
        "boris-watermark": paths.state_dir
        / f"boris_intake_watermark_{BORIS_CHANNEL}.txt",
        "config": paths.hermes_home / "config.yaml",
        "boris-launchd-plist": paths.boris_launchd_plist,
    }
    for channel in LEGACY_CHANNELS:
        assets[f"channel-watermark-{channel}"] = (
            paths.state_dir / f"slack_channel_watch_{channel}.json"
        )
    return assets


def preflight(paths: Paths, now: Any, recovery_ref: str) -> Manifest:
    """Validate exact identities and create a complete, recoverable snapshot."""
    created_epoch = _epoch(now)
    _assert_store_paths(paths)
    archive_dir = _unique_archive_dir(paths, created_epoch)
    files: dict[str, FileFingerprint] = {}
    try:
        with cron_jobs.use_cron_store(paths.hermes_home):
            with cron_jobs._jobs_lock():
                all_jobs = cron_jobs.load_jobs()
                selected: dict[str, dict[str, Any]] = {}
                for wanted in (*sorted(RETIRE), RETAIN_ID):
                    matches = [job for job in all_jobs if job.get("id") == wanted]
                    if len(matches) != 1:
                        raise RetirementError(
                            f"job fingerprint mismatch: expected exactly one record for {wanted}"
                        )
                    selected[wanted] = copy.deepcopy(matches[0])
                _validate_job_fingerprints(paths, selected)
                archive_dir.mkdir(parents=True, mode=0o700)
                files["jobs-registry"] = _snapshot(
                    paths.jobs_path, archive_dir / "preflight" / "cron" / "jobs.json"
                )

        # The registry lock is deliberately released before unrelated file,
        # database, board, and Git hashing begins.
        latest = _latest_executions(paths.executions_path)
        task_census = _task_census(paths.board_path)
        repository_commit, repository_trees = _repository_identity(
            paths.source_root, recovery_ref
        )
        for key, source in _preflight_asset_sources(paths).items():
            destination = archive_dir / "preflight" / "assets" / key
            files[key] = _snapshot(source, destination)
        files["boris-db"] = _sqlite_snapshot(
            paths.state_dir / "boris_intake.db",
            archive_dir / "preflight" / "assets" / "boris-db.sqlite",
        )
        reaction_raw = json.loads(
            (paths.state_dir / "just_do_it_review_reactions.json").read_text(
                encoding="utf-8"
            )
        )
        manifest_path = archive_dir / "manifest.json"
        manifest = Manifest(
            manifest_path=manifest_path,
            paths=paths,
            created_epoch=created_epoch,
            created_iso=_iso(created_epoch),
            archive_dir=archive_dir,
            jobs=copy.deepcopy(selected),
            files=files,
            latest_executions=latest,
            task_census=task_census,
            repository_commit=repository_commit,
            repository_tree_hashes=repository_trees,
            legacy_reaction_projection_sha256=_json_hash(
                _legacy_projection(reaction_raw)
            ),
        )
        manifest.save()
        return manifest
    except BaseException:
        # Preserve any completed backup material as failure evidence.  It is
        # outside active paths and cannot run accidentally.
        raise


def _assert_manifest_paths(paths: Paths, manifest: Manifest) -> None:
    if paths.to_dict() != manifest.paths.to_dict():
        raise RetirementError("resolved paths do not match the preflight manifest")
    if not manifest.manifest_path.is_relative_to(manifest.archive_dir):
        raise RetirementError("manifest is outside its recorded archive directory")


def pause_producers(paths: Paths, manifest: Manifest, now: Any) -> Manifest:
    """Pause only the two exact producers and record cutoff after verification."""
    _assert_manifest_paths(paths, manifest)
    if manifest.phase == "paused":
        _validate_paused_registry(paths, manifest, now)
        return manifest
    if manifest.phase != "preflight":
        raise RetirementError(f"cannot pause from manifest phase {manifest.phase}")
    manifest_bytes = manifest.manifest_path.read_bytes()
    manifest_mode = stat.S_IMODE(manifest.manifest_path.stat().st_mode)
    manifest_state = (
        manifest.phase,
        manifest.cutoff_epoch,
        manifest.cutoff_iso,
    )
    with cron_jobs.use_cron_store(paths.hermes_home):
        with cron_jobs._jobs_lock():
            original_jobs = copy.deepcopy(cron_jobs.load_jobs())
            selected: dict[str, dict[str, Any]] = {}
            for wanted in (*sorted(RETIRE), RETAIN_ID):
                matches = [job for job in original_jobs if job.get("id") == wanted]
                if len(matches) != 1:
                    raise RetirementError(
                        "job fingerprint mismatch: expected exactly one record "
                        f"for {wanted}"
                    )
                selected[wanted] = copy.deepcopy(matches[0])
            _validate_preflight_job_selection(paths, manifest, selected)
            try:
                for job_id in sorted(RETIRE):
                    updated = cron_jobs.pause_job(
                        job_id, reason="guarded just-do-it retirement"
                    )
                    if updated is None:
                        raise RetirementError(f"exact retiring job disappeared: {job_id}")
                current = {job["id"]: job for job in cron_jobs.load_jobs()}
                for job_id in RETIRE:
                    job = current.get(job_id)
                    if (
                        not job
                        or job.get("enabled") is not False
                        or job.get("state") != "paused"
                    ):
                        raise RetirementError(
                            f"failed to verify exact paused state for {job_id}"
                        )
                if current.get(RETAIN_ID) != manifest.jobs[RETAIN_ID]:
                    raise RetirementError("retained job changed while pausing producers")
                cutoff = _epoch(now)
                manifest.cutoff_epoch = cutoff
                manifest.cutoff_iso = _iso(cutoff)
                manifest.phase = "paused"
                manifest.save()
            except BaseException as pause_error:
                manifest.phase, manifest.cutoff_epoch, manifest.cutoff_iso = manifest_state
                restore_errors: list[BaseException] = []
                try:
                    cron_jobs.save_jobs(original_jobs)
                except BaseException as exc:
                    restore_errors.append(exc)
                try:
                    _atomic_write_bytes(
                        manifest.manifest_path, manifest_bytes, manifest_mode
                    )
                except BaseException as exc:
                    restore_errors.append(exc)
                if restore_errors:
                    raise RetirementError(
                        "producer pause failed and exact preflight state could not be restored"
                    ) from restore_errors[0]
                raise pause_error
    return manifest


def _load_process_listing(paths: Paths) -> list[dict[str, Any]]:
    if paths.process_listing_path is not None:
        if _is_live_home(paths.hermes_home):
            raise RetirementError(
                "--process-listing-path is forbidden for a live Hermes home"
            )
        raw = json.loads(paths.process_listing_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not all(isinstance(row, dict) for row in raw):
            raise RetirementError("injected process listing must be a JSON list of objects")
        return [{**row, "_injected": True} for row in raw]
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RetirementError("unable to obtain process listing")
    rows: list[dict[str, Any]] = []
    pattern = re.compile(r"^\s*(\d+)\s+(.*)$")
    for line in result.stdout.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        pid = int(match.group(1))
        rows.append(
            {
                "pid": pid,
                "started_at": get_process_start_time(pid),
                "command": match.group(2),
            }
        )
    return rows


def _launchd_pid(output: str) -> Optional[int]:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith('"PID"') or stripped.startswith("PID"):
            raw = stripped.split("=", 1)[-1].strip().rstrip(";").strip('"')
            try:
                pid = int(raw)
            except ValueError:
                return None
            return pid if pid > 0 else None
        parts = stripped.split()
        if len(parts) >= 3 and parts[-1] == BORIS_LAUNCHD_LABEL:
            try:
                pid = int(parts[0])
            except ValueError:
                return None
            return pid if pid > 0 else None
    return None


def _load_boris_service_status(paths: Paths) -> dict[str, Any]:
    if paths.boris_service_status_path is not None:
        if _is_live_home(paths.hermes_home):
            raise RetirementError(
                "--boris-service-status-path is forbidden for a live Hermes home"
            )
        raw = json.loads(
            paths.boris_service_status_path.read_text(encoding="utf-8")
        )
        if not isinstance(raw, dict):
            raise RetirementError(
                "injected Boris service status must be a JSON object"
            )
        return raw
    try:
        result = subprocess.run(
            ["launchctl", "list", BORIS_LAUNCHD_LABEL],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RetirementError(f"unable to query Boris launchd service: {exc}") from exc
    pid = _launchd_pid(result.stdout) if result.returncode == 0 else None
    return {
        "label": BORIS_LAUNCHD_LABEL,
        "state": "running" if pid is not None else "stopped",
        "pid": pid,
    }


def _command_basenames(command: str) -> set[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return {Path(token).name for token in tokens if token and not token.startswith("-")}


def _command_exact_paths(command: str) -> set[Path]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    result: set[Path] = set()
    for token in tokens:
        if not token.startswith("/"):
            continue
        try:
            result.add(_resolved(token))
        except OSError:
            continue
    return result


def verify_quiescent(
    paths: Paths,
    manifest: Manifest,
    now: Any,
    grace_seconds: int = 180,
) -> list[str]:
    """Return every quiescence violation; never mutate the protected state."""
    errors: list[str] = []
    try:
        _assert_manifest_paths(paths, manifest)
    except RetirementError as exc:
        return [str(exc)]
    if manifest.cutoff_epoch is None:
        return ["pause cutoff has not been recorded"]
    current_epoch = _epoch(now)
    if current_epoch - manifest.cutoff_epoch < int(grace_seconds):
        errors.append(
            f"quiescence grace has not elapsed: {current_epoch - manifest.cutoff_epoch}s "
            f"of {grace_seconds}s"
        )
    try:
        with _connect_readonly(paths.executions_path) as connection:
            rows = connection.execute(
                """SELECT job_id, status, claimed_at, started_at, finished_at,
                          pid, process_started_at
                   FROM executions
                   WHERE job_id IN (?, ?)
                     AND (
                       status IN ('claimed', 'running', 'unknown')
                       OR unixepoch(claimed_at) > ?
                     )""",
                (*sorted(RETIRE), manifest.cutoff_epoch),
            ).fetchall()
            invalid_timestamps = connection.execute(
                """SELECT job_id, claimed_at FROM executions
                   WHERE job_id IN (?, ?) AND unixepoch(claimed_at) IS NULL""",
                tuple(sorted(RETIRE)),
            ).fetchall()
    except (sqlite3.Error, RetirementError) as exc:
        errors.append(f"execution ledger query failed: {exc}")
        rows = []
        invalid_timestamps = []

    for row in invalid_timestamps:
        errors.append(
            f"execution {row['job_id']} has an invalid claimed_at timestamp: "
            f"{row['claimed_at']}"
        )

    try:
        processes = _load_process_listing(paths)
    except (OSError, ValueError, json.JSONDecodeError, RetirementError) as exc:
        errors.append(f"process census failed: {exc}")
        processes = []
    process_by_identity = {
        (int(row.get("pid", -1)), int(row.get("started_at", -1))): row
        for row in processes
        if row.get("pid") is not None and row.get("started_at") is not None
    }
    for row in rows:
        item = dict(row)
        claimed_epoch = None
        try:
            claimed_epoch = int(
                datetime.fromisoformat(str(item["claimed_at"])).timestamp()
            )
        except (TypeError, ValueError):
            errors.append(
                f"execution {item['job_id']} has an invalid claimed_at timestamp"
            )
        if claimed_epoch is not None and claimed_epoch > manifest.cutoff_epoch:
            errors.append(
                f"post-pause execution for {item['job_id']} claimed at {item['claimed_at']}"
            )
        if item["status"] in {"claimed", "running", "unknown"}:
            errors.append(
                f"nonterminal execution for {item['job_id']}: {item['status']}"
            )
        identity = (int(item["pid"]), int(item["process_started_at"] or -1))
        if identity in process_by_identity:
            errors.append(
                f"live execution owner for {item['job_id']}: pid={identity[0]}"
            )

    retired_names = {expected["script"] for expected in RETIRE.values()}
    retired_paths = {
        paths.live_scripts / expected["script"] for expected in RETIRE.values()
    }
    for process in processes:
        command = str(process.get("command") or "")
        if process.get("_injected"):
            matches = sorted(_command_basenames(command) & retired_names)
        else:
            matches = sorted(
                path.name for path in (_command_exact_paths(command) & retired_paths)
            )
        if matches:
            errors.append(
                f"retired script process is still present: {', '.join(matches)} "
                f"pid={process.get('pid')}"
            )
    try:
        errors.extend(_task_census_drift(manifest.task_census, _task_census(paths.board_path)))
    except (sqlite3.Error, RetirementError) as exc:
        errors.append(f"board census failed: {exc}")
    return errors


def _quiescent_sources(paths: Paths) -> dict[str, Path]:
    sources = {
        "retired-output-ee4582d0598f": paths.cron_output / "ee4582d0598f",
        "retired-output-ff865c33c5ea": paths.cron_output / "ff865c33c5ea",
        "requeue-ledger": paths.state_dir / "just_do_it_blocked_review_requeue.json",
        "reaction-ledger": paths.state_dir / "just_do_it_review_reactions.json",
    }
    for channel in LEGACY_CHANNELS:
        sources[f"channel-watermark-{channel}"] = (
            paths.state_dir / f"slack_channel_watch_{channel}.json"
        )
    return sources


def _load_registry_snapshot(manifest: Manifest) -> list[dict[str, Any]]:
    fingerprint = manifest.files["jobs-registry"]
    _require_match(fingerprint, fingerprint.archive_path)
    raw_snapshot = json.loads(fingerprint.archive_path.read_text(encoding="utf-8"))
    original = raw_snapshot.get("jobs", raw_snapshot)
    if not isinstance(original, list) or not all(
        isinstance(job, dict) for job in original
    ):
        raise RetirementError("backed-up jobs registry is invalid")
    original_by_id = {str(job.get("id")): job for job in original}
    for job_id, expected in manifest.jobs.items():
        if original_by_id.get(job_id) != expected:
            raise RetirementError(
                f"backed-up jobs registry record differs for {job_id}"
            )
    return copy.deepcopy(original)


def _restore_registry_with_records(
    paths: Paths,
    manifest: Manifest,
    protected_records: Mapping[str, dict[str, Any]],
) -> None:
    original = _load_registry_snapshot(manifest)
    protected = set(RETIRE) | {RETAIN_ID}
    if set(protected_records) != protected:
        raise RetirementError("protected registry restore set is incomplete")
    with cron_jobs.use_cron_store(paths.hermes_home):
        with cron_jobs._jobs_lock():
            current = cron_jobs.load_jobs()
            current_by_id = {str(job.get("id")): job for job in current}
            rebuilt: list[dict[str, Any]] = []
            seen: set[str] = set()
            for old in original:
                old_id = str(old.get("id"))
                if old_id in protected:
                    rebuilt.append(copy.deepcopy(protected_records[old_id]))
                    seen.add(old_id)
                elif old_id in current_by_id:
                    rebuilt.append(copy.deepcopy(current_by_id[old_id]))
                    seen.add(old_id)
            for current_job in current:
                current_id = str(current_job.get("id"))
                if current_id not in seen:
                    rebuilt.append(copy.deepcopy(current_job))
            cron_jobs.save_jobs(rebuilt)
            restored = {str(job.get("id")): job for job in cron_jobs.load_jobs()}
            for job_id, expected in protected_records.items():
                if restored.get(job_id) != expected:
                    raise RetirementError(
                        f"job record not restored exactly: {job_id}"
                    )


def _restore_registry_records(paths: Paths, manifest: Manifest) -> None:
    _restore_registry_with_records(paths, manifest, manifest.jobs)


def _restore_paused_registry(
    paths: Paths,
    manifest: Manifest,
    paused_records: Mapping[str, dict[str, Any]],
    *,
    finalize_now: Any,
) -> None:
    """Atomically restore paused retirees while preserving fresh retained state."""
    exact_paused_records: dict[str, dict[str, Any]] = {}
    for job_id in RETIRE:
        record = paused_records.get(job_id)
        if record is None:
            raise RetirementError(
                f"paused compensation record is missing: {job_id}"
            )
        _validate_paused_retiring_record(paths, job_id, record)
        exact_paused_records[job_id] = copy.deepcopy(record)

    retained_error: Optional[BaseException] = None
    with cron_jobs.use_cron_store(paths.hermes_home):
        with cron_jobs._jobs_lock():
            current = copy.deepcopy(cron_jobs.load_jobs())
            compensation_now = max(
                _utc_datetime(finalize_now),
                _utc_datetime(cron_jobs._hermes_now()),
            )
            retained_records = [
                copy.deepcopy(job)
                for job in current
                if job.get("id") == RETAIN_ID
            ]
            if len(retained_records) != 1:
                retained_error = RetirementError(
                    "retained job is missing or duplicated during finalize compensation"
                )
            else:
                try:
                    _validate_retained_record_progress(
                        paths,
                        manifest.jobs[RETAIN_ID],
                        retained_records[0],
                        preflight_epoch=manifest.created_epoch,
                        finalize_now=compensation_now,
                    )
                except BaseException as exc:
                    retained_error = exc

            rebuilt: list[dict[str, Any]] = []
            restored_retirees: set[str] = set()
            for job in current:
                job_id = str(job.get("id"))
                if job_id in RETIRE:
                    if job_id not in restored_retirees:
                        rebuilt.append(copy.deepcopy(exact_paused_records[job_id]))
                        restored_retirees.add(job_id)
                    continue
                rebuilt.append(copy.deepcopy(job))
            for job_id in sorted(set(RETIRE) - restored_retirees):
                rebuilt.append(copy.deepcopy(exact_paused_records[job_id]))

            cron_jobs.save_jobs(rebuilt)
            restored = cron_jobs.load_jobs()
            for job_id, expected in exact_paused_records.items():
                matches = [job for job in restored if job.get("id") == job_id]
                if matches != [expected]:
                    raise RetirementError(
                        f"paused job record not restored exactly: {job_id}"
                    )
            restored_retained = [
                job for job in restored if job.get("id") == RETAIN_ID
            ]
            if restored_retained != retained_records:
                raise RetirementError(
                    "latest retained job record changed during compensation"
                )

            if retained_error is not None:
                raise RetirementError(
                    "retained job failed validation during finalize compensation"
                ) from retained_error


def _protect_remove_output(paths: Paths, manifest: Manifest, job_id: str) -> None:
    output = paths.cron_output / job_id
    expected = manifest.quiescent_files[f"retired-output-{job_id}"]
    _require_match(expected, output)
    protected = manifest.protected_output_paths[job_id]
    if protected.exists():
        raise RetirementError(f"protected output staging path already exists: {protected}")
    removal_error: Optional[BaseException] = None
    with cron_jobs.use_cron_store(paths.hermes_home):
        with cron_jobs._jobs_lock():
            matches = [job for job in cron_jobs.load_jobs() if job.get("id") == job_id]
            if len(matches) != 1:
                raise RetirementError(
                    f"job {job_id} fingerprint mismatch before exact removal"
                )
            _validate_paused_retiring_record(paths, job_id, matches[0])
            protected.parent.mkdir(parents=True, exist_ok=True)
            os.replace(output, protected)
            try:
                if not cron_jobs.remove_job(job_id):
                    raise RetirementError(f"exact retiring job could not be removed: {job_id}")
            except BaseException as exc:
                removal_error = exc
            finally:
                if protected.exists():
                    if output.exists():
                        raise RetirementError(
                            f"output path unexpectedly recreated during removal: {output}"
                        )
                    os.replace(protected, output)
    _require_match(expected, output)
    if removal_error is not None:
        raise removal_error


def _restore_protected_outputs(
    paths: Paths, manifest: Manifest
) -> list[BaseException]:
    errors: list[BaseException] = []
    for job_id in sorted(RETIRE):
        try:
            output = paths.cron_output / job_id
            expected = manifest.quiescent_files[f"retired-output-{job_id}"]
            protected = manifest.protected_output_paths[job_id]
            if protected.exists():
                if output.exists():
                    raise RetirementError(
                        "output path and protected staging path both exist during "
                        f"finalize compensation: {output}"
                    )
                os.replace(protected, output)
            _require_match(expected, output)
            if protected.exists():
                raise RetirementError(
                    f"protected output staging path remains after compensation: {protected}"
                )
        except BaseException as exc:
            errors.append(exc)
    return errors


def _validate_paused_retiring_record(
    paths: Paths, job_id: str, job: Mapping[str, Any]
) -> None:
    expected = RETIRE[job_id]
    conditions = {
        "name": job.get("name") == expected["name"],
        "schedule": _matches_schedule_identity(
            job.get("schedule"), expected["schedule"]
        ),
        "script": _matches_script_identity(
            job.get("script"), paths.live_scripts, expected["script"]
        ),
        "enabled": job.get("enabled") is False,
        "state": job.get("state") == "paused",
    }
    failed = [name for name, valid in conditions.items() if not valid]
    if failed:
        raise RetirementError(
            f"job {job_id} fingerprint mismatch before exact removal: "
            f"{', '.join(failed)}"
        )


def _validate_retained_record_progress(
    paths: Paths,
    preflight_record: Mapping[str, Any],
    current_record: Any,
    *,
    preflight_epoch: int,
    finalize_now: Any,
) -> dict[str, Any]:
    """Validate a reachable recurring-scheduler transition.

    Recurring scheduler storage has three observable phases: an idle/completed
    record, a preemptively advanced ``next_run_at``, or an external
    ``fire_claim`` whose timestamp determines that next run.  Only
    ``mark_job_run`` changes completion fields.  One-shot ``run_claim`` state
    is unreachable for this cron job.
    """
    missing = object()
    schedule_identity = {"kind": "cron", "expr": "* * * * *"}
    runtime_fields = (
        "last_run_at",
        "next_run_at",
        "last_status",
        "last_error",
        "last_delivery_error",
        "fire_claim",
        "run_claim",
        # upstream (Aug 2026) keeps a per-job consecutive-failure counter that
        # mark_job_run rewrites on every run: scheduler progress, not config.
        "failure_streak",
    )

    def fail(detail: str) -> None:
        raise RetirementError(f"retained job {detail} before finalize")

    preflight_time = _utc_datetime(preflight_epoch)
    finalize_time = _utc_datetime(finalize_now)
    if finalize_time < preflight_time:
        fail("finalize observation time predates preflight")

    def timestamp(
        value: Any, field_name: str, record_name: str
    ) -> Optional[datetime]:
        if value is missing or value is None:
            return None
        if not isinstance(value, str) or not value or value != value.strip():
            fail(f"{record_name} {field_name} has an invalid runtime type")
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            fail(f"{record_name} {field_name} has an invalid runtime shape")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            fail(f"{record_name} {field_name} must be timezone-aware")
        return parsed.astimezone(UTC)

    def claim(
        value: Any, field_name: str, record_name: str
    ) -> Optional[datetime]:
        if value is missing or value is None:
            return None
        if not isinstance(value, Mapping) or set(value) != {"at", "by"}:
            fail(f"{record_name} {field_name} has an invalid runtime shape")
        claimed_at = timestamp(value["at"], f"{field_name}.at", record_name)
        if claimed_at is None:
            fail(f"{record_name} {field_name}.at is required")
        owner = value["by"]
        if not isinstance(owner, str) or not owner.strip():
            fail(f"{record_name} {field_name}.by has an invalid runtime type")
        return claimed_at

    def completed(value: Any, record_name: str) -> int:
        if value is missing:
            return 0
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            fail(f"{record_name} repeat.completed has an invalid runtime type")
        return value

    def validate_completion_shape(
        runtime: Mapping[str, Any],
        last_run: Optional[datetime],
        completion_count: int,
        record_name: str,
    ) -> None:
        status = runtime["last_status"]
        # upstream (Aug 2026) records a run whose agent succeeded but whose delivery
        # failed as ``delivery_failed`` (never ``ok``); it is still a terminal completion.
        if status is not missing and status is not None and status not in (
            "ok",
            "delivery_failed",
            "error",
        ):
            fail(f"{record_name} last_status has an invalid runtime value")
        for field_name in ("last_error", "last_delivery_error"):
            value = runtime[field_name]
            if (
                value is not missing
                and value is not None
                and not isinstance(value, str)
            ):
                fail(f"{record_name} {field_name} has an invalid runtime type")
        if last_run is None:
            for field_name in (
                "last_status",
                "last_error",
                "last_delivery_error",
            ):
                if runtime[field_name] not in (missing, None):
                    fail(
                        f"{record_name} {field_name} is not coupled to a completion"
                    )
            if completion_count != 0:
                fail(
                    f"{record_name} repeat.completed is not coupled to a completion"
                )
        elif status not in ("ok", "delivery_failed", "error"):
            fail(f"{record_name} completed run requires a terminal status")
        if status in ("ok", "delivery_failed") and runtime["last_error"] not in (missing, None):
            fail(f"{record_name} successful run cannot retain last_error")

    def scheduled_next(base: datetime, detail: str) -> datetime:
        try:
            computed = cron_jobs.compute_next_run(
                schedule_identity, base.isoformat()
            )
        except Exception as exc:
            fail(f"could not compute {detail}: {exc}")
        parsed = timestamp(computed, detail, "scheduler")
        if parsed is None:
            fail(f"could not compute {detail}")
        return parsed

    def require_schedule_occurrence(value: datetime, detail: str) -> None:
        preceding = value - timedelta(microseconds=1)
        if scheduled_next(preceding, detail) != value:
            fail(f"{detail} is not a retained cron occurrence")

    def project(
        raw: Any, record_name: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(raw, Mapping):
            fail(f"{record_name} record has an invalid shape")
        record = copy.deepcopy(dict(raw))
        if record.get("id") != RETAIN_ID:
            fail(f"{record_name} id is invalid")
        if not _matches_script_identity(
            record.get("script"), paths.live_scripts, RETAIN_SCRIPT
        ):
            fail(f"{record_name} script identity is invalid")
        if not _matches_schedule_identity(
            record.get("schedule"), schedule_identity
        ):
            fail(f"{record_name} schedule identity is invalid")
        if record.get("no_agent") is not True:
            fail(f"{record_name} no_agent identity is invalid")
        if record.get("enabled") is not True or record.get("state") != "scheduled":
            fail(f"{record_name} must remain enabled and scheduled")

        runtime = {
            field_name: record.pop(field_name, missing)
            for field_name in runtime_fields
        }
        repeat = record.get("repeat", missing)
        runtime["repeat.completed"] = missing
        if repeat is not missing and repeat is not None:
            if not isinstance(repeat, Mapping):
                fail(f"{record_name} repeat has an invalid shape")
            repeat_projection = copy.deepcopy(dict(repeat))
            runtime["repeat.completed"] = repeat_projection.pop(
                "completed", missing
            )
            repeat_limit = repeat_projection.get("times")
            if repeat_limit is not None and (
                not isinstance(repeat_limit, int)
                or isinstance(repeat_limit, bool)
                or repeat_limit <= 0
            ):
                fail(f"{record_name} repeat limit has an invalid type")
            record["repeat"] = repeat_projection
        return record, runtime

    preflight_projection, preflight_runtime = project(
        preflight_record, "preflight"
    )
    current_projection, current_runtime = project(current_record, "current")
    if current_projection != preflight_projection:
        fail("immutable/config fingerprint mismatch")

    for field_name in (*runtime_fields, "repeat.completed"):
        if (
            preflight_runtime[field_name] is not missing
            and current_runtime[field_name] is missing
        ):
            fail(f"current {field_name} was removed")

    preflight_last_run = timestamp(
        preflight_runtime["last_run_at"], "last_run_at", "preflight"
    )
    current_last_run = timestamp(
        current_runtime["last_run_at"], "last_run_at", "current"
    )
    if preflight_last_run is not None and (
        current_last_run is None or current_last_run < preflight_last_run
    ):
        fail("last_run_at runtime progress regressed")
    last_run_advanced = (
        current_last_run is not None
        and (
            preflight_last_run is None
            or current_last_run > preflight_last_run
        )
    )
    if (
        not last_run_advanced
        and current_runtime["last_run_at"]
        != preflight_runtime["last_run_at"]
    ):
        fail("last_run_at changed without a newer completion")
    if last_run_advanced:
        if current_last_run < preflight_time:
            fail("new completion predates preflight observation window")
        if current_last_run > finalize_time:
            fail("new completion exceeds finalize observation window")

    preflight_next_run = timestamp(
        preflight_runtime["next_run_at"], "next_run_at", "preflight"
    )
    current_next_run = timestamp(
        current_runtime["next_run_at"], "next_run_at", "current"
    )
    if preflight_next_run is None or current_next_run is None:
        fail("next_run_at is required for the scheduled recurring job")
    if current_next_run < preflight_next_run:
        fail("next_run_at runtime progress regressed")
    require_schedule_occurrence(preflight_next_run, "preflight next_run_at")
    require_schedule_occurrence(current_next_run, "current next_run_at")
    if preflight_last_run is not None and preflight_next_run <= preflight_last_run:
        fail("preflight next_run_at must follow the latest completed run")
    if current_last_run is not None and current_next_run <= current_last_run:
        fail("current next_run_at must follow the latest completed run")

    preflight_completed = completed(
        preflight_runtime["repeat.completed"], "preflight"
    )
    current_completed = completed(
        current_runtime["repeat.completed"], "current"
    )
    if current_completed < preflight_completed:
        fail("repeat.completed runtime progress regressed")

    validate_completion_shape(
        preflight_runtime,
        preflight_last_run,
        preflight_completed,
        "preflight",
    )
    validate_completion_shape(
        current_runtime,
        current_last_run,
        current_completed,
        "current",
    )

    completion_fields = (
        "last_status",
        "last_error",
        "last_delivery_error",
        "repeat.completed",
    )
    if last_run_advanced:
        for field_name in completion_fields:
            if current_runtime[field_name] is missing:
                fail(f"new completion did not persist {field_name}")
        if current_completed <= preflight_completed:
            fail("new completion did not increase repeat.completed")
        completion_next = scheduled_next(
            current_last_run, "next_run_at after completion"
        )
        if current_next_run < completion_next:
            fail("next_run_at regressed behind the latest completion")
    else:
        for field_name in completion_fields:
            if current_runtime[field_name] != preflight_runtime[field_name]:
                fail(f"{field_name} changed without a newer completion")

    for record_name, runtime in (
        ("preflight", preflight_runtime),
        ("current", current_runtime),
    ):
        run_claim = runtime["run_claim"]
        if run_claim not in (missing, None):
            fail(f"{record_name} run_claim is impossible for a cron job")
    if current_runtime["run_claim"] != preflight_runtime["run_claim"]:
        fail("run_claim storage changed for a cron job")

    preflight_fire_at = claim(
        preflight_runtime["fire_claim"], "fire_claim", "preflight"
    )
    current_fire_at = claim(
        current_runtime["fire_claim"], "fire_claim", "current"
    )

    if (
        last_run_advanced
        and current_runtime["fire_claim"] not in (missing, None)
    ):
        fail("new completion did not clear fire_claim")

    if preflight_fire_at is not None:
        if (
            preflight_last_run is not None
            and preflight_fire_at <= preflight_last_run
        ):
            fail("preflight fire_claim is not after the latest completion")
        if scheduled_next(
            preflight_fire_at, "preflight claimed next_run_at"
        ) != preflight_next_run:
            fail("preflight fire_claim is not coupled to next_run_at")

    if current_fire_at is not None:
        if current_fire_at < preflight_time:
            fail("current fire_claim predates preflight observation window")
        if current_fire_at > finalize_time:
            fail("current fire_claim exceeds finalize observation window")
        if current_last_run is not None and current_fire_at <= current_last_run:
            fail("current fire_claim is not after the latest completion")
        if scheduled_next(
            current_fire_at, "current claimed next_run_at"
        ) != current_next_run:
            fail("current fire_claim is not coupled to next_run_at")
        claim_changed = (
            current_runtime["fire_claim"]
            != preflight_runtime["fire_claim"]
        )
        if claim_changed:
            if preflight_fire_at is not None and current_fire_at <= preflight_fire_at:
                fail("current fire_claim timestamp did not advance")
    elif (
        not last_run_advanced
        and current_runtime["fire_claim"] != preflight_runtime["fire_claim"]
    ):
        fail("fire_claim changed without a claim or completion")

    return copy.deepcopy(dict(current_record))


def _validate_paused_registry(
    paths: Paths, manifest: Manifest, observation_now: Any
) -> dict[str, dict[str, Any]]:
    jobs = _select_exact_job_records(
        _load_job_records(paths), (*sorted(RETIRE), RETAIN_ID)
    )
    for job_id in RETIRE:
        job = jobs.get(job_id)
        if job is None:
            raise RetirementError(
                f"job {job_id} fingerprint mismatch before exact removal"
            )
        _validate_paused_retiring_record(paths, job_id, job)
    retained = _validate_retained_record_progress(
        paths,
        manifest.jobs[RETAIN_ID],
        jobs.get(RETAIN_ID),
        preflight_epoch=manifest.created_epoch,
        finalize_now=observation_now,
    )
    paused_records = {
        job_id: copy.deepcopy(jobs[job_id])
        for job_id in sorted(RETIRE)
    }
    paused_records[RETAIN_ID] = retained
    return paused_records


def _validate_finalize_plan(
    paths: Paths, manifest: Manifest, now: Any
) -> Optional[dict[str, dict[str, Any]]]:
    _assert_manifest_paths(paths, manifest)
    errors = verify_quiescent(paths, manifest, now)
    if errors:
        raise RetirementError("quiescence check failed: " + "; ".join(errors))
    if manifest.phase not in {"paused", "finalized"}:
        raise RetirementError(f"cannot finalize from manifest phase {manifest.phase}")
    if manifest.phase == "finalized":
        retired_errors = verify_retired(paths, manifest, now)
        if retired_errors:
            raise RetirementError(
                "finalized manifest no longer verifies: "
                + "; ".join(retired_errors)
            )
        return None
    paused_records = _validate_paused_registry(paths, manifest, now)
    for key in ENTRYPOINTS:
        _require_match(manifest.files[key])
    for source in _quiescent_sources(paths).values():
        _measure(source)
    for job_id in RETIRE:
        protected = manifest.archive_dir / "protected-output" / job_id
        if protected.exists():
            raise RetirementError(
                f"protected output staging path already exists: {protected}"
            )
    return paused_records


def finalize(paths: Paths, manifest: Manifest, now: Any) -> Manifest:
    """Archive exact entrypoints and remove exact jobs after quiescence proof."""
    paused_records = _validate_finalize_plan(paths, manifest, now)
    if paused_records is None:
        return manifest

    quiescent_base = manifest.archive_dir / f"quiescent-{_epoch(now)}"
    quiescent_dir = quiescent_base
    quiescent_suffix = 0
    while quiescent_dir.exists():
        quiescent_suffix += 1
        quiescent_dir = Path(f"{quiescent_base}-{quiescent_suffix}")
    quiescent: dict[str, FileFingerprint] = {}
    for key, source in _quiescent_sources(paths).items():
        quiescent[key] = _snapshot(source, quiescent_dir / key)
    manifest.quiescent_files = quiescent
    manifest.legacy_reaction_projection_sha256 = _json_hash(
        _archived_legacy_projection(quiescent["reaction-ledger"], "quiescent")
    )
    manifest.protected_output_paths = {
        job_id: manifest.archive_dir / "protected-output" / job_id
        for job_id in RETIRE
    }
    manifest.save()
    paused_manifest_bytes = manifest.manifest_path.read_bytes()
    paused_manifest_mode = stat.S_IMODE(manifest.manifest_path.stat().st_mode)
    paused_phase = manifest.phase
    paused_finalized_epoch = manifest.finalized_epoch
    paused_finalized_iso = manifest.finalized_iso

    moved: list[tuple[Path, Path, str, FileFingerprint]] = []
    try:
        for key in ENTRYPOINTS:
            fingerprint = manifest.files[key]
            _require_match(fingerprint)
            destination = manifest.archive_dir / "retired-entrypoints" / fingerprint.active_path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise RetirementError(f"retired archive path already exists: {destination}")
            moved.append((fingerprint.active_path, destination, key, fingerprint))
            os.replace(fingerprint.active_path, destination)
            moved_fingerprint = replace(fingerprint, archive_path=destination)
            _require_match(moved_fingerprint, destination)
            manifest.files[key] = moved_fingerprint

        for job_id in sorted(RETIRE):
            _protect_remove_output(paths, manifest, job_id)
        completed = _epoch(now)
        manifest.finalized_epoch = completed
        manifest.finalized_iso = _iso(completed)
        manifest.phase = "finalized"
        manifest.save()
    except BaseException as finalize_error:
        compensation_errors: list[BaseException] = []
        source_compensation_errors: list[BaseException] = []
        for active, archived, key, original_fingerprint in reversed(moved):
            try:
                if archived.exists() and active.exists():
                    raise RetirementError(
                        "entrypoint source and archive both exist during "
                        f"finalize compensation: {active}"
                    )
                if archived.exists():
                    os.replace(archived, active)
                _require_match(original_fingerprint)
            except BaseException as exc:
                source_compensation_errors.append(exc)
            manifest.files[key] = original_fingerprint
        compensation_errors.extend(source_compensation_errors)
        output_compensation_errors = _restore_protected_outputs(paths, manifest)
        compensation_errors.extend(output_compensation_errors)
        if not source_compensation_errors and not output_compensation_errors:
            try:
                _restore_paused_registry(
                    paths,
                    manifest,
                    paused_records,
                    finalize_now=now,
                )
            except BaseException as exc:
                compensation_errors.append(exc)
        manifest.phase = paused_phase
        manifest.finalized_epoch = paused_finalized_epoch
        manifest.finalized_iso = paused_finalized_iso
        try:
            _atomic_write_bytes(
                manifest.manifest_path,
                paused_manifest_bytes,
                paused_manifest_mode,
            )
        except BaseException as exc:
            compensation_errors.append(exc)
        if compensation_errors:
            raise RetirementError(
                "finalize failed and exact paused-state compensation failed"
            ) from compensation_errors[0]
        raise finalize_error
    return manifest


def _latest_execution(path: Path, job_id: str) -> Optional[dict[str, Any]]:
    with _connect_readonly(path) as connection:
        row = connection.execute(
            """SELECT id, job_id, status, claimed_at, started_at, finished_at,
                      pid, process_started_at
               FROM executions WHERE job_id=?
               ORDER BY unixepoch(claimed_at) DESC, id DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _validate_retained_job(
    job: Optional[dict[str, Any]],
    paths: Paths,
    expected_name: str,
    expected_prompt: Optional[str] = None,
) -> list[str]:
    if job is None:
        return ["retained job is missing"]
    checks = {
        "id": job.get("id") == RETAIN_ID,
        "script": _matches_script_identity(
            job.get("script"), paths.live_scripts, RETAIN_SCRIPT
        ),
        "schedule": _matches_schedule_identity(
            job.get("schedule"), {"kind": "cron", "expr": "* * * * *"}
        ),
        "no_agent": job.get("no_agent") is True,
        "enabled": job.get("enabled") is True,
        "state": job.get("state") == "scheduled",
        "name": job.get("name") == expected_name,
    }
    if expected_prompt is not None:
        checks["prompt"] = job.get("prompt") == expected_prompt
    return [f"retained job {key} is invalid" for key, valid in checks.items() if not valid]


def _sqlite_logical_state(path: Path) -> dict[str, Any]:
    with _connect_readonly(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            if not str(row[0]).startswith("sqlite_")
        ]
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }
    return {"integrity": integrity, "tables": tables, "counts": counts}


def _sqlite_baseline_is_subset(snapshot: Path, current: Path) -> bool:
    """Prove durable dedup/lane rows survived while allowing new intake rows."""
    source = _connect_readonly(snapshot)
    live = _connect_readonly(current)
    try:
        for table in ("seen_events", "lanes"):
            source_columns = [
                row[1] for row in source.execute(f'PRAGMA table_info("{table}")').fetchall()
            ]
            live_columns = [
                row[1] for row in live.execute(f'PRAGMA table_info("{table}")').fetchall()
            ]
            if source_columns != live_columns or not source_columns:
                return False
            select = ", ".join(f'"{column}"' for column in source_columns)
            baseline_rows = {tuple(row) for row in source.execute(f'SELECT {select} FROM "{table}"')}
            live_rows = {tuple(row) for row in live.execute(f'SELECT {select} FROM "{table}"')}
            if not baseline_rows <= live_rows:
                return False
        return True
    finally:
        live.close()
        source.close()


def _watermark_value(path: Path) -> Decimal:
    try:
        value = Decimal(path.read_text(encoding="utf-8").strip())
    except (OSError, InvalidOperation) as exc:
        raise RetirementError(f"invalid Boris watermark at {path}: {exc}") from exc
    if not value.is_finite():
        raise RetirementError(f"invalid Boris watermark at {path}: value is not finite")
    return value


def _source_tree_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_file() and not path.is_symlink():
            result[relative.as_posix()] = _file_hash(path)
    return result


def _boris_source_matches_versioned(paths: Paths) -> bool:
    versioned = paths.source_root / "cron" / "scripts" / "boris_intake"
    live = paths.live_scripts / "boris_intake"
    versioned_files = _source_tree_hashes(versioned)
    return bool(versioned_files) and versioned_files == _source_tree_hashes(live)


def _file_prefix_matches(snapshot: Path, current: Path) -> bool:
    try:
        expected = snapshot.read_bytes()
        actual = current.read_bytes()
    except OSError:
        return False
    return actual.startswith(expected)


def _checkpoint_files(raw: Mapping[str, Any]) -> dict[str, FileFingerprint]:
    return {
        key: FileFingerprint.from_dict(value)
        for key, value in raw.get("files", {}).items()
    }


def _verified_archive_bytes(
    fingerprint: FileFingerprint, baseline_name: str
) -> bytes:
    archive_path = fingerprint.archive_path
    try:
        path_before = os.lstat(archive_path)
    except FileNotFoundError as exc:
        raise RetirementError(
            f"{baseline_name} reaction-ledger snapshot is missing"
        ) from exc
    except OSError as exc:
        raise RetirementError(
            f"{baseline_name} reaction-ledger snapshot could not be inspected: {exc}"
        ) from exc
    if fingerprint.kind != "file" or not stat.S_ISREG(path_before.st_mode):
        raise RetirementError(
            f"{baseline_name} reaction-ledger snapshot is not a regular file"
        )

    flags = os.O_RDONLY
    for option in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= int(getattr(os, option, 0))
    try:
        descriptor = os.open(archive_path, flags)
    except OSError as exc:
        raise RetirementError(
            f"{baseline_name} reaction-ledger snapshot changed during verified read"
        ) from exc

    def stable_state(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise RetirementError(
                    f"{baseline_name} reaction-ledger snapshot is not a regular file"
                )
            if stable_state(path_before) != stable_state(opened):
                raise RetirementError(
                    f"{baseline_name} reaction-ledger snapshot changed during verified read"
                )
            if (
                stat.S_IMODE(opened.st_mode) != fingerprint.mode
                or opened.st_size != fingerprint.size
            ):
                raise RetirementError(
                    f"{baseline_name} reaction-ledger snapshot fingerprint mismatch"
                )
            payload = handle.read(fingerprint.size + 1)
            after_read = os.fstat(handle.fileno())
    except RetirementError:
        raise
    except OSError as exc:
        raise RetirementError(
            f"{baseline_name} reaction-ledger snapshot changed during verified read"
        ) from exc

    try:
        path_after = os.lstat(archive_path)
    except OSError as exc:
        raise RetirementError(
            f"{baseline_name} reaction-ledger snapshot changed during verified read"
        ) from exc
    if (
        not stat.S_ISREG(path_after.st_mode)
        or stable_state(opened) != stable_state(after_read)
        or stable_state(after_read) != stable_state(path_after)
    ):
        raise RetirementError(
            f"{baseline_name} reaction-ledger snapshot changed during verified read"
        )
    if (
        len(payload) != fingerprint.size
        or hashlib.sha256(payload).hexdigest() != fingerprint.sha256
    ):
        raise RetirementError(
            f"{baseline_name} reaction-ledger snapshot fingerprint mismatch"
        )
    return payload


def _archived_legacy_projection(
    fingerprint: FileFingerprint, baseline_name: str
) -> dict[str, Any]:
    try:
        raw = json.loads(_verified_archive_bytes(fingerprint, baseline_name))
    except (ValueError, TypeError) as exc:
        raise RetirementError(
            f"{baseline_name} reaction-ledger snapshot is malformed: {exc}"
        ) from exc
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("posts"), dict)
        or not isinstance(raw.get("reacted"), dict)
    ):
        raise RetirementError(
            f"{baseline_name} reaction-ledger snapshot is malformed"
        )
    return _legacy_projection(raw)


def _legacy_reaction_baseline(manifest: Manifest) -> dict[str, Any]:
    checkpoints = manifest.component_checkpoints.get("retained-reaction", [])
    if checkpoints:
        baseline_name = "latest retained-reaction checkpoint"
        try:
            checkpoint = checkpoints[-1]
            files = checkpoint["files"]
            raw_fingerprint = files["reaction-ledger"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise RetirementError(
                f"{baseline_name} reaction-ledger snapshot is missing"
            ) from exc
        try:
            fingerprint = FileFingerprint.from_dict(raw_fingerprint)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise RetirementError(
                f"{baseline_name} reaction-ledger fingerprint is malformed"
            ) from exc
    else:
        baseline_name = "quiescent"
        fingerprint = manifest.quiescent_files.get("reaction-ledger")
        if fingerprint is None:
            raise RetirementError(
                f"{baseline_name} reaction-ledger snapshot is missing"
            )
    return _archived_legacy_projection(fingerprint, baseline_name)


def _latest_reaction_checkpoint_fingerprint(manifest: Manifest) -> FileFingerprint:
    checkpoints = manifest.component_checkpoints.get("retained-reaction", [])
    if not checkpoints:
        raise RetirementError("retained-reaction checkpoint is missing")
    try:
        raw = checkpoints[-1]["files"]["reaction-ledger"]
        fingerprint = FileFingerprint.from_dict(raw)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise RetirementError(
            "latest retained-reaction checkpoint fingerprint is malformed"
        ) from exc
    _verified_archive_bytes(fingerprint, "latest retained-reaction checkpoint")
    return fingerprint


@contextlib.contextmanager
def _reaction_ledger_lock(state_path: Path):
    lock_path = state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RetirementError("reaction ledger lock is busy") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_finalized_retired_boundary(
    paths: Paths,
    manifest: Manifest,
    jobs: Iterable[Mapping[str, Any]],
    *,
    operation: str,
) -> Mapping[str, Any]:
    _assert_manifest_paths(paths, manifest)
    if manifest.phase != "finalized":
        raise RetirementError(
            f"{operation} requires finalized manifest, found {manifest.phase}"
        )
    job_records = list(jobs)
    retained_records = [
        job for job in job_records if str(job.get("id")) == RETAIN_ID
    ]
    if len(retained_records) != 1:
        raise RetirementError(
            "expected exactly one retained job record, "
            f"found {len(retained_records)}"
        )
    by_id = {str(job.get("id")): job for job in job_records}
    present_retired = sorted(set(RETIRE) & set(by_id))
    if present_retired:
        raise RetirementError(
            f"retired jobs remain registered: {', '.join(present_retired)}"
        )
    retained = retained_records[0]
    for key in ENTRYPOINTS:
        fingerprint = manifest.files[key]
        if fingerprint.active_path.exists():
            raise RetirementError(
                f"retired entrypoint remains active: {fingerprint.active_path}"
            )
        if not _matches(fingerprint, fingerprint.archive_path):
            raise RetirementError(
                f"retired archive fingerprint mismatch: {fingerprint.archive_path}"
            )
    return retained


def _validate_paused_retired_boundary(
    paths: Paths,
    manifest: Manifest,
    jobs: Iterable[Mapping[str, Any]],
) -> None:
    retained = _validate_finalized_retired_boundary(
        paths,
        manifest,
        jobs,
        operation="paused retained operation",
    )
    paused_checks = {
        "id": retained.get("id") == RETAIN_ID,
        "name": retained.get("name") == RETAIN_POST_MIGRATION_NAME,
        "script": _matches_script_identity(
            retained.get("script"), paths.live_scripts, RETAIN_SCRIPT
        ),
        "schedule": _matches_schedule_identity(
            retained.get("schedule"), {"kind": "cron", "expr": "* * * * *"}
        ),
        "no_agent": retained.get("no_agent") is True,
        "enabled": retained.get("enabled") is False,
        "state": retained.get("state") == "paused",
    }
    failed = [name for name, valid in paused_checks.items() if not valid]
    if failed:
        if set(failed) & {"enabled", "state"}:
            raise RetirementError("retained job must be paused")
        raise RetirementError(
            f"paused retained job fingerprint mismatch: {', '.join(failed)}"
        )


def _compensation_dir(manifest: Manifest, epoch: int) -> Path:
    root = manifest.archive_dir / "compensations"
    root.mkdir(parents=True, exist_ok=True)
    base = root / f"legacy-alerted-at-{epoch}"
    candidate = base
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = Path(f"{base}-{counter}")
    candidate.mkdir(mode=0o700)
    return candidate


def configure_retained_boris_only_prompt(
    paths: Paths,
    manifest: Manifest,
    *,
    apply: bool,
) -> dict[str, Any]:
    """Change only the paused retained job prompt under exact guards."""
    with cron_jobs.use_cron_store(paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            _validate_paused_retired_boundary(paths, manifest, jobs)
            retained = next(job for job in jobs if job.get("id") == RETAIN_ID)
            current_prompt = retained.get("prompt")
            if current_prompt not in {
                RETAIN_ALL_CHANNEL_PROMPT,
                RETAIN_BORIS_ONLY_PROMPT,
            }:
                raise RetirementError("retained prompt precondition failed")

            status = (
                "already_configured"
                if current_prompt == RETAIN_BORIS_ONLY_PROMPT
                else "planned"
            )
            report = {
                "apply": bool(apply),
                "status": status,
                "job_id": RETAIN_ID,
                "before_prompt": current_prompt,
                "after_prompt": RETAIN_BORIS_ONLY_PROMPT,
            }
            if status == "already_configured" or not apply:
                return report

            expected_jobs = copy.deepcopy(jobs)
            expected_retained = next(
                job for job in expected_jobs if job.get("id") == RETAIN_ID
            )
            expected_retained["prompt"] = RETAIN_BORIS_ONLY_PROMPT
            try:
                cron_jobs.save_jobs(expected_jobs)
                reloaded = cron_jobs.load_jobs()
                if reloaded != expected_jobs:
                    raise RetirementError(
                        "retained prompt update changed unexpected job fields"
                    )
            except BaseException:
                try:
                    cron_jobs.save_jobs(jobs)
                    if cron_jobs.load_jobs() != jobs:
                        raise RetirementError(
                            "restored registry differs from pre-update job records"
                        )
                except BaseException as restore_exc:
                    raise RetirementError(
                        "retained prompt verification failed and registry restoration failed"
                    ) from restore_exc
                raise
            report["status"] = "configured"
            return report


def compensate_legacy_alerted_at(
    paths: Paths,
    manifest: Manifest,
    permalink: str,
    expected_current: int,
    expected_baseline: int,
    now: Any,
    *,
    apply: bool,
) -> dict[str, Any]:
    """Restore one proven legacy alert timestamp from the immutable checkpoint."""
    match = _REACTION_PERMALINK.fullmatch(str(permalink or ""))
    if match is None or match.group("channel").upper() not in LEGACY_CHANNELS:
        raise RetirementError("selected permalink is not legacy")
    if isinstance(expected_current, bool) or not isinstance(expected_current, int):
        raise RetirementError("expected current alerted_at must be an integer")
    if isinstance(expected_baseline, bool) or not isinstance(expected_baseline, int):
        raise RetirementError("expected baseline alerted_at must be an integer")

    state_path = paths.state_dir / "just_do_it_review_reactions.json"
    with _reaction_ledger_lock(state_path):
        with cron_jobs.use_cron_store(paths.hermes_home):
            with cron_jobs._jobs_lock():
                jobs = cron_jobs.load_jobs()
                _validate_paused_retired_boundary(paths, manifest, jobs)

                checkpoint_fingerprint = _latest_reaction_checkpoint_fingerprint(
                    manifest
                )
                checkpoint_legacy = _legacy_reaction_baseline(manifest)
                try:
                    current_raw = json.loads(state_path.read_bytes())
                except (OSError, ValueError, TypeError) as exc:
                    raise RetirementError(
                        f"live reaction ledger is unreadable: {exc}"
                    ) from exc
                if (
                    not isinstance(current_raw, dict)
                    or not isinstance(current_raw.get("posts"), dict)
                    or not isinstance(current_raw.get("reacted"), dict)
                ):
                    raise RetirementError("live reaction ledger is malformed")

                baseline_record = checkpoint_legacy["posts"].get(permalink)
                current_record = current_raw["posts"].get(permalink)
                if not isinstance(baseline_record, dict):
                    raise RetirementError(
                        "selected permalink is missing from the checkpoint"
                    )
                if not isinstance(current_record, dict):
                    raise RetirementError("selected permalink is missing from live state")
                baseline_alerted_at = baseline_record.get("alerted_at")
                current_alerted_at = current_record.get("alerted_at")
                if baseline_alerted_at != expected_baseline:
                    raise RetirementError("baseline alerted_at precondition failed")

                current_legacy = _legacy_projection(current_raw)
                if _canonical_json_bytes(current_legacy) == _canonical_json_bytes(
                    checkpoint_legacy
                ):
                    return {
                        "apply": bool(apply),
                        "status": "already_compensated",
                        "permalink": permalink,
                        "before_alerted_at": current_alerted_at,
                        "after_alerted_at": baseline_alerted_at,
                    }
                if current_alerted_at != expected_current:
                    raise RetirementError("current alerted_at precondition failed")

                expected = copy.deepcopy(current_raw)
                expected["posts"][permalink]["alerted_at"] = expected_baseline
                if _canonical_json_bytes(
                    _legacy_projection(expected)
                ) != _canonical_json_bytes(checkpoint_legacy):
                    raise RetirementError(
                        "legacy drift is not limited to the selected alerted_at"
                    )
                planned = {
                    "apply": False,
                    "status": "planned",
                    "permalink": permalink,
                    "before_alerted_at": current_alerted_at,
                    "after_alerted_at": expected_baseline,
                }
                if not apply:
                    return planned

                epoch = _epoch(now)
                evidence_dir = _compensation_dir(manifest, epoch)
                before_fingerprint = _snapshot(
                    state_path, evidence_dir / "reaction-ledger-before.json"
                )
                payload = json.dumps(expected, indent=2, sort_keys=True).encode()
                state_mode = stat.S_IMODE(state_path.stat().st_mode)
                try:
                    _atomic_write_bytes(state_path, payload, state_mode)
                    reloaded = json.loads(state_path.read_bytes())
                    if _canonical_json_bytes(reloaded) != _canonical_json_bytes(
                        expected
                    ):
                        raise RetirementError(
                            "compensated ledger differs from the exact expected state"
                        )
                    if _canonical_json_bytes(
                        _legacy_projection(reloaded)
                    ) != _canonical_json_bytes(checkpoint_legacy):
                        raise RetirementError(
                            "compensated legacy projection does not match checkpoint"
                        )
                    if _canonical_json_bytes(
                        _boris_projection(reloaded)
                    ) != _canonical_json_bytes(_boris_projection(current_raw)):
                        raise RetirementError(
                            "Boris projection changed during legacy compensation"
                        )
                    after_fingerprint = _snapshot(
                        state_path, evidence_dir / "reaction-ledger-after.json"
                    )
                    evidence = {
                        "created_epoch": epoch,
                        "created_iso": _iso(epoch),
                        "permalink": permalink,
                        "before_alerted_at": current_alerted_at,
                        "after_alerted_at": expected_baseline,
                        "checkpoint": checkpoint_fingerprint.to_dict(),
                        "files": {
                            "before": before_fingerprint.to_dict(),
                            "after": after_fingerprint.to_dict(),
                        },
                    }
                    evidence_path = evidence_dir / "evidence.json"
                    _atomic_write_bytes(
                        evidence_path,
                        (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode(),
                        0o600,
                    )
                except BaseException:
                    try:
                        _atomic_write_bytes(
                            state_path,
                            before_fingerprint.archive_path.read_bytes(),
                            before_fingerprint.mode,
                        )
                        _require_match(before_fingerprint)
                    except BaseException as restore_exc:
                        raise RetirementError(
                            "legacy compensation failed and original ledger restoration failed"
                        ) from restore_exc
                    raise

                return {
                    "apply": True,
                    "status": "compensated",
                    "permalink": permalink,
                    "before_alerted_at": current_alerted_at,
                    "after_alerted_at": expected_baseline,
                    "evidence_path": str(evidence_path),
                }


def verify_retired(paths: Paths, manifest: Manifest, now: Any) -> list[str]:
    """Verify the complete retired state without mutating any live artifact."""
    del now  # verification is identity/state based once a cutoff exists
    errors: list[str] = []
    try:
        _assert_manifest_paths(paths, manifest)
    except RetirementError as exc:
        return [str(exc)]
    with cron_jobs.use_cron_store(paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
    jobs_by_id = {job.get("id"): job for job in jobs}
    retained_records = [job for job in jobs if job.get("id") == RETAIN_ID]
    for job_id in RETIRE:
        if job_id in jobs_by_id:
            errors.append(f"retired job remains registered: {job_id}")
    for key in ENTRYPOINTS:
        fingerprint = manifest.files[key]
        if fingerprint.active_path.exists():
            errors.append(f"retired entrypoint remains active: {fingerprint.active_path}")
        if not _matches(fingerprint, fingerprint.archive_path):
            errors.append(f"retired archive fingerprint mismatch: {fingerprint.archive_path}")
    if manifest.cutoff_epoch is None:
        errors.append("pause cutoff has not been recorded")
    else:
        try:
            with _connect_readonly(paths.executions_path) as connection:
                count = connection.execute(
                    """SELECT COUNT(*) FROM executions
                       WHERE job_id IN (?, ?) AND unixepoch(claimed_at) > ?""",
                    (*sorted(RETIRE), manifest.cutoff_epoch),
                ).fetchone()[0]
                invalid_timestamps = connection.execute(
                    """SELECT job_id, claimed_at FROM executions
                       WHERE job_id IN (?, ?) AND unixepoch(claimed_at) IS NULL""",
                    tuple(sorted(RETIRE)),
                ).fetchall()
            if count:
                errors.append(f"retired jobs have {count} post-cutoff executions")
            for row in invalid_timestamps:
                errors.append(
                    f"execution {row['job_id']} has an invalid claimed_at timestamp: "
                    f"{row['claimed_at']}"
                )
        except (sqlite3.Error, RetirementError) as exc:
            errors.append(f"execution verification failed: {exc}")
    try:
        errors.extend(_task_census_drift(manifest.task_census, _task_census(paths.board_path)))
    except (sqlite3.Error, RetirementError) as exc:
        errors.append(f"board verification failed: {exc}")
    for key in (
        "retired-output-ee4582d0598f",
        "retired-output-ff865c33c5ea",
        "requeue-ledger",
        *(f"channel-watermark-{channel}" for channel in LEGACY_CHANNELS),
    ):
        fingerprint = manifest.quiescent_files.get(key)
        if fingerprint is None or not _matches(fingerprint):
            errors.append(f"quiescent audit fingerprint mismatch: {key}")
    reaction_checkpoints = manifest.component_checkpoints.get(
        "retained-reaction", []
    )
    boris_checkpoints = manifest.component_checkpoints.get("boris-intake", [])
    expected_retained_name = (
        RETAIN_POST_MIGRATION_NAME
        if reaction_checkpoints
        else RETAIN_PRE_MIGRATION_NAME
    )
    expected_retained_prompt = (
        RETAIN_BORIS_ONLY_PROMPT
        if reaction_checkpoints or boris_checkpoints
        else None
    )
    if not retained_records:
        errors.append("retained job is missing")
    elif len(retained_records) != 1:
        errors.append(
            "expected exactly one retained job record, "
            f"found {len(retained_records)}"
        )
    else:
        errors.extend(
            _validate_retained_job(
                retained_records[0],
                paths,
                expected_retained_name,
                expected_retained_prompt,
            )
        )
    try:
        latest = _latest_execution(paths.executions_path, RETAIN_ID)
        if latest is None or latest.get("status") != "completed":
            errors.append("retained job latest execution is not successful")
    except (sqlite3.Error, RetirementError) as exc:
        errors.append(f"retained execution verification failed: {exc}")
    try:
        baseline_projection = _legacy_reaction_baseline(manifest)
        reaction_raw = json.loads(
            (paths.state_dir / "just_do_it_review_reactions.json").read_bytes()
        )
        current_projection = _legacy_projection(reaction_raw)
        if _canonical_json_bytes(current_projection) != _canonical_json_bytes(
            baseline_projection
        ):
            errors.append("legacy reaction projection changed")
    except (OSError, RetirementError, ValueError, TypeError) as exc:
        errors.append(f"reaction ledger verification failed: {exc}")

    if boris_checkpoints:
        checkpoint = boris_checkpoints[-1]
        fingerprints = _checkpoint_files(checkpoint)
        if not _boris_source_matches_versioned(paths):
            errors.append("Boris source does not match the versioned source tree")
        if not _matches(fingerprints["config"]):
            errors.append("Boris config fingerprint changed")
        if not _matches(fingerprints["boris-launchd-plist"]):
            errors.append("Boris launchd plist fingerprint changed")
        if not _file_prefix_matches(
            fingerprints["boris-audit"].archive_path,
            fingerprints["boris-audit"].active_path,
        ):
            errors.append("Boris audit continuity failed")
        try:
            current_db = _sqlite_logical_state(paths.state_dir / "boris_intake.db")
            baseline = checkpoint["logical_db"]
            if current_db["integrity"] != "ok" or current_db["tables"] != baseline["tables"]:
                errors.append("Boris store schema/integrity continuity failed")
            for table, count in baseline["counts"].items():
                if table in BORIS_ROLLING_COUNT_TABLES:
                    continue
                if current_db["counts"].get(table, -1) < count:
                    errors.append(f"Boris store row count regressed for {table}")
            if not _sqlite_baseline_is_subset(
                fingerprints["boris-db"].archive_path,
                fingerprints["boris-db"].active_path,
            ):
                errors.append("Boris store dedup/lane continuity failed")
        except (sqlite3.Error, RetirementError, KeyError) as exc:
            errors.append(f"Boris store verification failed: {exc}")
        try:
            if _watermark_value(fingerprints["boris-watermark"].active_path) < _watermark_value(
                fingerprints["boris-watermark"].archive_path
            ):
                errors.append("Boris watermark regressed")
        except RetirementError as exc:
            errors.append(str(exc))
        heartbeat = fingerprints["boris-heartbeat"]
        try:
            if heartbeat.active_path.stat().st_mtime <= float(checkpoint["heartbeat_mtime"]):
                errors.append("Boris heartbeat did not advance")
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"Boris heartbeat verification failed: {exc}")
        try:
            service = _load_boris_service_status(paths)
            pid = service.get("pid")
            if (
                service.get("label") != BORIS_LAUNCHD_LABEL
                or service.get("state") != "running"
                or not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
            ):
                errors.append("exact Boris launchd service is not running")
        except (OSError, ValueError, json.JSONDecodeError, RetirementError) as exc:
            errors.append(f"Boris service verification failed: {exc}")
    return errors


def _new_checkpoint_dir(manifest: Manifest, component: str, epoch: int) -> Path:
    base = manifest.archive_dir / "checkpoints" / f"{component}-{epoch}"
    candidate = base
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = Path(f"{base}-{counter}")
    candidate.mkdir(parents=True, mode=0o700)
    return candidate


def _load_job_records(paths: Paths) -> list[dict[str, Any]]:
    with cron_jobs.use_cron_store(paths.hermes_home):
        with cron_jobs._jobs_lock():
            return copy.deepcopy(cron_jobs.load_jobs())


def _validate_checkpoint(paths: Paths, manifest: Manifest, component: str) -> dict[str, Any]:
    if component not in {"retained-reaction", "boris-intake"}:
        raise RetirementError(
            "component must be retained-reaction or boris-intake"
        )
    jobs = _load_job_records(paths)
    retained = _validate_finalized_retired_boundary(
        paths,
        manifest,
        jobs,
        operation="checkpoint",
    )
    if component == "retained-reaction":
        if retained.get("enabled") is not False or retained.get("state") != "paused":
            raise RetirementError("retained job must be paused before reaction checkpoint")
    else:
        retained_errors = _validate_retained_job(
            retained,
            paths,
            RETAIN_POST_MIGRATION_NAME,
            RETAIN_BORIS_ONLY_PROMPT,
        )
        latest = _latest_execution(paths.executions_path, RETAIN_ID)
        if retained_errors or latest is None or latest.get("status") != "completed":
            raise RetirementError(
                "retained watchdog must be enabled and healthy before Boris checkpoint"
            )
    return retained


def checkpoint_component(
    paths: Paths,
    manifest: Manifest,
    component: str,
    now: Any,
) -> Manifest:
    """Append a fresh, complete checkpoint for one rollout component."""
    retained = _validate_checkpoint(paths, manifest, component)
    checkpoint_epoch = _epoch(now)
    checkpoint_dir = _new_checkpoint_dir(manifest, component, checkpoint_epoch)
    fingerprints: dict[str, FileFingerprint] = {}
    if component == "retained-reaction":
        sources = {
            "reaction-live-script": paths.live_scripts / RETAIN_SCRIPT,
            "reaction-live-test": paths.live_scripts
            / "test_just_do_it_review_reaction_watchdog.py",
            "reaction-ledger": paths.state_dir / "just_do_it_review_reactions.json",
        }
        for key, source in sources.items():
            fingerprints[key] = _snapshot(source, checkpoint_dir / key)
        record: dict[str, Any] = {
            "created_epoch": checkpoint_epoch,
            "created_iso": _iso(checkpoint_epoch),
            "job": copy.deepcopy(retained),
            "files": {key: value.to_dict() for key, value in fingerprints.items()},
        }
        reaction_raw = json.loads(
            (paths.state_dir / "just_do_it_review_reactions.json").read_text(
                encoding="utf-8"
            )
        )
        refreshed_legacy_hash = _json_hash(_legacy_projection(reaction_raw))
        record["legacy_reaction_projection_sha256"] = refreshed_legacy_hash
        manifest.legacy_reaction_projection_sha256 = refreshed_legacy_hash
    else:
        sources = {
            "boris-live-source": paths.live_scripts / "boris_intake",
            "boris-versioned-source": paths.source_root
            / "cron"
            / "scripts"
            / "boris_intake",
            "boris-versioned-tests": paths.source_root / "tests" / "cron" / "boris_intake",
            "boris-audit": paths.state_dir / "boris_intake_audit.jsonl",
            "boris-watermark": paths.state_dir
            / f"boris_intake_watermark_{BORIS_CHANNEL}.txt",
            "boris-heartbeat": paths.state_dir / "boris_intake_heartbeat",
            "config": paths.hermes_home / "config.yaml",
            "boris-launchd-plist": paths.boris_launchd_plist,
        }
        for key, source in sources.items():
            fingerprints[key] = _snapshot(source, checkpoint_dir / key)
        fingerprints["boris-db"] = _sqlite_snapshot(
            paths.state_dir / "boris_intake.db", checkpoint_dir / "boris-db.sqlite"
        )
        record = {
            "created_epoch": checkpoint_epoch,
            "created_iso": _iso(checkpoint_epoch),
            "files": {key: value.to_dict() for key, value in fingerprints.items()},
            "logical_db": _sqlite_logical_state(paths.state_dir / "boris_intake.db"),
            "audit_lines": len(
                (paths.state_dir / "boris_intake_audit.jsonl").read_bytes().splitlines()
            ),
            "heartbeat_mtime": (paths.state_dir / "boris_intake_heartbeat").stat().st_mtime,
        }
    manifest.component_checkpoints.setdefault(component, []).append(record)
    manifest.save()
    return manifest


def _atomic_restore_file(snapshot: Path, active: Path) -> None:
    active.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=active.parent, prefix=f".{active.name}.")
    os.close(fd)
    temp = Path(temp_name)
    try:
        shutil.copy2(snapshot, temp)
        os.replace(temp, active)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def _atomic_restore_tree(snapshot: Path, active: Path) -> None:
    active.parent.mkdir(parents=True, exist_ok=True)
    staged = active.parent / f".{active.name}.restore-{uuid.uuid4().hex}"
    old = active.parent / f".{active.name}.old-{uuid.uuid4().hex}"
    shutil.copytree(snapshot, staged, copy_function=shutil.copy2)
    moved_old = False
    try:
        if active.exists():
            os.replace(active, old)
            moved_old = True
        os.replace(staged, active)
    except BaseException:
        if moved_old and old.exists() and not active.exists():
            os.replace(old, active)
        raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)
        if old.exists():
            shutil.rmtree(old)


def _restore_fingerprint(fingerprint: FileFingerprint) -> None:
    if _matches(fingerprint):
        return
    if fingerprint.kind == "tree":
        _atomic_restore_tree(fingerprint.archive_path, fingerprint.active_path)
    elif fingerprint.kind == "sqlite":
        source_conn = sqlite3.connect(str(fingerprint.archive_path), timeout=10)
        destination_conn = sqlite3.connect(str(fingerprint.active_path), timeout=10)
        try:
            source_conn.backup(destination_conn)
            destination_conn.commit()
        finally:
            destination_conn.close()
            source_conn.close()
        os.chmod(fingerprint.active_path, fingerprint.mode)
    else:
        _atomic_restore_file(fingerprint.archive_path, fingerprint.active_path)
    _require_match(fingerprint)


def _component_primary_changed(
    component: str, checkpoint: Mapping[str, Any]
) -> bool:
    fingerprints = _checkpoint_files(checkpoint)
    if component == "retained-reaction":
        return any(
            not _matches(fingerprints[key])
            for key in (
                "reaction-live-script",
                "reaction-live-test",
                "reaction-ledger",
            )
        )
    if component == "boris-intake":
        install_surfaces = (
            "boris-live-source",
            "boris-versioned-source",
            "boris-versioned-tests",
            "config",
            "boris-launchd-plist",
        )
        return any(not _matches(fingerprints[key]) for key in install_surfaces)
    raise RetirementError(f"unknown checkpoint component: {component}")


def _validate_rollback_plan(
    paths: Paths, manifest: Manifest
) -> set[str]:
    _assert_manifest_paths(paths, manifest)
    _load_registry_snapshot(manifest)
    for key in ENTRYPOINTS:
        fingerprint = manifest.files[key]
        if not _matches(fingerprint, fingerprint.archive_path):
            raise RetirementError(
                "refusing job restore because source archive hash differs: "
                f"{fingerprint.archive_path}"
            )

    planned = set(manifest.changed_components)
    for component in ("retained-reaction", "boris-intake"):
        checkpoints = manifest.component_checkpoints.get(component, [])
        if checkpoints and _component_primary_changed(component, checkpoints[-1]):
            planned.add(component)
    for component in planned:
        checkpoints = manifest.component_checkpoints.get(component, [])
        if not checkpoints:
            raise RetirementError(
                f"changed component has no completed checkpoint: {component}"
            )
        fingerprints = _checkpoint_files(checkpoints[-1])
        for fingerprint in fingerprints.values():
            _require_match(fingerprint, fingerprint.archive_path)
        if component == "boris-intake" and not _file_prefix_matches(
            fingerprints["boris-audit"].archive_path,
            fingerprints["boris-audit"].active_path,
        ):
            raise RetirementError("Boris audit continuity failed before rollback")
    return planned


def rollback(
    paths: Paths,
    manifest: Manifest,
    now: Any,
    event: Optional[Callable[[str], None]] = None,
) -> Manifest:
    """Restore compatible sources/state before restoring any job metadata."""
    _assert_manifest_paths(paths, manifest)
    if manifest.phase == "restored":
        errors = _verify_restored(paths, manifest)
        if errors:
            raise RetirementError(
                "completed rollback no longer verifies: " + "; ".join(errors)
            )
        return manifest
    planned_components = _validate_rollback_plan(paths, manifest)
    emit = event or (lambda _name: None)

    for key in ENTRYPOINTS:
        fingerprint = manifest.files[key]
        if not fingerprint.archive_path.exists():
            raise RetirementError(f"retired source backup is missing: {fingerprint.archive_path}")
        if not _matches(fingerprint, fingerprint.archive_path):
            raise RetirementError(
                f"refusing job restore because source archive hash differs: {fingerprint.archive_path}"
            )
        if not _matches(fingerprint):
            _atomic_restore_file(fingerprint.archive_path, fingerprint.active_path)
        _require_match(fingerprint)
    emit("sources_restored")

    marker_changed = False
    for component in sorted(planned_components):
        if component not in manifest.changed_components:
            manifest.changed_components.append(component)
            marker_changed = True
    if marker_changed:
        # Persist the derived rollout scope before the first component restore.
        manifest.save()

    for component in ("retained-reaction", "boris-intake"):
        checkpoints = manifest.component_checkpoints.get(component, [])
        if not checkpoints or component not in manifest.changed_components:
            continue
        fingerprints = _checkpoint_files(checkpoints[-1])
        if component == "boris-intake" and not _file_prefix_matches(
            fingerprints["boris-audit"].archive_path,
            fingerprints["boris-audit"].active_path,
        ):
            raise RetirementError("Boris audit continuity failed before rollback")
        for key, fingerprint in fingerprints.items():
            if component == "boris-intake" and key == "boris-audit":
                continue
            _restore_fingerprint(fingerprint)

    # Exact source verification is the final gate before either retired record
    # can re-enter the registry.
    for key in ENTRYPOINTS:
        _require_match(manifest.files[key])
    _restore_registry_records(paths, manifest)
    emit("jobs_restored")

    restored = _epoch(now)
    manifest.restored_epoch = restored
    manifest.restored_iso = _iso(restored)
    manifest.phase = "restored"
    manifest.save()
    return manifest


def rehearse_reaction_roundtrip(
    state_path: Path | str,
    watchdog_path: Path | str,
) -> dict[str, Any]:
    """Exercise modern/reverse Boris rendering entirely in memory."""
    state_path = _resolved(state_path)
    watchdog_path = _resolved(watchdog_path)
    supplied = json.loads(state_path.read_text(encoding="utf-8"))
    working = copy.deepcopy(supplied)
    legacy_before = copy.deepcopy(_legacy_projection(working))

    module_name = f"_retirement_watchdog_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, watchdog_path)
    if spec is None or spec.loader is None:
        raise RetirementError(f"unable to import watchdog: {watchdog_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)

    posts = working.get("posts") if isinstance(working, dict) else None
    if not isinstance(posts, dict):
        raise RetirementError("reaction ledger has no posts mapping")
    applied: dict[str, set[str]] = {}
    for permalink, record in posts.items():
        if not isinstance(record, dict):
            continue
        current = record.get("emoji")
        applied[permalink] = {str(current)} if current else set()

    def run_pass(style: str) -> int:
        calls = 0

        def reactor(permalink: str, emoji: str, remove: bool) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            reactions = applied.setdefault(permalink, set())
            if remove:
                existed = emoji in reactions
                reactions.discard(emoji)
                return {"ok": True, "no_reaction": not existed}
            already = emoji in reactions
            reactions.add(emoji)
            return {"ok": True, "already_reacted": already}

        for permalink, record in sorted(posts.items()):
            if f"/{BORIS_CHANNEL}/" not in permalink or not isinstance(record, dict):
                continue
            semantic = module.normalize_review_state(record.get("state"))
            if semantic is None:
                values = list((record.get("lanes") or {}).values())
                semantic = module.aggregate_state(values)
            if semantic is None:
                semantic = module.normalize_review_state(record.get("emoji"))
            if semantic is None or record.get("unresolved_lanes"):
                continue
            desired = module.render_reaction(permalink, semantic, style)
            current = record.get("emoji")
            pending = list(record.get("pending_removals") or [])
            if current == desired and not pending:
                continue
            result = module.apply_transition(
                permalink,
                desired,
                record,
                reactor,
                module.owned_reactions(permalink),
            )
            if not result.get("ok"):
                raise RetirementError(
                    f"in-memory reaction transition failed: {result.get('error')}"
                )
            record["state"] = semantic
            record["emoji"] = desired
            record["pending_removals"] = result.get("pending_removals") or []
        return calls

    report = {
        "modern_first_pass_calls": run_pass("modern"),
        "modern_second_pass_calls": run_pass("modern"),
        "legacy_first_pass_calls": run_pass("legacy"),
        "legacy_second_pass_calls": run_pass("legacy"),
    }
    if (
        report["modern_first_pass_calls"] <= 0
        or report["legacy_first_pass_calls"] <= 0
    ):
        raise RetirementError(
            "reaction rehearsal must exercise a non-vacuous modern and legacy pass: "
            f"{report}"
        )
    report["legacy_records_unchanged"] = _legacy_projection(working) == legacy_before
    report["supplied_ledger_unchanged"] = (
        json.loads(state_path.read_text(encoding="utf-8")) == supplied
    )
    return report


def _verify_restored(paths: Paths, manifest: Manifest) -> list[str]:
    errors: list[str] = []
    jobs = _load_job_records(paths)
    for job_id, expected in manifest.jobs.items():
        matches = [job for job in jobs if str(job.get("id")) == job_id]
        if matches != [expected]:
            errors.append(f"job record not restored exactly: {job_id}")
    for key in ENTRYPOINTS:
        if not _matches(manifest.files[key]):
            errors.append(f"source not restored exactly: {key}")
    return errors


def _validate_pause_plan(paths: Paths, manifest: Manifest, now: Any) -> None:
    _assert_manifest_paths(paths, manifest)
    if manifest.phase == "paused":
        _validate_paused_registry(paths, manifest, now)
        return
    if manifest.phase != "preflight":
        raise RetirementError(f"cannot pause from manifest phase {manifest.phase}")
    selected = _job_map_locked(paths)
    _validate_preflight_job_selection(paths, manifest, selected)


def _paths_from_args(args: argparse.Namespace) -> Paths:
    home = _resolved(args.hermes_home or get_hermes_home())
    source = _resolved(args.source_root or Path.cwd())
    archive = _resolved(args.archive_root or home / "forensics")
    paths = Paths(
        hermes_home=home,
        source_root=source,
        archive_root=archive,
        board_path=Path(args.board_path) if args.board_path else None,
        boris_launchd_plist=(
            Path(args.boris_launchd_plist) if args.boris_launchd_plist else None
        ),
        process_listing_path=(
            Path(args.process_listing_path) if args.process_listing_path else None
        ),
        boris_service_status_path=(
            Path(args.boris_service_status_path)
            if args.boris_service_status_path
            else None
        ),
    )
    _reject_live_test_injection(paths, args.now_epoch)
    return paths


def _load_cli_manifest(args: argparse.Namespace) -> tuple[Paths, Manifest]:
    manifest_arg = Path(args.manifest)
    if not manifest_arg.is_absolute():
        raise RetirementError("--manifest must be an absolute path")
    manifest = Manifest.load(manifest_arg)
    paths = manifest.paths
    overrides = {
        "hermes_home": args.hermes_home,
        "source_root": args.source_root,
        "board_path": args.board_path,
        "boris_launchd_plist": args.boris_launchd_plist,
        "process_listing_path": args.process_listing_path,
        "boris_service_status_path": args.boris_service_status_path,
    }
    for name, raw in overrides.items():
        if raw is not None and _resolved(raw) != getattr(paths, name):
            raise RetirementError(f"--{name.replace('_', '-')} differs from manifest")
    _reject_live_test_injection(paths, args.now_epoch)
    return paths, manifest


def _reject_live_test_injection(paths: Paths, now_epoch: Optional[int]) -> None:
    if not _is_live_home(paths.hermes_home):
        return
    flags = []
    if now_epoch is not None:
        flags.append("--now-epoch")
    if paths.process_listing_path is not None:
        flags.append("--process-listing-path")
    if paths.boris_service_status_path is not None:
        flags.append("--boris-service-status-path")
    if flags:
        raise RetirementError(
            f"{', '.join(flags)} is forbidden for a live Hermes home"
        )


def _now_from_args(args: argparse.Namespace, paths: Paths) -> int:
    if args.now_epoch is not None:
        _reject_live_test_injection(paths, args.now_epoch)
        return int(args.now_epoch)
    return int(time.time())


def _add_path_options(parser: argparse.ArgumentParser, *, archive: bool = False) -> None:
    parser.add_argument("--hermes-home")
    parser.add_argument("--source-root")
    if archive:
        parser.add_argument("--archive-root")
    parser.add_argument("--board-path")
    parser.add_argument("--boris-launchd-plist")
    parser.add_argument("--process-listing-path", help=argparse.SUPPRESS)
    parser.add_argument("--boris-service-status-path", help=argparse.SUPPRESS)


def _add_manifest_options(parser: argparse.ArgumentParser, *, apply: bool = False) -> None:
    parser.add_argument("--manifest", required=True)
    _add_path_options(parser)
    parser.add_argument("--now-epoch", type=int)
    if apply:
        parser.add_argument("--apply", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight_parser = subparsers.add_parser("preflight")
    _add_path_options(preflight_parser, archive=True)
    preflight_parser.add_argument("--recovery-ref", required=True)
    preflight_parser.add_argument("--now-epoch", type=int)
    for name, requires_apply in (
        ("pause", True),
        ("check-quiescence", False),
        ("finalize", True),
        ("rollback", True),
    ):
        command = subparsers.add_parser(name)
        _add_manifest_options(command, apply=requires_apply)
    checkpoint_parser = subparsers.add_parser("checkpoint")
    _add_manifest_options(checkpoint_parser, apply=True)
    checkpoint_parser.add_argument(
        "--component", required=True, choices=("retained-reaction", "boris-intake")
    )
    compensation_parser = subparsers.add_parser(
        "compensate-legacy-alerted-at"
    )
    _add_manifest_options(compensation_parser, apply=True)
    compensation_parser.add_argument("--permalink", required=True)
    compensation_parser.add_argument(
        "--expected-current-alerted-at", required=True, type=int
    )
    compensation_parser.add_argument(
        "--expected-baseline-alerted-at", required=True, type=int
    )
    retained_prompt_parser = subparsers.add_parser("configure-retained-prompt")
    _add_manifest_options(retained_prompt_parser, apply=True)
    verify_parser = subparsers.add_parser("verify")
    _add_manifest_options(verify_parser)
    verify_parser.add_argument("--expect", required=True, choices=("retired", "restored"))
    rehearsal = subparsers.add_parser("rehearse-reactions")
    rehearsal.add_argument("--state-path", required=True)
    rehearsal.add_argument("--watchdog-path", required=True)
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, sort_keys=True))


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "rehearse-reactions":
            _print(rehearse_reaction_roundtrip(args.state_path, args.watchdog_path))
            return 0
        if args.command == "preflight":
            paths = _paths_from_args(args)
            now = _now_from_args(args, paths)
            manifest = preflight(paths, now, args.recovery_ref)
            _print({"ok": True, "manifest_path": str(manifest.manifest_path)})
            return 0
        paths, manifest = _load_cli_manifest(args)
        now = _now_from_args(args, paths)
        if args.command == "pause":
            if not args.apply:
                _validate_pause_plan(paths, manifest, now)
                _print({"apply": False, "action": "pause", "job_ids": sorted(RETIRE)})
                return 0
            pause_producers(paths, manifest, now)
        elif args.command == "check-quiescence":
            errors = verify_quiescent(paths, manifest, now)
            if errors:
                raise RetirementError("; ".join(errors))
        elif args.command == "checkpoint":
            if not args.apply:
                _validate_checkpoint(paths, manifest, args.component)
                _print({"apply": False, "action": "checkpoint", "component": args.component})
                return 0
            checkpoint_component(paths, manifest, args.component, now)
        elif args.command == "compensate-legacy-alerted-at":
            report = compensate_legacy_alerted_at(
                paths,
                manifest,
                args.permalink,
                args.expected_current_alerted_at,
                args.expected_baseline_alerted_at,
                now,
                apply=args.apply,
            )
            _print(report)
            return 0
        elif args.command == "configure-retained-prompt":
            report = configure_retained_boris_only_prompt(
                paths,
                manifest,
                apply=args.apply,
            )
            _print(report)
            return 0
        elif args.command == "finalize":
            if not args.apply:
                _validate_finalize_plan(paths, manifest, now)
                _print(
                    {
                        "apply": False,
                        "action": "finalize",
                        "job_ids": sorted(RETIRE),
                        "entrypoints": [str(manifest.files[key].active_path) for key in ENTRYPOINTS],
                    }
                )
                return 0
            finalize(paths, manifest, now)
        elif args.command == "verify":
            errors = (
                verify_retired(paths, manifest, now)
                if args.expect == "retired"
                else _verify_restored(paths, manifest)
            )
            if errors:
                raise RetirementError("; ".join(errors))
        elif args.command == "rollback":
            if not args.apply:
                _validate_rollback_plan(paths, manifest)
                _print(
                    {
                        "apply": False,
                        "action": "rollback",
                        "job_ids": [*sorted(RETIRE), RETAIN_ID],
                    }
                )
                return 0
            rollback(paths, manifest, now)
        _print({"ok": True, "command": args.command, "manifest_path": str(manifest.manifest_path)})
        return 0
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error, RetirementError) as exc:
        _print({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
