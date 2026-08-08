#!/usr/bin/env python3
"""Guarded, exact-ID retirement for the two deprecated just-do-it producers.

The module is intentionally a one-shot operator control plane rather than a
runtime scheduler feature.  Every mutation is scoped to explicit paths and
exact job IDs, and every recoverable input is fingerprinted before use.
"""

from __future__ import annotations

import argparse
import copy
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from cron import jobs as cron_jobs


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
DEPRECATED_CREATOR = "just-do-it-blocked-review-requeue-watchdog"
DEPRECATED_IDEMPOTENCY_PREFIX = "just-do-it-rereview:"

BORIS_CHANNEL = "C0BPCKPEFCG"
LEGACY_CHANNELS = ("C08K8FQKX9S", "C07BU8AAPB3", "C09339EGZ9U")
LIVE_HOME = Path("/Users/ctao/.hermes")
ENTRYPOINTS = {
    "retired-channel-script": "slack_channel_watch.py",
    "retired-channel-test": "test_slack_channel_watch.py",
    "retired-requeue-script": "just_do_it_blocked_review_requeue_watchdog.py",
    "retired-requeue-test": "test_just_do_it_blocked_review_requeue_watchdog.py",
}


class RetirementError(RuntimeError):
    """A fail-closed retirement guard rejected the requested operation."""


