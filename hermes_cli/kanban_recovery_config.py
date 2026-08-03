"""Validated, fail-closed configuration for blocked-Kanban recovery."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal, Mapping

import yaml


RecoveryMode = Literal["paused", "shadow", "live"]
_RECOVERY_MODES = frozenset({"paused", "shadow", "live"})
RolloutMode = Literal["paused", "shadow", "canary", "live"]
_ROLLOUT_MODES = frozenset({"paused", "shadow", "canary", "live"})


@dataclass(frozen=True)
class ManagedBoardControl:
    board: str
    review_mode: RecoveryMode
    transient_mode: RecoveryMode
    human_owner: str | None
    review_allowlist: tuple[str, ...]
    transient_retry_budget: int = 2
    transient_allow_engineer_lane_idle: bool = False
    clean_censuses_required: int = 2


@dataclass(frozen=True)
class ControllerJobSpec:
    role: str
    job_id: str
    role_tag: str
    source_manifest: tuple[str, ...]
    source_manifest_digest: str
    schedule_digest: str
    delivery_scope: str
    desired_mode: RecoveryMode


ControlUpdateOutcome = Literal["committed", "precondition_changed", "invalid"]


@dataclass(frozen=True)
class RecoveryControlUpdate:
    expected_digest: str
    operation: Literal["set_board", "set_review_allowlist", "set_controller"]
    values: Mapping[str, object]


@dataclass(frozen=True)
class ControlUpdateResult:
    outcome: ControlUpdateOutcome
    previous_digest: str
    current_digest: str
    reason: str = ""


@dataclass(frozen=True)
class RecoveryControl:
    boards: Mapping[str, ManagedBoardControl]
    mutation_enabled: bool
    errors: tuple[str, ...]
    controller_jobs: Mapping[str, ControllerJobSpec]
    router_lease_seconds: int
    target_stability_seconds: int
    verifier_retry_delays_seconds: tuple[int, ...]
    judge_retry_delays_seconds: tuple[int, ...]
    verification_deadline_seconds: int
    judge_deadline_seconds: int
    mutation_cap: int
    rollout_mode: RolloutMode

    def digest(self) -> str:
        """Return a stable digest of every validated recovery control."""
        payload = {
            "boards": {
                name: {
                    "review_mode": board.review_mode,
                    "transient_mode": board.transient_mode,
                    "human_owner": board.human_owner,
                    "review_allowlist": list(board.review_allowlist),
                    "transient_retry_budget": board.transient_retry_budget,
                    "transient_allow_engineer_lane_idle": board.transient_allow_engineer_lane_idle,
                    "clean_censuses_required": board.clean_censuses_required,
                }
                for name, board in self.boards.items()
            },
            "controller_jobs": {
                name: {
                    "role": job.role,
                    "job_id": job.job_id,
                    "role_tag": job.role_tag,
                    "source_manifest": list(job.source_manifest),
                    "source_manifest_digest": job.source_manifest_digest,
                    "schedule_digest": job.schedule_digest,
                    "delivery_scope": job.delivery_scope,
                    "desired_mode": job.desired_mode,
                }
                for name, job in self.controller_jobs.items()
            },
            "router_lease_seconds": self.router_lease_seconds,
            "target_stability_seconds": self.target_stability_seconds,
            "verifier_retry_delays_seconds": list(self.verifier_retry_delays_seconds),
            "judge_retry_delays_seconds": list(self.judge_retry_delays_seconds),
            "verification_deadline_seconds": self.verification_deadline_seconds,
            "judge_deadline_seconds": self.judge_deadline_seconds,
            "mutation_cap": self.mutation_cap,
            "rollout_mode": self.rollout_mode,
            "errors": list(self.errors),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, object],
        previous: "RecoveryControl | None" = None,
    ) -> "RecoveryControl":
        kanban = config.get("kanban", {})
        recovery = kanban.get("blocked_recovery", {}) if isinstance(kanban, Mapping) else {}
        boards = recovery.get("managed_boards", {}) if isinstance(recovery, Mapping) else {}
        if not isinstance(boards, Mapping) or not boards:
            return cls(
                {}, False, ("managed_boards must contain at least one known board",), {},
                300, 300, (300, 900, 1800, 3600), (300, 900, 1800, 3600), 7200, 7200, 1, "shadow",
            )
        parsed: dict[str, ManagedBoardControl] = {}
        errors: list[str] = []
        previous_boards = previous.boards if previous else {}
        for board, raw_board in boards.items():
            if not isinstance(board, str) or not board.strip() or not isinstance(raw_board, Mapping):
                errors.append("managed_boards entries must be named configuration objects")
                continue
            review_mode = raw_board.get("review_mode", "shadow")
            transient_mode = raw_board.get("transient_mode", "shadow")
            if review_mode not in _RECOVERY_MODES:
                errors.append(f"{board}.review_mode must be paused, shadow, or live")
                continue
            if transient_mode not in _RECOVERY_MODES:
                errors.append(f"{board}.transient_mode must be paused, shadow, or live")
                continue
            if board not in previous_boards and previous is not None:
                review_mode = "shadow"
                transient_mode = "shadow"
            elif transient_mode == "live" and review_mode != "live":
                errors.append(f"{board}.transient_mode cannot be live before review_mode")
            human_owner = raw_board.get("human_owner")
            if human_owner is not None and (not isinstance(human_owner, str) or not human_owner):
                errors.append(f"{board}.human_owner must be a non-empty string or null")
                human_owner = None
            review_allowlist = raw_board.get("review_allowlist", [])
            if not isinstance(review_allowlist, list) or not all(
                isinstance(item, str) and item for item in review_allowlist
            ):
                errors.append(f"{board}.review_allowlist must be a list of non-empty strings")
                review_allowlist = []
            transient_retry_budget = raw_board.get("transient_retry_budget", 2)
            if (
                isinstance(transient_retry_budget, bool)
                or not isinstance(transient_retry_budget, int)
                or transient_retry_budget <= 0
            ):
                errors.append(f"{board}.transient_retry_budget must be a positive integer")
                transient_retry_budget = 2
            engineer_lane_idle = raw_board.get("transient_allow_engineer_lane_idle", False)
            if not isinstance(engineer_lane_idle, bool):
                errors.append(f"{board}.transient_allow_engineer_lane_idle must be a boolean")
                engineer_lane_idle = False
            clean_censuses_required = raw_board.get("clean_censuses_required", 2)
            if (
                isinstance(clean_censuses_required, bool)
                or not isinstance(clean_censuses_required, int)
                or clean_censuses_required <= 0
            ):
                errors.append(f"{board}.clean_censuses_required must be a positive integer")
                clean_censuses_required = 2
            parsed[board] = ManagedBoardControl(
                board=board,
                review_mode=review_mode,
                transient_mode=transient_mode,
                human_owner=human_owner,
                review_allowlist=tuple(review_allowlist),
                transient_retry_budget=transient_retry_budget,
                transient_allow_engineer_lane_idle=engineer_lane_idle,
                clean_censuses_required=clean_censuses_required,
            )
        controllers = recovery.get("controller_jobs", {}) if isinstance(recovery, Mapping) else {}
        parsed_controllers: dict[str, ControllerJobSpec] = {}
        if not isinstance(controllers, Mapping):
            errors.append("controller_jobs must be a mapping")
        else:
            required_fields = (
                "source_manifest_digest", "role", "job_id", "role_tag", "source_manifest",
                "schedule_digest", "delivery_scope", "desired_mode",
            )
            for role, raw_job in controllers.items():
                if not isinstance(role, str) or not isinstance(raw_job, Mapping):
                    errors.append("controller_jobs entries must be named configuration objects")
                    continue
                missing = next((key for key in required_fields if key not in raw_job), None)
                if missing is not None:
                    errors.append(f"{role}.{missing} is required")
                    continue
                if set(raw_job) != set(required_fields):
                    errors.append(f"{role} must contain exactly the controller job fields")
                    continue
                source_manifest = raw_job["source_manifest"]
                desired_mode = raw_job["desired_mode"]
                values = tuple(raw_job[key] for key in required_fields if key != "source_manifest")
                if (
                    not isinstance(source_manifest, list)
                    or not all(isinstance(path, str) and path for path in source_manifest)
                    or not all(isinstance(value, str) and value for value in values)
                    or desired_mode not in _RECOVERY_MODES
                ):
                    errors.append(f"{role} has invalid controller job fields")
                    continue
                parsed_controllers[role] = ControllerJobSpec(
                    role=raw_job["role"], job_id=raw_job["job_id"], role_tag=raw_job["role_tag"],
                    source_manifest=tuple(source_manifest),
                    source_manifest_digest=raw_job["source_manifest_digest"],
                    schedule_digest=raw_job["schedule_digest"],
                    delivery_scope=raw_job["delivery_scope"], desired_mode=desired_mode,
                )
        def positive_int(name: str, default: int) -> int:
            value = recovery.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                errors.append(f"{name} must be a positive integer")
                return default
            return value

        def retry_delays(name: str) -> tuple[int, ...]:
            default = (300, 900, 1800, 3600)
            value = recovery.get(name, list(default))
            if (
                not isinstance(value, list)
                or len(value) != 4
                or any(isinstance(delay, bool) or not isinstance(delay, int) or delay <= 0 for delay in value)
            ):
                errors.append(f"{name} must contain four positive retry delays")
                return default
            return tuple(value)

        router_lease_seconds = positive_int("router_lease_seconds", 300)
        target_stability_seconds = positive_int("target_stability_seconds", 300)
        verifier_retry_delays_seconds = retry_delays("verifier_retry_delays_seconds")
        judge_retry_delays_seconds = retry_delays("judge_retry_delays_seconds")
        verification_deadline_seconds = positive_int("verification_deadline_seconds", 7200)
        judge_deadline_seconds = positive_int("judge_deadline_seconds", 7200)
        mutation_cap = positive_int("mutation_cap", 1)
        rollout_mode = recovery.get("rollout_mode", "shadow")
        if rollout_mode not in _ROLLOUT_MODES:
            errors.append("rollout_mode must be paused, shadow, canary, or live")
            rollout_mode = "shadow"
        if rollout_mode in {"shadow", "canary"} and mutation_cap != 1:
            errors.append("mutation_cap must be 1 while rollout_mode is shadow or canary")
        if rollout_mode in {"paused", "shadow", "canary"}:
            for board, control in parsed.items():
                if control.review_mode == "live":
                    errors.append(
                        f"{board}.review_mode cannot be live while rollout_mode is {rollout_mode}"
                    )
                if control.transient_mode == "live":
                    errors.append(
                        f"{board}.transient_mode cannot be live while rollout_mode is {rollout_mode}"
                    )
            for role, controller in parsed_controllers.items():
                if controller.desired_mode == "live":
                    errors.append(
                        f"{role}.desired_mode cannot be live while rollout_mode is {rollout_mode}"
                    )
        return cls(
            parsed, not errors, tuple(errors), parsed_controllers,
            router_lease_seconds, target_stability_seconds,
            verifier_retry_delays_seconds, judge_retry_delays_seconds,
            verification_deadline_seconds, judge_deadline_seconds, mutation_cap, rollout_mode,
        )


def _shared_config_path() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "config.yaml"


def _trusted_baseline_control() -> RecoveryControl:
    """Return the only board set allowed to be live after a cold start."""
    from hermes_cli.config import DEFAULT_CONFIG

    return RecoveryControl.from_config(DEFAULT_CONFIG)


def _control_from_raw(raw: Mapping[str, object]) -> RecoveryControl:
    """Validate a shared config against the cold-start admission baseline."""
    return RecoveryControl.from_config(
        _effective_config(raw), previous=_trusted_baseline_control()
    )


def load_recovery_control() -> RecoveryControl:
    """Load and validate the shared root recovery controls."""
    config_path = _shared_config_path()
    try:
        raw = _read_raw_config_for_update(config_path)
    except (OSError, ValueError, yaml.YAMLError):
        return RecoveryControl.from_config(
            {"kanban": {"blocked_recovery": {"managed_boards": []}}}
        )
    return _control_from_raw(raw)


@contextmanager
def _config_lock(config_path: Path) -> Iterator[None]:
    """Serialize read/compare/write updates for one config file."""
    lock_path = config_path.with_name(config_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            lock_file.write(b"0")
            lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _effective_config(raw: Mapping[str, object]) -> dict[str, object]:
    from hermes_cli.config import DEFAULT_CONFIG, _deep_merge
    from hermes_cli.managed_scope import apply_managed_overlay

    return apply_managed_overlay(_deep_merge(copy.deepcopy(DEFAULT_CONFIG), dict(raw)))


def _read_raw_config_for_update(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {}
    with open(config_path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml must contain a mapping")
    return raw


def _recovery_mapping(raw: dict[str, object]) -> dict[str, object]:
    kanban = raw.setdefault("kanban", {})
    if not isinstance(kanban, dict):
        raise ValueError("kanban must be a mapping")
    recovery = kanban.setdefault("blocked_recovery", {})
    if not isinstance(recovery, dict):
        raise ValueError("kanban.blocked_recovery must be a mapping")
    return recovery


def _apply_update(
    raw: dict[str, object],
    request: RecoveryControlUpdate,
    previous: RecoveryControl,
) -> None:
    recovery = _recovery_mapping(raw)
    values = dict(request.values)
    if request.operation == "set_board":
        board = values.pop("board", None)
        allowed = {
            "review_mode", "transient_mode", "human_owner", "review_allowlist",
            "transient_retry_budget", "transient_allow_engineer_lane_idle", "clean_censuses_required",
        }
        if not isinstance(board, str) or not board or not values or set(values) - allowed:
            raise ValueError("set_board requires board and explicit board control fields")
        boards = recovery.setdefault("managed_boards", {})
        if not isinstance(boards, dict):
            raise ValueError("managed_boards must be a mapping")
        current = boards.setdefault(board, {})
        if not isinstance(current, dict):
            raise ValueError(f"managed_boards.{board} must be a mapping")
        if board not in previous.boards:
            for mode_key in ("review_mode", "transient_mode"):
                if values.get(mode_key) == "live":
                    values[mode_key] = "shadow"
        current.update(values)
        return
    if request.operation == "set_review_allowlist":
        board = values.pop("board", None)
        allowlist = values.pop("review_allowlist", None)
        if not isinstance(board, str) or not board or values or not isinstance(allowlist, list):
            raise ValueError("set_review_allowlist requires board and review_allowlist")
        boards = recovery.setdefault("managed_boards", {})
        if not isinstance(boards, dict):
            raise ValueError("managed_boards must be a mapping")
        current = boards.setdefault(board, {})
        if not isinstance(current, dict):
            raise ValueError(f"managed_boards.{board} must be a mapping")
        current["review_allowlist"] = allowlist
        return
    if request.operation == "set_controller":
        role = values.pop("controller_role", None)
        if not isinstance(role, str) or not role or set(values) != {
            "role", "job_id", "role_tag", "source_manifest", "source_manifest_digest",
            "schedule_digest", "delivery_scope", "desired_mode",
        }:
            raise ValueError("set_controller requires controller_role and all controller job fields")
        controllers = recovery.setdefault("controller_jobs", {})
        if not isinstance(controllers, dict):
            raise ValueError("controller_jobs must be a mapping")
        controllers[role] = values
        return
    raise ValueError(f"unsupported recovery control operation: {request.operation}")


def _atomic_yaml_write(config_path: Path, raw: Mapping[str, object]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{config_path.name}.", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(dict(raw), handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, config_path)
        directory_fd = os.open(config_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _clear_config_caches(config_path: Path) -> None:
    from hermes_cli import config

    path_key = str(config_path)
    config._RAW_CONFIG_CACHE.pop(path_key, None)
    config._LOAD_CONFIG_CACHE.pop(path_key, None)
    config._LAST_EXPANDED_CONFIG_BY_PATH.pop(path_key, None)


def guarded_update_recovery_control(request: RecoveryControlUpdate) -> ControlUpdateResult:
    """Compare-and-swap a validated recovery-control-only config update."""
    from hermes_cli.config import (
        require_readable_config_before_write,
        validate_config_structure,
    )

    config_path = _shared_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with _config_lock(config_path):
        try:
            require_readable_config_before_write(config_path)
            raw = _read_raw_config_for_update(config_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            return ControlUpdateResult("invalid", "", "", str(exc))
        current = _control_from_raw(raw)
        previous_digest = current.digest()
        if request.expected_digest != previous_digest:
            return ControlUpdateResult("precondition_changed", previous_digest, previous_digest)
        candidate = copy.deepcopy(raw)
        try:
            _apply_update(candidate, request, current)
            validated = _control_from_raw(candidate)
        except (TypeError, ValueError) as exc:
            return ControlUpdateResult("invalid", previous_digest, previous_digest, str(exc))
        config_errors = [
            issue.message
            for issue in validate_config_structure(_effective_config(candidate))
            if issue.severity == "error"
        ]
        if config_errors:
            return ControlUpdateResult(
                "invalid", previous_digest, previous_digest, "; ".join(config_errors)
            )
        if not validated.mutation_enabled:
            return ControlUpdateResult(
                "invalid", previous_digest, previous_digest, "; ".join(validated.errors)
            )
        try:
            _atomic_yaml_write(config_path, candidate)
        except OSError as exc:
            return ControlUpdateResult("invalid", previous_digest, previous_digest, str(exc))
        _clear_config_caches(config_path)
        return ControlUpdateResult("committed", previous_digest, validated.digest())
