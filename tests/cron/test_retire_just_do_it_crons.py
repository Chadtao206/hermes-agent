from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from cron import jobs as cron_jobs
from scripts.ops import retire_just_do_it_crons as retirement_controller
from scripts.ops.retire_just_do_it_crons import (
    Manifest,
    Paths,
    RetirementError,
    checkpoint_component,
    finalize,
    pause_producers,
    preflight,
    rehearse_reaction_roundtrip,
    rollback,
    main as controller_main,
    verify_quiescent,
    verify_retired,
)


RETIRE_IDS = {"ee4582d0598f", "ff865c33c5ea"}
RETAIN_ID = "6daa151da351"


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if child.is_file():
            relative = child.relative_to(path).as_posix().encode()
            digest.update(relative + b"\0" + child.read_bytes())
    return digest.hexdigest()


def _job(job_id: str, name: str, script: Path, schedule: dict) -> dict:
    return {
        "id": job_id,
        "name": name,
        "prompt": f"prompt for {name}",
        "script": str(script),
        "schedule": schedule,
        "no_agent": True,
        "enabled": True,
        "state": "scheduled",
        "next_run_at": "2026-08-08T20:00:00+00:00",
        "last_run_at": "2026-08-08T19:59:00+00:00",
        "last_status": "ok",
        "completed_count": 7,
    }


@dataclass
class RetirementFixture:
    root: Path
    paths: Paths
    now: int
    jobs: list[dict]
    reaction_state_path: Path
    watchdog_source_path: Path
    process_listing_path: Path
    service_status_path: Path

    def utc_iso(self, epoch: int) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    def write_jobs(self) -> None:
        self.paths.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        self.paths.jobs_path.write_text(
            json.dumps({"jobs": self.jobs, "updated_at": self.utc_iso(self.now)}, indent=2)
        )

    def read_jobs(self) -> list[dict]:
        return json.loads(self.paths.jobs_path.read_text())["jobs"]

    def insert_execution(
        self,
        job_id: str,
        *,
        status: str,
        claimed_at: str,
        pid: int = 991,
        process_started_at: int = 1001,
    ) -> str:
        execution_id = f"exec-{job_id}-{claimed_at}-{status}"
        with sqlite3.connect(self.paths.executions_path) as conn:
            conn.execute(
                """INSERT INTO executions
                   (id, job_id, source, process_id, pid, process_started_at,
                    status, claimed_at, started_at, finished_at, error)
                   VALUES (?, ?, 'builtin', 'fixture', ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    execution_id,
                    job_id,
                    pid,
                    process_started_at,
                    status,
                    claimed_at,
                    claimed_at if status != "claimed" else None,
                    claimed_at if status in {"completed", "failed", "unknown"} else None,
                ),
            )
        return execution_id

    def preflight_only(self):
        return preflight(self.paths, self.now)

    def preflight_and_pause(self):
        manifest = self.preflight_only()
        return pause_producers(self.paths, manifest, self.now + 1)

    def preflight_pause_and_age(self):
        return self.preflight_and_pause()

    def complete_retirement(self):
        manifest = self.preflight_and_pause()
        return finalize(self.paths, manifest, manifest.cutoff_epoch + 181)

    def finish_pre_pause_execution_and_write_output(self) -> None:
        self.insert_execution(
            "ee4582d0598f",
            status="completed",
            claimed_at=self.utc_iso(self.now - 5),
        )
        output = self.paths.cron_output / "ee4582d0598f" / "finished.md"
        output.write_text("legitimate pre-pause completion\n")
        (self.paths.state_dir / "just_do_it_blocked_review_requeue.json").write_text(
            '{"completed":"pre-pause"}\n'
        )

    def audit_hashes(self) -> dict[str, str]:
        paths = {
            "board": self.paths.board_path,
            "requeue-ledger": self.paths.state_dir
            / "just_do_it_blocked_review_requeue.json",
            "reaction-ledger": self.reaction_state_path,
        }
        for channel in ("C08K8FQKX9S", "C07BU8AAPB3", "C09339EGZ9U"):
            paths[f"watermark-{channel}"] = (
                self.paths.state_dir / f"slack_channel_watch_{channel}.json"
            )
        result = {name: _hash_file(path) for name, path in paths.items()}
        for job_id in sorted(RETIRE_IDS):
            result[f"output-{job_id}"] = _hash_tree(self.paths.cron_output / job_id)
        return result

    def output_paths_are_present_for(self, ids: set[str]) -> bool:
        return all((self.paths.cron_output / job_id).is_dir() for job_id in ids)

    def retired_live_paths_are_absent(self) -> bool:
        return all(not path.exists() for path in self.retired_live_paths())

    def retired_live_paths(self) -> list[Path]:
        return [
            self.paths.live_scripts / "slack_channel_watch.py",
            self.paths.live_scripts / "test_slack_channel_watch.py",
            self.paths.live_scripts / "just_do_it_blocked_review_requeue_watchdog.py",
            self.paths.live_scripts / "test_just_do_it_blocked_review_requeue_watchdog.py",
        ]

    def archived_paths_match_hashes(self, manifest) -> bool:
        for key in (
            "retired-channel-script",
            "retired-channel-test",
            "retired-requeue-script",
            "retired-requeue-test",
        ):
            fingerprint = manifest.files[key]
            if not fingerprint.archive_path.exists():
                return False
            if _hash_file(fingerprint.archive_path) != fingerprint.sha256:
                return False
        return True

    def all_fingerprints_match(self, manifest) -> bool:
        if not all(path.exists() for path in self.retired_live_paths()):
            return False
        for key in (
            "retired-channel-script",
            "retired-channel-test",
            "retired-requeue-script",
            "retired-requeue-test",
        ):
            fingerprint = manifest.files[key]
            if _hash_file(fingerprint.active_path) != fingerprint.sha256:
                return False
        return {job["id"] for job in self.read_jobs()} == RETIRE_IDS | {RETAIN_ID}

    def checkpoint_and_change_reaction(self, manifest):
        with cron_jobs.use_cron_store(self.paths.hermes_home):
            cron_jobs.pause_job(RETAIN_ID, reason="installation lock")
        manifest = checkpoint_component(
            self.paths, manifest, "retained-reaction", manifest.cutoff_epoch + 200
        )
        (self.paths.live_scripts / "just_do_it_review_reaction_watchdog.py").write_text(
            "changed reaction script\n"
        )
        (self.paths.live_scripts / "test_just_do_it_review_reaction_watchdog.py").write_text(
            "changed reaction test\n"
        )
        changed = json.loads(self.reaction_state_path.read_text())
        changed["posts"]["https://ylopo.slack.com/archives/C0BPCKPEFCG/p1111111111111111"][
            "emoji"
        ] = "face_vomiting"
        self.reaction_state_path.write_text(json.dumps(changed, indent=2))
        with cron_jobs.use_cron_store(self.paths.hermes_home):
            with cron_jobs._jobs_lock():
                jobs = cron_jobs.load_jobs()
                for job in jobs:
                    if job["id"] == RETAIN_ID:
                        job.update(
                            name="boris-review-reaction-state-watchdog",
                            enabled=True,
                            state="scheduled",
                        )
                cron_jobs.save_jobs(jobs)
        self.insert_execution(
            RETAIN_ID,
            status="completed",
            claimed_at=self.utc_iso(manifest.cutoff_epoch + 199),
        )
        return manifest

    def checkpoint_and_change_boris(self, manifest):
        manifest = checkpoint_component(
            self.paths, manifest, "boris-intake", manifest.cutoff_epoch + 210
        )
        (self.paths.live_scripts / "boris_intake" / "slack_io.py").write_text(
            "changed boris source\n"
        )
        audit = self.paths.state_dir / "boris_intake_audit.jsonl"
        audit.write_text(audit.read_text() + '{"event":"changed"}\n')
        (
            self.paths.source_root / "tests" / "cron" / "boris_intake" / "test_daemon.py"
        ).write_text("changed durable Boris test\n")
        return manifest

    def reaction_matches_latest_checkpoint(self) -> bool:
        return (
            self.paths.live_scripts / "just_do_it_review_reaction_watchdog.py"
        ).read_text() == "reaction live\n"

    def boris_matches_latest_checkpoint(self) -> bool:
        live_matches = (
            self.paths.live_scripts / "boris_intake" / "slack_io.py"
        ).read_text() == "boris live slack\n"
        test_matches = (
            self.paths.source_root / "tests" / "cron" / "boris_intake" / "test_daemon.py"
        ).read_text() == "durable boris test\n"
        return live_matches and test_matches

    def legacy_reaction_records(self) -> dict:
        state = json.loads(self.reaction_state_path.read_text())
        return {
            permalink: value
            for permalink, value in state["posts"].items()
            if "/C0BPCKPEFCG/" not in permalink
        }


def _initialize_execution_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE executions (
                 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
                 process_id TEXT NOT NULL, pid INTEGER NOT NULL,
                 process_started_at INTEGER, status TEXT NOT NULL,
                 claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                 error TEXT)"""
        )


