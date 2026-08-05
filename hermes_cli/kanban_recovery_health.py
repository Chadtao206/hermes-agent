"""Bounded public health state for blocked-recovery controllers.

This module deliberately accepts only a small trusted vocabulary.  Task bodies,
comments, patches, subprocess errors, and other operator-controlled prose have no
field in the schema and cannot be smuggled through identifier fields.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

try:  # pragma: no cover - Windows uses the in-process lock below.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


SCHEMA_VERSION = 1
MAX_SNAPSHOT_AGE_SECONDS = 900
MAX_BOARDS = 64
MAX_ESCALATIONS = 1024
MAX_COUNTER_VALUE = 1_000_000_000
MAX_PUBLIC_BYTES = 1_048_576
HEALTH_FILENAME = "kanban-blocked-recovery-health.json"

_PHASES = (
    "target_stabilizing", "target_unavailable", "repair_pending",
    "awaiting_review", "review_retry_pending", "review_exhausted",
    "superseded", "verification_deferred", "verification_exhausted",
    "judge_deferred", "judge_exhausted", "approved_pending_completion",
    "rejected", "invalid_review", "resolved_external", "resolved",
)
REQUIRED_COUNTERS = frozenset({
    "blocked.total",
    "blocked_kind.review", "blocked_kind.transient", "blocked_kind.human",
    "blocked_kind.unknown",
    "blocked_age.lt_24h", "blocked_age.24h_to_72h", "blocked_age.gte_72h",
    *(f"handoff_phase.{phase}" for phase in _PHASES),
    "mutation.total", "mutation.deterministic_24h", "defer.total", "retry.total",
    "escalation.initial", "escalation.24h", "escalation.72h",
    "ownership.ownerless", "ownership.multiple",
    "lease.active", "lease.stale", "lease.max_age_seconds",
    "fallback.total", "intent.unresolved", "intent.unknown_outcome",
    "gateway_fingerprint.known", "gateway_fingerprint.unknown",
    "cron.required", "cron.healthy", "cron.unhealthy",
    "controller_jobs.required", "controller_jobs.healthy", "controller_jobs.missing",
    "config.validated_revision",
})

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_COUNTER = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PROCESS_LOCK = threading.RLock()
TRUSTED_ESCALATION_REASONS: Mapping[str, str] = MappingProxyType({
    "human_action_required": "ownerless_blocked",
    "target_unavailable": "target_unavailable",
    "repair_required": "repair_required",
    "review_retry_exhausted": "review_retry_exhausted",
    "verification_exhausted": "verification_exhausted",
    "judge_exhausted": "judge_exhausted",
    "reviewer_rejected": "reviewer_rejected",
    "invalid_review": "invalid_review",
})


class RecoveryHealthError(RuntimeError):
    """Base class for an unavailable public health snapshot."""


class RecoveryHealthUnavailable(RecoveryHealthError):
    """The health snapshot does not exist."""


class RecoveryHealthCorrupt(RecoveryHealthError):
    """The health snapshot cannot be authenticated or decoded."""


class RecoveryHealthStale(RecoveryHealthError):
    """The health snapshot is valid but too old to be authoritative."""


class RecoveryHealthRevisionConflict(RecoveryHealthError):
    """A writer observed older source data than the current public revision."""


def _plain_text(value: object, label: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} is not bounded trusted text")
    return value


def _count(value: object, label: str) -> int:
    if type(value) is not int or value < 0 or value > MAX_COUNTER_VALUE:
        raise ValueError(f"{label} must be a bounded nonnegative integer")
    return value


def _epoch(value: object, label: str) -> int:
    if type(value) is not int or value < 0 or value > 4_102_444_800:
        raise ValueError(f"{label} must be a bounded nonnegative epoch")
    return value


@dataclass(frozen=True)
class PublicEscalation:
    board: str
    task_id: str
    signature: str
    bound_input_signature: str
    task_event_signature: str
    outcome_class: str
    tier: Literal["initial", "24h", "72h"]
    acknowledged: bool
    reason_code: str

    def validate(self) -> "PublicEscalation":
        _plain_text(self.board, "board", _IDENTIFIER)
        _plain_text(self.task_id, "task id", _IDENTIFIER)
        _plain_text(self.signature, "escalation signature", _DIGEST)
        _plain_text(self.bound_input_signature, "bound input signature", _DIGEST)
        _plain_text(self.task_event_signature, "task event signature", _DIGEST)
        _plain_text(self.outcome_class, "outcome class", _CODE)
        _plain_text(self.reason_code, "reason code", _CODE)
        if self.outcome_class not in TRUSTED_ESCALATION_REASONS:
            raise ValueError("unsupported trusted escalation outcome")
        if self.reason_code != TRUSTED_ESCALATION_REASONS[self.outcome_class]:
            raise ValueError("invalid escalation outcome/reason mapping")
        if self.tier not in {"initial", "24h", "72h"}:
            raise ValueError("unsupported escalation tier")
        if type(self.acknowledged) is not bool:
            raise TypeError("acknowledged must be a boolean")
        return self


@dataclass(frozen=True)
class BoardRecoveryHealth:
    board: str
    review_mode: Literal["paused", "shadow", "live"]
    transient_mode: Literal["paused", "shadow", "live"]
    census_healthy: bool
    ownerless_blocked_count: int
    multiply_owned_blocked_count: int
    validated_config_digest: str
    counters: Mapping[str, int]

    def __post_init__(self) -> None:
        if type(self.counters) is not dict and type(self.counters) is not MappingProxyType:
            raise TypeError("counters must be a plain dict")
        object.__setattr__(self, "counters", MappingProxyType(dict(self.counters)))

    def validate(self) -> "BoardRecoveryHealth":
        _plain_text(self.board, "board", _IDENTIFIER)
        if self.review_mode not in {"paused", "shadow", "live"}:
            raise ValueError("unsupported review mode")
        if self.transient_mode not in {"paused", "shadow", "live"}:
            raise ValueError("unsupported transient mode")
        if type(self.census_healthy) is not bool:
            raise TypeError("census_healthy must be a boolean")
        _count(self.ownerless_blocked_count, "ownerless blocked count")
        _count(self.multiply_owned_blocked_count, "multiply owned blocked count")
        _plain_text(self.validated_config_digest, "validated config digest", _DIGEST)
        if type(self.counters) is not dict and type(self.counters) is not MappingProxyType:
            raise TypeError("counters must be a plain dict")
        if len(self.counters) > len(REQUIRED_COUNTERS):
            # Unknown fields are rejected rather than becoming a prose tunnel.
            raise ValueError("counter dimensions do not match the health schema")
        if set(self.counters) != REQUIRED_COUNTERS:
            raise ValueError("counter dimensions do not match the health schema")
        for key, value in self.counters.items():
            _plain_text(key, "counter name", _COUNTER)
            _count(value, f"counter {key}")
        if self.counters["ownership.ownerless"] != self.ownerless_blocked_count:
            raise ValueError("ownerless ownership counters disagree")
        if self.counters["ownership.multiple"] != self.multiply_owned_blocked_count:
            raise ValueError("multiple ownership counters disagree")
        return self


@dataclass(frozen=True)
class RecoveryHealthSnapshot:
    schema_version: int
    revision: int
    recorded_at: int
    boards: tuple[BoardRecoveryHealth, ...]
    escalations: tuple[PublicEscalation, ...]
    checksum: str

    def validate(self, *, require_checksum: bool = False) -> "RecoveryHealthSnapshot":
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported recovery health schema")
        _count(self.revision, "revision")
        _epoch(self.recorded_at, "recorded at")
        if type(self.boards) is not tuple or not self.boards or len(self.boards) > MAX_BOARDS:
            raise ValueError("boards must be a bounded nonempty tuple")
        if type(self.escalations) is not tuple or len(self.escalations) > MAX_ESCALATIONS:
            raise ValueError("escalations must be a bounded tuple")
        if len({board.board for board in self.boards}) != len(self.boards):
            raise ValueError("duplicate health board")
        for board in self.boards:
            if type(board) is not BoardRecoveryHealth:
                raise TypeError("boards must contain BoardRecoveryHealth values")
            board.validate()
        seen: set[tuple[str, str]] = set()
        boards = {board.board for board in self.boards}
        for escalation in self.escalations:
            if type(escalation) is not PublicEscalation:
                raise TypeError("escalations must contain PublicEscalation values")
            escalation.validate()
            key = (escalation.board, escalation.signature)
            if escalation.board not in boards or key in seen:
                raise ValueError("escalation board/signature is invalid or duplicate")
            seen.add(key)
        if self.checksum:
            _plain_text(self.checksum, "checksum", _DIGEST)
        elif require_checksum:
            raise ValueError("checksum is required")
        return self

    def canonical_json(self) -> str:
        return _canonical_bytes(_snapshot_data(self, include_checksum=True)).decode("utf-8")


def _snapshot_data(value: RecoveryHealthSnapshot, *, include_checksum: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": value.schema_version,
        "revision": value.revision,
        "recorded_at": value.recorded_at,
        "boards": [
            {
                "board": board.board,
                "review_mode": board.review_mode,
                "transient_mode": board.transient_mode,
                "census_healthy": board.census_healthy,
                "ownerless_blocked_count": board.ownerless_blocked_count,
                "multiply_owned_blocked_count": board.multiply_owned_blocked_count,
                "validated_config_digest": board.validated_config_digest,
                "counters": dict(board.counters),
            }
            for board in value.boards
        ],
        "escalations": [asdict(escalation) for escalation in value.escalations],
    }
    if include_checksum:
        payload["checksum"] = value.checksum
    return payload


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")


def _checksum(value: RecoveryHealthSnapshot) -> str:
    return hashlib.sha256(_canonical_bytes(_snapshot_data(value, include_checksum=False))).hexdigest()


def _health_path() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "state" / HEALTH_FILENAME


def _duplicate_safe_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise RecoveryHealthCorrupt("duplicate JSON key")
        value[key] = item
    return value


def _exact_dict(value: object, keys: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise RecoveryHealthCorrupt(f"invalid {label} fields")
    return value


def _decode(encoded: str) -> RecoveryHealthSnapshot:
    try:
        raw = json.loads(encoded, object_pairs_hook=_duplicate_safe_object)
        root = _exact_dict(
            raw, {"schema_version", "revision", "recorded_at", "boards", "escalations", "checksum"},
            "snapshot",
        )
        if type(root["boards"]) is not list or type(root["escalations"]) is not list:
            raise RecoveryHealthCorrupt("health collections must be arrays")
        boards = []
        for item in root["boards"]:
            data = _exact_dict(
                item,
                {"board", "review_mode", "transient_mode", "census_healthy", "ownerless_blocked_count", "multiply_owned_blocked_count", "validated_config_digest", "counters"},
                "board",
            )
            if type(data["counters"]) is not dict:
                raise RecoveryHealthCorrupt("counters must be an object")
            boards.append(BoardRecoveryHealth(**data))
        escalations = []
        escalation_keys = {
            "board", "task_id", "signature", "bound_input_signature", "task_event_signature",
            "outcome_class", "tier", "acknowledged", "reason_code",
        }
        for item in root["escalations"]:
            escalations.append(PublicEscalation(**_exact_dict(item, escalation_keys, "escalation")))
        value = RecoveryHealthSnapshot(
            schema_version=root["schema_version"], revision=root["revision"],
            recorded_at=root["recorded_at"], boards=tuple(boards),
            escalations=tuple(escalations), checksum=root["checksum"],
        ).validate(require_checksum=True)
        if not hashlib.sha256(
            _canonical_bytes(_snapshot_data(value, include_checksum=False))
        ).hexdigest() == value.checksum:
            raise RecoveryHealthCorrupt("health checksum mismatch")
        return value
    except RecoveryHealthCorrupt:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryHealthCorrupt("invalid recovery health snapshot") from exc


def _read_existing(path: Path) -> RecoveryHealthSnapshot | None:
    fd: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb", closefd=True) as stream:
            fd = None
            encoded = stream.read(MAX_PUBLIC_BYTES + 1)
        if len(encoded) > MAX_PUBLIC_BYTES:
            raise RecoveryHealthCorrupt("recovery health snapshot is too large")
        return _decode(encoded.decode("ascii"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise RecoveryHealthCorrupt("recovery health snapshot is unreadable") from exc
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def _writer_lock(path: Path):
    with _PROCESS_LOCK:
        lock_path = path.with_suffix(path.suffix + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_recovery_health(value: RecoveryHealthSnapshot) -> None:
    """Durably replace the public snapshot with the next serialized revision."""
    if type(value) is not RecoveryHealthSnapshot:
        raise TypeError("value must be a RecoveryHealthSnapshot")
    value.validate()
    path = _health_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _fsync_directory(path.parent)
    with _writer_lock(path):
        current = _read_existing(path)
        if current is not None and value.recorded_at < current.recorded_at:
            raise RecoveryHealthRevisionConflict("health observation predates current revision")
        revision = 1 if current is None else current.revision + 1
        unsigned = replace(value, schema_version=SCHEMA_VERSION, revision=revision, checksum="")
        committed = replace(unsigned, checksum=_checksum(unsigned))
        encoded = committed.canonical_json().encode("ascii")
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            _fsync_directory(path.parent)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


def read_recovery_health() -> RecoveryHealthSnapshot:
    """Return an authenticated, fresh snapshot; absence is never an empty view."""
    value = _read_existing(_health_path())
    if value is None:
        raise RecoveryHealthUnavailable("recovery health snapshot is missing")
    age = int(time.time()) - value.recorded_at
    if age > MAX_SNAPSHOT_AGE_SECONDS or age < -MAX_SNAPSHOT_AGE_SECONDS:
        raise RecoveryHealthStale("recovery health snapshot is stale")
    return value
