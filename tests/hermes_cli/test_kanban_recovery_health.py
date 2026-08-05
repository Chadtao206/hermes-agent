"""Behavioral tests for the bounded blocked-recovery health snapshot."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest


def _counters(**overrides: int) -> dict[str, int]:
    from hermes_cli.kanban_recovery_health import REQUIRED_COUNTERS

    values = {name: 0 for name in REQUIRED_COUNTERS}
    values.update(overrides)
    return values


def _snapshot(*, recorded_at: int | None = None, board: str = "default"):
    from hermes_cli.kanban_recovery_health import (
        BoardRecoveryHealth,
        PublicEscalation,
        RecoveryHealthSnapshot,
    )

    return RecoveryHealthSnapshot(
        schema_version=1,
        revision=0,
        recorded_at=int(time.time()) if recorded_at is None else recorded_at,
        boards=(
            BoardRecoveryHealth(
                board=board,
                review_mode="shadow",
                transient_mode="shadow",
                census_healthy=True,
                ownerless_blocked_count=0,
                multiply_owned_blocked_count=0,
                validated_config_digest="d" * 64,
                counters=_counters(),
            ),
        ),
        escalations=(
            PublicEscalation(
                board=board,
                task_id="t_safe1234",
                signature="a" * 64,
                bound_input_signature="b" * 64,
                task_event_signature="c" * 64,
                outcome_class="human_action_required",
                tier="initial",
                acknowledged=False,
                reason_code="ownerless_blocked",
            ),
        ),
        checksum="",
    )


def test_public_health_round_trip_uses_default_root_not_active_profile(tmp_path, monkeypatch):
    import hermes_constants
    from hermes_cli.kanban_recovery_health import read_recovery_health, write_recovery_health

    root = tmp_path / "root"
    active_profile = root / "profiles" / "coder"
    monkeypatch.setenv("HERMES_HOME", str(active_profile))
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)

    write_recovery_health(_snapshot())
    loaded = read_recovery_health()

    assert loaded.revision == 1
    assert loaded.escalations[0].signature == "a" * 64
    assert loaded.boards[0].validated_config_digest == "d" * 64
    assert loaded.checksum
    assert (root / "state" / "kanban-blocked-recovery-health.json").is_file()
    assert not (active_profile / "state" / "kanban-blocked-recovery-health.json").exists()


def test_public_snapshot_contains_no_untrusted_prose_or_ansi(tmp_path, monkeypatch):
    import hermes_constants
    from hermes_cli.kanban_recovery_health import write_recovery_health

    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)
    value = _snapshot()
    write_recovery_health(value)
    encoded = (tmp_path / "state" / "kanban-blocked-recovery-health.json").read_text()

    assert "task body" not in encoded
    assert "comment" not in encoded
    assert "patch" not in encoded
    assert "error text" not in encoded
    assert "\x1b" not in encoded
    assert "|" not in encoded


@pytest.mark.parametrize(
    ("field", "attack"),
    [
        ("board", "default\x00other"),
        ("board", "default|other"),
        ("board", "\x1b[31mdefault"),
        ("task_id", "t_safe\nACTION_REQUIRED"),
        ("reason_code", "ownerless: task body"),
    ],
)
def test_public_snapshot_rejects_control_and_delimiter_attacks(field, attack):
    from dataclasses import replace

    value = _snapshot()
    escalation = value.escalations[0]
    if field == "board":
        value = replace(value, boards=(replace(value.boards[0], board=attack),))
    else:
        value = replace(value, escalations=(replace(escalation, **{field: attack}),))

    with pytest.raises((TypeError, ValueError)):
        value.validate()


def test_public_snapshot_rejects_mapping_subclasses_and_missing_dimensions():
    from dataclasses import replace

    class Sneaky(dict):
        pass

    value = _snapshot()
    with pytest.raises(TypeError, match="plain dict"):
        replace(value.boards[0], counters=Sneaky(_counters())).validate()
    incomplete = dict(_counters())
    incomplete.pop("cron.unhealthy")
    with pytest.raises(ValueError, match="counter dimensions"):
        replace(value.boards[0], counters=incomplete).validate()


def test_read_rejects_duplicate_corrupt_and_stale_json(tmp_path, monkeypatch):
    import hermes_constants
    from hermes_cli.kanban_recovery_health import (
        RecoveryHealthCorrupt,
        RecoveryHealthStale,
        read_recovery_health,
        write_recovery_health,
    )

    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)
    path = tmp_path / "state" / "kanban-blocked-recovery-health.json"
    path.parent.mkdir()
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(RecoveryHealthCorrupt):
        read_recovery_health()

    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(RecoveryHealthCorrupt):
        read_recovery_health()

    path.unlink()
    write_recovery_health(_snapshot(recorded_at=int(time.time()) - 3600))
    with pytest.raises(RecoveryHealthStale):
        read_recovery_health()


def test_concurrent_writers_serialize_monotonic_revisions(tmp_path, monkeypatch):
    import hermes_constants
    from hermes_cli.kanban_recovery_health import read_recovery_health, write_recovery_health

    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            write_recovery_health(_snapshot())
        except BaseException as exc:  # pragma: no cover - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert read_recovery_health().revision == len(threads)


def test_atomic_write_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    import hermes_constants
    import hermes_cli.kanban_recovery_health as health

    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)
    real_fsync = os.fsync
    fsynced_kinds: list[str] = []

    import stat

    def fixed_recording_fsync(fd: int) -> None:
        fsynced_kinds.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(health.os, "fsync", fixed_recording_fsync)
    health.write_recovery_health(_snapshot())

    assert "file" in fsynced_kinds
    assert "dir" in fsynced_kinds


def test_oversized_snapshot_is_rejected():
    from dataclasses import replace

    value = _snapshot()
    with pytest.raises(ValueError, match="boards"):
        replace(value, boards=value.boards * 65).validate()
    with pytest.raises(ValueError, match="counter"):
        replace(value.boards[0], counters={**_counters(), "x" * 65: 1}).validate()


def test_public_snapshot_rejects_unvalidated_config_identity():
    from dataclasses import replace

    value = _snapshot()
    with pytest.raises(ValueError, match="validated config digest"):
        replace(value.boards[0], validated_config_digest="not-a-digest").validate()


def test_read_rejects_oversized_file_before_json_decode(tmp_path, monkeypatch):
    import hermes_constants
    import hermes_cli.kanban_recovery_health as health

    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)
    path = tmp_path / "state" / health.HEALTH_FILENAME
    path.parent.mkdir()
    path.write_bytes(b"{" + b"x" * (health.MAX_PUBLIC_BYTES + 1))
    monkeypatch.setattr(
        health.json,
        "loads",
        lambda *_args, **_kwargs: pytest.fail("oversized input reached JSON decoding"),
    )

    with pytest.raises(health.RecoveryHealthCorrupt, match="too large"):
        health.read_recovery_health()


def test_older_concurrent_observation_cannot_replace_newer_revision(tmp_path, monkeypatch):
    import hermes_constants
    from hermes_cli.kanban_recovery_health import (
        RecoveryHealthRevisionConflict,
        read_recovery_health,
        write_recovery_health,
    )

    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)
    observed = int(time.time())
    write_recovery_health(_snapshot(recorded_at=observed))

    with pytest.raises(RecoveryHealthRevisionConflict):
        write_recovery_health(_snapshot(recorded_at=observed - 1))

    loaded = read_recovery_health()
    assert loaded.revision == 1
    assert loaded.recorded_at == observed