def _initialize_board(path: Path, now: int) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE tasks (
                 id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,
                 assignee TEXT, status TEXT NOT NULL, created_by TEXT,
                 created_at INTEGER NOT NULL, idempotency_key TEXT)"""
        )
        conn.execute(
            """INSERT INTO tasks
               (id, title, body, assignee, status, created_by, created_at,
                idempotency_key) VALUES
               ('historical', 'old rereview', 'historical audit row', 'reviewer',
                'done', 'just-do-it-blocked-review-requeue-watchdog', ?,
                'just-do-it-rereview:old')""",
            (now - 1000,),
        )


def _initialize_boris_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """CREATE TABLE seen_events (channel TEXT, ts TEXT,
                                           PRIMARY KEY(channel, ts));
               CREATE TABLE lanes (idempotency_key TEXT PRIMARY KEY,
                                   lane_id TEXT NOT NULL);
               INSERT INTO seen_events VALUES ('C0BPCKPEFCG', '1.0');
               INSERT INTO lanes VALUES ('boris-review:C0BPCKPEFCG:1.0:x', 'lane-1');"""
        )


def _git_commit(source_root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=source_root, check=True)
    subprocess.run(["git", "add", "."], cwd=source_root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=source_root,
        check=True,
    )


@pytest.fixture
def retirement_fixture(tmp_path) -> RetirementFixture:
    now = 1786222800
    hermes_home = tmp_path / "home"
    source_root = tmp_path / "source"
    archive_root = tmp_path / "archive"
    live_scripts = hermes_home / "scripts"
    state_dir = hermes_home / "state"
    cron_output = hermes_home / "cron" / "output"
    for directory in (live_scripts, state_dir, cron_output, source_root, archive_root):
        directory.mkdir(parents=True, exist_ok=True)

    process_listing_path = tmp_path / "processes.json"
    service_status_path = tmp_path / "service-status.json"
    paths = Paths(
        hermes_home=hermes_home,
        source_root=source_root,
        archive_root=archive_root,
        jobs_path=hermes_home / "cron" / "jobs.json",
        executions_path=hermes_home / "cron" / "executions.db",
        board_path=hermes_home / "kanban.db",
        live_scripts=live_scripts,
        state_dir=state_dir,
        cron_output=cron_output,
        boris_launchd_plist=tmp_path / "ai.hermes.boris-intake.plist",
        process_listing_path=process_listing_path,
        boris_service_status_path=service_status_path,
    )

    jobs = [
        _job(
            "ee4582d0598f",
            "just-do-it-channel-watch",
            live_scripts / "slack_channel_watch.py",
            {"kind": "interval", "minutes": 2},
        ),
        _job(
            "ff865c33c5ea",
            "just-do-it-blocked-review-requeue-watchdog",
            live_scripts / "just_do_it_blocked_review_requeue_watchdog.py",
            {"kind": "cron", "expr": "*/2 * * * *"},
        ),
        _job(
            RETAIN_ID,
            "just-do-it-review-reaction-watchdog",
            live_scripts / "just_do_it_review_reaction_watchdog.py",
            {"kind": "cron", "expr": "* * * * *"},
        ),
    ]

    fixture = RetirementFixture(
        root=tmp_path,
        paths=paths,
        now=now,
        jobs=jobs,
        reaction_state_path=state_dir / "just_do_it_review_reactions.json",
        watchdog_source_path=source_root
        / "cron/scripts/just_do_it_review_reaction_watchdog.py",
        process_listing_path=process_listing_path,
        service_status_path=service_status_path,
    )
    fixture.write_jobs()

    live_contents = {
        "slack_channel_watch.py": "channel live\n",
        "test_slack_channel_watch.py": "channel test live\n",
        "just_do_it_blocked_review_requeue_watchdog.py": "requeue live\n",
        "test_just_do_it_blocked_review_requeue_watchdog.py": "requeue test live\n",
        "just_do_it_review_reaction_watchdog.py": "reaction live\n",
        "test_just_do_it_review_reaction_watchdog.py": "reaction test live\n",
    }
    for name, content in live_contents.items():
        path = live_scripts / name
        path.write_text(content)
        path.chmod(0o755 if not name.startswith("test_") else 0o644)

    boris_live = live_scripts / "boris_intake"
    boris_live.mkdir()
    (boris_live / "__init__.py").write_text("")
    (boris_live / "daemon.py").write_text("boris live daemon\n")
    (boris_live / "slack_io.py").write_text("boris live slack\n")

    source_files = {
        "cron/scripts/just_do_it_blocked_review_requeue_watchdog.py": "durable requeue\n",
        "tests/cron/test_just_do_it_blocked_review_requeue_watchdog.py": "durable requeue test\n",
        "tests/cron/test_just_do_it_review_reaction_watchdog.py": "durable reaction test\n",
        "cron/scripts/boris_intake/__init__.py": "",
        "cron/scripts/boris_intake/daemon.py": "durable boris daemon\n",
        "cron/scripts/boris_intake/slack_io.py": "durable boris slack\n",
        "tests/cron/boris_intake/test_daemon.py": "durable boris test\n",
    }
    for relative, content in source_files.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    from cron.scripts import just_do_it_review_reaction_watchdog as versioned_watchdog

    shutil.copy2(versioned_watchdog.__file__, fixture.watchdog_source_path)
    _git_commit(source_root)

    for job_id in RETIRE_IDS | {RETAIN_ID}:
        output = cron_output / job_id
        output.mkdir(parents=True)
        (output / "1786222700.md").write_text(f"historical output for {job_id}\n")

    for channel in ("C08K8FQKX9S", "C07BU8AAPB3", "C09339EGZ9U"):
        (state_dir / f"slack_channel_watch_{channel}.json").write_text(
            json.dumps({"channel": channel, "watermark": "1786222000.000001"})
        )
    (state_dir / "just_do_it_blocked_review_requeue.json").write_text(
        '{"completed":"historical"}\n'
    )
    fixture.reaction_state_path.write_text(
        json.dumps(
            {
                "version": 4,
                "posts": {
                    "https://ylopo.slack.com/archives/C0BPCKPEFCG/p1111111111111111": {
                        "state": "active",
                        "emoji": "thinkies",
                        "lanes": {"boris-lane": "active"},
                        "pending_removals": [],
                    },
                    "https://ylopo.slack.com/archives/C08K8FQKX9S/p2222222222222222": {
                        "emoji": "doge_notes",
                        "lanes": {"legacy-lane": "doge_notes"},
                        "pending_removals": [],
                        "opaque": {"preserve": [1, 2, 3]},
                    },
                },
                "reacted": {},
            },
            indent=2,
        )
    )
    (state_dir / "boris_intake_audit.jsonl").write_text('{"event":"accepted"}\n')
    (state_dir / "boris_intake_heartbeat").write_text("100\n")
    (state_dir / "boris_intake_watermark_C0BPCKPEFCG.txt").write_text("1.0\n")
    _initialize_boris_db(state_dir / "boris_intake.db")
    (hermes_home / "config.yaml").write_text(
        "boris_intake:\n  mode: live\n  signing_secret: never-embed-this\n"
    )
    paths.boris_launchd_plist.write_text(
        "<plist><dict><string>ai.hermes.boris-intake</string></dict></plist>\n"
    )

    _initialize_execution_db(paths.executions_path)
    fixture.insert_execution(
        RETAIN_ID,
        status="completed",
        claimed_at=fixture.utc_iso(now - 60),
    )
    _initialize_board(paths.board_path, now)
    process_listing_path.write_text(
        json.dumps(
            [
                {
                    "pid": 777,
                    "started_at": 900,
                    "command": f"python {live_scripts / 'boris_intake/daemon.py'}",
                }
            ]
        )
    )
    service_status_path.write_text(
        json.dumps(
            {
                "label": "ai.hermes.boris-intake",
                "state": "running",
                "pid": 777,
            }
        )
    )
    return fixture


def test_preflight_refuses_name_schedule_or_script_drift(retirement_fixture):
    fixture = retirement_fixture
    fixture.jobs[0]["script"] = "unexpected.py"
    fixture.write_jobs()
    with pytest.raises(RetirementError, match="fingerprint mismatch"):
        preflight(fixture.paths, fixture.now)


def test_quiescence_rejects_running_or_post_pause_execution(retirement_fixture):
    manifest = retirement_fixture.preflight_and_pause()
    retirement_fixture.insert_execution(
        "ff865c33c5ea",
        status="running",
        claimed_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 1),
    )
    errors = verify_quiescent(
        retirement_fixture.paths,
        manifest,
        manifest.cutoff_epoch + 181,
    )
    assert any("post-pause execution" in error for error in errors)


def test_finalize_removes_only_retired_ids_and_archives_entrypoints(retirement_fixture):
    manifest = retirement_fixture.preflight_pause_and_age()
    finalize(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181)
    ids = {job["id"] for job in retirement_fixture.read_jobs()}
    assert ids == {RETAIN_ID}
    assert retirement_fixture.retired_live_paths_are_absent()
    assert retirement_fixture.archived_paths_match_hashes(manifest)


def test_finalize_preserves_outputs_ledgers_and_tasks(retirement_fixture):
    before = retirement_fixture.audit_hashes()
    manifest = retirement_fixture.preflight_pause_and_age()
    finalize(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181)
    assert retirement_fixture.audit_hashes() == before
    assert retirement_fixture.output_paths_are_present_for(RETIRE_IDS)


def test_finalize_preserves_a_pre_pause_execution_output(retirement_fixture):
    manifest = retirement_fixture.preflight_and_pause()
    retirement_fixture.finish_pre_pause_execution_and_write_output()
    expected = retirement_fixture.audit_hashes()
    finalize(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181)
    assert retirement_fixture.audit_hashes() == expected


def test_rollback_restores_sources_before_jobs(retirement_fixture):
    manifest = retirement_fixture.complete_retirement()
    events = []
    rollback(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240, events.append)
    assert events.index("sources_restored") < events.index("jobs_restored")
    assert retirement_fixture.all_fingerprints_match(manifest)


def test_reaction_checkpoint_requires_retained_job_paused(retirement_fixture):
    manifest = retirement_fixture.preflight_only()
    with pytest.raises(RetirementError, match="retained job must be paused"):
        checkpoint_component(
            retirement_fixture.paths,
            manifest,
            "retained-reaction",
            retirement_fixture.now,
        )


def test_rollback_uses_latest_component_checkpoints(retirement_fixture):
    manifest = retirement_fixture.complete_retirement()
    manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)
    manifest = retirement_fixture.checkpoint_and_change_boris(manifest)
    rollback(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240)
    assert retirement_fixture.reaction_matches_latest_checkpoint()
    assert retirement_fixture.boris_matches_latest_checkpoint()


def test_reaction_rehearsal_roundtrips_only_boris_records(retirement_fixture):
    before_legacy = retirement_fixture.legacy_reaction_records()
    report = rehearse_reaction_roundtrip(
        retirement_fixture.reaction_state_path,
        retirement_fixture.watchdog_source_path,
    )
    assert report["modern_first_pass_calls"] > 0
    assert report["modern_second_pass_calls"] == 0
    assert report["legacy_first_pass_calls"] > 0
    assert report["legacy_second_pass_calls"] == 0
    assert report["legacy_records_unchanged"] is True
    assert retirement_fixture.legacy_reaction_records() == before_legacy


def test_quiescence_rejects_a_new_deprecated_provenance_task(retirement_fixture):
    manifest = retirement_fixture.preflight_and_pause()
    with sqlite3.connect(retirement_fixture.paths.board_path) as conn:
        conn.execute(
            """INSERT INTO tasks
               (id, title, body, assignee, status, created_by, created_at,
                idempotency_key) VALUES
               ('new-lane', 'new lane', 'body', 'reviewer', 'ready', 'other', ?,
                'just-do-it-rereview:new')""",
            (manifest.cutoff_epoch + 2,),
        )
    errors = verify_quiescent(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
    )
    assert any("new deprecated-provenance task" in error for error in errors)


def test_quiescence_uses_injected_process_listing(retirement_fixture):
    manifest = retirement_fixture.preflight_and_pause()
    retirement_fixture.process_listing_path.write_text(
        json.dumps(
            [
                {
                    "pid": 1234,
                    "started_at": 5678,
                    "command": "python /tmp/slack_channel_watch.py",
                }
            ]
        )
    )
    errors = verify_quiescent(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
    )
    assert any("retired script process" in error for error in errors)


def test_verify_retired_accepts_the_guarded_final_state(retirement_fixture):
    manifest = retirement_fixture.complete_retirement()
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            jobs[0]["name"] = "boris-review-reaction-state-watchdog"
            cron_jobs.save_jobs(jobs)
    retirement_fixture.insert_execution(
        RETAIN_ID,
        status="completed",
        claimed_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 200),
    )
    assert verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    ) == []


def test_manifest_does_not_embed_secret_config_contents(retirement_fixture):
    manifest = retirement_fixture.preflight_only()
    serialized = manifest.to_json()
    assert "never-embed-this" not in serialized
    config_fingerprint = manifest.files["config"]
    assert config_fingerprint.archive_path.read_bytes() == (
        retirement_fixture.paths.hermes_home / "config.yaml"
    ).read_bytes()
    assert config_fingerprint.mode == 0o644


def test_failed_second_removal_restores_everything_and_can_retry(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_and_pause()
    real_remove = cron_jobs.remove_job

    def fail_second(job_id):
        if job_id == "ff865c33c5ea":
            return False
        return real_remove(job_id)

    monkeypatch.setattr(cron_jobs, "remove_job", fail_second)
    with pytest.raises(RetirementError, match="could not be removed"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )

    assert {job["id"] for job in retirement_fixture.read_jobs()} == (
        RETIRE_IDS | {RETAIN_ID}
    )
    assert all(path.exists() for path in retirement_fixture.retired_live_paths())
    assert retirement_fixture.output_paths_are_present_for(RETIRE_IDS)

    monkeypatch.setattr(cron_jobs, "remove_job", real_remove)
    recovered = Manifest.load(manifest.manifest_path)
    finalize(
        retirement_fixture.paths, recovered, recovered.cutoff_epoch + 182
    )
    assert {job["id"] for job in retirement_fixture.read_jobs()} == {RETAIN_ID}


def test_quiescence_rejects_an_unparseable_claim_timestamp(retirement_fixture):
    manifest = retirement_fixture.preflight_and_pause()
    retirement_fixture.insert_execution(
        "ee4582d0598f",
        status="completed",
        claimed_at="not-an-iso-timestamp",
    )
    errors = verify_quiescent(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
    )
    assert any("invalid claimed_at" in error for error in errors)


def test_finalize_revalidates_the_paused_job_fingerprint(retirement_fixture):
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    next(job for job in jobs if job["id"] == "ee4582d0598f")["name"] = "drifted"
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="fingerprint mismatch"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )
    assert all(path.exists() for path in retirement_fixture.retired_live_paths())
    assert retirement_fixture.output_paths_are_present_for(RETIRE_IDS)


def test_boris_verification_rejects_source_or_watermark_regression(
    retirement_fixture,
):
    manifest = retirement_fixture.complete_retirement()
    manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)
    manifest = checkpoint_component(
        retirement_fixture.paths,
        manifest,
        "boris-intake",
        manifest.cutoff_epoch + 210,
    )
    audit = retirement_fixture.paths.state_dir / "boris_intake_audit.jsonl"
    audit.write_text(audit.read_text() + '{"event":"ordinary-new-intake"}\n')
    heartbeat = retirement_fixture.paths.state_dir / "boris_intake_heartbeat"
    heartbeat.write_text("200\n")
    checkpoint_mtime = manifest.component_checkpoints["boris-intake"][-1][
        "heartbeat_mtime"
    ]
    os.utime(heartbeat, (checkpoint_mtime + 5, checkpoint_mtime + 5))
    (
        retirement_fixture.paths.state_dir
        / "boris_intake_watermark_C0BPCKPEFCG.txt"
    ).write_text("0.5\n")
    (
        retirement_fixture.paths.live_scripts / "boris_intake" / "daemon.py"
    ).write_text("not the versioned Boris daemon\n")

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )
    assert any("Boris source" in error for error in errors)
    assert any("Boris watermark regressed" in error for error in errors)


def test_failed_second_pause_restores_preflight_records_and_can_retry(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_only()
    original = retirement_fixture.read_jobs()
    real_pause = cron_jobs.pause_job

    def fail_second(job_id, reason=None):
        if job_id == "ff865c33c5ea":
            raise OSError("injected second pause failure")
        return real_pause(job_id, reason=reason)

    monkeypatch.setattr(cron_jobs, "pause_job", fail_second)
    with pytest.raises(OSError, match="injected second pause failure"):
        pause_producers(retirement_fixture.paths, manifest, retirement_fixture.now + 1)
    assert retirement_fixture.read_jobs() == original
    assert manifest.phase == "preflight"
    assert manifest.cutoff_epoch is None

    monkeypatch.setattr(cron_jobs, "pause_job", real_pause)
    paused = pause_producers(
        retirement_fixture.paths, manifest, retirement_fixture.now + 2
    )
    assert paused.phase == "paused"


def test_failed_pause_postcondition_restores_exact_preflight_records(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_only()
    original = retirement_fixture.read_jobs()
    real_pause = cron_jobs.pause_job

    def corrupt_second_paused_record(job_id, reason=None):
        updated = real_pause(job_id, reason=reason)
        if job_id == "ff865c33c5ea":
            with cron_jobs._jobs_lock():
                jobs = cron_jobs.load_jobs()
                target = next(job for job in jobs if job["id"] == job_id)
                target["state"] = "scheduled"
                cron_jobs.save_jobs(jobs)
        return updated

    monkeypatch.setattr(cron_jobs, "pause_job", corrupt_second_paused_record)
    with pytest.raises(RetirementError, match="failed to verify exact paused state"):
        pause_producers(retirement_fixture.paths, manifest, retirement_fixture.now + 1)

    assert retirement_fixture.read_jobs() == original
    assert manifest.phase == "preflight"
    assert manifest.cutoff_epoch is None
    assert Manifest.load(manifest.manifest_path).phase == "preflight"


def test_failed_pause_manifest_save_restores_registry_and_manifest(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_only()
    original_jobs = retirement_fixture.read_jobs()
    original_manifest = manifest.manifest_path.read_bytes()

    def fail_save():
        raise OSError("injected manifest save failure")

    monkeypatch.setattr(manifest, "save", fail_save)
    with pytest.raises(OSError, match="injected manifest save failure"):
        pause_producers(retirement_fixture.paths, manifest, retirement_fixture.now + 1)

    assert retirement_fixture.read_jobs() == original_jobs
    assert manifest.manifest_path.read_bytes() == original_manifest
    assert manifest.phase == "preflight"
    assert manifest.cutoff_epoch is None


def test_finalize_compensation_never_writes_enabled_retired_records(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_and_pause()
    real_remove = cron_jobs.remove_job
    real_save = cron_jobs.save_jobs
    enabled_writes: list[set[str]] = []

    def observe_save(jobs):
        enabled_writes.append(
            {
                job["id"]
                for job in jobs
                if job.get("id") in RETIRE_IDS and job.get("enabled") is True
            }
        )
        return real_save(jobs)

    def fail_second(job_id):
        if job_id == "ff865c33c5ea":
            return False
        return real_remove(job_id)

    monkeypatch.setattr(cron_jobs, "save_jobs", observe_save)
    monkeypatch.setattr(cron_jobs, "remove_job", fail_second)
    with pytest.raises(RetirementError, match="could not be removed"):
        finalize(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181)

    assert enabled_writes
    assert all(not enabled for enabled in enabled_writes)
    restored = {job["id"]: job for job in retirement_fixture.read_jobs()}
    assert all(restored[job_id]["enabled"] is False for job_id in RETIRE_IDS)
    assert all(restored[job_id]["state"] == "paused" for job_id in RETIRE_IDS)


def test_finalize_compensates_a_persisted_removal_when_output_verification_fails(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_and_pause()
    target = retirement_fixture.paths.cron_output / "ee4582d0598f"
    real_require = retirement_controller._require_match
    target_checks = 0

    def fail_post_removal_verification(fingerprint, path=None):
        nonlocal target_checks
        checked_path = path or fingerprint.active_path
        if checked_path == target:
            target_checks += 1
            if target_checks == 2:
                raise RetirementError("injected post-removal verification failure")
        return real_require(fingerprint, path)

    monkeypatch.setattr(
        retirement_controller, "_require_match", fail_post_removal_verification
    )
    with pytest.raises(RetirementError, match="post-removal verification failure"):
        finalize(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181)

    restored = {job["id"]: job for job in retirement_fixture.read_jobs()}
    assert set(restored) == RETIRE_IDS | {RETAIN_ID}
    assert all(restored[job_id]["enabled"] is False for job_id in RETIRE_IDS)
    assert retirement_fixture.output_paths_are_present_for(RETIRE_IDS)
    assert all(path.exists() for path in retirement_fixture.retired_live_paths())


def test_finalize_compensates_a_failed_final_manifest_save_and_can_retry(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_and_pause()
    original_cutoff = manifest.cutoff_epoch
    real_save = manifest.save
    save_calls = 0

    def fail_final_save():
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise OSError("injected final manifest save failure")
        real_save()

    monkeypatch.setattr(manifest, "save", fail_final_save)
    with pytest.raises(OSError, match="injected final manifest save failure"):
        finalize(retirement_fixture.paths, manifest, original_cutoff + 181)

    durable = Manifest.load(manifest.manifest_path)
    restored = {job["id"]: job for job in retirement_fixture.read_jobs()}
    assert durable.phase == "paused"
    assert durable.cutoff_epoch == original_cutoff
    assert set(restored) == RETIRE_IDS | {RETAIN_ID}
    assert all(restored[job_id]["enabled"] is False for job_id in RETIRE_IDS)
    assert all(path.exists() for path in retirement_fixture.retired_live_paths())
    assert retirement_fixture.output_paths_are_present_for(RETIRE_IDS)

    monkeypatch.setattr(manifest, "save", real_save)
    recovered = Manifest.load(manifest.manifest_path)
    finalize(
        retirement_fixture.paths, recovered, recovered.cutoff_epoch + 182
    )
    assert {job["id"] for job in retirement_fixture.read_jobs()} == {RETAIN_ID}


def test_quiescence_uses_canonical_process_start_identity(
    retirement_fixture, monkeypatch
):
    paths = replace(retirement_fixture.paths, process_listing_path=None)
    manifest = preflight(paths, retirement_fixture.now)
    manifest = pause_producers(paths, manifest, retirement_fixture.now + 1)
    retirement_fixture.insert_execution(
        "ee4582d0598f",
        status="running",
        claimed_at=retirement_fixture.utc_iso(manifest.cutoff_epoch - 1),
        pid=4321,
        process_started_at=7777,
    )
    monkeypatch.setattr(
        retirement_controller.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=" 4321 python worker.py\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        retirement_controller, "get_process_start_time", lambda pid: 7777, raising=False
    )

    matching = verify_quiescent(paths, manifest, manifest.cutoff_epoch + 181)
    assert any("live execution owner" in error for error in matching)

    monkeypatch.setattr(
        retirement_controller, "get_process_start_time", lambda pid: 8888, raising=False
    )
    recycled = verify_quiescent(paths, manifest, manifest.cutoff_epoch + 181)
    assert not any("live execution owner" in error for error in recycled)


def test_repeated_pause_is_a_verified_noop_that_preserves_the_cutoff(
    retirement_fixture,
):
    manifest = retirement_fixture.preflight_and_pause()
    original_cutoff = manifest.cutoff_epoch
    jobs_before = retirement_fixture.paths.jobs_path.read_bytes()
    manifest_before = manifest.manifest_path.read_bytes()

    repeated = pause_producers(
        retirement_fixture.paths, manifest, original_cutoff + 600
    )

    assert repeated.cutoff_epoch == original_cutoff
    assert retirement_fixture.paths.jobs_path.read_bytes() == jobs_before
    assert manifest.manifest_path.read_bytes() == manifest_before


def test_pause_dry_run_accepts_the_same_verified_paused_noop(
    retirement_fixture, capsys
):
    manifest = retirement_fixture.preflight_and_pause()
    cutoff = manifest.cutoff_epoch
    jobs_before = retirement_fixture.paths.jobs_path.read_bytes()
    manifest_before = manifest.manifest_path.read_bytes()

    result = controller_main(
        ["pause", "--manifest", str(manifest.manifest_path)]
    )

    assert result == 0
    assert '"apply": false' in capsys.readouterr().out
    assert Manifest.load(manifest.manifest_path).cutoff_epoch == cutoff
    assert retirement_fixture.paths.jobs_path.read_bytes() == jobs_before
    assert manifest.manifest_path.read_bytes() == manifest_before


@pytest.mark.parametrize("drift", ("retained", "duplicate"))
def test_pause_apply_rejects_protected_registry_drift_before_any_write(
    retirement_fixture, monkeypatch, drift
):
    manifest = retirement_fixture.preflight_only()
    jobs = retirement_fixture.read_jobs()
    if drift == "retained":
        next(job for job in jobs if job["id"] == RETAIN_ID)["name"] = "drifted"
    else:
        jobs.append(
            json.loads(
                json.dumps(next(job for job in jobs if job["id"] == "ee4582d0598f"))
            )
        )
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()
    registry_before = retirement_fixture.paths.jobs_path.read_bytes()
    real_save = cron_jobs.save_jobs
    save_calls = 0

    def observe_save(records):
        nonlocal save_calls
        save_calls += 1
        return real_save(records)

    monkeypatch.setattr(cron_jobs, "save_jobs", observe_save)

    with pytest.raises(
        RetirementError, match="drifted after preflight|expected exactly one record"
    ):
        pause_producers(
            retirement_fixture.paths, manifest, retirement_fixture.now + 1
        )

    assert save_calls == 0
    assert retirement_fixture.paths.jobs_path.read_bytes() == registry_before
    assert Manifest.load(manifest.manifest_path).phase == "preflight"


def test_rollback_preserves_boris_audit_appends(retirement_fixture):
    manifest = retirement_fixture.complete_retirement()
    manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)
    manifest = checkpoint_component(
        retirement_fixture.paths,
        manifest,
        "boris-intake",
        manifest.cutoff_epoch + 210,
    )
    (retirement_fixture.paths.live_scripts / "boris_intake" / "daemon.py").write_text(
        "changed Boris daemon\n"
    )
    audit = retirement_fixture.paths.state_dir / "boris_intake_audit.jsonl"
    audit.write_text(audit.read_text() + '{"event":"ordinary-new-intake"}\n')
    expected_audit = audit.read_bytes()

    rollback(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240)

    assert audit.read_bytes() == expected_audit


def test_rollback_does_not_mark_audit_only_boris_activity_as_an_install_change(
    retirement_fixture,
):
    manifest = retirement_fixture.complete_retirement()
    manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)
    manifest = checkpoint_component(
        retirement_fixture.paths,
        manifest,
        "boris-intake",
        manifest.cutoff_epoch + 210,
    )
    audit = retirement_fixture.paths.state_dir / "boris_intake_audit.jsonl"
    audit.write_text(audit.read_text() + '{"event":"ordinary-new-intake"}\n')
    expected = audit.read_bytes()

    rollback(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )
    assert audit.read_bytes() == expected
    assert "boris-intake" not in manifest.changed_components


@pytest.mark.parametrize(
    ("relative_path", "replacement"),
    (
        ("versioned-source/daemon.py", "changed versioned Boris source\n"),
        ("versioned-tests/test_daemon.py", "changed versioned Boris test\n"),
        ("config", "boris_intake:\n  mode: changed\n"),
        ("plist", "<plist><dict><string>changed</string></dict></plist>\n"),
    ),
)
def test_rollback_detects_each_boris_install_surface_change(
    retirement_fixture, relative_path, replacement
):
    manifest = retirement_fixture.complete_retirement()
    manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)
    manifest = checkpoint_component(
        retirement_fixture.paths,
        manifest,
        "boris-intake",
        manifest.cutoff_epoch + 210,
    )
    targets = {
        "versioned-source/daemon.py": retirement_fixture.paths.source_root
        / "cron/scripts/boris_intake/daemon.py",
        "versioned-tests/test_daemon.py": retirement_fixture.paths.source_root
        / "tests/cron/boris_intake/test_daemon.py",
        "config": retirement_fixture.paths.hermes_home / "config.yaml",
        "plist": retirement_fixture.paths.boris_launchd_plist,
    }
    target = targets[relative_path]
    expected = target.read_bytes()
    target.write_text(replacement)

    rollback(retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240)

    assert target.read_bytes() == expected
    assert "boris-intake" in manifest.changed_components


def test_reaction_checkpoint_refreshes_the_legacy_projection_baseline(
    retirement_fixture,
):
    manifest = retirement_fixture.complete_retirement()
    state = json.loads(retirement_fixture.reaction_state_path.read_text())
    legacy_permalink = "https://ylopo.slack.com/archives/C07BU8AAPB3/p99"
    state["posts"][legacy_permalink] = {
        "state": "active",
        "emoji": "thinkies",
        "lanes": {"legacy-new": "active"},
        "pending_removals": [],
    }
    state["reacted"][f"legacy-new:{legacy_permalink}"] = {"at": 123}
    retirement_fixture.reaction_state_path.write_text(json.dumps(state, indent=2))
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        cron_jobs.pause_job(RETAIN_ID, reason="installation lock")
    manifest = checkpoint_component(
        retirement_fixture.paths,
        manifest,
        "retained-reaction",
        manifest.cutoff_epoch + 200,
    )
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            retained = next(job for job in jobs if job["id"] == RETAIN_ID)
            retained.update(
                name="boris-review-reaction-state-watchdog",
                enabled=True,
                state="scheduled",
            )
            cron_jobs.save_jobs(jobs)
    retirement_fixture.insert_execution(
        RETAIN_ID,
        status="completed",
        claimed_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 201),
    )

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert not any("legacy reaction projection changed" in error for error in errors)


def test_reaction_projection_classifies_reacted_records_by_permalink_key(
    retirement_fixture,
):
    state = json.loads(retirement_fixture.reaction_state_path.read_text())
    boris_permalink = "https://ylopo.slack.com/archives/C0BPCKPEFCG/p1111111111111111"
    legacy_permalink = "https://ylopo.slack.com/archives/C08K8FQKX9S/p2222222222222222"
    state["reacted"] = {
        f"boris-lane:{boris_permalink}": {"at": 100},
        f"legacy-lane:{legacy_permalink}": {"at": 100},
    }
    retirement_fixture.reaction_state_path.write_text(json.dumps(state, indent=2))
    manifest = retirement_fixture.complete_retirement()
    changed = json.loads(retirement_fixture.reaction_state_path.read_text())
    changed["reacted"][f"boris-lane:{boris_permalink}"]["at"] = 200
    retirement_fixture.reaction_state_path.write_text(json.dumps(changed, indent=2))
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            next(job for job in jobs if job["id"] == RETAIN_ID)["name"] = (
                "boris-review-reaction-state-watchdog"
            )
            cron_jobs.save_jobs(jobs)
    retirement_fixture.insert_execution(
        RETAIN_ID,
        status="completed",
        claimed_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 200),
    )

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert not any("legacy reaction projection changed" in error for error in errors)


def test_reaction_rehearsal_rejects_a_vacuous_roundtrip(
    retirement_fixture,
):
    state = json.loads(retirement_fixture.reaction_state_path.read_text())
    state["posts"] = {
        permalink: record
        for permalink, record in state["posts"].items()
        if "/C0BPCKPEFCG/" not in permalink
    }
    retirement_fixture.reaction_state_path.write_text(json.dumps(state, indent=2))

    with pytest.raises(RetirementError, match="non-vacuous"):
        rehearse_reaction_roundtrip(
            retirement_fixture.reaction_state_path,
            retirement_fixture.watchdog_source_path,
        )


@pytest.mark.parametrize(
    "service_status",
    (
        {"label": "ai.hermes.unrelated", "state": "running", "pid": 777},
        {"label": "ai.hermes.boris-intake", "state": "stopped", "pid": 777},
        {"label": "ai.hermes.boris-intake", "state": "running", "pid": None},
    ),
)
def test_boris_verification_requires_the_exact_running_launchd_service(
    retirement_fixture, service_status
):
    manifest = retirement_fixture.complete_retirement()
    manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)
    manifest = checkpoint_component(
        retirement_fixture.paths,
        manifest,
        "boris-intake",
        manifest.cutoff_epoch + 210,
    )
    heartbeat = retirement_fixture.paths.state_dir / "boris_intake_heartbeat"
    checkpoint_mtime = manifest.component_checkpoints["boris-intake"][-1][
        "heartbeat_mtime"
    ]
    os.utime(heartbeat, (checkpoint_mtime + 5, checkpoint_mtime + 5))
    retirement_fixture.service_status_path.write_text(json.dumps(service_status))

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert any("exact Boris launchd service is not running" in error for error in errors)


def test_verify_retired_reports_a_non_finite_boris_watermark(retirement_fixture):
    manifest = retirement_fixture.complete_retirement()
    manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)
    manifest = checkpoint_component(
        retirement_fixture.paths,
        manifest,
        "boris-intake",
        manifest.cutoff_epoch + 210,
    )
    heartbeat = retirement_fixture.paths.state_dir / "boris_intake_heartbeat"
    checkpoint_mtime = manifest.component_checkpoints["boris-intake"][-1][
        "heartbeat_mtime"
    ]
    os.utime(heartbeat, (checkpoint_mtime + 5, checkpoint_mtime + 5))
    (
        retirement_fixture.paths.state_dir
        / "boris_intake_watermark_C0BPCKPEFCG.txt"
    ).write_text("NaN\n")

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )
    assert any("invalid Boris watermark" in error for error in errors)


def test_verify_retired_rejects_a_malformed_retired_claim_timestamp(
    retirement_fixture,
):
    manifest = retirement_fixture.complete_retirement()
    retirement_fixture.insert_execution(
        "ee4582d0598f",
        status="completed",
        claimed_at="not-an-iso-timestamp",
    )

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert any("invalid claimed_at" in error for error in errors)


def test_cli_mutating_commands_are_dry_run_without_apply(
    retirement_fixture, capsys
):
    manifest = retirement_fixture.preflight_only()
    manifest_path = str(manifest.manifest_path)

    jobs_before = retirement_fixture.paths.jobs_path.read_bytes()
    manifest_before = manifest.manifest_path.read_bytes()
    assert controller_main(["pause", "--manifest", manifest_path]) == 0
    assert retirement_fixture.paths.jobs_path.read_bytes() == jobs_before
    assert manifest.manifest_path.read_bytes() == manifest_before

    manifest = pause_producers(
        retirement_fixture.paths, manifest, retirement_fixture.now + 1
    )
    jobs_before = retirement_fixture.paths.jobs_path.read_bytes()
    manifest_before = manifest.manifest_path.read_bytes()
    assert controller_main(
        [
            "finalize",
            "--manifest",
            manifest_path,
            "--now-epoch",
            str(manifest.cutoff_epoch + 181),
        ]
    ) == 0
    assert retirement_fixture.paths.jobs_path.read_bytes() == jobs_before
    assert manifest.manifest_path.read_bytes() == manifest_before

    manifest = finalize(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
    )
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        cron_jobs.pause_job(RETAIN_ID, reason="installation lock")
    jobs_before = retirement_fixture.paths.jobs_path.read_bytes()
    manifest_before = manifest.manifest_path.read_bytes()
    archive_before = _hash_tree(manifest.archive_dir)
    assert controller_main(
        [
            "checkpoint",
            "--manifest",
            manifest_path,
            "--component",
            "retained-reaction",
        ]
    ) == 0
    assert retirement_fixture.paths.jobs_path.read_bytes() == jobs_before
    assert manifest.manifest_path.read_bytes() == manifest_before
    assert _hash_tree(manifest.archive_dir) == archive_before

    assert controller_main(["rollback", "--manifest", manifest_path]) == 0
    assert retirement_fixture.paths.jobs_path.read_bytes() == jobs_before
    assert manifest.manifest_path.read_bytes() == manifest_before
    assert _hash_tree(manifest.archive_dir) == archive_before
    assert '"apply": false' in capsys.readouterr().out


def test_finalize_dry_run_performs_the_apply_path_registry_validation(
    retirement_fixture, capsys
):
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    next(job for job in jobs if job["id"] == "ee4582d0598f")["name"] = "drifted"
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    result = controller_main(
        [
            "finalize",
            "--manifest",
            str(manifest.manifest_path),
            "--now-epoch",
            str(manifest.cutoff_epoch + 181),
        ]
    )

    assert result == 2
    assert "fingerprint mismatch before exact removal" in capsys.readouterr().out
    assert all(path.exists() for path in retirement_fixture.retired_live_paths())


def test_rollback_dry_run_verifies_the_jobs_snapshot_before_reading_it(
    retirement_fixture, capsys
):
    manifest = retirement_fixture.complete_retirement()
    jobs_snapshot = manifest.files["jobs-registry"].archive_path
    jobs_snapshot.write_text('{"jobs": []}\n')

    result = controller_main(
        ["rollback", "--manifest", str(manifest.manifest_path)]
    )

    assert result == 2
    assert "fingerprint mismatch" in capsys.readouterr().out
    assert Manifest.load(manifest.manifest_path).phase == "finalized"


def test_rollback_verifies_exact_job_records_before_marking_restored(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.complete_retirement()
    real_save = cron_jobs.save_jobs

    def corrupt_restored_record(jobs):
        real_save(jobs)
        if RETIRE_IDS <= {job.get("id") for job in jobs}:
            corrupted = json.loads(json.dumps(jobs))
            next(job for job in corrupted if job["id"] == "ee4582d0598f")[
                "name"
            ] = "corrupted-after-save"
            real_save(corrupted)

    monkeypatch.setattr(cron_jobs, "save_jobs", corrupt_restored_record)

    with pytest.raises(RetirementError, match="job record not restored exactly"):
        rollback(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
        )

    assert Manifest.load(manifest.manifest_path).phase == "finalized"


def test_isolated_home_defaults_to_an_isolated_launchd_plist(
    tmp_path, monkeypatch
):
    current_home = (tmp_path / "current-home").resolve()
    default_home = (tmp_path / "default-home").resolve()
    isolated_home = (tmp_path / "isolated-home").resolve()
    monkeypatch.setattr(
        retirement_controller, "get_hermes_home", lambda: current_home
    )
    monkeypatch.setattr(
        retirement_controller, "get_default_hermes_root", lambda: default_home
    )

    paths = Paths(
        hermes_home=isolated_home,
        source_root=tmp_path / "source",
        archive_root=tmp_path / "archive",
    )

    assert paths.boris_launchd_plist == (
        isolated_home
        / "Library"
        / "LaunchAgents"
        / "ai.hermes.boris-intake.plist"
    )


def test_cli_rejects_now_override_for_the_designated_live_home(
    tmp_path, monkeypatch, capsys
):
    designated_live = tmp_path / "designated-live"
    monkeypatch.setattr(
        retirement_controller,
        "get_hermes_home",
        lambda: designated_live.resolve(),
        raising=False,
    )
    monkeypatch.setattr(
        retirement_controller,
        "get_default_hermes_root",
        lambda: designated_live.resolve(),
        raising=False,
    )

    assert controller_main(
        [
            "preflight",
            "--hermes-home",
            str(designated_live),
            "--source-root",
            str(tmp_path / "source"),
            "--now-epoch",
            "1",
        ]
    ) == 2
    assert "--now-epoch is forbidden" in capsys.readouterr().out
    assert not designated_live.exists()


@pytest.mark.parametrize("home_kind", ("current", "default", "profile"))
@pytest.mark.parametrize(
    ("option", "fixture_name"),
    (
        ("--now-epoch", "1"),
        ("--process-listing-path", "processes.json"),
        ("--boris-service-status-path", "service.json"),
    ),
)
def test_cli_rejects_test_injection_for_every_live_home(
    tmp_path, monkeypatch, capsys, home_kind, option, fixture_name
):
    current_home = (tmp_path / "current-home").resolve()
    default_home = (tmp_path / "default-home").resolve()
    homes = {
        "current": current_home,
        "default": default_home,
        "profile": default_home / "profiles" / "boris",
    }
    monkeypatch.setattr(
        retirement_controller,
        "get_hermes_home",
        lambda: current_home,
        raising=False,
    )
    monkeypatch.setattr(
        retirement_controller,
        "get_default_hermes_root",
        lambda: default_home,
        raising=False,
    )
    injected = tmp_path / fixture_name
    injected.write_text("[]\n" if "process" in option else '{}\n')

    result = controller_main(
        [
            "preflight",
            "--hermes-home",
            str(homes[home_kind]),
            "--source-root",
            str(tmp_path / "source"),
            option,
            "1" if option == "--now-epoch" else str(injected),
        ]
    )

    assert result == 2
    assert "forbidden for a live Hermes home" in capsys.readouterr().out


def test_cli_accepts_test_injection_for_an_isolated_home(
    retirement_fixture, tmp_path, monkeypatch, capsys
):
    current_home = (tmp_path / "current-home").resolve()
    default_home = (tmp_path / "default-home").resolve()
    monkeypatch.setattr(
        retirement_controller,
        "get_hermes_home",
        lambda: current_home,
        raising=False,
    )
    monkeypatch.setattr(
        retirement_controller,
        "get_default_hermes_root",
        lambda: default_home,
        raising=False,
    )
    service_status = tmp_path / "service.json"
    service_status.write_text(
        json.dumps(
            {
                "label": "ai.hermes.boris-intake",
                "state": "running",
                "pid": 777,
            }
        )
    )

    result = controller_main(
        [
            "preflight",
            "--hermes-home",
            str(retirement_fixture.paths.hermes_home),
            "--source-root",
            str(retirement_fixture.paths.source_root),
            "--archive-root",
            str(retirement_fixture.paths.archive_root),
            "--board-path",
            str(retirement_fixture.paths.board_path),
            "--boris-launchd-plist",
            str(retirement_fixture.paths.boris_launchd_plist),
            "--process-listing-path",
            str(retirement_fixture.process_listing_path),
            "--boris-service-status-path",
            str(service_status),
            "--now-epoch",
            str(retirement_fixture.now),
        ]
    )

    assert result == 0
    assert '"ok": true' in capsys.readouterr().out


def test_completed_rollback_is_idempotent_without_replaying_component_restore(
    retirement_fixture,
):
    manifest = retirement_fixture.complete_retirement()
    manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)
    manifest = retirement_fixture.checkpoint_and_change_boris(manifest)
    manifest = rollback(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )
    assert set(manifest.changed_components) == {
        "retained-reaction",
        "boris-intake",
    }
    config = retirement_fixture.paths.hermes_home / "config.yaml"
    config.write_text("unrelated_after_completed_rollback: preserve-me\n")

    rollback(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 300
    )
    assert config.read_text() == "unrelated_after_completed_rollback: preserve-me\n"