def _resolved(value: Path | str) -> Path:
    return Path(value).expanduser().resolve()


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

    def __post_init__(self) -> None:
        home = _resolved(self.hermes_home)
        source = _resolved(self.source_root)
        archive = _resolved(self.archive_root)
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
            "boris_launchd_plist": Path.home()
            / "Library"
            / "LaunchAgents"
            / "ai.hermes.boris-intake.plist",
        }
        for name, default in defaults.items():
            raw = getattr(self, name)
            object.__setattr__(self, name, _resolved(raw if raw is not None else default))
        if self.process_listing_path is not None:
            object.__setattr__(
                self, "process_listing_path", _resolved(self.process_listing_path)
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            name: str(value) if isinstance(value, Path) else value
            for name, value in asdict(self).items()
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Paths":
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
    def from_dict(cls, raw: Mapping[str, Any]) -> "FileFingerprint":
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
    def load(cls, path: Path | str) -> "Manifest":
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
        current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(current.timestamp())
    return int(value)


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


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


def _job_map_locked(paths: Paths) -> dict[str, dict[str, Any]]:
    _assert_store_paths(paths)
    with cron_jobs.use_cron_store(paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            selected: dict[str, dict[str, Any]] = {}
            for wanted in (*sorted(RETIRE), RETAIN_ID):
                matches = [job for job in jobs if job.get("id") == wanted]
                if len(matches) != 1:
                    raise RetirementError(
                        f"job fingerprint mismatch: expected exactly one record for {wanted}"
                    )
                selected[wanted] = copy.deepcopy(matches[0])
            return selected


def _validate_job_fingerprints(paths: Paths, selected: Mapping[str, dict[str, Any]]) -> None:
    for job_id, expected in RETIRE.items():
        job = selected[job_id]
        wanted_script = paths.live_scripts / expected["script"]
        conditions = {
            "name": job.get("name") == expected["name"],
            "schedule": job.get("schedule") == expected["schedule"],
            "script": bool(job.get("script"))
            and _resolved(str(job["script"])) == wanted_script,
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
        "script": bool(retained.get("script"))
        and _resolved(str(retained["script"])) == paths.live_scripts / RETAIN_SCRIPT,
        "schedule": retained.get("schedule") == {"kind": "cron", "expr": "* * * * *"},
        "no_agent": retained.get("no_agent") is True,
        "enabled": retained.get("enabled") is True,
        "state": retained.get("state") == "scheduled",
    }
    failed = [name for name, valid in retained_conditions.items() if not valid]
    if failed:
        raise RetirementError(
            f"job {RETAIN_ID} fingerprint mismatch: {', '.join(failed)}"
        )


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
    legacy_reacted = {
        key: value
        for key, value in reacted.items()
        if f"/{BORIS_CHANNEL}/" not in json.dumps(value, sort_keys=True)
    }
    return {"posts": legacy_posts, "reacted": legacy_reacted}


def _json_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _repository_identity(source_root: Path) -> tuple[str, dict[str, str]]:
    durable = (
        "cron/scripts/just_do_it_blocked_review_requeue_watchdog.py",
        "tests/cron/test_just_do_it_blocked_review_requeue_watchdog.py",
        "cron/scripts/just_do_it_review_reaction_watchdog.py",
        "tests/cron/test_just_do_it_review_reaction_watchdog.py",
        "cron/scripts/boris_intake",
        "tests/cron/boris_intake",
    )

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(source_root), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RetirementError(
                f"unable to resolve committed recovery object for source root: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    commit = git("rev-parse", "HEAD")
    if git("status", "--porcelain", "--", *durable):
        raise RetirementError("durable recovery source has uncommitted changes")
    hashes = {relative: git("rev-parse", f"HEAD:{relative}") for relative in durable}
    return commit, hashes


def _unique_archive_dir(paths: Paths, epoch: int) -> Path:
    stamp = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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


def preflight(paths: Paths, now: Any) -> Manifest:
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
        repository_commit, repository_trees = _repository_identity(paths.source_root)
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
    if manifest.phase not in {"preflight", "paused"}:
        raise RetirementError(f"cannot pause from manifest phase {manifest.phase}")
    with cron_jobs.use_cron_store(paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs_before = {job["id"]: job for job in cron_jobs.load_jobs()}
            for job_id in RETIRE:
                if jobs_before.get(job_id) != manifest.jobs[job_id] and manifest.phase == "preflight":
                    raise RetirementError(f"job {job_id} drifted after preflight")
            for job_id in sorted(RETIRE):
                updated = cron_jobs.pause_job(
                    job_id, reason="guarded just-do-it retirement"
                )
                if updated is None:
                    raise RetirementError(f"exact retiring job disappeared: {job_id}")
            current = {job["id"]: job for job in cron_jobs.load_jobs()}
            for job_id in RETIRE:
                job = current.get(job_id)
                if not job or job.get("enabled") is not False or job.get("state") != "paused":
                    raise RetirementError(f"failed to verify exact paused state for {job_id}")
            if current.get(RETAIN_ID) != manifest.jobs[RETAIN_ID]:
                raise RetirementError("retained job changed while pausing producers")
    cutoff = _epoch(now)
    manifest.cutoff_epoch = cutoff
    manifest.cutoff_iso = _iso(cutoff)
    manifest.phase = "paused"
    manifest.save()
    return manifest


def _load_process_listing(paths: Paths) -> list[dict[str, Any]]:
    if paths.process_listing_path is not None:
        raw = json.loads(paths.process_listing_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not all(isinstance(row, dict) for row in raw):
            raise RetirementError("injected process listing must be a JSON list of objects")
        return [{**row, "_injected": True} for row in raw]
    result = subprocess.run(
        ["ps", "-axo", "pid=,lstart=,command="],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RetirementError("unable to obtain process listing")
    rows: list[dict[str, Any]] = []
    pattern = re.compile(
        r"^\s*(\d+)\s+([A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d+\s+\d+:\d+:\d+\s+\d+)\s+(.*)$"
    )
    for line in result.stdout.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        try:
            started = int(
                datetime.strptime(match.group(2), "%a %b %d %H:%M:%S %Y")
                .timestamp()
            )
        except ValueError:
            continue
        rows.append(
            {"pid": int(match.group(1)), "started_at": started, "command": match.group(3)}
        )
    return rows


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
    except (sqlite3.Error, RetirementError) as exc:
        errors.append(f"execution ledger query failed: {exc}")
        rows = []

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
                datetime.fromisoformat(str(item["claimed_at"]).replace("Z", "+00:00")).timestamp()
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


def _restore_registry_records(paths: Paths, manifest: Manifest) -> None:
    with cron_jobs.use_cron_store(paths.hermes_home):
        with cron_jobs._jobs_lock():
            current = cron_jobs.load_jobs()
            current_by_id = {str(job.get("id")): job for job in current}
            raw_snapshot = json.loads(
                manifest.files["jobs-registry"].archive_path.read_text(encoding="utf-8")
            )
            original = raw_snapshot.get("jobs", raw_snapshot)
            if not isinstance(original, list):
                raise RetirementError("backed-up jobs registry is invalid")
            rebuilt: list[dict[str, Any]] = []
            seen: set[str] = set()
            protected = set(RETIRE) | {RETAIN_ID}
            for old in original:
                old_id = str(old.get("id"))
                if old_id in protected:
                    rebuilt.append(copy.deepcopy(manifest.jobs[old_id]))
                    seen.add(old_id)
                elif old_id in current_by_id:
                    rebuilt.append(copy.deepcopy(current_by_id[old_id]))
                    seen.add(old_id)
            for current_job in current:
                current_id = str(current_job.get("id"))
                if current_id not in seen:
                    rebuilt.append(copy.deepcopy(current_job))
            cron_jobs.save_jobs(rebuilt)


def _protect_remove_output(paths: Paths, manifest: Manifest, job_id: str) -> None:
    output = paths.cron_output / job_id
    expected = manifest.quiescent_files[f"retired-output-{job_id}"]
    _require_match(expected, output)
    protected = manifest.protected_output_paths[job_id]
    if protected.exists():
        raise RetirementError(f"protected output staging path already exists: {protected}")
    protected.parent.mkdir(parents=True, exist_ok=True)
    os.replace(output, protected)
    removal_error: Optional[BaseException] = None
    try:
        with cron_jobs.use_cron_store(paths.hermes_home):
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


def finalize(paths: Paths, manifest: Manifest, now: Any) -> Manifest:
    """Archive exact entrypoints and remove exact jobs after quiescence proof."""
    _assert_manifest_paths(paths, manifest)
    errors = verify_quiescent(paths, manifest, now)
    if errors:
        raise RetirementError("quiescence check failed: " + "; ".join(errors))
    if manifest.phase not in {"paused", "finalized"}:
        raise RetirementError(f"cannot finalize from manifest phase {manifest.phase}")
    if manifest.phase == "finalized":
        retired_errors = verify_retired(paths, manifest, now)
        if retired_errors:
            raise RetirementError("finalized manifest no longer verifies: " + "; ".join(retired_errors))
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
    manifest.protected_output_paths = {
        job_id: manifest.archive_dir / "protected-output" / job_id
        for job_id in RETIRE
    }
    manifest.save()

    moved: list[tuple[Path, Path, str, FileFingerprint]] = []
    removed: list[str] = []
    try:
        for key in ENTRYPOINTS:
            fingerprint = manifest.files[key]
            _require_match(fingerprint)
            destination = manifest.archive_dir / "retired-entrypoints" / fingerprint.active_path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise RetirementError(f"retired archive path already exists: {destination}")
            os.replace(fingerprint.active_path, destination)
            moved.append((fingerprint.active_path, destination, key, fingerprint))
            moved_fingerprint = replace(fingerprint, archive_path=destination)
            _require_match(moved_fingerprint, destination)
            manifest.files[key] = moved_fingerprint

        for job_id in sorted(RETIRE):
            _protect_remove_output(paths, manifest, job_id)
            removed.append(job_id)
    except BaseException:
        if removed:
            _restore_registry_records(paths, manifest)
        for active, archived, key, original_fingerprint in reversed(moved):
            if archived.exists() and not active.exists():
                os.replace(archived, active)
            manifest.files[key] = original_fingerprint
        manifest.save()
        raise

    completed = _epoch(now)
    manifest.finalized_epoch = completed
    manifest.finalized_iso = _iso(completed)
    manifest.phase = "finalized"
    manifest.save()
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


def _validate_retained_job(job: Optional[dict[str, Any]], paths: Paths) -> list[str]:
    if job is None:
        return ["retained job is missing"]
    checks = {
        "id": job.get("id") == RETAIN_ID,
        "script": bool(job.get("script"))
        and _resolved(str(job["script"])) == paths.live_scripts / RETAIN_SCRIPT,
        "schedule": job.get("schedule") == {"kind": "cron", "expr": "* * * * *"},
        "no_agent": job.get("no_agent") is True,
        "enabled": job.get("enabled") is True,
        "state": job.get("state") == "scheduled",
        "name": job.get("name") == "boris-review-reaction-state-watchdog",
    }
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
            if count:
                errors.append(f"retired jobs have {count} post-cutoff executions")
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
    errors.extend(_validate_retained_job(jobs_by_id.get(RETAIN_ID), paths))
    try:
        latest = _latest_execution(paths.executions_path, RETAIN_ID)
        if latest is None or latest.get("status") != "completed":
            errors.append("retained job latest execution is not successful")
    except (sqlite3.Error, RetirementError) as exc:
        errors.append(f"retained execution verification failed: {exc}")
    try:
        reaction_raw = json.loads(
            (paths.state_dir / "just_do_it_review_reactions.json").read_text(
                encoding="utf-8"
            )
        )
        if _json_hash(_legacy_projection(reaction_raw)) != manifest.legacy_reaction_projection_sha256:
            errors.append("legacy reaction projection changed")
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f"reaction ledger verification failed: {exc}")

    boris_checkpoints = manifest.component_checkpoints.get("boris-intake", [])
    if boris_checkpoints:
        checkpoint = boris_checkpoints[-1]
        fingerprints = _checkpoint_files(checkpoint)
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
                if current_db["counts"].get(table, -1) < count:
                    errors.append(f"Boris store row count regressed for {table}")
        except (sqlite3.Error, RetirementError, KeyError) as exc:
            errors.append(f"Boris store verification failed: {exc}")
        heartbeat = fingerprints["boris-heartbeat"]
        try:
            if heartbeat.active_path.stat().st_mtime <= float(checkpoint["heartbeat_mtime"]):
                errors.append("Boris heartbeat did not advance")
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"Boris heartbeat verification failed: {exc}")
        try:
            processes = _load_process_listing(paths)
            if not any("boris_intake" in str(row.get("command") or "") for row in processes):
                errors.append("Boris launchd process is not running")
        except (OSError, ValueError, json.JSONDecodeError, RetirementError) as exc:
            errors.append(f"Boris process verification failed: {exc}")
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


def _load_jobs(paths: Paths) -> dict[str, dict[str, Any]]:
    with cron_jobs.use_cron_store(paths.hermes_home):
        with cron_jobs._jobs_lock():
            return {job["id"]: copy.deepcopy(job) for job in cron_jobs.load_jobs()}


def _validate_checkpoint(paths: Paths, manifest: Manifest, component: str) -> dict[str, Any]:
    if component not in {"retained-reaction", "boris-intake"}:
        raise RetirementError(
            "component must be retained-reaction or boris-intake"
        )
    _assert_manifest_paths(paths, manifest)
    jobs = _load_jobs(paths)
    retained = jobs.get(RETAIN_ID)
    if retained is None:
        raise RetirementError("retained job is missing")
    if component == "retained-reaction":
        if retained.get("enabled") is not False or retained.get("state") != "paused":
            raise RetirementError("retained job must be paused before reaction checkpoint")
    else:
        retained_errors = _validate_retained_job(retained, paths)
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
    else:
        sources = {
            "boris-live-source": paths.live_scripts / "boris_intake",
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


def rollback(
    paths: Paths,
    manifest: Manifest,
    now: Any,
    event: Optional[Callable[[str], None]] = None,
) -> Manifest:
    """Restore compatible sources/state before restoring any job metadata."""
    _assert_manifest_paths(paths, manifest)
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

    for component in ("retained-reaction", "boris-intake"):
        checkpoints = manifest.component_checkpoints.get(component, [])
        if not checkpoints:
            continue
        fingerprints = _checkpoint_files(checkpoints[-1])
        restore_order = list(fingerprints)
        for key in restore_order:
            _restore_fingerprint(fingerprints[key])

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
    report["legacy_records_unchanged"] = _legacy_projection(working) == legacy_before
    report["supplied_ledger_unchanged"] = (
        json.loads(state_path.read_text(encoding="utf-8")) == supplied
    )
    return report


def _verify_restored(paths: Paths, manifest: Manifest) -> list[str]:
    errors: list[str] = []
    jobs = _load_jobs(paths)
    for job_id, expected in manifest.jobs.items():
        if jobs.get(job_id) != expected:
            errors.append(f"job record not restored exactly: {job_id}")
    for key in ENTRYPOINTS:
        if not _matches(manifest.files[key]):
            errors.append(f"source not restored exactly: {key}")
    return errors


def _paths_from_args(args: argparse.Namespace) -> Paths:
    home = _resolved(args.hermes_home or LIVE_HOME)
    source = _resolved(args.source_root or Path.cwd())
    archive = _resolved(args.archive_root or home / "forensics")
    return Paths(
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
    )


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
    }
    for name, raw in overrides.items():
        if raw is not None and _resolved(raw) != getattr(paths, name):
            raise RetirementError(f"--{name.replace('_', '-')} differs from manifest")
    return paths, manifest


def _now_from_args(args: argparse.Namespace, paths: Paths) -> int:
    if args.now_epoch is not None:
        if paths.hermes_home == LIVE_HOME:
            raise RetirementError("--now-epoch is forbidden for the live Hermes home")
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
            manifest = preflight(paths, now)
            _print({"ok": True, "manifest_path": str(manifest.manifest_path)})
            return 0
        paths, manifest = _load_cli_manifest(args)
        now = _now_from_args(args, paths)
        if args.command == "pause":
            if not args.apply:
                selected = _job_map_locked(paths)
                _validate_job_fingerprints(paths, selected)
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
        elif args.command == "finalize":
            if not args.apply:
                errors = verify_quiescent(paths, manifest, now)
                if errors:
                    raise RetirementError("; ".join(errors))
                for key in ENTRYPOINTS:
                    _require_match(manifest.files[key])
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
                for key in ENTRYPOINTS:
                    _require_match(manifest.files[key], manifest.files[key].archive_path)
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
